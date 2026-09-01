"""Read-only tools. No approval needed — these can never change anything.

Ojo: "no cambian nada" no quiere decir "cualquiera puede leer cualquier
cosa". `estado_pedido` y `pedido_habitual` están acotadas al cliente que
escribió (ver app/tools/alcance.py); antes no lo estaban.
"""

from __future__ import annotations

import os

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app import erpnext, formato, log
from app.tools import alcance

_log = log.get("catalogo")


def _stock_confiable() -> bool:
    # Se lee en cada llamada, no al importar: así el interruptor se puede
    # girar con un restart del contenedor y sin rebuild.
    return os.getenv("STOCK_CONFIABLE", "false").lower() == "true"


@tool
def buscar_producto(consulta: str) -> str:
    """Busca productos del catálogo por nombre. Usar cuando el cliente
    menciona un producto (leche, queso, yogur, manteca, dulce de leche)."""
    try:
        items = erpnext.get_list(
            "Item",
            filters=[["item_name", "like", f"%{consulta}%"], ["disabled", "=", 0]],
            fields=["item_code", "item_name", "stock_uom", "description"],
            limit=8,
        )
    except erpnext.ERPNextError:
        return "No pude consultar el catálogo ahora. Avisale que verificás y volvés."
    if not items:
        return f"No se encontraron productos para '{consulta}'."

    out = []
    for it in items:
        try:
            precios = erpnext.get_list(
                "Item Price",
                filters=[["item_code", "=", it["item_code"]], ["selling", "=", 1]],
                fields=["price_list_rate", "currency"],
                limit=1,
                order_by="valid_from desc",
            )
        except erpnext.ERPNextError:
            precios = []
        precio = (
            f"{precios[0].get('currency') or ''} {precios[0]['price_list_rate']}".strip()
            if precios
            else "precio a confirmar"
        )
        out.append(f"- {it['item_name']} ({it['item_code']}) — {precio} por {it['stock_uom']}")
    return "\n".join(out)


@tool
def consultar_stock(item_code: str) -> str:
    """Consulta la disponibilidad de un producto.

    Devuelve un NIVEL, no un número exacto, porque parte de las ventas
    ocurren fuera del sistema (mostrador, reparto) y el número exacto puede
    estar desactualizado. Nunca le prometas al cliente una cantidad exacta.
    """
    if not _stock_confiable():
        # Ni siquiera consultamos: en fase 1 la respuesta no depende del
        # número, y así no gastamos una llamada REST por mensaje.
        return (
            f"{item_code}: no confirmes disponibilidad exacta. Decí que lo cargás "
            f"y que el equipo confirma stock al preparar el pedido."
        )

    try:
        bins = erpnext.get_list(
            "Bin",
            filters=[["item_code", "=", item_code]],
            fields=["warehouse", "actual_qty", "reserved_qty"],
            limit=20,
        )
    except erpnext.ERPNextError:
        return f"No pude consultar stock de {item_code}. Decí que verificás con el equipo."
    if not bins:
        return f"Sin registro de stock para {item_code}. Confirmá con el equipo."

    # OJO: reserved_qty en ERPNext refleja pedidos CONFIRMADOS, no borradores.
    # Lo prometido en borradores lo descuenta policy._comprometido_en_borradores
    # antes de auto-confirmar. Acá solo damos un nivel, así que el buffer
    # alcanza para absorber esa diferencia.
    fisico = sum(float(b.get("actual_qty") or 0) for b in bins)
    reservado = sum(float(b.get("reserved_qty") or 0) for b in bins)
    disponible = fisico - reservado
    buffer = float(os.getenv("STOCK_BUFFER_PCT", "20")) / 100.0
    seguro = disponible * (1 - buffer)

    if seguro <= 0:
        return f"{item_code}: SIN STOCK. Ofrecé una alternativa o anotá el pedido para cuando haya."
    if seguro < float(os.getenv("STOCK_POCO", "20")):
        return f"{item_code}: POCO STOCK. Podés tomar pedidos chicos, avisá que es sujeto a confirmación."
    return f"{item_code}: DISPONIBLE."


@tool
def estado_pedido(numero_pedido: str, config: RunnableConfig = None) -> str:
    """Consulta el estado de un pedido existente por su número.

    Solo devuelve pedidos del cliente que está escribiendo.
    """
    try:
        so = erpnext.get_doc("Sales Order", numero_pedido)
    except erpnext.ERPNextError:
        return f"No encontré el pedido {numero_pedido}."

    if not alcance.puede_ver_pedido(config, so):
        # Misma respuesta que "no existe": no confirmamos ni desmentimos la
        # existencia de un pedido de otro cliente.
        return f"No encontré el pedido {numero_pedido}."

    estados = {0: "borrador (pendiente de confirmación)", 1: "confirmado", 2: "cancelado"}
    return (
        f"Pedido {so['name']}: {estados.get(so.get('docstatus'), 'desconocido')}. "
        f"Total: {so.get('currency', '')} {so.get('grand_total', 0)}. "
        f"Entrega estimada: {so.get('delivery_date', 'a confirmar')}."
    )


@tool
def pedido_habitual(config: RunnableConfig = None) -> str:
    """Devuelve el último pedido confirmado del cliente que escribe, para
    repetirlo. Usar cuando dice "lo de siempre", "lo mismo que la vez
    pasada", "repetime el pedido".

    Es el camino más rápido: la mayoría de los clientes piden siempre lo mismo.
    """
    cliente = alcance.cliente_code(config)
    if not cliente:
        return (
            "Todavía no tengo su ficha, así que no tengo pedidos anteriores. "
            "Pedile qué necesita y registralo con crear_lead."
        )
    try:
        sos = erpnext.get_list(
            "Sales Order",
            filters=[["customer", "=", cliente], ["docstatus", "=", 1]],
            fields=["name"],
            limit=1,
            order_by="transaction_date desc",
        )
    except erpnext.ERPNextError:
        return "No pude consultar sus pedidos anteriores. Pedile qué necesita."
    if not sos:
        return "No tiene pedidos anteriores confirmados. Pedile qué necesita."

    so = erpnext.get_doc("Sales Order", sos[0]["name"])
    lineas = "\n".join(
        f"  · {float(i.get('qty') or 0):g} x {i.get('item_name') or i['item_code']} "
        f"({i['item_code']})"
        for i in so.get("items", [])
    )
    return (
        f"Último pedido ({so['name']}, {so.get('transaction_date')}):\n{lineas}\n"
        f"Total {formato.pesos(float(so.get('grand_total') or 0))}. "
        f"Confirmá con el cliente si quiere lo mismo y usá crear_pedido con esos "
        f"item_code."
    )
