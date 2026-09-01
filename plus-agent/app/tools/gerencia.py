"""Management tools — the owner's assistant. READ-heavy, no customer exposure.

THE RULE THAT MATTERS HERE:
The agent must never compute business numbers itself. LLMs are bad at
arithmetic over long lists and will confidently invent a total.
Instead it calls ERPNext's own Query Reports — the same numbers his
accountant sees — and explains the result.

Deterministic numbers. LLM explanation. Never the other way round.
"""

from __future__ import annotations

from datetime import date, timedelta

from langchain_core.tools import tool

from app import erpnext, formato, log

_log = log.get("gerencia")

# Reportes que el agente puede correr. Es una allowlist y no una sugerencia
# del docstring: un nombre inventado por el modelo daba un error crudo de
# Frappe, y algunos reportes pesados pueden tumbar la instancia.
REPORTES = {
    "Accounts Receivable",
    "Stock Balance",
    "Sales Analytics",
    "Gross Profit",
    "Item-wise Sales History",
    "Sales Order Analysis",
    "Stock Projected Qty",
    "Sales Register",
}


def _filtros_cuenta_corriente(cliente: str | None = None) -> dict:
    """Accounts Receivable necesita fecha y rangos de antigüedad; sin eso
    tira error en varias versiones de ERPNext."""
    filtros: dict = {
        "company": erpnext.default_company(),
        "report_date": date.today().isoformat(),
        "ageing_based_on": "Due Date",
        "range1": 30,
        "range2": 60,
        "range3": 90,
        "range4": 120,
    }
    if cliente:
        filtros["party_type"] = "Customer"
        filtros["party"] = [cliente]
    return filtros


@tool
def ejecutar_reporte(nombre_reporte: str, filtros: dict | None = None) -> str:
    """Ejecuta un reporte oficial de ERPNext y devuelve los datos reales.

    Reportes disponibles: 'Accounts Receivable', 'Stock Balance',
    'Sales Analytics', 'Gross Profit', 'Item-wise Sales History',
    'Sales Order Analysis', 'Stock Projected Qty', 'Sales Register'.

    Usar SIEMPRE esta herramienta para cifras. Nunca calcular a mano.
    """
    if nombre_reporte not in REPORTES:
        return (
            f"No tengo habilitado el reporte '{nombre_reporte}'. "
            f"Puedo correr: {', '.join(sorted(REPORTES))}."
        )
    usar = dict(filtros or {})
    if nombre_reporte == "Accounts Receivable":
        # Completamos lo obligatorio sin pisar lo que el modelo pidió.
        usar = {**_filtros_cuenta_corriente(), **usar}
    elif "company" not in usar:
        usar["company"] = erpnext.default_company()

    try:
        data = erpnext.run_report(nombre_reporte, usar)
    except erpnext.ERPNextError as e:
        return (
            f"El reporte '{nombre_reporte}' falló: {e}. "
            f"Decí que no pudiste obtener el dato — no lo estimes."
        )
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
    try:
        sos = erpnext.get_list(
            "Sales Order",
            filters=[["docstatus", "=", 0]],
            fields=["name", "customer", "grand_total", "delivery_date", "creation"],
            limit=50,
            order_by="creation asc",
        )
    except erpnext.ERPNextError as e:
        return f"No pude consultar los pedidos pendientes: {e}"
    if not sos:
        return "No hay pedidos pendientes de confirmación."
    lineas = [
        f"- {s['name']} · {s['customer']} · {formato.pesos(float(s.get('grand_total') or 0))} · "
        f"entrega {s.get('delivery_date')}"
        for s in sos
    ]
    return f"{len(sos)} pedidos pendientes de confirmar (más viejo primero):\n" + "\n".join(lineas)


@tool
def ventas_del_periodo(dias: int = 7) -> str:
    """Ventas confirmadas de los últimos N días."""
    dias = max(1, min(int(dias), 365))
    desde = (date.today() - timedelta(days=dias)).isoformat()
    try:
        sos = erpnext.get_list(
            "Sales Order",
            filters=[["docstatus", "=", 1], ["transaction_date", ">=", desde]],
            fields=["name", "customer", "grand_total", "transaction_date"],
            limit=500,
            order_by="transaction_date desc",
        )
    except erpnext.ERPNextError as e:
        return f"No pude consultar las ventas: {e}"
    if not sos:
        return f"Sin pedidos confirmados en los últimos {dias} días."
    total = sum(float(s.get("grand_total") or 0) for s in sos)
    aviso = (
        " (llegué al límite de 500 pedidos, el total real puede ser mayor)"
        if len(sos) >= 500
        else ""
    )
    return (
        f"Últimos {dias} días (desde {desde}): {len(sos)} pedidos confirmados, "
        f"total {formato.pesos(total)}. Promedio {formato.pesos(total / len(sos))} por pedido.{aviso}"
    )


@tool
def stock_bajo() -> str:
    """Productos por debajo del punto de reposición. Riesgo de quiebre de stock."""
    try:
        reglas = erpnext.get_list(
            "Item Reorder",
            fields=["parent", "warehouse", "warehouse_reorder_level"],
            limit=200,
        )
    except erpnext.ERPNextError as e:
        return f"No pude consultar los puntos de reposición: {e}"
    if not reglas:
        return (
            "No hay puntos de reposición configurados en ERPNext, así que no puedo "
            "avisar de stock bajo. Se configuran en cada Item, pestaña Reorder."
        )

    # Una sola consulta de Bin en lugar de una por producto: antes esto hacía
    # hasta 200 llamadas REST por pregunta.
    codigos = sorted({r["parent"] for r in reglas if r.get("parent")})
    try:
        bins = erpnext.get_list(
            "Bin",
            filters=[["item_code", "in", codigos]],
            fields=["item_code", "warehouse", "actual_qty"],
            limit=1000,
        )
    except erpnext.ERPNextError as e:
        return f"No pude consultar el stock: {e}"
    por_clave = {(b["item_code"], b["warehouse"]): float(b.get("actual_qty") or 0) for b in bins}

    alertas = []
    for r in reglas:
        minimo = float(r.get("warehouse_reorder_level") or 0)
        qty = por_clave.get((r["parent"], r.get("warehouse")), 0.0)
        if qty <= minimo:
            alertas.append(f"- {r['parent']}: {qty:g} (mínimo {minimo:g})")
    if not alertas:
        return "Sin alertas de stock."
    return "Stock bajo:\n" + "\n".join(alertas)


@tool
def cobranzas_vencidas() -> str:
    """Facturas vencidas y no cobradas. Usa el reporte oficial de ERPNext."""
    try:
        data = erpnext.run_report("Accounts Receivable", _filtros_cuenta_corriente())
    except erpnext.ERPNextError as e:
        return (
            f"No pude correr el reporte de cuenta corriente: {e}. "
            f"Decí que no pudiste obtener el dato."
        )
    vencidas = [
        r for r in data if isinstance(r, dict) and float(r.get("outstanding_amount") or 0) > 0
    ]
    if not vencidas:
        return "No hay saldos pendientes de cobro."
    total = sum(float(r.get("outstanding_amount") or 0) for r in vencidas)
    top = sorted(vencidas, key=lambda r: -float(r.get("outstanding_amount") or 0))[:10]
    lineas = [
        f"- {r.get('customer_name') or r.get('party')}: "
        f"{formato.pesos(float(r.get('outstanding_amount') or 0))}"
        for r in top
    ]
    return (
        f"Total a cobrar {formato.pesos(total)} en {len(vencidas)} facturas "
        f"(reporte Accounts Receivable al {date.today().isoformat()}).\n" + "\n".join(lineas)
    )


@tool
def ficha_cliente(nombre_o_codigo: str) -> str:
    """Vista 360 de un cliente: datos, últimos pedidos y saldo."""
    try:
        clientes = erpnext.get_list(
            "Customer",
            filters=[["customer_name", "like", f"%{nombre_o_codigo}%"]],
            fields=["name", "customer_name", "customer_group", "mobile_no"],
            limit=5,
        )
    except erpnext.ERPNextError as e:
        return f"No pude buscar el cliente: {e}"
    if not clientes:
        return f"No encontré un cliente que coincida con '{nombre_o_codigo}'."
    if len(clientes) > 1:
        opciones = ", ".join(f"{c['customer_name']} ({c['name']})" for c in clientes)
        return f"Hay varios que coinciden: {opciones}. ¿Cuál?"

    c = clientes[0]
    try:
        sos = erpnext.get_list(
            "Sales Order",
            filters=[["customer", "=", c["name"]], ["docstatus", "=", 1]],
            fields=["name", "transaction_date", "grand_total", "status"],
            limit=10,
            order_by="transaction_date desc",
        )
    except erpnext.ERPNextError:
        sos = []
    hist = (
        "\n".join(
            f"  · {s.get('transaction_date')} {s['name']} "
            f"{formato.pesos(float(s.get('grand_total') or 0))} ({s.get('status')})"
            for s in sos
        )
        or "  · sin pedidos confirmados"
    )
    return (
        f"{c['customer_name']} ({c['name']}) · {c.get('customer_group') or ''} · "
        f"{c.get('mobile_no') or 's/tel'}\nÚltimos pedidos:\n{hist}"
    )
