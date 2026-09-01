"""Offline-sale capture. The hardest problem in the whole build.

THE PROBLEM
The owner has always sold offline: counter, truck route, phone calls, cash.
None of that touches ERPNext. So stock in the system drifts from reality
within one day, and the WhatsApp bot starts promising milk that is already
in someone's fridge. A bot with wrong stock is WORSE than no bot — it
damages his reputation with his own customers.

THE DESIGN
Do not ask him to learn data entry. Let the staff report sales the way they
already communicate: a WhatsApp message. These tools turn "vendí 20 litros
a Don José" into a real ERPNext document — as a DRAFT, like everything else.

Capture first. Accurate stock promises come only AFTER capture works.

DOS PAYLOADS QUE ROMPÍAN AL CONFIRMAR
1. La factura llevaba `is_pos: 1`. En ERPNext eso exige un POS Profile y la
   tabla de pagos; sin eso el borrador se guarda pero el submit del dueño
   falla con un error incomprensible — justo en el momento en que él está
   confiando en el sistema. Ahora solo se pone si hay POS Profile
   configurado, y el "cobrado" se anota como forma de pago o en el remark.
2. La Stock Reconciliation no llevaba `valuation_rate`. Para productos sin
   stock previo ERPNext lo exige (el propio deploy/seed_dairy.py lo manda:
   este archivo era el inconsistente).
"""

from __future__ import annotations

import os
from datetime import date

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app import erpnext, log

_log = log.get("captura")

POS_PROFILE = os.getenv("ERPNEXT_POS_PROFILE", "").strip()


class LineaVenta(BaseModel):
    item_code: str = Field(description="Código del producto")
    cantidad: float = Field(gt=0)
    precio_unitario: float | None = Field(default=None, description="Si difiere de lista")


@tool
def registrar_venta_offline(
    cliente: str,
    lineas: list[LineaVenta],
    cobrado: bool = True,
    nota: str = "",
) -> str:
    """Registra una venta que ya ocurrió fuera del sistema (mostrador, reparto,
    teléfono). Usar cuando alguien del equipo dice que vendió algo.

    Ejemplos: "vendí 20 litros a Don José", "el reparto entregó 15 kg de queso
    a La Esquina", "salieron 3 hormas al mostrador".

    Crea una factura en BORRADOR. El dueño la confirma y recién ahí baja stock.
    """
    if not lineas:
        return "Necesito saber qué productos se vendieron."

    deposito = erpnext.default_warehouse()
    items = []
    for linea in lineas:
        item = {"item_code": linea.item_code, "qty": linea.cantidad, "warehouse": deposito}
        if linea.precio_unitario is not None:
            item["rate"] = linea.precio_unitario
        items.append(item)

    payload: dict = {
        "customer": cliente,
        "posting_date": date.today().isoformat(),
        "update_stock": 1,  # this invoice moves stock on submit
        "set_warehouse": deposito,
        "items": items,
        "remarks": (
            f"Venta offline registrada por WhatsApp. "
            f"{'Cobrada en efectivo. ' if cobrado else 'A cuenta (no cobrada). '}"
            f"{nota}"
        ).strip(),
    }

    # `is_pos` solo si hay un POS Profile de verdad. Sin él, el submit falla.
    if cobrado and POS_PROFILE:
        payload["is_pos"] = 1
        payload["pos_profile"] = POS_PROFILE

    try:
        doc = erpnext.create_doc("Sales Invoice", payload)
    except erpnext.ERPNextError as e:
        _log.error("no pude cargar la venta offline de %s: %s", cliente, e)
        return (
            f"No pude cargarlo: {e}. Fijate que el cliente '{cliente}' y los "
            f"códigos de producto existan en el sistema."
        )

    erpnext.add_comment(
        "Sales Invoice",
        doc["name"],
        "Venta offline cargada por Agente IA vía WhatsApp. Requiere confirmación.",
    )
    detalle = ", ".join(f"{linea.cantidad:g} x {linea.item_code}" for linea in lineas)
    aviso = (
        ""
        if (not cobrado or POS_PROFILE)
        else (
            " (ojo: no hay POS Profile configurado, así que la cobranza la tenés "
            "que registrar a mano al confirmar)"
        )
    )
    return (
        f"Cargado como {doc['name']} en borrador ({detalle}) para {cliente}. "
        f"Confirmalo en el sistema y se descuenta del stock.{aviso}"
    )


@tool
def contar_stock(item_code: str, cantidad_real: float, deposito: str = "") -> str:
    """Corrige el stock de un producto al valor contado físicamente.
    Usar en el conteo de la mañana o cuando alguien dice "quedan X".

    Ejemplo: "quedan 12 kilos de queso cremoso" -> contar_stock('QUE-CRE', 12)
    """
    if cantidad_real < 0:
        return "La cantidad contada no puede ser negativa."

    dep = deposito or erpnext.default_warehouse()
    try:
        bins = erpnext.get_list(
            "Bin",
            filters=[["item_code", "=", item_code], ["warehouse", "=", dep]],
            fields=["actual_qty", "valuation_rate"],
            limit=1,
        )
    except erpnext.ERPNextError as e:
        return f"No pude leer el stock actual de {item_code}: {e}"

    sistema = float(bins[0].get("actual_qty") or 0) if bins else 0.0
    valuacion = float(bins[0].get("valuation_rate") or 0) if bins else 0.0
    if valuacion <= 0:
        valuacion = _valuacion_de_respaldo(item_code)

    renglon: dict = {"item_code": item_code, "warehouse": dep, "qty": cantidad_real}
    if valuacion > 0:
        # Obligatorio cuando el producto no tiene stock previo. Sin esto el
        # submit del dueño falla.
        renglon["valuation_rate"] = valuacion

    try:
        doc = erpnext.create_doc(
            "Stock Reconciliation",
            {
                "purpose": "Stock Reconciliation",
                "posting_date": date.today().isoformat(),
                "company": erpnext.default_company(),
                "items": [renglon],
            },
        )
    except erpnext.ERPNextError as e:
        _log.error("no pude cargar el conteo de %s: %s", item_code, e)
        return f"No pude cargar el conteo de {item_code}: {e}"

    erpnext.add_comment(
        "Stock Reconciliation",
        doc["name"],
        f"Conteo físico por WhatsApp. Sistema: {sistema:g}, contado: {cantidad_real:g}.",
    )
    dif = cantidad_real - sistema
    signo = "faltan" if dif < 0 else "sobran"
    falta_valuacion = (
        " (ojo: no encontré valuación para este producto, puede que tengas que "
        "completarla al confirmar)"
        if valuacion <= 0
        else ""
    )
    return (
        f"Conteo cargado ({doc['name']}). El sistema decía {sistema:g}, "
        f"vos contaste {cantidad_real:g} — {signo} {abs(dif):g}. "
        f"Confirmá el ajuste en el sistema.{falta_valuacion}"
    )


def _valuacion_de_respaldo(item_code: str) -> float:
    """Si el Bin no tiene valuación (producto sin movimientos), buscamos la
    del Item, y si no hay, el precio de compra."""
    try:
        items = erpnext.get_list(
            "Item",
            filters=[["item_code", "=", item_code]],
            fields=["valuation_rate", "last_purchase_rate"],
            limit=1,
        )
    except erpnext.ERPNextError:
        return 0.0
    if not items:
        return 0.0
    it = items[0]
    return float(it.get("valuation_rate") or it.get("last_purchase_rate") or 0)


@tool
def confirmar_entrega(numero_pedido: str, nota: str = "") -> str:
    """Marca un pedido como entregado por el reparto.
    Usar cuando el repartidor avisa que dejó la mercadería."""
    try:
        so = erpnext.get_doc("Sales Order", numero_pedido)
    except erpnext.ERPNextError:
        return f"No encontré el pedido {numero_pedido}."
    if so.get("docstatus") != 1:
        return (
            f"El pedido {numero_pedido} todavía está en borrador. "
            f"Hay que confirmarlo antes de marcarlo entregado."
        )

    # Solo lo que falta entregar: si ya hubo un remito parcial, no lo
    # duplicamos. ERPNext rechaza la sobre-entrega, pero mejor que el
    # borrador salga bien de entrada.
    renglones = []
    for i in so.get("items", []):
        pendiente = float(i.get("qty") or 0) - float(i.get("delivered_qty") or 0)
        if pendiente <= 0:
            continue
        renglones.append(
            {
                "item_code": i["item_code"],
                "qty": pendiente,
                "against_sales_order": so["name"],
                "so_detail": i["name"],
                "warehouse": i.get("warehouse") or erpnext.default_warehouse(),
            }
        )
    if not renglones:
        return f"El pedido {numero_pedido} ya está entregado por completo."

    try:
        doc = erpnext.create_doc(
            "Delivery Note",
            {
                "customer": so["customer"],
                "posting_date": date.today().isoformat(),
                "company": erpnext.default_company(),
                "items": renglones,
                "remarks": f"Entrega reportada por WhatsApp. {nota}".strip(),
            },
        )
    except erpnext.ERPNextError as e:
        _log.error("no pude crear el remito de %s: %s", numero_pedido, e)
        return f"No pude crear el remito de {numero_pedido}: {e}"

    erpnext.add_comment(
        "Delivery Note",
        doc["name"],
        f"Entrega de {so['name']} reportada por WhatsApp.",
    )
    return (
        f"Remito {doc['name']} creado en borrador para {so['customer']}. "
        f"Confirmalo y baja el stock."
    )


@tool
def redactar_mensaje_cliente(cliente: str, intencion: str) -> str:
    """Redacta un mensaje de WhatsApp para enviarle a un cliente.
    NO lo envía — devuelve el texto para que una persona lo revise y mande.

    Ejemplo: "avisale a Don José que ya llegó el queso cremoso".
    """
    try:
        fichas = erpnext.get_list(
            "Customer",
            filters=[["customer_name", "like", f"%{cliente}%"]],
            fields=["customer_name", "mobile_no"],
            limit=1,
        )
    except erpnext.ERPNextError as e:
        return f"No pude buscar a '{cliente}': {e}"
    if not fichas:
        return f"No encontré a '{cliente}' en el sistema."
    c = fichas[0]
    return (
        f"Borrador para {c['customer_name']} ({c.get('mobile_no') or 'sin teléfono'}), "
        f"sobre: {intencion}\n\n"
        f"[Redactá el mensaje acá, en tono cordial y breve, y mostráselo al usuario "
        f"para que lo apruebe antes de mandarlo.]"
    )
