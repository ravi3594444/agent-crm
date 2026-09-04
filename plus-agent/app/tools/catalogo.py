"""Read-only customer/management tools with server-enforced authorization."""
import os
from datetime import date

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app import erpnext, inventario, policy
from app.formato import pesos
from app.runtime_context import RuntimeContextError, actor_context, require_customer


@tool
def buscar_producto(consulta: str) -> str:
    """Busca productos del catálogo por nombre y muestra su unidad exacta."""
    items = erpnext.get_list(
        "Item",
        filters=[["item_name", "like", f"%{consulta}%"], ["disabled", "=", 0]],
        fields=["item_code", "item_name", "stock_uom", "description"],
        limit=8,
    )
    if not items:
        return f"No se encontraron productos para '{consulta}'."

    price_list = os.getenv("AUTO_CONFIRM_PRICE_LIST", "").strip()
    currency = os.getenv("AUTO_CONFIRM_CURRENCY", "").strip()
    try:
        today = policy._hoy_del_negocio()
    except erpnext.ERPNextError:
        today = None
    out = []
    for item in items:
        filters = [["item_code", "=", item["item_code"]], ["selling", "=", 1]]
        if price_list:
            filters.append(["price_list", "=", price_list])
        if currency:
            filters.append(["currency", "=", currency])
        prices = (
            erpnext.get_list(
                "Item Price",
                filters=filters,
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
                limit=20,
            )
            if price_list and currency and today is not None
            else []
        )
        matching = next(
            (
                price
                for price in prices
                if _catalog_price_is_valid(
                    price,
                    str(item.get("stock_uom") or ""),
                    price_list,
                    currency,
                    today,
                )
            ),
            None,
        )
        price_text = (
            f"{matching['currency']} {matching['price_list_rate']}"
            if matching
            else "precio a confirmar"
        )
        out.append(
            f"- {item['item_name']} ({item['item_code']}) — {price_text} "
            f"por {item['stock_uom']}"
        )
    return "\n".join(out)


def _catalog_price_is_valid(
    price: dict,
    uom: str,
    price_list: str,
    currency: str,
    today: date,
) -> bool:
    if str(price.get("price_list") or "") != price_list:
        return False
    if str(price.get("currency") or "") != currency:
        return False
    if str(price.get("uom") or "") != uom:
        return False
    if price.get("customer") or price.get("batch_no"):
        return False
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
    except ValueError:
        return False
    return valid_from <= today <= valid_upto


@tool
def consultar_stock(item_code: str) -> str:
    """Consulta un nivel orientativo en el depósito de preparación."""
    try:
        warehouse = erpnext.default_warehouse()
    except erpnext.ERPNextError:
        return (
            f"No pude verificar el depósito de preparación para {item_code}. "
            "No confirmes disponibilidad."
        )
    # Trust is earned per product by a confirmed count, and it expires.
    fresco, sin_confianza = inventario.confiable(item_code, warehouse)
    if not fresco:
        return (
            f"{item_code}: {sin_confianza}. No confirmes disponibilidad; "
            "el pedido solo puede quedar pendiente de revisión."
        )
    try:
        bins = erpnext.get_list(
            "Bin",
            filters=[
                ["item_code", "=", item_code],
                ["warehouse", "=", warehouse],
            ],
            fields=["warehouse", "actual_qty", "reserved_qty"],
            limit=10,
        )
    except erpnext.ERPNextError:
        return (
            f"No pude verificar el depósito de preparación para {item_code}. "
            "No confirmes disponibilidad."
        )
    if not bins:
        return (
            f"Sin registro de stock para {item_code} en el depósito de preparación. "
            "No confirmes disponibilidad."
        )

    available = sum(
        float(row.get("actual_qty") or 0) - float(row.get("reserved_qty") or 0)
        for row in bins
    )
    try:
        # The same deduction the auto-confirmation rule makes. A draft holds
        # units ERPNext has not reserved yet, so on Bin alone this tool
        # answered "hay stock" for milk another customer is already waiting
        # for — and the customer heard that as a promise.
        available -= policy.comprometido_en_borradores(item_code, warehouse)
    except erpnext.ERPNextError:
        return (
            f"No pude verificar cuánto de {item_code} ya está comprometido. "
            "No confirmes disponibilidad."
        )
    buffer = float(os.getenv("STOCK_BUFFER_PCT", "20")) / 100.0
    if buffer < 0 or buffer >= 1:
        return f"{item_code}: configuración de stock inválida. No confirmes disponibilidad."
    safe = available * (1 - buffer)
    if safe <= 0:
        return f"{item_code}: SIN STOCK. Ofrecé una alternativa."
    if safe < float(os.getenv("STOCK_POCO", "20")):
        return (
            f"{item_code}: POCO STOCK. El pedido requiere validación de cantidad "
            "y puede quedar pendiente."
        )
    return (
        f"{item_code}: stock registrado. La cantidad exacta se vuelve a validar "
        "al crear y antes de confirmar el pedido."
    )


@tool
def estado_pedido(numero_pedido: str, config: RunnableConfig) -> str:
    """Consulta un pedido; clientes solo pueden ver pedidos de su propia cuenta."""
    try:
        actor = actor_context(config)
    except RuntimeContextError:
        return "No pude autorizar la consulta del pedido."
    try:
        order = erpnext.get_doc("Sales Order", numero_pedido)
    except erpnext.ERPNextError:
        return f"No encontré el pedido {numero_pedido}."
    # `gerencia_verificada`, no `is_management`: el alcance lo pone el webhook,
    # pero leer el pedido de CUALQUIER cliente lo habilita únicamente un
    # teléfono que sigue estando en la lista del equipo.
    if not actor.gerencia_verificada and (
        not actor.customer_code or order.get("customer") != actor.customer_code
    ):
        # Deliberately indistinguishable from a missing order to prevent ID
        # enumeration across customer accounts.
        return f"No encontré el pedido {numero_pedido}."
    states = {
        0: "borrador (pendiente de confirmación)",
        1: "confirmado",
        2: "cancelado",
    }
    return (
        f"Pedido {order['name']}: "
        f"{states.get(order.get('docstatus'), 'desconocido')}. "
        f"Total: {order.get('currency', '')} {order.get('grand_total', 0)}. "
        f"Entrega estimada: {order.get('delivery_date', 'a confirmar')}."
    )


@tool
def pedido_habitual(config: RunnableConfig) -> str:
    """Devuelve el último pedido confirmado del cliente autenticado."""
    try:
        actor = require_customer(config)
    except RuntimeContextError:
        return "No pude identificar una cuenta de cliente registrada."
    orders = erpnext.get_list(
        "Sales Order",
        filters=[["customer", "=", actor.customer_code], ["docstatus", "=", 1]],
        fields=["name"],
        limit=1,
    )
    if not orders:
        return "Esta cuenta no tiene pedidos anteriores confirmados."
    order = erpnext.get_doc("Sales Order", orders[0]["name"])
    lines = "\n".join(
        f"  · {float(item['qty']):g} {item.get('uom') or item.get('stock_uom')} "
        f"de {item.get('item_name') or item['item_code']}"
        for item in order.get("items", [])
    )
    return (
        f"Último pedido ({order['name']}, {order.get('transaction_date')}):\n{lines}\n"
        f"Total {pesos(order.get('grand_total', 0))}. Confirmá productos, cantidades, "
        "unidades y una nueva fecha de entrega antes de crear otro pedido."
    )
