"""Read-only tools. No approval needed — these can never change anything."""
import os

from langchain_core.tools import tool

from app import erpnext

# Master switch. Leave FALSE until offline sales are actually being captured.
# A bot that promises stock it does not have is worse than no bot at all.
STOCK_CONFIABLE = os.getenv("STOCK_CONFIABLE", "false").lower() == "true"


@tool
def buscar_producto(consulta: str) -> str:
    """Busca productos del catálogo por nombre. Usar cuando el cliente
    menciona un producto (leche, queso, yogur, manteca, dulce de leche)."""
    items = erpnext.get_list(
        "Item",
        filters=[["item_name", "like", f"%{consulta}%"], ["disabled", "=", 0]],
        fields=["item_code", "item_name", "stock_uom", "description"],
        limit=8,
    )
    if not items:
        return f"No se encontraron productos para '{consulta}'."

    out = []
    for it in items:
        precios = erpnext.get_list(
            "Item Price",
            filters=[["item_code", "=", it["item_code"]], ["selling", "=", 1]],
            fields=["price_list_rate", "currency"],
            limit=1,
        )
        precio = (
            f"{precios[0]['currency']} {precios[0]['price_list_rate']}"
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
    bins = erpnext.get_list(
        "Bin",
        filters=[["item_code", "=", item_code]],
        fields=["warehouse", "actual_qty", "reserved_qty"],
        limit=10,
    )
    if not bins:
        return f"Sin registro de stock para {item_code}. Confirmá con el equipo."

    # Reserved qty covers drafts already promised, so two customers can't be
    # sold the same milk. The buffer absorbs offline sales not yet loaded.
    disponible = sum(b["actual_qty"] - b.get("reserved_qty", 0) for b in bins)
    buffer = float(os.getenv("STOCK_BUFFER_PCT", "20")) / 100.0
    seguro = disponible * (1 - buffer)

    if not STOCK_CONFIABLE:
        return (
            f"{item_code}: no confirmes disponibilidad exacta. Decí que lo cargás "
            f"y que el equipo confirma stock al preparar el pedido."
        )
    if seguro <= 0:
        return f"{item_code}: SIN STOCK. Ofrecé una alternativa o anotá el pedido para cuando haya."
    if seguro < float(os.getenv("STOCK_POCO", "20")):
        return f"{item_code}: POCO STOCK. Podés tomar pedidos chicos, avisá que es sujeto a confirmación."
    return f"{item_code}: DISPONIBLE."


@tool
def estado_pedido(numero_pedido: str) -> str:
    """Consulta el estado de un pedido existente por su número."""
    try:
        so = erpnext.get_doc("Sales Order", numero_pedido)
    except erpnext.ERPNextError:
        return f"No encontré el pedido {numero_pedido}."
    estados = {0: "borrador (pendiente de confirmación)", 1: "confirmado", 2: "cancelado"}
    return (
        f"Pedido {so['name']}: {estados.get(so['docstatus'], 'desconocido')}. "
        f"Total: {so.get('currency', '')} {so.get('grand_total', 0)}. "
        f"Entrega estimada: {so.get('delivery_date', 'a confirmar')}."
    )


@tool
def pedido_habitual(cliente: str) -> str:
    """Devuelve el último pedido confirmado del cliente, para repetirlo.
    Usar cuando dice "lo de siempre", "lo mismo que la vez pasada", "repetime el pedido".
    Es el camino más rápido: la mayoría de los clientes piden siempre lo mismo."""
    sos = erpnext.get_list(
        "Sales Order",
        filters=[["customer", "=", cliente], ["docstatus", "=", 1]],
        fields=["name"],
        limit=1,
    )
    if not sos:
        return f"{cliente} no tiene pedidos anteriores confirmados."
    so = erpnext.get_doc("Sales Order", sos[0]["name"])
    lineas = "\n".join(
        f"  · {i['qty']:g} x {i.get('item_name') or i['item_code']}"
        for i in so.get("items", [])
    )
    return (
        f"Último pedido ({so['name']}, {so.get('transaction_date')}):\n{lineas}\n"
        f"Total ${so.get('grand_total', 0):,.0f}. "
        f"Confirmá con el cliente si quiere lo mismo y usá crear_pedido."
    )
