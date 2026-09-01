"""Auto-confirmation policy. The thing that removes the wait.

THE INSIGHT
Most orders are boring: a known customer, ordering what they always order,
at list price, in stock. Those should confirm INSTANTLY. Only the unusual
ones need a human.

THE SAFETY
Notice what this file is: deterministic Python. It never sees the customer's
words. The LLM cannot call it, cannot argue with it, cannot be talked into
widening it. The agent still has NO submit tool — the policy engine submits,
and only when every rule passes.

That is how you get instant confirmation without giving an LLM the keys.

LO QUE SE CORRIGIÓ (y por qué importaba)
1. STOCK: en ERPNext los borradores NO reservan. `Bin.reserved_qty` sube al
   confirmar. Así que el chequeo viejo ignoraba todo lo ya prometido en
   borradores y podía vender dos veces lo mismo. Ahora se resta lo
   comprometido en borradores explícitamente, y el submit va bajo lock
   (app/lock.py).
2. PRECIO: se comparaba contra un `Item Price` cualquiera (limit=1, sin
   filtrar por lista de precios ni ordenar). Con dos listas de venta el
   chequeo era una moneda al aire. Ahora se compara contra la lista que el
   propio Sales Order dice estar usando, y cualquier descuento a nivel
   documento manda a revisión.
3. DEUDA: si el reporte fallaba se devolvía inf para siempre y la
   auto-confirmación quedaba muerta sin una sola línea de log. Ahora el
   reporte se llama con los filtros que realmente pide, y el fallo se
   registra distinguiéndolo de "no tiene deuda".
4. FECHA: solo se rechazaban fechas muy lejanas. Una fecha en el pasado
   pasaba el filtro.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta

from app import erpnext, formato, log

_log = log.get("policy")

# DEFAULTS, no valores congelados. Cada perilla se lee en cada evaluación
# (ver `_num`), porque el plan del README es que el dueño las suba de a poco
# mirando las decisiones — y eso no puede requerir rebuildear la imagen.
DEFAULTS = {
    "AUTO_CONFIRM_MAX": 0.0,  # 0 = apagado
    "AUTO_CONFIRM_MULT": 2.0,  # x el promedio del cliente
    "AUTO_CONFIRM_MIN_ORDERS": 3.0,  # historial mínimo
    "AUTO_CONFIRM_MAX_DEBT": 0.0,  # deuda vencida tolerada
    "AUTO_CONFIRM_MAX_DIAS": 30.0,  # días de entrega hacia adelante
    "AUTO_CONFIRM_TOLERANCIA_PRECIO": 0.01,  # pesos, no centavos
    "STOCK_BUFFER_PCT": 20.0,
}


@dataclass
class Decision:
    auto: bool
    motivos: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return "auto-confirmado" if self.auto else "; ".join(self.motivos)


def _num(env: str, default: float | None = None) -> float:
    """Lee una perilla del entorno, con el default de DEFAULTS."""
    if default is None:
        default = DEFAULTS[env]
    crudo = os.getenv(env)
    if crudo is None or crudo.strip() == "":
        return default
    try:
        return float(crudo)
    except ValueError:
        _log.warning("%s=%r no es un número, uso %s", env, crudo, default)
        return default


def activa() -> bool:
    """¿Está la auto-confirmación habilitada?"""
    return _num("AUTO_CONFIRM_MAX") > 0


def evaluar(sales_order: dict) -> Decision:
    """Every rule must pass. Any single failure sends it to a human."""
    motivos: list[str] = []

    tope = _num("AUTO_CONFIRM_MAX")
    if tope <= 0:
        return Decision(False, ["auto-confirmación desactivada"])

    total = float(sales_order.get("grand_total") or 0)
    cliente = sales_order.get("customer")

    if not cliente:
        return Decision(False, ["pedido sin cliente"])
    if total <= 0:
        return Decision(False, ["pedido sin monto"])

    # 1. Hard ceiling. Nothing large auto-confirms, ever.
    if total > tope:
        motivos.append(f"monto {formato.pesos(total)} supera el tope de {formato.pesos(tope)}")

    # 2. Known customer with real history.
    #    order_by explícito: "los últimos N confirmados" tiene que ser
    #    reproducible, no depender del orden interno de Frappe.
    try:
        historial = erpnext.get_list(
            "Sales Order",
            filters=[["customer", "=", cliente], ["docstatus", "=", 1]],
            fields=["grand_total"],
            limit=50,
            order_by="transaction_date desc",
        )
    except erpnext.ERPNextError:
        _log.warning("no pude leer historial de %s", cliente)
        return Decision(False, ["no pude verificar el historial del cliente"])

    if len(historial) < int(_num("AUTO_CONFIRM_MIN_ORDERS")):
        motivos.append(f"cliente con solo {len(historial)} pedidos confirmados")
    else:
        montos = [float(h.get("grand_total") or 0) for h in historial]
        promedio = sum(montos) / len(montos)
        if promedio > 0 and total > promedio * _num("AUTO_CONFIRM_MULT"):
            motivos.append(
                f"pedido {formato.pesos(total)} es {total / promedio:.1f}x su promedio de {formato.pesos(promedio)}"
            )

    # 3. No overdue balance.
    deuda, verificada = _saldo_vencido(cliente)
    if not verificada:
        motivos.append("no pude verificar la deuda del cliente")
    elif deuda > _num("AUTO_CONFIRM_MAX_DEBT"):
        motivos.append(f"tiene {formato.pesos(deuda)} vencidos")

    # 4. Everything actually in stock, above the safety buffer, descontando
    #    lo ya prometido en borradores.
    items = sales_order.get("items") or []
    if not items:
        motivos.append("pedido sin renglones")
    for item in items:
        codigo = item.get("item_code")
        if not codigo:
            motivos.append("renglón sin código de producto")
            continue
        ok, detalle = _hay_stock(codigo, float(item.get("qty") or 0), sales_order.get("name"))
        if not ok:
            motivos.append(f"stock insuficiente de {codigo} ({detalle})")

    # 5. Standard prices only — no negotiated rates slipping through.
    lista = sales_order.get("selling_price_list")
    if not lista:
        motivos.append("el pedido no declara lista de precios")
    else:
        for item in items:
            codigo = item.get("item_code")
            if not codigo:
                continue
            ok, detalle = _precio_de_lista(codigo, float(item.get("rate") or 0), lista)
            if not ok:
                motivos.append(f"precio fuera de lista en {codigo} ({detalle})")

    # 5b. Descuentos a nivel documento: el renglón puede estar a precio de
    #     lista y el total venir con 30% off igual.
    if float(sales_order.get("discount_amount") or 0) > 0:
        motivos.append("el pedido tiene un descuento aplicado")
    if float(sales_order.get("additional_discount_percentage") or 0) > 0:
        motivos.append("el pedido tiene un descuento porcentual aplicado")

    # 6. Reasonable delivery date — ni en el pasado, ni muy lejana.
    entrega = sales_order.get("delivery_date")
    if entrega:
        try:
            fecha = date.fromisoformat(str(entrega)[:10])
        except ValueError:
            motivos.append(f"fecha de entrega ilegible: {entrega}")
        else:
            if fecha < date.today():
                motivos.append(f"fecha de entrega en el pasado ({fecha.isoformat()})")
            elif fecha > date.today() + timedelta(days=int(_num("AUTO_CONFIRM_MAX_DIAS"))):
                motivos.append("fecha de entrega muy lejana")

    decision = Decision(not motivos, motivos)
    _log.info(
        "policy %s cliente=%s total=%.0f -> %s",
        sales_order.get("name"),
        cliente,
        total,
        decision,
    )
    return decision


def _saldo_vencido(cliente: str) -> tuple[float, bool]:
    """Devuelve (deuda_vencida, se_pudo_verificar).

    El reporte Accounts Receivable de ERPNext necesita `report_date` y los
    rangos de antigüedad; sin ellos tira error en varias versiones. Antes eso
    caía en el `except`, devolvía inf, y la auto-confirmación quedaba
    apagada para siempre sin ninguna señal.
    """
    hoy = date.today().isoformat()
    filtros = {
        "company": erpnext.default_company(),
        "party_type": "Customer",
        "party": [cliente],
        "report_date": hoy,
        "ageing_based_on": "Due Date",
        "range1": 30,
        "range2": 60,
        "range3": 90,
        "range4": 120,
    }
    try:
        rows = erpnext.run_report("Accounts Receivable", filtros)
    except erpnext.ERPNextError as e:
        _log.error(
            "Accounts Receivable falló para %s (%s). La regla de deuda no se "
            "puede verificar, así que este pedido va a revisión humana.",
            cliente,
            e,
        )
        return 0.0, False

    total = 0.0
    for r in rows:
        if not isinstance(r, dict):
            continue
        # `age` > 0 significa vencido según el ageing pedido arriba.
        if (r.get("age") or 0) > 0:
            total += float(r.get("outstanding_amount") or 0)
    return total, True


def _comprometido_en_borradores(item_code: str, excluir: str | None) -> float:
    """Cantidad de este producto ya prometida en Sales Orders en BORRADOR.

    Esto es la corrección importante: en ERPNext un borrador no toca
    `Bin.reserved_qty`. Si no lo restamos a mano, cada borrador nuevo ve el
    stock como si los anteriores no existieran.
    """
    try:
        filas = erpnext.get_list(
            "Sales Order Item",
            filters=[["item_code", "=", item_code], ["docstatus", "=", 0]],
            fields=["parent", "qty"],
            limit=200,
        )
    except erpnext.ERPNextError:
        _log.warning("no pude leer borradores de %s; asumo lo peor", item_code)
        return float("inf")
    return sum(float(f.get("qty") or 0) for f in filas if f.get("parent") != excluir)


def _hay_stock(item_code: str, qty: float, pedido_actual: str | None = None) -> tuple[bool, str]:
    try:
        bins = erpnext.get_list(
            "Bin",
            filters=[["item_code", "=", item_code]],
            fields=["actual_qty", "reserved_qty"],
            limit=20,
        )
    except erpnext.ERPNextError:
        return False, "no pude consultar el stock"
    if not bins:
        return False, "sin registro de stock"

    fisico = sum(float(b.get("actual_qty") or 0) for b in bins)
    reservado = sum(float(b.get("reserved_qty") or 0) for b in bins)
    en_borradores = _comprometido_en_borradores(item_code, pedido_actual)

    disponible = fisico - reservado - en_borradores
    buffer = _num("STOCK_BUFFER_PCT") / 100.0
    seguro = disponible * (1 - buffer)
    if seguro >= qty:
        return True, ""
    return (
        False,
        f"pide {qty:g}, disponible seguro {seguro:g} "
        f"(físico {fisico:g} - reservado {reservado:g} - borradores {en_borradores:g})",
    )


def _precio_de_lista(item_code: str, rate: float, price_list: str) -> tuple[bool, str]:
    """El renglón tiene que estar al precio de LA lista que el pedido declara.

    Antes: `limit=1` sin filtrar por lista ni ordenar -> con dos listas de
    venta comparaba contra una al azar.
    """
    try:
        precios = erpnext.get_list(
            "Item Price",
            filters=[
                ["item_code", "=", item_code],
                ["selling", "=", 1],
                ["price_list", "=", price_list],
            ],
            fields=["price_list_rate", "valid_from", "valid_upto"],
            limit=5,
            order_by="valid_from desc",
        )
    except erpnext.ERPNextError:
        return False, "no pude consultar el precio"
    if not precios:
        return False, f"sin precio cargado en la lista {price_list}"

    esperado = float(precios[0].get("price_list_rate") or 0)
    if abs(esperado - rate) < _num("AUTO_CONFIRM_TOLERANCIA_PRECIO"):
        return True, ""
    return False, f"cobra {rate:g}, la lista dice {esperado:g}"
