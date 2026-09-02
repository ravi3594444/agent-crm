"""Deterministic, fail-closed Sales Order auto-confirmation policy."""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import erpnext
from app.formato import pesos
from app.locks import distributed_lock

MAX_AUTO = float(os.getenv("AUTO_CONFIRM_MAX", "0"))
MAX_MULT = float(os.getenv("AUTO_CONFIRM_MULT", "2.0"))
MIN_PEDIDOS = int(os.getenv("AUTO_CONFIRM_MIN_ORDERS", "3"))
MAX_DEUDA = float(os.getenv("AUTO_CONFIRM_MAX_DEBT", "0"))
STOCK_CONFIABLE = os.getenv("STOCK_CONFIABLE", "false").strip().lower() == "true"
PRICE_LIST = os.getenv("AUTO_CONFIRM_PRICE_LIST", "").strip()
CURRENCY = os.getenv("AUTO_CONFIRM_CURRENCY", "").strip()

# The three states in which ERPNext itself stops counting an order against
# stock: its get_reserved_qty (erpnext/stock/stock_balance.py) sums Sales Order
# Item rows `where docstatus = 1 and status not in ('On Hold', 'Closed')`, and
# a cancelled order consumes nothing. Spelled as ERPNext spells them, because
# the list is also sent as a filter.
ESTADOS_SIN_RESERVA = ("Closed", "Cancelled", "On Hold")
_SIN_RESERVA = frozenset(estado.lower() for estado in ESTADOS_SIN_RESERVA)

# Truncation guards. A silently cut list reads as "less is promised", which
# oversells, so both reads ask for one row more than they will accept.
MAX_BORRADORES = 500
MAX_RENGLONES_POR_PEDIDO = 20
# Parent names travel in a GET query string; a few hundred of them exceed the
# default gunicorn request-line limit and come back as a 414.
LOTE_BORRADORES = 50


@dataclass
class Decision:
    auto: bool
    motivos: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return "auto-confirmado" if self.auto else "; ".join(self.motivos)


def _hoy_del_negocio() -> date:
    zone_name = os.getenv(
        "BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires"
    ).strip()
    try:
        return datetime.now(ZoneInfo(zone_name)).date()
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise erpnext.ERPNextError("BUSINESS_TIMEZONE inválida") from exc


def _float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError) as exc:
        raise erpnext.ERPNextError("ERPNext devolvió un importe inválido") from exc


def _zero(value: object) -> bool:
    return abs(_float(value)) < 0.000001


def sin_reserva(status: object) -> bool:
    """True when ERPNext itself does not count this order against stock."""
    return str(status or "").strip().lower() in _SIN_RESERVA


def _es_borrador(fila: dict) -> bool:
    return abs(_float(fila.get("docstatus"))) < 0.000001


def _clave_fifo(creacion: object, nombre: str) -> tuple[datetime, str] | None:
    """Position in the queue: when it was asked for, then which order it is."""
    try:
        return (datetime.fromisoformat(str(creacion)), nombre)
    except (TypeError, ValueError):
        return None


def _reclamo_previo(
    creacion: object, nombre: str, desde: str, propio: str
) -> bool:
    """True when that order's claim on these units comes before ours.

    FIFO on (creation, order id). The timestamp alone is not enough: two
    drafts can be saved in the same instant — one queue worker per inbound
    message, both writing through the same ERPNext — and then each would read
    the other as the earlier claim and defer to it. Both customers wait, and
    the dairy that HAS the stock for one of them sells it to neither. The order
    id breaks the tie identically in both evaluations, so exactly one of the
    two claims the units and the other genuinely waits.

    An unreadable timestamp on either side means "I cannot tell who was
    first", and then the safe answer is that the other order has the claim.
    """
    otra = _clave_fifo(creacion, nombre)
    mia = _clave_fifo(desde, propio)
    if otra is None or mia is None:
        return True
    return otra < mia


def _cantidad_en_stock_uom(item: dict) -> float:
    """Quantity of one order line expressed in the item's STOCK unit.

    Bin.actual_qty and Bin.reserved_qty are stored in the stock unit, so a line
    sold by the box cannot be compared against them as it stands. ERPNext keeps
    the converted figure in ``stock_qty``; when that is absent the only safe
    readings are an explicit conversion factor, or a line whose unit already IS
    the stock unit. Anything else raises, which every caller turns into "I
    could not verify the stock" — never into a confirmation.
    """
    qty = _float(item.get("qty"))
    stock_qty = _float(item.get("stock_qty"))
    if stock_qty > 0:
        return stock_qty
    if stock_qty < 0:
        raise erpnext.ERPNextError("ERPNext devolvió una cantidad de stock negativa")

    factor_declarado = item.get("conversion_factor")
    if factor_declarado not in (None, ""):
        factor = _float(factor_declarado)
        if factor <= 0:
            raise erpnext.ERPNextError("factor de conversión inválido")
        return qty * factor

    uom = str(item.get("uom") or "").strip()
    stock_uom = str(item.get("stock_uom") or "").strip()
    if uom and stock_uom and uom != stock_uom:
        raise erpnext.ERPNextError(
            "no puedo comparar unidades distintas sin factor de conversión"
        )
    return qty


def evaluar(sales_order: dict) -> Decision:
    """Return auto=True only when every independently verified rule passes."""
    if MAX_AUTO <= 0:
        return Decision(False, ["auto-confirmación desactivada"])

    motivos: list[str] = []
    if not STOCK_CONFIABLE:
        motivos.append("inventario no marcado como confiable")
    if not PRICE_LIST:
        motivos.append("lista estándar de auto-confirmación no configurada")
    if not CURRENCY:
        motivos.append("moneda de auto-confirmación no configurada")

    cliente = str(sales_order.get("customer") or "").strip()
    if not cliente:
        motivos.append("cliente ausente")

    try:
        total = _float(sales_order.get("grand_total"))
    except erpnext.ERPNextError:
        total = 0
        motivos.append("total inválido")
    if total <= 0:
        motivos.append("total no positivo")
    elif total > MAX_AUTO:
        motivos.append(f"monto {pesos(total)} supera el tope de {pesos(MAX_AUTO)}")

    if PRICE_LIST and str(sales_order.get("selling_price_list") or "") != PRICE_LIST:
        motivos.append("lista de precios distinta de la autorizada")
    if CURRENCY and str(sales_order.get("currency") or "") != CURRENCY:
        motivos.append("moneda distinta de la autorizada")
    for field_name in (
        "discount_amount",
        "base_discount_amount",
        "additional_discount_percentage",
    ):
        try:
            if not _zero(sales_order.get(field_name)):
                motivos.append("descuento general no autorizado")
                break
        except erpnext.ERPNextError:
            motivos.append("descuento general inválido")
            break

    if cliente:
        try:
            historial = erpnext.get_list(
                "Sales Order",
                filters=[["customer", "=", cliente], ["docstatus", "=", 1]],
                fields=["grand_total"],
                limit=50,
            )
            importes = [_float(row.get("grand_total")) for row in historial]
            if len(importes) < MIN_PEDIDOS:
                motivos.append(
                    f"cliente con solo {len(importes)} pedidos confirmados"
                )
            else:
                promedio = sum(importes) / len(importes)
                if promedio <= 0:
                    motivos.append("historial sin un promedio positivo")
                elif total > promedio * MAX_MULT:
                    motivos.append(
                        f"pedido {pesos(total)} supera {MAX_MULT:g}x su promedio"
                    )
        except (erpnext.ERPNextError, KeyError):
            motivos.append("no se pudo verificar el historial")

        deuda = _saldo_vencido(cliente)
        if deuda is None:
            motivos.append("no se pudo verificar la deuda vencida")
        elif deuda > MAX_DEUDA:
            motivos.append(f"tiene {pesos(deuda)} vencidos")

    items = sales_order.get("items") or []
    if not isinstance(items, list) or not items:
        motivos.append("pedido sin productos")
        items = []

    # Duplicate item rows must consume their combined quantity, not each pass
    # independently against the same available Bin quantity.
    cantidades: dict[tuple[str, str], float] = {}
    for item in items:
        code = str(item.get("item_code") or "").strip()
        warehouse = str(item.get("warehouse") or "").strip()
        if not code or not warehouse:
            motivos.append("producto o depósito ausente")
            continue
        try:
            qty = _cantidad_en_stock_uom(item)
        except erpnext.ERPNextError:
            motivos.append(f"cantidad inválida para {code}")
            continue
        if qty <= 0:
            motivos.append(f"cantidad no positiva para {code}")
            continue
        key = (code, warehouse)
        cantidades[key] = cantidades.get(key, 0) + qty

    if STOCK_CONFIABLE:
        # This order's own quantity is the one being checked, so it must not be
        # counted as competition against itself; and an order that was asked
        # for LATER cannot take units away from this one.
        este_pedido = str(sales_order.get("name") or "").strip()
        empresa = str(sales_order.get("company") or "").strip()
        pedido_desde = str(sales_order.get("creation") or "").strip()
        for (code, warehouse), qty in cantidades.items():
            try:
                if not _hay_stock(
                    code,
                    qty,
                    warehouse,
                    excluir=este_pedido,
                    company=empresa,
                    desde=pedido_desde,
                ):
                    motivos.append(f"stock insuficiente de {code}")
            except erpnext.ERPNextError as exc:
                # The reason the team gets is deliberately the same for every
                # cause, so log the real one: a row cap or a 414 looks exactly
                # like an ERPNext outage from the outside.
                print(f"[policy] stock no verificable item={code} causa={exc}")
                motivos.append(f"no se pudo verificar stock de {code}")

    order_day = _order_day(sales_order, motivos)
    if PRICE_LIST and CURRENCY and order_day is not None:
        for item in items:
            code = str(item.get("item_code") or "").strip() or "producto"
            try:
                if not _precio_estandar(item, order_day):
                    motivos.append(f"precio fuera de lista en {code}")
            except erpnext.ERPNextError:
                motivos.append(f"no se pudo verificar precio de {code}")

    entrega = sales_order.get("delivery_date")
    try:
        today = _hoy_del_negocio()
        delivery_day = date.fromisoformat(str(entrega))
        if delivery_day < today:
            motivos.append("fecha de entrega vencida")
        elif delivery_day > today + timedelta(days=30):
            motivos.append("fecha de entrega muy lejana")
    except (ValueError, TypeError, erpnext.ERPNextError):
        motivos.append("fecha de entrega inválida")

    return Decision(not motivos, motivos)


def _order_day(sales_order: dict, motivos: list[str]) -> date | None:
    raw = sales_order.get("transaction_date")
    if not raw:
        try:
            return _hoy_del_negocio()
        except erpnext.ERPNextError:
            motivos.append("fecha del negocio no disponible")
            return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        motivos.append("fecha del pedido inválida")
        return None


def _saldo_vencido(cliente: str) -> float | None:
    """Return overdue balance, or None when privileged verification fails."""
    try:
        today = _hoy_del_negocio()
        rows = erpnext.policy_run_report(
            "Accounts Receivable",
            {
                "company": erpnext.default_company(),
                "customer": [cliente],
                "based_on": "Due Date",
                "report_date": today.isoformat(),
            },
        )
        overdue = 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            amount = _float(row.get("outstanding_amount"))
            if amount <= 0:
                continue
            raw_due = row.get("due_date")
            if not raw_due:
                raise erpnext.ERPNextError("reporte sin fecha de vencimiento")
            if date.fromisoformat(str(raw_due)) < today:
                overdue += amount
        return overdue
    except (erpnext.ERPNextError, ValueError, TypeError):
        return None


def _borradores_que_reservan(company: str, desde: str, propio: str) -> list[str]:
    """The draft orders that still hold the stock they promised.

    Asks ERPNext for the ORDERS first, and only for the ones that still
    reserve. Counting item rows first and discarding most of them afterwards
    let orders that reserve nothing — above all the rejected ones
    app/decisiones.py keeps for ever — fill the truncation cap, and once it
    filled, that product could never auto-confirm again.

    Skipped, and why:
      * ``docstatus != 0`` — a submitted order is already inside
        Bin.reserved_qty, and counting it here would subtract it twice; a
        cancelled one consumes nothing.
      * ``status`` in ESTADOS_SIN_RESERVA — the same states ERPNext itself does
        not count, which is where a manual rejection leaves the draft.
      * another company.
      * asked for AFTER the order being evaluated, FIFO on
        (creation, order id). Without it, two drafts for the last 8 units each
        defer to the other and a dairy that has the stock sells it to nobody;
        without the id in the key, the same happens whenever two drafts share a
        timestamp.
    """
    filtros: list[list] = [
        ["docstatus", "=", 0],
        ["status", "not in", list(ESTADOS_SIN_RESERVA)],
    ]
    if company:
        filtros.append(["company", "=", company])
    padres = erpnext.policy_get_list(
        "Sales Order",
        filters=filtros,
        fields=["name", "docstatus", "status", "company", "creation"],
        limit=MAX_BORRADORES + 1,
    )
    if len(padres) > MAX_BORRADORES:
        raise erpnext.ERPNextError(
            "demasiados pedidos en borrador para verificar el stock"
        )

    # Re-check locally what was asked of the server: the answer to "can this
    # confirm with nobody watching" cannot rest on the filters having been
    # honoured.
    vivos: list[str] = []
    for fila in padres:
        nombre = str(fila.get("name") or "").strip()
        if not nombre:
            raise erpnext.ERPNextError("ERPNext devolvió un pedido sin número")
        if not _es_borrador(fila):
            continue
        if company and str(fila.get("company") or "").strip() != company:
            continue
        if sin_reserva(fila.get("status")):
            continue
        if (
            desde
            and propio
            and not _reclamo_previo(fila.get("creation"), nombre, desde, propio)
        ):
            continue
        vivos.append(nombre)
    return vivos


def _comprometido_en_borradores(
    item_code: str, warehouse: str, *, excluir: str, company: str, desde: str
) -> float:
    """How much of this product is already promised in OTHER draft orders.

    ERPNext only raises Bin.reserved_qty when a Sales Order is SUBMITTED. Two
    drafts created minutes apart therefore both saw the same last units as
    free, and both passed the stock rule — one of those customers was going to
    find out on delivery day. Those units are already promised to somebody, so
    they are treated here as a virtual reservation.

    Raises rather than guessing. An unreadable or truncated answer must never
    be read as "nothing is promised": the caller turns the error into an order
    that waits for a person, which is the safe direction.
    """
    vivos = [
        nombre
        for nombre in _borradores_que_reservan(company, desde, excluir)
        if nombre != excluir
    ]
    if not vivos:
        return 0.0

    total = 0.0
    for inicio in range(0, len(vivos), LOTE_BORRADORES):
        lote = vivos[inicio : inicio + LOTE_BORRADORES]
        tope = len(lote) * MAX_RENGLONES_POR_PEDIDO
        filas = erpnext.policy_get_list(
            "Sales Order Item",
            filters=[
                ["item_code", "=", item_code],
                ["warehouse", "=", warehouse],
                ["docstatus", "=", 0],
                ["parent", "in", lote],
            ],
            fields=[
                "parent",
                "item_code",
                "warehouse",
                "docstatus",
                "qty",
                "stock_qty",
                "uom",
                "stock_uom",
                "conversion_factor",
            ],
            limit=tope + 1,
            parent="Sales Order",
        )
        if len(filas) > tope:
            raise erpnext.ERPNextError(
                f"demasiados renglones de {item_code} para verificar el stock"
            )
        permitidos = set(lote)
        for fila in filas:
            codigo = str(fila.get("item_code") or item_code).strip()
            deposito = str(fila.get("warehouse") or warehouse).strip()
            if codigo != item_code or deposito != warehouse:
                continue
            if not _es_borrador(fila):
                continue
            pedido = str(fila.get("parent") or "").strip()
            if pedido not in permitidos:
                continue
            promesa = _cantidad_en_stock_uom(fila)
            if promesa < 0:
                raise erpnext.ERPNextError(
                    f"cantidad negativa en un borrador de {item_code}"
                )
            total += promesa
    return total


def comprometido_en_borradores(item_code: str, warehouse: str) -> float:
    """Units of this product already promised in orders nobody confirmed yet.

    Public entry point for read-only callers that just want the figure — the
    orientative level the customer agent answers with. It counts EVERY live
    claim: there is no order of our own to exclude and no place in the queue to
    respect, because nobody has ordered anything yet.

    The auto-confirmation rule uses the keyword form directly instead; it has
    both of those to take into account.
    """
    return _comprometido_en_borradores(
        item_code,
        warehouse,
        excluir="",
        company=erpnext.default_company(),
        desde="",
    )


def _hay_stock(
    item_code: str,
    qty: float,
    warehouse: str,
    *,
    excluir: str = "",
    company: str = "",
    desde: str = "",
) -> bool:
    """True only when ``qty`` fits above the safety buffer, drafts included."""
    if not warehouse or qty <= 0:
        return False
    # A misconfigured buffer is refused before anything is read: it would
    # otherwise turn the whole rule upside down.
    buffer = float(os.getenv("STOCK_BUFFER_PCT", "20")) / 100.0
    if buffer < 0 or buffer >= 1:
        raise erpnext.ERPNextError("STOCK_BUFFER_PCT fuera de rango")
    bins = erpnext.get_list(
        "Bin",
        filters=[
            ["item_code", "=", item_code],
            ["warehouse", "=", warehouse],
        ],
        fields=["actual_qty", "reserved_qty"],
        limit=10,
    )
    disponible = sum(
        _float(row.get("actual_qty")) - _float(row.get("reserved_qty"))
        for row in bins
    )
    disponible -= _comprometido_en_borradores(
        item_code, warehouse, excluir=excluir, company=company, desde=desde
    )
    # One subtraction per competing draft accumulates binary error: 10 minus
    # three promises of 1.1 is 6.699999999999999, which refused an order for
    # exactly 6.7 that the dairy could fill. Quantising the availability keeps
    # the never-oversell direction — it moves by less than a millionth of a
    # unit, far below anything a dairy weighs.
    disponible = round(disponible, 6)
    return disponible * (1 - buffer) >= qty


def _precio_estandar(item: dict, order_day: date) -> bool:
    """Verify exact unscoped price, currency, UOM, validity, and no discounts."""
    code = str(item.get("item_code") or "").strip()
    uom = str(item.get("uom") or "").strip()
    stock_uom = str(item.get("stock_uom") or "").strip()
    if not code or not uom or uom != stock_uom:
        return False
    if abs(_float(item.get("conversion_factor")) - 1.0) > 0.000001:
        return False
    for field_name in (
        "discount_percentage",
        "discount_amount",
        "distributed_discount_amount",
    ):
        if not _zero(item.get(field_name)):
            return False

    rate = _float(item.get("rate"))
    list_rate = _float(item.get("price_list_rate"))
    if rate <= 0 or list_rate <= 0 or abs(rate - list_rate) >= 0.01:
        return False

    prices = erpnext.get_list(
        "Item Price",
        filters=[
            ["item_code", "=", code],
            ["selling", "=", 1],
            ["price_list", "=", PRICE_LIST],
            ["currency", "=", CURRENCY],
        ],
        fields=[
            "price_list_rate",
            "price_list",
            "currency",
            "uom",
            "valid_from",
            "valid_upto",
            "customer",
            "batch_no",
        ],
        limit=100,
    )
    for price in prices:
        if str(price.get("price_list") or "") != PRICE_LIST:
            continue
        if str(price.get("currency") or "") != CURRENCY:
            continue
        if str(price.get("uom") or "") != uom:
            continue
        if price.get("customer") or price.get("batch_no"):
            continue
        try:
            valid_from = (
                date.fromisoformat(str(price["valid_from"]))
                if price.get("valid_from")
                else date.min
            )
            valid_upto = (
                date.fromisoformat(str(price["valid_upto"]))
                if price.get("valid_upto")
                else date.max
            )
            configured_rate = _float(price.get("price_list_rate"))
        except (ValueError, erpnext.ERPNextError):
            continue
        if valid_from <= order_day <= valid_upto and abs(configured_rate - rate) < 0.01:
            return True
    return False


@contextmanager
def auto_submit_lock() -> Iterator[None]:
    """Serialize the final stock recheck and submit across all app workers."""
    with distributed_lock("auto-submit-global", lease_seconds=300, wait_seconds=5):
        yield
