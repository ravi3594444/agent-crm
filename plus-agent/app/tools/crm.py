"""Lo que el dueño puede CAMBIAR: reversible, y sin que se mueva plata.

DÓNDE ESTÁ LA LÍNEA, Y POR QUÉ ESTÁ AHÍ
---------------------------------------
Durante meses la respuesta a «que el agente pueda hacer lo que le pido» fue que
escribir es peligroso. Era una respuesta demasiado ancha, y la línea correcta no
es leer-contra-escribir: es **irreversible × plata**.

Lo que se abre acá: la ficha de un cliente (grupo, condición de pago),
notas y recordatorios sobre cualquier registro, un
presupuesto en borrador, la corrección de un pedido QUE SIGUE EN BORRADOR, y la
descripción y el punto de reposición de un producto. Todo eso se deshace
escribiendo de nuevo, y nada de eso le cobra un peso a nadie.

Lo que NO se abre, y no está en este archivo ni alcanzable desde él:
  · emitir cualquier cosa (`submit_doc` usa la credencial de política, que
    ninguna herramienta toca);
  · cancelar un documento ya emitido;
  · **Item Price por la puerta genérica.** `erpnext.DOCTYPES_EDITABLES` sigue
    sin incluirlo, así que `actualizar_registro` y cualquier herramienta que se
    escriba mañana lo tienen negado por el cliente HTTP y no por la buena
    conducta de este archivo. Lo que SÍ se abrió, abajo, es `cambiar_precio`:
    una puerta del ancho de un precio, con la lista y la moneda tomadas de
    `policy` y la unidad del `stock_uom` del producto, y acotada por
    `PRECIO_CAMBIO_MAX_PCT`. La diferencia importa porque un precio gobierna EN
    SILENCIO lo que se auto-confirma: escribir uno al que le falta una de esas
    tres columnas no da error en ninguna parte, simplemente deja de confirmarse
    solo. La puerta angosta hace que esa forma no se pueda escribir;
  · los límites del dueño, que siguen necesitando su código de cuatro dígitos;
  · **el teléfono del cliente**, que es su IDENTIDAD acá (así lo encuentra el
    webhook): `erpnext.CAMPOS_PROHIBIDOS` se lo niega a cualquier llamador, no
    sólo a la herramienta que hoy no lo pide.

EL MODELO NO PONE UN PRECIO. NUNCA.
-----------------------------------
`LineaSimple` tiene código, cantidad y unidad, y no tiene `rate`. Es la misma
forma que `pedidos.LineaPedido` y no es una coincidencia: el precio lo resuelve
ERPNext contra su lista, y `policy._precio_autorizado` lo verifica por
price_list, currency y uom. Una herramienta donde el precio es un argumento del
modelo convierte al modelo en la autoridad de precios —es literalmente lo que
hace `erpnext_sales_order_create` del servidor de terceros que se midió en
`docs/MCP.md`— y eso da vuelta la regla 1 del prompt.

UN BORRADOR ES UN BORRADOR, Y SE COMPRUEBA CADA VEZ
---------------------------------------------------
`editar_borrador` relee el pedido y se niega si `docstatus != 0`. No alcanza con
que el dueño haya pedido «el pedido que está esperando»: entre que lo dijo y
esto corre, el barrido o una persona pueden haberlo emitido. La comprobación va
sobre el documento, en el momento, y no sobre lo que alguien creía.
"""
from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app import erpnext, idioma
from app.runtime_context import RuntimeContextError, require_management

# Qué palabra del dueño apunta a qué doctype. CERRADO: un doctype libre sería
# un puntero a cualquier cosa, y el catálogo de ERPNext es grande.
OBJETOS = {
    "cliente": "Customer",
    "pedido": "Sales Order",
    "producto": "Item",
    "presupuesto": "Quotation",
}


class LineaSimple(BaseModel):
    """Una línea de pedido o presupuesto. SIN precio, a propósito."""

    item_code: str = Field(min_length=1, description="Código exacto del catálogo")
    cantidad: float = Field(gt=0, description="Cuántos")
    unidad: str = Field(min_length=1, description="La unidad del catálogo")


def _sin_permiso() -> str:
    return idioma.t("permiso.sin_autorizacion", idioma.gerencia())


def _buscar_cliente(nombre_o_codigo: str) -> tuple[dict | None, str]:
    """(ficha, problema). El código exacto primero, después el nombre.

    UN `like` QUE DEVUELVE VARIOS NO ELIGE: ésta es la diferencia con
    `ficha_cliente`, y es por lo que se hace de la ficha después. Leer la de
    «San José» cuando hay dos y mostrar la primera es un informe incompleto;
    ESCRIBIRLE a la primera es cambiarle los datos al cliente equivocado, y el
    dueño no tiene cómo enterarse: la respuesta dice el nombre que él escribió.
    Así que se piden DOS y con dos se devuelve el problema.

    `name` es la clave del documento y no puede coincidir con dos, así que el
    camino exacto no necesita esto.
    """
    from app import clientes

    ficha, candidatos = clientes.buscar_una(nombre_o_codigo)
    if ficha is not None:
        return ficha, ""
    lengua = idioma.gerencia()
    if not candidatos:
        return None, idioma.t(
            "crm.cliente_no_encontrado", lengua, nombre_o_codigo=nombre_o_codigo
        )
    cuales = ", ".join(
        f"{c.get('customer_name') or c['name']} ({c['name']})" for c in candidatos
    )
    return None, idioma.t(
        "crm.cliente_ambiguo", lengua,
        nombre_o_codigo=nombre_o_codigo, cuales=cuales,
    )


def _lineas(lineas: list[LineaSimple]) -> list[dict]:
    return [
        {
            "item_code": linea.item_code,
            "qty": float(linea.cantidad),
            "uom": linea.unidad,
            "conversion_factor": 1,
        }
        for linea in lineas
    ]


def _con_lo_hecho(hecho: list[str], problema: str) -> str:
    """Un error que llega DESPUÉS de un cambio ya escrito lo nombra igual.

    `actualizar_producto` hace hasta dos escrituras y no son atómicas. Si la
    descripción se guardó y el punto de reposición falla, contestar sólo el
    error deja al dueño creyendo que no pasó nada — y va a volver a pedir el
    cambio de descripción, o peor, a no confiar en lo que ya está guardado.
    «No pasó nada» y «pasó la mitad» son cosas distintas para el que lee, que es
    la misma razón por la que `anotar_en_ficha` distingue la nota de la tarea.
    """
    if not hecho:
        return problema
    return f"{item_o_lo_hecho(hecho)} {problema}"


def item_o_lo_hecho(hecho: list[str]) -> str:
    return idioma.t("crm.lo_hecho", idioma.gerencia(), hecho=", ".join(hecho))


# --------------------------------------------------------------- el cliente

@tool
def actualizar_cliente(
    config: RunnableConfig,
    cliente: Annotated[
        str, Field(description="Nombre o código del cliente, cualquiera de los dos.")
    ],
    grupo: Annotated[
        str | None,
        Field(description="Grupo de cliente de ERPNext, si el dueño lo quiere cambiar."),
    ] = None,
    condicion_de_pago: Annotated[
        str | None,
        Field(description="Plantilla de condiciones de pago de ERPNext, con su nombre "
                          "exacto. No inventes una: si no sabés cuál, preguntale."),
    ] = None,
) -> str:
    """Corrige los datos de la ficha de un cliente. Se deshace escribiendo de nuevo.

    Usala cuando el dueño dice «cambiale el grupo», «a éste cobrale a 30 días».
    Sólo cambia lo que te pasen: lo que venga vacío queda como estaba.

    NO cambia el TELÉFONO, y no es un olvido: en este sistema el teléfono ES la
    identidad del cliente —así lo encuentra el webhook—, así que cambiarlo acá
    deja los mensajes del número viejo sin dueño. Si hay que cambiarlo, lo hace
    una persona en ERPNext.

    NO cambia precios ni límites, y no confirma ni cancela nada.
    """
    try:
        require_management(config)
    except RuntimeContextError:
        return _sin_permiso()

    ficha, problema = _buscar_cliente(cliente)
    if ficha is None:
        return problema

    lengua = idioma.gerencia()
    cambios: dict = {}
    if grupo:
        cambios["customer_group"] = grupo.strip()
    if condicion_de_pago:
        cambios["payment_terms"] = condicion_de_pago.strip()

    if not cambios:
        return idioma.t("crm.cliente_sin_cambios", lengua)
    try:
        erpnext.update_doc("Customer", ficha["name"], cambios)
    except erpnext.ERPNextError as exc:
        return idioma.t(
            "crm.cliente_error", lengua,
            cliente=ficha.get("customer_name") or cliente, exc=exc,
        )
    detalle = ", ".join(f"{k} = {v}" for k, v in cambios.items())
    return idioma.t(
        "crm.cliente_listo", lengua,
        cliente=ficha.get("customer_name") or ficha["name"], detalle=detalle,
    )


# ------------------------------------------------------------- notas y tareas

@tool
def anotar_en_ficha(
    config: RunnableConfig,
    sobre: Annotated[
        Literal["cliente", "pedido", "producto", "presupuesto"],
        Field(description="Qué clase de registro estás anotando."),
    ],
    cual: Annotated[
        str, Field(description="El código o nombre exacto de ese registro.")
    ],
    nota: Annotated[
        str, Field(description="Lo que hay que dejar escrito, en una o dos frases.")
    ],
    recordarle_a: Annotated[
        str | None,
        Field(description="Usuario de ERPNext al que dejarle la tarea pendiente. "
                          "Vacío = queda sólo como nota, sin tarea."),
    ] = None,
    para_cuando: Annotated[
        str | None, Field(description="Fecha de la tarea en AAAA-MM-DD.")
    ] = None,
) -> str:
    """Deja una nota en un registro, y opcionalmente una tarea para alguien.

    Es lo que el dueño quiere decir con «anotá que este cliente reclama», «que
    alguien lo llame el lunes». La nota queda en el historial del documento —la
    ve cualquiera que lo abra— y la tarea aparece en el ERPNext de esa persona.

    OJO: esto NO es `anotar_dato`. Un dato del negocio que el agente tiene que
    recordar para siempre («la panadería paga los viernes») va en `anotar_dato`,
    que vive en la memoria del agente. Esto se escribe en el registro, para
    personas.
    """
    try:
        require_management(config)
    except RuntimeContextError:
        return _sin_permiso()

    lengua = idioma.gerencia()
    doctype = OBJETOS[sobre]
    texto = " ".join((nota or "").split())
    if not texto:
        return idioma.t("crm.nota_vacia", lengua)
    try:
        erpnext.registrar_comentario(doctype, cual, texto)
    except erpnext.ERPNextError as exc:
        return idioma.t("crm.nota_error", lengua, cual=cual, exc=exc)

    if not recordarle_a:
        return idioma.t("crm.nota_hecha", lengua, cual=cual)
    tarea: dict = {
        "description": texto,
        "allocated_to": recordarle_a.strip(),
        "reference_type": doctype,
        "reference_name": cual,
        "status": "Open",
    }
    if para_cuando:
        tarea["date"] = para_cuando.strip()
    try:
        erpnext.create_doc("ToDo", tarea)
    except erpnext.ERPNextError as exc:
        # La nota YA quedó. Decirlo así es la diferencia entre «no pasó nada» y
        # «pasó la mitad», y el dueño necesita saber cuál de las dos fue.
        return idioma.t(
            "crm.tarea_error", lengua,
            cual=cual, recordarle_a=recordarle_a, exc=exc,
        )
    return idioma.t(
        "crm.nota_y_tarea", lengua, cual=cual, recordarle_a=recordarle_a
    )


# ------------------------------------------------------------- el presupuesto

@tool
def armar_presupuesto(
    config: RunnableConfig,
    cliente: Annotated[str, Field(description="Nombre o código del cliente.")],
    lineas: Annotated[
        list[LineaSimple],
        Field(description="Una línea por producto, con código, cantidad y unidad. "
                          "El precio lo pone ERPNext: vos no lo pasás."),
    ],
    valido_hasta: Annotated[
        str | None,
        Field(description="Hasta cuándo vale la oferta, en AAAA-MM-DD. Vacío = el "
                          "que ERPNext tenga por defecto."),
    ] = None,
) -> str:
    """Arma un presupuesto EN BORRADOR para un cliente. No es una venta.

    Un presupuesto no reserva stock, no compromete nada y se borra si no va a
    ningún lado. Sirve para cuando el dueño dice «pasame una cotización para
    tal» o quiere mandarle precios a alguien antes de que pida.

    EL PRECIO NO SE PASA: lo resuelve ERPNext desde su lista. Si el precio que
    sale no es el que el dueño esperaba, eso se arregla en la lista de precios
    —que esta herramienta no toca— y no escribiéndolo a mano acá.
    """
    try:
        require_management(config)
    except RuntimeContextError:
        return _sin_permiso()
    lengua = idioma.gerencia()
    if not lineas:
        return idioma.t("crm.presupuesto_vacio", lengua)

    ficha, problema = _buscar_cliente(cliente)
    if ficha is None:
        return problema

    payload: dict = {
        "quotation_to": "Customer",
        "party_name": ficha["name"],
        "items": _lineas(lineas),
    }
    if valido_hasta:
        payload["valid_till"] = valido_hasta.strip()
    try:
        doc = erpnext.create_doc("Quotation", payload)
    except erpnext.ERPNextError as exc:
        return idioma.t("crm.presupuesto_error", lengua, exc=exc)
    return idioma.t(
        "crm.presupuesto_listo", lengua,
        presupuesto=doc.get("name"),
        cliente=ficha.get("customer_name") or ficha["name"],
        renglones=len(lineas),
    )


# ---------------------------------------------------- el borrador de un pedido

@tool
def editar_borrador(
    config: RunnableConfig,
    pedido: Annotated[str, Field(description="Número del pedido, exacto.")],
    lineas: Annotated[
        list[LineaSimple] | None,
        Field(description="Los renglones COMPLETOS como tienen que quedar: esto "
                          "REEMPLAZA los que había, no se suma. Vacío = no se tocan."),
    ] = None,
    fecha_entrega: Annotated[
        str | None, Field(description="Nueva fecha de entrega en AAAA-MM-DD.")
    ] = None,
) -> str:
    """Corrige un pedido que TODAVÍA está en borrador. Nunca uno ya confirmado.

    Es para «agregale dos cajones», «cambiale la fecha al jueves», «sacale el
    queso». Si el pedido ya se confirmó esto se niega, y con razón: un pedido
    emitido reservó stock y le prometió algo a alguien, y eso se cambia por el
    camino de siempre —una persona, con su código— y no desde acá.
    """
    try:
        require_management(config)
    except RuntimeContextError:
        return _sin_permiso()
    lengua = idioma.gerencia()
    if not lineas and not fecha_entrega:
        return idioma.t("crm.borrador_sin_cambios", lengua)

    try:
        doc = erpnext.get_doc("Sales Order", pedido)
    except erpnext.ERPNextError as exc:
        return idioma.t("crm.pedido_no_leido", lengua, pedido=pedido, exc=exc)

    # LA COMPROBACIÓN VA ACÁ Y AHORA, sobre el documento que acabo de leer. Que
    # el dueño lo llame «el que está esperando» no prueba que siga esperando:
    # entre que lo dijo y esto corre, el barrido o una persona pueden haberlo
    # emitido.
    estado = int(doc.get("docstatus") or 0)
    if estado != 0:
        cual = idioma.t(
            "crm.estado_confirmado" if estado == 1 else "crm.estado_cancelado", lengua
        )
        return idioma.t("crm.pedido_no_borrador", lengua, pedido=pedido, cual=cual)

    cambios: dict = {}
    if lineas:
        cambios["items"] = _lineas(lineas)
    if fecha_entrega:
        cambios["delivery_date"] = fecha_entrega.strip()
    try:
        erpnext.update_doc("Sales Order", pedido, cambios)
    except erpnext.ERPNextError as exc:
        return idioma.t("crm.pedido_error", lengua, pedido=pedido, exc=exc)

    dicho = []
    if lineas:
        dicho.append(
            idioma.t("crm.cambio_renglones", lengua, renglones=len(lineas))
        )
    if fecha_entrega:
        dicho.append(
            idioma.t("crm.cambio_entrega", lengua, fecha_entrega=fecha_entrega)
        )
    return idioma.t(
        "crm.pedido_actualizado", lengua, pedido=pedido, dicho=", ".join(dicho)
    )


# ------------------------------------------------------------- el producto

@tool
def actualizar_producto(
    config: RunnableConfig,
    item_code: Annotated[str, Field(description="Código exacto del producto.")],
    descripcion: Annotated[
        str | None,
        Field(description="Descripción nueva, la que lee una persona en el catálogo."),
    ] = None,
    punto_de_reposicion: Annotated[
        float | None,
        Field(ge=0,
              description="Debajo de esta cantidad, el producto aparece en el informe "
                          "de stock bajo. Necesita `deposito`. Nunca negativo: un "
                          "negativo apaga el aviso sin que se note."),
    ] = None,
    deposito: Annotated[
        str | None,
        Field(description="El depósito al que le ponés el punto de reposición."),
    ] = None,
) -> str:
    """Cambia la descripción de un producto o cuándo avisar que se está por acabar.

    El punto de reposición es lo que hace que un producto salga en
    `informe(que="stock_bajo")`. Subirlo es avisar antes; bajarlo, después.

    NO TOCA EL PRECIO. El precio de un producto vive en su lista de precios, la
    gobierna `policy` y no se cambia desde una herramienta: un precio escrito mal
    no da error, simplemente deja de auto-confirmarse y nadie se entera.
    """
    try:
        require_management(config)
    except RuntimeContextError:
        return _sin_permiso()

    lengua = idioma.gerencia()
    hecho: list[str] = []
    if descripcion:
        try:
            erpnext.update_doc("Item", item_code, {"description": descripcion.strip()})
            hecho.append(idioma.t("crm.hecho_descripcion", lengua))
        except erpnext.ERPNextError as exc:
            return idioma.t(
                "crm.producto_descripcion_error", lengua, item_code=item_code, exc=exc
            )

    if punto_de_reposicion is not None:
        if not deposito:
            return _con_lo_hecho(
                hecho, idioma.t("crm.reposicion_sin_deposito", lengua)
            )
        try:
            filas = erpnext.get_list(
                "Item Reorder",
                fields=["name", "warehouse", "warehouse_reorder_level"],
                filters=[["parent", "=", item_code], ["warehouse", "=", deposito]],
                limit=1,
                # Frappe se niega a listar una tabla hija sin su padre.
                parent="Item",
            )
        except erpnext.ERPNextError as exc:
            return _con_lo_hecho(hecho, idioma.t(
                "crm.reposicion_no_leida", lengua, item_code=item_code, exc=exc))
        if not filas:
            return _con_lo_hecho(hecho, idioma.t(
                "crm.reposicion_sin_regla", lengua,
                item_code=item_code, deposito=deposito))
        try:
            erpnext.update_doc(
                "Item Reorder", filas[0]["name"],
                {"warehouse_reorder_level": float(punto_de_reposicion)},
            )
            hecho.append(idioma.t(
                "crm.hecho_reposicion", lengua, deposito=deposito,
                punto_de_reposicion=f"{punto_de_reposicion:g}"))
        except erpnext.ERPNextError as exc:
            return _con_lo_hecho(hecho, idioma.t(
                "crm.reposicion_error", lengua, item_code=item_code, exc=exc))

    if not hecho:
        return idioma.t("crm.producto_sin_cambios", lengua)
    return idioma.t(
        "crm.producto_listo", lengua, item_code=item_code, hecho=", ".join(hecho)
    )


@tool
def cambiar_precio(
    config: RunnableConfig,
    producto: Annotated[
        str, Field(description="Código exacto del producto en el catálogo.")
    ],
    precio: Annotated[
        float,
        Field(gt=0, description="El precio NUEVO de lista, en la moneda del "
                                "negocio. Sin símbolo y sin puntos de miles."),
    ],
) -> str:
    """Cambia el precio de lista de UN producto. Sin código y sin confirmar.

    Automática a propósito: el dueño puso la banda UNA vez («banda de precio
    15%») y adentro de esa banda esto corre solo para siempre. Fuera de la banda
    NO pide permiso — se niega y le dice la cuenta. Con la banda en 0, que es el
    default, no cambia ningún precio y lo dice.

    Un cambio por producto por día: la banda acota UN salto y no una serie, y
    vos podés llamar a esto cinco veces en el mismo turno. El segundo intento
    del día sobre el mismo producto no escribe — no es un permiso que falta, es
    que ya lo cambiaste hoy.

    LO ÚNICO QUE APORTÁS ES EL NÚMERO. La lista y la moneda salen de `policy` y
    la unidad del `stock_uom` del producto: si eso viniera de vos, un precio con
    la unidad equivocada quedaría escrito sin que nadie se entere y dejaría de
    auto-confirmarse. Y relee después de escribir, así que lo que te contesta es
    lo que quedó, no lo que se mandó.
    """
    try:
        require_management(config)
    except RuntimeContextError:
        return _sin_permiso()
    from app import precios

    return precios.cambiar(producto, precio, "")
