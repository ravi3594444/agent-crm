"""Management tools — the owner's assistant. READ-heavy, no customer exposure.

THE RULE THAT MATTERS HERE:
The agent must never compute business numbers itself. LLMs are bad at
arithmetic over long lists and will confidently invent a total.
Instead it calls ERPNext's own Query Reports — the same numbers his
accountant sees — and explains the result.

Deterministic numbers. LLM explanation. Never the other way round.
"""
from datetime import timedelta

from langchain_core.tools import tool

from app import erpnext, policy


@tool
def ejecutar_reporte(nombre_reporte: str, filtros: dict | None = None) -> str:
    """Ejecuta un reporte oficial de ERPNext y devuelve los datos reales.

    Reportes disponibles: 'Accounts Receivable', 'Stock Balance',
    'Sales Analytics', 'Gross Profit', 'Item-wise Sales History',
    'Sales Order Analysis', 'Stock Projected Qty'.

    Usar SIEMPRE esta herramienta para cifras. Nunca calcular a mano.
    """
    data = erpnext.run_report(nombre_reporte, filtros or {})
    if not data:
        return f"El reporte '{nombre_reporte}' no devolvió filas."
    filas = data[:40]
    return f"Reporte '{nombre_reporte}' ({len(data)} filas, muestro {len(filas)}):\n" + "\n".join(
        str(f) for f in filas
    )


@tool
def pedidos_pendientes() -> str:
    """Pedidos en borrador esperando confirmación del equipo.
    Esto es lo primero que debería revisar el dueño cada mañana."""
    sos = erpnext.get_list(
        "Sales Order",
        filters=[["docstatus", "=", 0]],
        fields=["name", "customer", "grand_total", "delivery_date", "creation"],
        limit=50,
    )
    if not sos:
        return "No hay pedidos pendientes de confirmación."
    lineas = [
        f"- {s['name']} · {s['customer']} · ${s['grand_total']:,.0f} · entrega {s['delivery_date']}"
        for s in sos
    ]
    return f"{len(sos)} pedidos pendientes de confirmar:\n" + "\n".join(lineas)


@tool
def ventas_del_periodo(dias: int = 7) -> str:
    """Ventas confirmadas de los últimos N días."""
    desde = (policy._hoy_del_negocio() - timedelta(days=dias)).isoformat()
    sos = erpnext.get_list(
        "Sales Order",
        filters=[["docstatus", "=", 1], ["transaction_date", ">=", desde]],
        fields=["name", "customer", "grand_total", "transaction_date"],
        limit=500,
    )
    total = sum(s["grand_total"] for s in sos)
    return (
        f"Últimos {dias} días: {len(sos)} pedidos confirmados, "
        f"total ${total:,.0f}. Promedio ${total / len(sos):,.0f} por pedido."
        if sos
        else f"Sin pedidos confirmados en los últimos {dias} días."
    )


@tool
def stock_bajo() -> str:
    """Productos por debajo del punto de reposición. Riesgo de quiebre de stock."""
    items = erpnext.get_list(
        "Item Reorder",
        fields=["parent", "warehouse", "warehouse_reorder_level"],
        limit=200,
    )
    alertas = []
    for it in items:
        bins = erpnext.get_list(
            "Bin",
            filters=[["item_code", "=", it["parent"]], ["warehouse", "=", it["warehouse"]]],
            fields=["actual_qty"],
            limit=1,
        )
        qty = bins[0]["actual_qty"] if bins else 0
        if qty <= it["warehouse_reorder_level"]:
            alertas.append(
                f"- {it['parent']}: {qty:g} (mínimo {it['warehouse_reorder_level']:g})"
            )
    return "Stock bajo:\n" + "\n".join(alertas) if alertas else "Sin alertas de stock."


@tool
def cobranzas_vencidas() -> str:
    """Facturas vencidas y no cobradas. Usa el reporte oficial de ERPNext."""
    data = erpnext.run_report("Accounts Receivable", {"company": erpnext.default_company()})
    vencidas = [r for r in data if isinstance(r, dict) and (r.get("outstanding_amount") or 0) > 0]
    if not vencidas:
        return "No hay saldos pendientes de cobro."
    total = sum(r["outstanding_amount"] for r in vencidas)
    top = sorted(vencidas, key=lambda r: -r["outstanding_amount"])[:10]
    lineas = [
        f"- {r.get('customer_name') or r.get('party')}: ${r['outstanding_amount']:,.0f}"
        for r in top
    ]
    return f"Total a cobrar ${total:,.0f} en {len(vencidas)} facturas.\n" + "\n".join(lineas)


@tool
def ficha_cliente(nombre_o_codigo: str) -> str:
    """Vista 360 de un cliente: datos, últimos pedidos y saldo."""
    clientes = erpnext.get_list(
        "Customer",
        filters=[["customer_name", "like", f"%{nombre_o_codigo}%"]],
        fields=["name", "customer_name", "customer_group", "mobile_no"],
        limit=1,
    )
    if not clientes:
        return f"No encontré un cliente que coincida con '{nombre_o_codigo}'."
    c = clientes[0]
    sos = erpnext.get_list(
        "Sales Order",
        filters=[["customer", "=", c["name"]], ["docstatus", "=", 1]],
        fields=["name", "transaction_date", "grand_total", "status"],
        limit=10,
    )
    hist = "\n".join(
        f"  · {s['transaction_date']} {s['name']} ${s['grand_total']:,.0f} ({s['status']})"
        for s in sos
    ) or "  · sin pedidos confirmados"
    return (
        f"{c['customer_name']} ({c['name']}) · {c.get('customer_group', '')} · "
        f"{c.get('mobile_no', 's/tel')}\nÚltimos pedidos:\n{hist}"
    )
