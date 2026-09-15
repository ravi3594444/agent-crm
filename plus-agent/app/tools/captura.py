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
un ajuste de inventario, un remito— y `avisar_al_cliente` le manda un WhatsApp
a un cliente con el visto bueno del dueño. Dos de ellas no chequeaban nada:
alcanzaba con que el router dejara pasar el mensaje. Ahora las cuatro llaman a
``require_management`` antes de leer o escribir cualquier cosa
(app/runtime_context.py).
"""
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app import (
    clientes,
    erpnext,
    idioma,
    notificar,
    outbound_status,
    policy,
    salidas,
    telefono,
)
from app.runtime_context import RuntimeContextError, require_management


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
        return idioma.t("permiso.sin_autorizacion", idioma.gerencia())

    lengua = idioma.gerencia()
    if not lineas:
        return idioma.t("captura.venta_sin_lineas", lengua)

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
    return idioma.t(
        "captura.venta_cargada",
        lengua,
        factura=doc["name"],
        detalle=detalle,
        cliente=cliente,
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
        return idioma.t("captura.conteo_sin_autenticar", idioma.gerencia())
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
        return idioma.t(
            "captura.conteo_sin_diferencia",
            idioma.gerencia(),
            sistema=f"{sistema:g}",
            item_code=item_code,
            dep=dep,
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
        return idioma.t("permiso.sin_autorizacion", idioma.gerencia())

    lengua = idioma.gerencia()
    try:
        so = erpnext.get_doc("Sales Order", numero_pedido)
    except erpnext.ERPNextError:
        return idioma.t(
            "captura.pedido_no_encontrado", lengua, numero_pedido=numero_pedido
        )
    if so["docstatus"] != 1:
        return idioma.t(
            "captura.pedido_en_borrador", lengua, numero_pedido=numero_pedido
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
    return idioma.t(
        "captura.remito_creado",
        lengua,
        remito=doc["name"],
        cliente=so["customer"],
    )


# DE UN BORRADOR QUE HABÍA QUE COPIAR A MANO, A UN MENSAJE QUE SALE
# ----------------------------------------------------------------
# Esto era `redactar_mensaje_cliente`, y devolvía un borrador con un hueco
# literal —«[Redactá el mensaje acá, en tono cordial y breve, y mostráselo al
# usuario…]»— para que una persona lo copiara y lo mandara desde su propio
# WhatsApp. O sea que la frase del dueño «yo le digo cualquier cosa al manager
# y él lo hace» terminaba, para todo lo que sale hacia un cliente, en copiar y
# pegar.
#
# Ahora el modelo escribe el mensaje FINAL y Python se lo muestra al dueño con
# un botón. Nada sale hasta que lo toca, y lo que toca es el texto exacto.
#
# NO es una herramienta más: reemplaza a la que había. La superficie de
# gerencia sigue en 13. Una herramienta que redacta y otra que manda serían dos
# candidatas plausibles para «avisale a Don José», que es exactamente el
# solapamiento que hoy se sacó de `informe` y de `ver_ajustes`.
@tool
def avisar_al_cliente(
    config: RunnableConfig,
    cliente: Annotated[
        str,
        Field(description="A quién hay que escribirle, como lo nombró el dueño, o su "
                          "código de ERPNext."),
    ],
    mensaje: Annotated[
        str,
        Field(description="El mensaje COMPLETO y ya redactado, tal como querés que lo "
                          "lea el cliente. No es la intención ni un resumen: es el "
                          "texto que va a salir. Breve, cordial y de vos."),
    ],
) -> str:
    """Le manda un WhatsApp a un cliente, después de que el dueño lo apruebe.

    Vos escribís el mensaje; el dueño lo ve tal cual y lo aprueba con un botón.
    NO sale nada hasta que lo toque: nunca le digas al dueño que el cliente ya
    fue avisado.

    Ejemplos:
    - «avisale a Don José que ya llegó el queso cremoso»
    - «decile a la panadería que mañana paso más tarde»
    """
    try:
        actor = require_management(config)
    except RuntimeContextError:
        return idioma.t("permiso.sin_autorizacion", idioma.gerencia())

    lengua = idioma.gerencia()
    # El código EXACTO primero y después el nombre, igual que en `ficha_cliente`:
    # `name` es la clave del documento y no puede coincidir con dos.
    # UN `like` QUE DEVUELVE VARIOS NO ELIGE. Esto tenía `limit=1` y se quedaba
    # con el primero: con dos «San José» cargados, el dueño aprobaba un mensaje
    # que decía un nombre y salía para el otro comercio. La regla es la misma
    # que la de escribir una ficha y vive en un solo lugar.
    ficha, candidatos = clientes.buscar_una(
        cliente, ["name", "customer_name", "mobile_no"]
    )
    if ficha is None:
        if not candidatos:
            return idioma.t("salida.no_encontre", lengua, quien=cliente)
        cuales = ", ".join(
            f"{c.get('customer_name') or c['name']} ({c['name']})" for c in candidatos
        )
        return idioma.t("salida.cliente_ambiguo", lengua, quien=cliente, cuales=cuales)
    nombre = ficha.get("customer_name") or ficha["name"]
    # NORMALIZADO, no crudo. La ventana de 24 h se indexa por el número
    # canónico que mandó Meta en el webhook de entrada; un `mobile_no` cargado
    # como «+54 9 351 123-4567» da otra clave, así que la ventana de una charla
    # que SÍ está abierta se leía cerrada y el mensaje no salía nunca.
    telefono_cliente = telefono.normalizar(ficha.get("mobile_no"))
    if not telefono_cliente:
        return idioma.t("salida.sin_telefono", lengua, cliente=nombre)

    # LA VENTANA DE 24 h, ANTES DE MOLESTAR AL DUEÑO. Meta no deja escribirle
    # primero a alguien que hace más de un día que no escribe, salvo con una
    # plantilla aprobada, y para esto no hay ninguna. Sin este chequeo el dueño
    # tocaría el botón, el mensaje se encolaría, se gastaría los ocho reintentos
    # y moriría en la cola de descarte — y él se quedaría creyendo que avisó.
    if not outbound_status.window_open(telefono_cliente):
        return idioma.t("salida.fuera_de_ventana", lengua, cliente=nombre)

    try:
        salida = salidas.proponer(
            cliente=nombre, telefono=telefono_cliente,
            texto=mensaje, pedida_por=actor.actor_phone,
        )
    except salidas.SalidaError:
        return idioma.t("salida.no_pude_pedir", lengua)

    # Si el botón no sale, la salida guardada no sirve para nada: nadie la
    # puede aprobar y vence sola. Se consume acá mismo para no dejar basura con
    # el teléfono de un cliente adentro esperando una hora.
    if not notificar.pedir_visto_bueno_de_envio(
        actor.actor_phone, salida.id, nombre, telefono_cliente, salida.texto
    ):
        salidas.consumir(salida.id)
        return idioma.t("salida.no_pude_pedir", lengua)

    return idioma.t("salida.esperando_visto_bueno", lengua)
