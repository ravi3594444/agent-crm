"""Cómo se llama cada ajuste EN EL PANEL, y qué hace, en inglés.

Por qué existe un segundo nombre para cada ajuste, en vez de traducir el que
ya hay: son dos consumidores distintos del mismo dato y sólo uno es prosa.

`limites.Definicion.alias` es lo que el dueño TIPEA por WhatsApp —«monto
maximo», «rubro»—, y `app/main.py` lo matchea para saber qué ajuste está
cambiando. Cambiar esas palabras rompe el comando del otro lado, y por eso
`styles.css` las dejaba en castellano y sólo les arreglaba la mayúscula. El
panel, en cambio, no matchea nada: muestra. Así que acá viven los nombres
ingleses del panel y los aliases siguen intactos donde se tipean.

`significado` tenía el mismo problema una capa más abajo: lo lee el dueño por
WhatsApp (`ver_ajustes`) y también el panel, y está escrito en castellano para
el primero. Acá está el texto del segundo.

DOS REGLAS que hacen que esto no se pudra:

* **Todo ajuste tiene su fila.** `limites.TODOS` son 46 y acá hay 46; si
  alguien agrega el 47 sin tocar este archivo, `etiqueta()` NO cae al alias
  castellano —eso reintroduciría en silencio justo lo que este archivo vino a
  sacar—, sino a un nombre derivado de la variable, que es feo y en inglés. Feo
  se ve; castellano se confunde con lo correcto.
* **La descripción dice QUÉ HACE, no qué es.** Un ajuste sin explicación
  obliga a adivinar, y adivinar un tope de auto-confirmación se paga caro.
"""
from __future__ import annotations

# (nombre en el panel, qué hace). El orden sigue al de `limites.TODOS` para
# que comparar las dos listas sea leerlas en paralelo.
ETIQUETAS: dict[str, tuple[str, str]] = {
    # --- Automatic confirmation -------------------------------------------
    "AUTO_CONFIRM_MAX": (
        "Order ceiling",
        "The largest order that can be confirmed without anyone looking at it.",
    ),
    "AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO": (
        "Quantity per product",
        "The most of any single product an automatic order may take, in that "
        "product's stock unit (litre, kilo, unit).",
    ),
    "STOCK_BUFFER_PCT": (
        "Stock buffer",
        "Stock held back for sales that have not been entered yet.",
    ),
    "AUTO_CONFIRM_MAX_CLIENTE_NUEVO": (
        "New customer ceiling",
        "The ceiling for a customer without enough history. At 0, a new "
        "customer always waits for a person.",
    ),
    "AUTO_CONFIRM_MAX_DEBT": (
        "Overdue balance tolerated",
        "How much overdue debt is tolerated before a person has to look.",
    ),
    "AUTO_CONFIRM_MAX_DESCUENTO_PCT": (
        "Maximum discount",
        "The largest discount — line and order ADDED TOGETHER — that can "
        "confirm itself while discount approval is switched off.",
    ),
    "APROBACION_TIMEOUT_HORAS": (
        "Approval timeout",
        "How long a pending decision waits before it expires. It is also how "
        "long a draft awaiting an answer can hold stock: once the deadline "
        "passes, that order stops competing with live ones.",
    ),
    "REVISION_TIMEOUT_HORAS": (
        "Review deadline",
        "How long an order can wait for a person to review it — after the "
        "customer accepted and something had changed — before the draft "
        "closes and releases its stock.",
    ),
    "AUTO_CONFIRM_DESCUENTOS_APRUEBAN": (
        "Discounts need approval",
        "When yes, any discount — on the order or on a line — goes to a "
        "person. When no, a discount can confirm itself as long as the price "
        "does not go above the list.",
    ),
    "PENDIENTE_AVISO_HORAS": (
        "Pending reminder",
        "How long an undecided order waits before the customer is told it is "
        "still not confirmed, and before you are reminded. It confirms and "
        "cancels nothing: it only ends the silence. At NONE, nobody is told.",
    ),
    "AVISO_ANTES_DE_ENTREGA_HORAS": (
        "Notice before delivery",
        "How long before the promised delivery the customer is told their "
        "order is STILL not confirmed. It promises no day, hour or price: it "
        "repeats the deadline they already knew and says it did not get "
        "confirmed. It also needs a delivery hour set, because an ERPNext "
        "delivery date carries no time. At NONE, nobody is told.",
    ),
    "PENDIENTE_CIERRE_HORAS": (
        "Pending order closes",
        "After how many hours without a decision an order nobody looked at is "
        "closed, so it stops holding stock another customer could have taken. "
        "The customer is told plainly that it was not confirmed. At NONE "
        "nothing closes and the draft waits forever.",
    ),
    "PENDIENTE_NOCHE_DESDE": (
        "Do not disturb from",
        "From what time of day a reminder is no longer sent to a customer.",
    ),
    "PENDIENTE_NOCHE_HASTA": (
        "Do not disturb until",
        "Until what time of day a reminder is not sent to a customer.",
    ),
    "AUTO_CONFIRM_SOMBRA": (
        "Shadow mode",
        "When yes, every order left waiting records what the rules WOULD have "
        "said if the ceiling and stock trust were switched on. It confirms "
        "nothing: it just leaves the number, so the decision has data behind "
        "it.",
    ),
    "PRECIO_CAMBIO_MAX_PCT": (
        "Price change band",
        "How far a price may move in one go, as a percentage, without anyone "
        "looking. At 0 the agent changes no prices at all.",
    ),
    # --- Delivery and pickup ----------------------------------------------
    "ZONAS_ENTREGA_LOCALIDADES": (
        "Delivery localities",
        "The localities delivered to without a person looking. If postcodes "
        "are set too, an address needs BOTH to be allowed.",
    ),
    "ZONAS_ENTREGA_CP": (
        "Delivery postcodes",
        "The postcodes delivered to without a person looking. If localities "
        "are set too, an address needs BOTH to be allowed.",
    ),
    "ENTREGA_DIAS": (
        "Delivery days",
        "The days the normal delivery round goes out. This is what a customer "
        "is offered when a request expires with nobody answering it.",
    ),
    "ENTREGA_HORA": (
        "Delivery time",
        "The time promised for the normal delivery round.",
    ),
    "ENTREGA_EXCEPCION_ACTIVA": (
        "Off-day deliveries",
        "When yes, an order can be delivered on a day with no round without "
        "anyone looking, as long as the rest of the setup allows it.",
    ),
    "ENTREGA_EXCEPCION_DIAS": (
        "Off-day delivery days",
        "The days on which delivery outside the normal round is allowed.",
    ),
    "ENTREGA_EXCEPCION_HORA": (
        "Off-day delivery time",
        "The time promised for a delivery outside the normal round.",
    ),
    "ENTREGA_EXCEPCION_CARGO": (
        "Off-day delivery charge",
        "What is charged for a delivery outside the normal round. It is only "
        "written onto the order if the accounting account is set as well.",
    ),
    "ENTREGA_EXCEPCION_MIN_TOTAL": (
        "Off-day minimum order",
        "The order total needed for an off-day delivery to be pre-authorised. "
        "At 0 there is no minimum.",
    ),
    "RETIRO_LOCAL_ACTIVO": (
        "Pickup at the shop",
        "When yes, if there is no delivery round to put an order on, the "
        "customer can be offered to come and collect it.",
    ),
    "RETIRO_LOCAL_DIAS": (
        "Pickup days",
        "The days an order can be collected from the shop.",
    ),
    "RETIRO_LOCAL_HORA": (
        "Pickup time",
        "The time an order can be collected.",
    ),
    # --- Language ----------------------------------------------------------
    "IDIOMA_GERENCIA": (
        "Team language",
        "Which language the system answers your team in.",
    ),
    # --- Your business -----------------------------------------------------
    "NOMBRE_NEGOCIO": (
        "Business name",
        "What the business is called. It is the first line of both agent "
        "prompts.",
    ),
    "NOMBRE_AGENTE": (
        "Agent name",
        "The name the WhatsApp agent introduces itself by. Left empty, it "
        "introduces itself by what it does and invents no name.",
    ),
    "RUBRO_NEGOCIO": (
        "Trade",
        "What the business does, in a few words.",
    ),
    "HORARIO_ATENCION": (
        "Opening hours",
        "The hours the business is open, worded the way you would tell a "
        "customer.",
    ),
    # --- WhatsApp templates -------------------------------------------------
    "WHATSAPP_TEMPLATE_LANGUAGE": (
        "Template language",
        "The language your templates are REGISTERED in at Meta. If it does "
        "not match the registration, Meta answers that the template does not "
        "exist.",
    ),
    "WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE": (
        "Confirmed template",
        "Tells the customer their order was confirmed.",
    ),
    "WHATSAPP_CUSTOMER_REJECTED_TEMPLATE": (
        "Rejected template",
        "Tells the customer their order could not be taken.",
    ),
    "WHATSAPP_CUSTOMER_CANCELLED_TEMPLATE": (
        "Cancelled template",
        "Tells the customer their order was cancelled.",
    ),
    "WHATSAPP_CUSTOMER_EXPIRED_TEMPLATE": (
        "Expired template",
        "Tells the customer their request expired with no answer.",
    ),
    "WHATSAPP_CUSTOMER_FALLBACK_TEMPLATE": (
        "Fallback template",
        "The message to the customer when no other text applies.",
    ),
    "WHATSAPP_CUSTOMER_REVIEW_EXPIRED_TEMPLATE": (
        "Review expired template",
        "Tells the customer the review deadline on their order has passed.",
    ),
    "WHATSAPP_CUSTOMER_PENDING_TEMPLATE": (
        "Pending template",
        "Reminds the customer their order is still waiting on a decision.",
    ),
    "WHATSAPP_CUSTOMER_PENDING_CLOSED_TEMPLATE": (
        "Pending closed template",
        "Tells the customer their pending order was closed.",
    ),
    "WHATSAPP_CUSTOMER_DELIVERY_LEAD_TEMPLATE": (
        "Delivery notice template",
        "Tells the customer a few hours before their delivery arrives.",
    ),
    "WHATSAPP_STAFF_PENDING_TEMPLATE": (
        "Team pending template",
        "Tells the team an order is waiting for someone to look at it.",
    ),
    "WHATSAPP_STAFF_CONFIRMED_TEMPLATE": (
        "Team confirmed template",
        "Tells the team an order was confirmed.",
    ),
    "WHATSAPP_STAFF_ALERT_TEMPLATE": (
        "Team alert template",
        "The message to the team when something needs attention now.",
    ),
}

# Lo que no es un ajuste del dueño pero el panel muestra en la misma lista.
EXTRA: dict[str, tuple[str, str]] = {
    "STOCK_CONFIABLE_HORAS": (
        "Stock trust window",
        "How recent a confirmed stock count has to be before the policy will "
        "promise that stock.",
    ),
}


# La UNIDAD también viaja en castellano desde `limites` —es lo que el dueño lee
# por WhatsApp— y el panel la imprime al lado del valor. Un valor en inglés con
# la unidad en castellano es el mismo renglón partido en dos idiomas.
UNIDADES: dict[str, str] = {
    "$": "$",
    "%": "%",
    "h": "h",
    "hh:mm": "hh:mm",
    "códigos postales": "postcodes",
    "días": "days",
    "idioma": "language",
    "idioma de Meta": "Meta language",
    "localidades": "localities",
    "plantilla de Meta": "Meta template",
    "sí/no": "yes/no",
    "texto": "text",
    "unidad de stock": "stock unit",
}


def unidad_de(unidad: object) -> str:
    """La unidad en inglés. Una que no esté en la tabla vuelve tal cual: es
    mejor una unidad rara que una vacía al lado de un número."""
    texto = str(unidad or "").strip()
    return UNIDADES.get(texto, texto)


def _derivado(nombre: str) -> str:
    """El nombre de la variable como título. Feo a propósito, y en inglés.

    Es el caso de un ajuste nuevo que nadie agregó acá. Caer al alias sería
    castellano otra vez, en silencio y con cara de estar bien; esto se ve.
    """
    return nombre.replace("_", " ").strip().capitalize() or nombre


def etiqueta(nombre: str) -> tuple[str, str]:
    """(nombre del panel, qué hace) en inglés. Nunca devuelve castellano."""
    fila = ETIQUETAS.get(nombre) or EXTRA.get(nombre)
    if fila:
        return fila
    return _derivado(nombre), "No description has been written for this setting yet."


def nombre_de(nombre: str) -> str:
    """Sólo el nombre, para quien no necesita la explicación."""
    return etiqueta(nombre)[0]


def explicacion_de(nombre: str) -> str:
    """Sólo el qué hace."""
    return etiqueta(nombre)[1]
