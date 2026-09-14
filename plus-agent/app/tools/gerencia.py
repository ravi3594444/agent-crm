"""Management tools — the owner's assistant. READ-heavy, no customer exposure.

THE RULE THAT MATTERS HERE:
The agent must never compute business numbers itself. LLMs are bad at
arithmetic over long lists and will confidently invent a total.
Instead it calls ERPNext's own Query Reports — the same numbers his
accountant sees — and explains the result.

Deterministic numbers. LLM explanation. Never the other way round.

LA SEGUNDA REGLA: TODO ESTO SE AUTORIZA ACÁ TAMBIÉN.
Estas seis herramientas leen lo que el dueño no quiere que lea nadie más —la
lista de clientes con sus teléfonos, la facturación, los márgenes, la deuda de
cada uno— y ninguna gateaba por sí misma: se confiaban del router. Un router
es una sola puerta, y una sola puerta que se abre mal entrega el negocio
entero. Cada una llama a ``require_management`` con el teléfono que firmó Meta
(app/runtime_context.py), antes de tocar ERPNext.
"""
from datetime import timedelta
from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field

from app import erpnext, policy
from app.formato import pesos
from app.runtime_context import SIN_PERMISO, RuntimeContextError, require_management

# POR QUÉ ESTO ES UNA HERRAMIENTA Y NO CINCO
# ------------------------------------------
# El dueño lo pidió como «no me llenes el agente de herramientas, si le ponés
# 100 capaz se confunde». Tenía razón en el diagnóstico y no en el remedio, así
# que esto se quedó con la mitad de cada uno. Lo que está medido:
#
# - Lo que degrada de verdad la elección es el TAMAÑO DEL CATÁLOGO, y recién
#   lejos de acá: RAG-MCP (arXiv:2505.03275) mide 13,6% de acierto con todo el
#   catálogo en el prompt contra 43,1% con recuperación semántica, sobre
#   catálogos de cientos. LongFuncEval (IBM, arXiv:2505.10570) mide caídas de
#   7% a 85% escalando de 8K a 120K tokens de catálogo. Gerencia tenía 23
#   herramientas y unos 4.700 tokens: estamos en la meseta, no en el borde.
# - Y sin embargo hay una caída que SÍ nos toca: presentar de más cuesta aunque
#   la lista sea corta (93,1% eligiendo entre ~2 candidatos contra 87,1% entre
#   5 fijos, arXiv:2605.24660). Eso no es cantidad: es SOLAPAMIENTO. Y acá lo
#   había de verdad — `cobranzas_vencidas` era `run_report("Accounts
#   Receivable")` con un filtro, o sea una de las siete consultas que
#   `ejecutar_reporte` ya corría. Un «¿cuánto me deben?» tenía dos herramientas
#   plausibles y ninguna forma de elegir bien.
#
# LA LÍNEA QUE ESTO NO CRUZA. La guía de Anthropic para escribir herramientas
# dice consolidar por SUBDIVISIÓN NATURAL DE TAREAS, y desaconseja
# explícitamente el `manage_x(action=...)` que mete todo detrás de un modo,
# porque obliga al modelo a resolver el modo antes que la tarea. Así que la
# regla de esta casa es angosta y se escribe una sola vez:
#
#     detrás de un `Literal` van LECTURAS de un mismo tema, nunca una escritura.
#
# Los cinco informes son cinco lecturas de «los números del negocio»: una
# subdivisión natural, no un modo. `proponer_limite` y `proponer_accion` NO se
# tocan aunque se parezcan, y no sólo por esta regla: el router de
# `app/main.py` distingue sus confirmaciones POR EL LARGO DEL CÓDIGO —cuatro
# dígitos son un ajuste, seis son un pedido— así que fusionarlas es fusionar
# dos caminos de confirmación distintos.
#
# `que` es un `Literal` CERRADO. Cada valor cae en exactamente una rama de
# Python, con el mismo cuerpo y la misma guarda `require_management` que tenía
# su herramienta suelta. Universal acá quiere decir UN CAMPO TIPADO, nunca una
# cadena libre: un `query(sql)` cambia un esquema por un agujero.


@tool
def informe(
    config: RunnableConfig,
    que: Annotated[
        Literal["pendientes", "ventas", "stock_bajo", "cobranzas", "autonomia"],
        Field(
            description=(
                "Qué querés mirar. "
                "pendientes = pedidos en borrador esperando confirmación (lo primero "
                "de la mañana). "
                "ventas = cuánto se vendió en los últimos días. "
                "stock_bajo = productos por debajo del punto de reposición. "
                "cobranzas = facturas vencidas y no cobradas. "
                "autonomia = cuánto se confirma solo y qué lo frena."
            )
        ),
    ],
    dias: Annotated[
        int,
        Field(
            description=(
                "Cuántos días hacia atrás. Sólo lo usan «ventas» y «autonomia»; "
                "los demás lo ignoran. Si el dueño dijo «esta semana» son 7; si no "
                "dijo nada, 7."
            )
        ),
    ] = 7,
) -> str:
    """Los números del negocio. Sólo lectura: salen de ERPNext y se informan como vienen.

    Ejemplos de cómo se mapea lo que dice el dueño:
    - «¿qué tengo para confirmar?» / «¿qué quedó colgado?» -> que=pendientes
    - «¿cómo venimos este mes?» -> que=ventas, dias=30
    - «¿qué me falta?» / «¿de qué estoy corto?» -> que=stock_bajo
    - «¿quién me debe?» / «¿qué hay vencido?» -> que=cobranzas
    - «¿cuánto está pasando solo?» -> que=autonomia
    """
    if que == "pendientes":
        return _pedidos_pendientes(config)
    if que == "ventas":
        return _ventas_del_periodo(config, dias)
    if que == "stock_bajo":
        return _stock_bajo(config)
    if que == "cobranzas":
        return _cobranzas_vencidas(config)
    return _resumen_autonomia(config, dias)


@tool
def ejecutar_reporte(
    config: RunnableConfig,
    nombre_reporte: Annotated[
        str,
        Field(description="El nombre EXACTO del reporte en ERPNext. Si no estás seguro de "
                          "cuál es, preguntale al dueño qué quiere ver en vez de probar "
                          "nombres."),
    ],
    filtros: Annotated[
        dict | None,
        Field(description="Los filtros del reporte, tal como los pide ERPNext. Vacío si "
                          "no hace falta ninguno."),
    ] = None,
) -> str:
    """Ejecuta un reporte oficial de ERPNext y devuelve los datos reales.

    Reportes disponibles: 'Accounts Receivable', 'Stock Balance',
    'Sales Analytics', 'Gross Profit', 'Item-wise Sales History',
    'Sales Order Analysis', 'Stock Projected Qty'.

    Usar SIEMPRE esta herramienta para cifras. Nunca calcular a mano.
    """
    try:
        require_management(config)
    except RuntimeContextError:
        return SIN_PERMISO
    data = erpnext.run_report(nombre_reporte, filtros or {})
    if not data:
        return f"El reporte '{nombre_reporte}' no devolvió filas."
    filas = data[:40]
    return f"Reporte '{nombre_reporte}' ({len(data)} filas, muestro {len(filas)}):\n" + "\n".join(
        str(f) for f in filas
    )


def _resumen_autonomia(
    config: RunnableConfig,
    dias: Annotated[int, Field(description="Cuántos días atrás mirar. 7 si no dijo otra cosa.")] = 7,
) -> str:
    """Cuánto se confirma solo, cuánto lo confirma una persona y qué lo frena.

    Sólo lectura: cuenta hechos que ya están escritos en ERPNext. Informá los
    números tal como vienen y NO recomiendes subir ni bajar un límite — esa
    frase es del dueño, no tuya. Si un número dice «no pude leer», decilo así.
    """
    try:
        require_management(config)
    except RuntimeContextError:
        return SIN_PERMISO
    from app import autonomia, idioma

    try:
        datos = autonomia.resumen(int(dias or autonomia.DIAS_DEFAULT))
        return autonomia.texto(datos, idioma.gerencia())
    except Exception as exc:
        return f"No pude armar el resumen de autonomía: {type(exc).__name__}."


def _pedidos_pendientes(config: RunnableConfig) -> str:
    """Pedidos en borrador esperando confirmación del equipo.
    Esto es lo primero que debería revisar el dueño cada mañana."""
    try:
        require_management(config)
    except RuntimeContextError:
        return SIN_PERMISO
    sos = erpnext.get_list(
        "Sales Order",
        filters=[["docstatus", "=", 0]],
        fields=[
            "name",
            "customer",
            "customer_name",
            "grand_total",
            "delivery_date",
            "creation",
        ],
        limit=50,
    )
    if not sos:
        return "No hay pedidos pendientes de confirmación."
    # customer_name, no customer: `customer` es el CÓDIGO de ERPNext
    # («CUST-0009»), y el dueño dice «el de la panadería». Con el código en
    # pantalla sus propias palabras no coinciden con nada. Y la antigüedad, que
    # ya se pedía en `creation` y no se mostraba, es con lo que decide a cuál
    # atender primero.
    from app import pendientes

    lineas = []
    for s in sos:
        edad = pendientes.edad_horas(s)
        antiguedad = f" · hace {edad:.0f} h" if edad is not None else ""
        quien = s.get("customer_name") or s.get("customer")
        lineas.append(
            f"- {s['name']} · {quien} · {pesos(s['grand_total'])} "
            f"· entrega {s['delivery_date']}{antiguedad}"
        )
    return f"{len(sos)} pedidos pendientes de confirmar:\n" + "\n".join(lineas)


def _ventas_del_periodo(
    config: RunnableConfig,
    dias: Annotated[
        int,
        Field(description="Cuántos días hacia atrás mirar. Si el dueño dijo «esta semana» "
                          "son 7; si no dijo nada, 7."),
    ] = 7,
) -> str:
    """Ventas confirmadas de los últimos N días."""
    try:
        require_management(config)
    except RuntimeContextError:
        return SIN_PERMISO
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
        f"total {pesos(total)}. Promedio {pesos(total / len(sos))} por pedido."
        if sos
        else f"Sin pedidos confirmados en los últimos {dias} días."
    )


def _stock_bajo(config: RunnableConfig) -> str:
    """Productos por debajo del punto de reposición. Riesgo de quiebre de stock."""
    try:
        require_management(config)
    except RuntimeContextError:
        return SIN_PERMISO
    items = erpnext.get_list(
        "Item Reorder",
        fields=["parent", "warehouse", "warehouse_reorder_level"],
        limit=200,
        # `Item Reorder` es una TABLA HIJA de Item, y Frappe se niega a listar
        # una tabla hija sin que le digan de qué padre cuelga —lo dice el
        # comentario de `erpnext._list`, y las otras seis consultas a tablas
        # hijas del repo lo pasan—. Sin esto, «¿de qué estoy corto?» nunca
        # contestó contra un ERPNext de verdad: devolvía el error de la
        # herramienta. No se vio antes porque el banco de pruebas usa un
        # ERPNext falso, al que el parámetro no le importa.
        parent="Item",
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


def _cobranzas_vencidas(config: RunnableConfig) -> str:
    """Facturas vencidas y no cobradas. Usa el reporte oficial de ERPNext."""
    try:
        require_management(config)
    except RuntimeContextError:
        return SIN_PERMISO
    data = erpnext.run_report("Accounts Receivable", {"company": erpnext.default_company()})
    vencidas = [r for r in data if isinstance(r, dict) and (r.get("outstanding_amount") or 0) > 0]
    if not vencidas:
        return "No hay saldos pendientes de cobro."
    total = sum(r["outstanding_amount"] for r in vencidas)
    top = sorted(vencidas, key=lambda r: -r["outstanding_amount"])[:10]
    lineas = [
        f"- {r.get('customer_name') or r.get('party')}: {pesos(r['outstanding_amount'])}"
        for r in top
    ]
    return f"Total a cobrar {pesos(total)} en {len(vencidas)} facturas.\n" + "\n".join(lineas)


@tool
def ficha_cliente(
    config: RunnableConfig,
    nombre_o_codigo: Annotated[
        str,
        Field(description="El nombre del cliente como lo nombró el dueño, o su código de "
                          "ERPNext. Cualquiera de los dos sirve."),
    ],
) -> str:
    """Vista 360 de un cliente: datos, últimos pedidos y saldo."""
    try:
        require_management(config)
    except RuntimeContextError:
        return SIN_PERMISO
    # El parámetro promete las dos formas —«el nombre …, o su código de
    # ERPNext»— y esto buscaba sólo por `customer_name`, así que un código no
    # encontraba nada. Andaba de casualidad mientras cada cliente del banco de
    # pruebas se llamaba igual que su código; el primero que los tiene
    # separados (CUST-0009) lo dejó a la vista.
    #
    # El código EXACTO primero, porque `name` es la clave del documento y no
    # puede coincidir con dos; después el nombre, con el mismo `like` de antes.
    # No se amplía nada más: los mismos campos, el mismo limit=1 y el mismo
    # portón de gerencia de acá arriba.
    campos = ["name", "customer_name", "customer_group", "mobile_no"]
    clientes = erpnext.get_list(
        "Customer",
        filters=[["name", "=", nombre_o_codigo]],
        fields=campos,
        limit=1,
    ) or erpnext.get_list(
        "Customer",
        filters=[["customer_name", "like", f"%{nombre_o_codigo}%"]],
        fields=campos,
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
        # Mismo motivo que en `pedido_habitual` (app/tools/catalogo.py): sin
        # esto, «Últimos pedidos» eran los diez últimos MODIFICADOS, porque es
        # como ordena Frappe cuando nadie le dice otra cosa. Acá el efecto es
        # peor que allá: la respuesta imprime la fecha de cada uno, así que el
        # dueño leía una lista fechada que no estaba ordenada por fecha.
        order_by="transaction_date desc, creation desc",
    )
    hist = "\n".join(
        f"  · {s['transaction_date']} {s['name']} {pesos(s['grand_total'])} ({s['status']})"
        for s in sos
    ) or "  · sin pedidos confirmados"
    return (
        f"{c['customer_name']} ({c['name']}) · {c.get('customer_group', '')} · "
        f"{c.get('mobile_no', 's/tel')}\nÚltimos pedidos:\n{hist}"
    )
