"""Lo que el dueño puede CAMBIAR: reversible, y sin que se mueva plata.

DÓNDE ESTÁ LA LÍNEA, Y POR QUÉ ESTÁ AHÍ
---------------------------------------
Durante meses la respuesta a «que el agente pueda hacer lo que le pido» fue que
escribir es peligroso. Era una respuesta demasiado ancha, y la línea correcta no
es leer-contra-escribir: es **irreversible × plata**.

Lo que se abre acá: la ficha de un cliente (dirección, teléfono, grupo,
condición de pago), notas y recordatorios sobre cualquier registro, un
presupuesto en borrador, la corrección de un pedido QUE SIGUE EN BORRADOR, y la
descripción y el punto de reposición de un producto. Todo eso se deshace
escribiendo de nuevo, y nada de eso le cobra un peso a nadie.

Lo que NO se abre, y no está en este archivo ni alcanzable desde él:
  · emitir cualquier cosa (`submit_doc` usa la credencial de política, que
    ninguna herramienta toca);
  · cancelar un documento ya emitido;
  · **Item Price** — un precio gobierna EN SILENCIO lo que se auto-confirma, y
    escribir uno mal no da error en ninguna parte: simplemente deja de
    confirmarse solo. `erpnext.DOCTYPES_EDITABLES` no lo incluye, así que la
    negativa es del cliente HTTP y no de la buena conducta de este archivo;
  · los límites del dueño, que siguen necesitando su código de cuatro dígitos.

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


def _buscar_cliente(nombre_o_codigo: str) -> dict | None:
    """El código exacto primero, después el nombre. Igual que `ficha_cliente`.

    `name` es la clave del documento y no puede coincidir con dos; el nombre va
    con `like` y se queda con el primero, que es lo que ya hacía el resto.
    """
    exacto = erpnext.get_list(
        "Customer", filters=[["name", "=", nombre_o_codigo]],
        fields=["name", "customer_name"], limit=1,
    )
    if exacto:
        return exacto[0]
    from app.clientes import patron_like

    aproximado = erpnext.get_list(
        "Customer", filters=[["customer_name", "like", patron_like(nombre_o_codigo)]],
        fields=["name", "customer_name"], limit=1,
    )
    return aproximado[0] if aproximado else None


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

    ficha = _buscar_cliente(cliente)
    if ficha is None:
        return f"No encontré ningún cliente que se llame o se codifique «{cliente}»."

    cambios: dict = {}
    if grupo:
        cambios["customer_group"] = grupo.strip()
    if condicion_de_pago:
        cambios["payment_terms"] = condicion_de_pago.strip()

    if not cambios:
        return "No me dijiste qué cambiarle. Decime el grupo o la condición de pago."
    try:
        erpnext.update_doc("Customer", ficha["name"], cambios)
    except erpnext.ERPNextError as exc:
        return f"No pude cambiar la ficha de {ficha.get('customer_name') or cliente}: {exc}"
    detalle = ", ".join(f"{k} = {v}" for k, v in cambios.items())
    return f"Listo. {ficha.get('customer_name') or ficha['name']}: {detalle}."


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

    doctype = OBJETOS[sobre]
    texto = " ".join((nota or "").split())
    if not texto:
        return "No me dijiste qué anotar."
    try:
        erpnext.registrar_comentario(doctype, cual, texto)
    except erpnext.ERPNextError as exc:
        return f"No pude dejar la nota en {cual}: {exc}"

    if not recordarle_a:
        return f"Anotado en {cual}."
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
        return f"La nota quedó en {cual}, pero no pude crearle la tarea a {recordarle_a}: {exc}"
    return f"Anotado en {cual}, y le queda la tarea a {recordarle_a}."


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
    if not lineas:
        return "Un presupuesto vacío no sirve. Decime al menos un producto."

    ficha = _buscar_cliente(cliente)
    if ficha is None:
        return f"No encontré ningún cliente que se llame o se codifique «{cliente}»."

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
        return f"No pude armar el presupuesto: {exc}"
    return (
        f"Presupuesto {doc.get('name')} en borrador para "
        f"{ficha.get('customer_name') or ficha['name']}, con {len(lineas)} renglón/es. "
        "Queda sin emitir: miralo antes de mandarlo."
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
    if not lineas and not fecha_entrega:
        return "No me dijiste qué cambiarle: los renglones o la fecha."

    try:
        doc = erpnext.get_doc("Sales Order", pedido)
    except erpnext.ERPNextError as exc:
        return f"No pude leer el pedido {pedido}: {exc}"

    # LA COMPROBACIÓN VA ACÁ Y AHORA, sobre el documento que acabo de leer. Que
    # el dueño lo llame «el que está esperando» no prueba que siga esperando:
    # entre que lo dijo y esto corre, el barrido o una persona pueden haberlo
    # emitido.
    estado = int(doc.get("docstatus") or 0)
    if estado != 0:
        cual = "confirmado" if estado == 1 else "cancelado"
        return (
            f"El pedido {pedido} ya está {cual}, así que no lo toco. "
            "Un pedido confirmado se cambia por el camino de siempre, con tu código."
        )

    cambios: dict = {}
    if lineas:
        cambios["items"] = _lineas(lineas)
    if fecha_entrega:
        cambios["delivery_date"] = fecha_entrega.strip()
    try:
        erpnext.update_doc("Sales Order", pedido, cambios)
    except erpnext.ERPNextError as exc:
        return f"No pude cambiar el pedido {pedido}: {exc}"

    dicho = []
    if lineas:
        dicho.append(f"{len(lineas)} renglón/es")
    if fecha_entrega:
        dicho.append(f"entrega {fecha_entrega}")
    return (
        f"Pedido {pedido} actualizado ({', '.join(dicho)}). Sigue en BORRADOR: "
        "hay que confirmarlo para que salga."
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
        Field(description="Debajo de esta cantidad, el producto aparece en el informe "
                          "de stock bajo. Necesita `deposito`."),
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

    hecho: list[str] = []
    if descripcion:
        try:
            erpnext.update_doc("Item", item_code, {"description": descripcion.strip()})
            hecho.append("descripción")
        except erpnext.ERPNextError as exc:
            return f"No pude cambiar la descripción de {item_code}: {exc}"

    if punto_de_reposicion is not None:
        if not deposito:
            return (
                "Para el punto de reposición necesito el depósito: el mismo producto "
                "puede tener uno distinto en cada uno."
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
            return f"No pude leer el punto de reposición de {item_code}: {exc}"
        if not filas:
            return (
                f"{item_code} no tiene una regla de reposición en {deposito} todavía. "
                "Esa se crea en ERPNext una vez, y después la puedo ajustar."
            )
        try:
            erpnext.update_doc(
                "Item Reorder", filas[0]["name"],
                {"warehouse_reorder_level": float(punto_de_reposicion)},
            )
            hecho.append(f"punto de reposición en {deposito} = {punto_de_reposicion:g}")
        except erpnext.ERPNextError as exc:
            return f"No pude cambiar el punto de reposición de {item_code}: {exc}"

    if not hecho:
        return "No me dijiste qué cambiarle: la descripción o el punto de reposición."
    return f"{item_code}: {', '.join(hecho)}."
