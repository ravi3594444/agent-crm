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

LAS TRES ESCRIBEN, ASÍ QUE LAS TRES AUTORIZAN.
Estas herramientas crean documentos en ERPNext —una factura que mueve stock,
un ajuste de inventario, un remito— y `redactar_mensaje_cliente` devuelve el
teléfono de un cliente. Dos de ellas no chequeaban nada: alcanzaba con que el
router dejara pasar el mensaje. Ahora las tres llaman a ``require_management``
antes de leer o escribir cualquier cosa (app/runtime_context.py).
"""
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app import erpnext, idioma, notificar, policy
from app.runtime_context import SIN_PERMISO, RuntimeContextError, require_management


def _hoy() -> str:
    """Business-local date; the host clock may run in UTC."""
    return policy._hoy_del_negocio().isoformat()


class LineaVenta(BaseModel):
    item_code: str = Field(description="Código del producto")
    cantidad: float = Field(
        gt=0, description="Cuántas unidades, en la unidad del catálogo"
    )
    precio_unitario: float | None = Field(default=None, description="Si difiere de lista")


@tool
def registrar_venta_offline(
    config: RunnableConfig,
    cliente: Annotated[
        str,
        Field(description="A nombre de quién fue la venta, como lo nombró quien la dicta. "
                          "No lo inventes: si no lo dijo, preguntáselo."),
    ],
    lineas: Annotated[
        list[LineaVenta],
        Field(description="Una línea por producto vendido, con el código del "
                          "catálogo y la cantidad que se entregó."),
    ],
    cobrado: Annotated[
        bool,
        Field(description="Si ya se cobró. Verdadero salvo que hayan dicho que quedó a "
                          "cuenta."),
    ] = True,
    nota: Annotated[
        str,
        Field(description="Opcional: cualquier aclaración que hayan dicho, tal cual."),
    ] = "",
) -> str:
    """Registra una venta que ya ocurrió fuera del sistema (mostrador, reparto,
    teléfono). Usar cuando alguien del equipo dice que vendió algo.

    Ejemplos: "vendí 20 litros a Don José", "el reparto entregó 15 kg de queso
    a La Esquina", "salieron 3 hormas al mostrador".

    Crea una factura en BORRADOR. El dueño la confirma y recién ahí baja stock.
    """
    try:
        require_management(config)
    except RuntimeContextError:
        return SIN_PERMISO
    if not lineas:
        return "Necesito saber qué productos se vendieron."

    # Explicit company and warehouse: with update_stock ERPNext rejects any
    # stock item without a warehouse, and a multi-company site must never
    # pick a company by accident.
    company, warehouse = erpnext.default_context()
    items = []
    for linea in lineas:
        item = {"item_code": linea.item_code, "qty": linea.cantidad, "warehouse": warehouse}
        if linea.precio_unitario is not None:
            item["rate"] = linea.precio_unitario
        items.append(item)

    cobro = "Cobrado en el momento." if cobrado else "Pendiente de cobro."
    doc = erpnext.create_doc(
        "Sales Invoice",
        {
            "company": company,
            "customer": cliente,
            "posting_date": _hoy(),
            "set_posting_time": 1,
            "update_stock": 1,          # this invoice moves stock on submit
            "set_warehouse": warehouse,
            "is_pos": 1 if cobrado else 0,
            "items": items,
            "remarks": f"Venta offline registrada por WhatsApp. {cobro} {nota}".strip(),
        },
    )
    erpnext.add_comment(
        "Sales Invoice", doc["name"],
        "Venta offline cargada por Agente IA vía WhatsApp. Requiere confirmación.",
    )
    detalle = ", ".join(f"{linea.cantidad:g} x {linea.item_code}" for linea in lineas)
    return (
        f"Cargado como {doc['name']} en borrador ({detalle}) para {cliente}. "
        f"Confirmalo en el sistema y se descuenta del stock."
    )


@tool
def contar_stock(
    item_code: Annotated[
        str,
        Field(description="El código EXACTO del producto en el catálogo, no las palabras "
                          "con que lo nombraron."),
    ],
    cantidad_real: Annotated[
        float,
        Field(description="Lo que contaron de verdad, en la unidad de stock del producto. "
                          "No lo estimes: es el número que dijeron."),
    ],
    config: RunnableConfig,
    deposito: Annotated[
        str,
        Field(description="Opcional: el depósito, si nombraron uno. Vacío usa el de "
                          "preparación."),
    ] = "",
) -> str:
    """Corrige el stock de un producto al valor contado físicamente.
    Usar en el conteo de la mañana o cuando alguien dice "quedan X".

    Ejemplo: "quedan 12 kilos de queso cremoso" -> contar_stock('QUE-CRE', 12)

    Queda como BORRADOR: el conteo recién vale cuando la persona lo confirma
    con el botón. Hasta entonces el bot sigue sin prometer stock.
    """
    try:
        actor = require_management(config)
    except RuntimeContextError:
        return "No pude autenticar quién cuenta; no cargué el conteo."
    company, default_warehouse = erpnext.default_context()
    dep = deposito or default_warehouse
    bins = erpnext.get_list(
        "Bin",
        filters=[["item_code", "=", item_code], ["warehouse", "=", dep]],
        fields=["actual_qty"], limit=1,
    )
    sistema = float(bins[0].get("actual_qty") or 0) if bins else 0.0
    if abs(cantidad_real - sistema) < 0.000001:
        # ERPNext refuses a reconciliation with no change; say so instead of
        # surfacing a technical error.
        return (
            f"El sistema ya tiene {sistema:g} de {item_code} en {dep}; "
            "no hace falta ningún ajuste."
        )

    doc = erpnext.create_doc(
        "Stock Reconciliation",
        {
            "company": company,
            "purpose": "Stock Reconciliation",
            "posting_date": _hoy(),
            "set_posting_time": 1,
            "items": [{"item_code": item_code, "warehouse": dep, "qty": cantidad_real}],
        },
    )
    erpnext.add_comment(
        "Stock Reconciliation", doc["name"],
        f"Conteo físico por WhatsApp. Sistema: {sistema:g}, contado: {cantidad_real:g}.",
    )
    dif = cantidad_real - sistema
    # El idioma del DUEÑO: esta herramienta es de gerencia (require_management)
    # y el cuerpo del botón le llega a él por WhatsApp, no al modelo.
    lengua = idioma.gerencia()
    resumen = idioma.t(
        "stock.conteo_faltan" if dif < 0 else "stock.conteo_sobran",
        lengua,
        producto=item_code,
        ajuste=doc["name"],
        sistema=f"{sistema:g}",
        contado=f"{cantidad_real:g}",
        diferencia=f"{abs(dif):g}",
    )
    # The count only starts counting when a person confirms it, so ask for that
    # in one tap instead of sending him into ERPNext.
    pedido = notificar.pedir_confirmacion_conteo(
        actor.actor_phone,
        doc["name"],
        f"{resumen}\n\n" + idioma.t("stock.conteo_confirmar", lengua),
    )
    if pedido:
        return idioma.t(
            "stock.conteo_boton_enviado",
            lengua,
            resumen=resumen,
            producto=item_code,
            # La etiqueta que el dueño tiene en la pantalla, no una copia de
            # ella: el modelo suele repetir esta frase tal cual.
            boton=idioma.t("boton.confirmar_conteo", lengua),
        )
    return idioma.t(
        "stock.conteo_sin_boton",
        lengua,
        resumen=resumen,
        producto=item_code,
        ajuste=doc["name"],
    )


@tool
def confirmar_entrega(
    config: RunnableConfig,
    numero_pedido: Annotated[
        str,
        Field(description="El número real del pedido que se entregó. No lo inventes."),
    ],
    nota: Annotated[
        str,
        Field(description="Opcional: lo que contó el repartidor, tal cual."),
    ] = "",
) -> str:
    """Marca un pedido como entregado por el reparto.
    Usar cuando el repartidor avisa que dejó la mercadería."""
    try:
        require_management(config)
    except RuntimeContextError:
        return SIN_PERMISO
    try:
        so = erpnext.get_doc("Sales Order", numero_pedido)
    except erpnext.ERPNextError:
        return f"No encontré el pedido {numero_pedido}."
    if so["docstatus"] != 1:
        return (
            f"El pedido {numero_pedido} todavía está en borrador. "
            f"Hay que confirmarlo antes de marcarlo entregado."
        )
    company, warehouse = erpnext.default_context()
    doc = erpnext.create_doc(
        "Delivery Note",
        {
            "company": so.get("company") or company,
            "customer": so["customer"],
            "posting_date": _hoy(),
            "set_posting_time": 1,
            "items": [
                {
                    "item_code": i["item_code"],
                    "qty": i["qty"],
                    "warehouse": i.get("warehouse") or warehouse,
                    "against_sales_order": so["name"],
                    "so_detail": i["name"],
                }
                for i in so.get("items", [])
            ],
            "remarks": f"Entrega reportada por WhatsApp. {nota}".strip(),
        },
    )
    return f"Remito {doc['name']} creado en borrador para {so['customer']}. Confirmalo y baja el stock."


@tool
def redactar_mensaje_cliente(
    config: RunnableConfig,
    cliente: Annotated[
        str,
        Field(description="A quién hay que escribirle, como lo nombró el dueño."),
    ],
    intencion: Annotated[
        str,
        Field(description="Qué le quiere decir, en una frase y con sus palabras. El texto "
                          "lo redactás vos, pero lo manda una persona."),
    ],
) -> str:
    """Redacta un mensaje de WhatsApp para enviarle a un cliente.
    NO lo envía — devuelve el texto para que una persona lo revise y mande.

    Ejemplo: "avisale a Don José que ya llegó el queso cremoso".
    """
    try:
        require_management(config)
    except RuntimeContextError:
        return SIN_PERMISO
    fichas = erpnext.get_list(
        "Customer",
        filters=[["customer_name", "like", f"%{cliente}%"]],
        fields=["customer_name", "mobile_no"], limit=1,
    )
    if not fichas:
        return f"No encontré a '{cliente}' en el sistema."
    c = fichas[0]
    return (
        f"Borrador para {c['customer_name']} ({c.get('mobile_no', 'sin teléfono')}), "
        f"sobre: {intencion}\n\n"
        f"[Redactá el mensaje acá, en tono cordial y breve, y mostráselo al usuario "
        f"para que lo apruebe antes de mandarlo.]"
    )
