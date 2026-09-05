"""Un solo catálogo para TODO lo que escribe Python, en español e inglés.

POR QUÉ ESTE MÓDULO EXISTE
Los mensajes que decide el modelo ya salen en el idioma del cliente: se lo pide
el prompt. Pero la mitad de lo que recibe una persona NO lo escribe el modelo —
lo escribe Python: el aviso de pedido pendiente, el código de confirmación, el
error de un código vencido, el estado del sistema. Esos textos son la parte que
NO se puede traducir con un LLM, porque son justamente los que autorizan algo.

LAS DOS REGLAS QUE NO SE NEGOCIAN

1. Acá no se traduce nada en tiempo real. Cada texto tiene sus dos versiones
   escritas a mano en CATALOGO. Un modelo nunca ve estos strings ni los reescribe:
   un mensaje de autorización traducido por una máquina es un mensaje de
   autorización que alguien puede empujar a decir otra cosa.

2. Los datos NO se traducen. El código de seis dígitos, el nombre del pedido, la
   cantidad, el precio, la fecha y el estado de ERPNext se interpolan tal cual,
   iguales byte a byte en los dos idiomas. Sólo cambia la prosa alrededor.

QUÉ PASA CUANDO FALTA UNA TRADUCCIÓN
Se cae al idioma por defecto y sigue. Una clave sin texto en inglés manda el
español y anota el problema en el log; NUNCA levanta una excepción ni devuelve
un texto vacío, porque un mensaje que no sale es un cliente que se queda sin
respuesta, y eso es peor que un mensaje en el otro idioma.
"""
from __future__ import annotations

import hashlib
import os

ES = "es"
EN = "en"
IDIOMAS = (ES, EN)

# El idioma al que se cae todo lo que no se pudo resolver. Configurable, pero
# nunca vacío: si alguien pone cualquier otra cosa, es español.
def por_defecto() -> str:
    crudo = str(os.getenv("IDIOMA_DEFAULT", ES) or "").strip().lower()
    return crudo if crudo in IDIOMAS else ES


# Cómo lo dice una persona. Se compara sin tildes y en minúsculas.
_DICHO = {
    ES: (
        "espanol", "espaniol", "castellano", "spanish", "es", "esp",
        "espanhol",
    ),
    EN: ("ingles", "english", "en", "eng", "ingl"),
}


def _sin_tildes(texto: object) -> str:
    import unicodedata

    crudo = str(texto or "").strip().lower()
    return "".join(
        c for c in unicodedata.normalize("NFD", crudo)
        if unicodedata.category(c) != "Mn"
    )


def normalizar(crudo: object) -> str | None:
    """El idioma que nombra ese texto, o None si no nombra ninguno.

    Deliberadamente estricto: sólo reconoce la PALABRA del idioma. No adivina
    por el idioma en que está escrita la frase — para eso está `detectar`.
    """
    limpio = _sin_tildes(crudo)
    if not limpio:
        return None
    for idioma, palabras in _DICHO.items():
        if limpio in palabras:
            return idioma
    # «manager language english», «idioma de gerencia inglés»: la última
    # palabra es la que manda.
    fichas = [f for f in limpio.replace(",", " ").split() if f]
    for ficha in reversed(fichas):
        for idioma, palabras in _DICHO.items():
            if ficha in palabras:
                return idioma
    return None


def valido(crudo: object) -> str:
    """El idioma, o el de por defecto. Nunca levanta."""
    return normalizar(crudo) or por_defecto()


# --------------------------------------------------------------- el catálogo

# clave -> {idioma: texto}. Las claves son estables: se usan en los tests para
# exigir que TODA clave tenga los dos idiomas.
CATALOGO: dict[str, dict[str, str]] = {
    # ---------------------------------------------------------------- acuse
    "ack.recibido": {
        ES: "Recibido, dame un momento mientras lo verifico.",
        EN: "Got it, give me a moment to check.",
    },
    "ack.solo_texto": {
        ES: (
            "Por ahora necesito que me escribas el pedido en texto para poder "
            "ayudarte."
        ),
        EN: (
            "For now I need you to write the order as text so I can help you."
        ),
    },
    "fallback.respuesta_vacia": {
        ES: "Perdón, no pude armar la respuesta. ¿Me lo escribís de nuevo?",
        EN: "Sorry, I couldn't put together a reply. Could you send that again?",
    },
    "fallback.problema_tecnico": {
        ES: (
            "Perdón, tuve un problema técnico y no pude procesar tu mensaje. "
            "Probá de nuevo en unos minutos."
        ),
        EN: (
            "Sorry, I hit a technical problem and couldn't process your message. "
            "Try again in a few minutes."
        ),
    },
    "fallback.problema_tecnico_avisado": {
        ES: (
            "Perdón, tuve un problema técnico. Ya avisé al equipo y te responden "
            "en un rato."
        ),
        EN: (
            "Sorry, I hit a technical problem. I've told the team and they'll get "
            "back to you shortly."
        ),
    },
    # ------------------------------------------------------ estado de pedido
    # Todos estos los recibe el CLIENTE. Antes iban en los dos idiomas pegados
    # —«outside a model turn the customer's language is unknown», decía el
    # docstring— y justamente eso es lo que dejó de ser cierto.
    "pedido.pendiente": {
        ES: (
            "Tu pedido {pedido} quedó registrado y le pregunté al encargado por "
            "lo que pediste. Te contesto en cuanto responda (dentro de {horas} h). "
            "Todavía no está confirmado: cuando tenga la respuesta vuelvo a "
            "chequear el stock antes de cerrarlo."
        ),
        EN: (
            "Your order {pedido} is registered and I have asked the manager about "
            "your request. I will reply as soon as they answer (within {horas} h). "
            "It is not confirmed yet, and I will re-check stock before closing it."
        ),
    },
    "pedido.confirmado_cliente": {
        ES: (
            "✅ Pedido {pedido} confirmado\n"
            "Items: {renglones}\nTotal: {total}\nEntrega: {entrega}"
        ),
        EN: (
            "✅ Order {pedido} confirmed\n"
            "Items: {renglones}\nTotal: {total}\nDelivery: {entrega}"
        ),
    },
    "pedido.entrega_a_coordinar": {
        ES: "a coordinar",
        EN: "to be arranged",
    },
    "pedido.rechazado": {
        ES: (
            "Hola! Sobre tu pedido {pedido}: no vamos a poder cumplirlo{motivo}. "
            "En breve te escribe alguien del equipo. Perdón por la molestia."
        ),
        EN: (
            "Hi! About your order {pedido}: we won't be able to fulfil it{motivo}. "
            "Someone from our team will message you shortly. Sorry about that."
        ),
    },
    "pedido.cancelado": {
        ES: (
            "Hola! Tu pedido {pedido} quedó cancelado ({motivo}). Si fue un error, "
            "escribinos y lo revisamos."
        ),
        EN: (
            "Hi! Your order {pedido} has been cancelled ({motivo}). If this is a "
            "mistake, message us and we will sort it out."
        ),
    },
    "pedido.sin_confirmar": {
        ES: "Tu pedido {pedido} sigue sin confirmar.",
        EN: "Your order {pedido} is still unconfirmed.",
    },
    # ------------------------------------------------- avisos a la gerencia
    # El encabezado y el cuerpo del aviso de pedido. Los COMANDOS que van
    # adentro ('confirmar X', 'ver X', 'cancelar X') se dejan en español a
    # propósito incluso en el texto en inglés: son el payload que parsea el
    # router y cambiarlos rompería lo que la gente ya escribe. El equivalente
    # en inglés también parsea, así que quien prefiera inglés puede usarlo.
    "gerencia.encabezado_pendiente": {
        ES: "🟡 Pedido pendiente de revisión",
        EN: "🟡 Order pending review",
    },
    "gerencia.encabezado_confirmado": {
        ES: "✅ Pedido confirmado automáticamente",
        EN: "✅ Order automatically confirmed",
    },
    "gerencia.cuerpo_pedido": {
        ES: (
            "Pedido: {pedido}\nCliente: {cliente}\nItems: {detalle}\n"
            "Total: {total}\nEntrega: {entrega}"
        ),
        EN: (
            "Order: {pedido}\nCustomer: {cliente}\nItems: {detalle}\n"
            "Total: {total}\nDelivery: {entrega}"
        ),
    },
    "gerencia.sin_observaciones": {ES: "Sin observaciones", EN: "No remarks"},
    "gerencia.sin_fecha": {ES: "Sin fecha", EN: "No date"},
    "gerencia.a_coordinar": {ES: "a coordinar", EN: "to be arranged"},
    "gerencia.no_registrado": {ES: "no registrado", EN: "not registered"},
    "gerencia.sin_dato": {ES: "n/d", EN: "n/a"},
    "gerencia.motivo": {
        ES: "Motivo: {motivo}",
        EN: "Reason: {motivo}",
    },
    "gerencia.responder_para_decidir": {
        ES: "Respondé 'confirmar {pedido}' o 'ver {pedido}'.",
        EN: "Reply 'confirmar {pedido}' or 'ver {pedido}'.",
    },
    "gerencia.confirmado_detalle": {
        ES: (
            "✅ Pedido {pedido} confirmado\nCliente: {cliente}\nItems: {detalle}\n"
            "Total: {total}\nEntrega: {entrega}\nOrigen: {fuente}\n"
            "Confirmado: {momento}\n"
            "Informativo: no hace falta responder, el pedido queda confirmado.\n"
            "Para anularlo dentro de las {horas} h: cancelar {pedido} <motivo>"
        ),
        EN: (
            "✅ Order {pedido} confirmed\nCustomer: {cliente}\nItems: {detalle}\n"
            "Total: {total}\nDelivery: {entrega}\nSource: {fuente}\n"
            "Confirmed: {momento}\n"
            "For your information: no reply needed, the order is confirmed.\n"
            "To void it within {horas} h: cancelar {pedido} <reason>"
        ),
    },
    "gerencia.escalamiento_asunto": {
        ES: "🙋 Un cliente necesita una persona",
        EN: "🙋 A customer needs a person",
    },
    "gerencia.escalamiento_cuerpo": {
        ES: "Cliente: {cliente}\nTel: {telefono}\nMotivo: {motivo}",
        EN: "Customer: {cliente}\nPhone: {telefono}\nReason: {motivo}",
    },
    "gerencia.escalamiento_tarea": {ES: "Tarea: {tarea}", EN: "Task: {tarea}"},
    "gerencia.cliente_no_avisado": {
        ES: "OJO: no pude avisarle al cliente.",
        EN: "HEADS UP: I couldn't notify the customer.",
    },
    # --------------------------------------------------- entrega / vencimiento
    "entrega.fuera_de_dia": {
        ES: "Esa entrega queda fuera de los días de reparto. La decide una persona.",
        EN: "That delivery falls outside the delivery days. A person decides it.",
    },
    "entrega.fuera_de_zona": {
        ES: "No repartimos en esa zona por ahora.",
        EN: "We don't deliver to that area right now.",
    },
    "entrega.solicitud_vencida": {
        ES: (
            "Se venció la espera por tu pedido {pedido} y no pude ofrecerte una "
            "alternativa. Lo ve una persona."
        ),
        EN: (
            "The wait on your order {pedido} expired and I couldn't offer an "
            "alternative. A person will look at it."
        ),
    },
    "entrega.aprobacion_vencida": {
        ES: "Venció el plazo para decidir el pedido {pedido}.",
        EN: "The deadline to decide order {pedido} has passed.",
    },
    # -------------------------------------------- códigos de cuatro dígitos
    "codigo.ajuste_pedido": {
        ES: (
            "Código para confirmar el cambio de ajuste:\n{cambio}\n\n"
            "Contestá *{codigo}* para aplicarlo. "
            "Si no contestás, en {minutos} minutos se descarta solo."
        ),
        EN: (
            "Code to confirm the setting change:\n{cambio}\n\n"
            "Reply *{codigo}* to apply it. "
            "If you don't reply, it's discarded on its own in {minutos} minutes."
        ),
    },
    "codigo.ajuste_preparado": {
        ES: (
            "Cambio preparado, todavía sin aplicar:\n{cambio}\n\n"
            "Te mandé el código de confirmación por separado: contestá con esos "
            "cuatro dígitos y lo aplico."
        ),
        EN: (
            "Change prepared, not applied yet:\n{cambio}\n\n"
            "I sent you the confirmation code separately: reply with those "
            "four digits and I'll apply it."
        ),
    },
    "codigo.ajuste_aplicado": {
        ES: (
            "Listo: *{ajuste}* pasó de {anterior} a {nuevo}. "
            "Rige desde el próximo pedido, sin reiniciar nada. "
            "Queda registrado a tu nombre ({ts})."
        ),
        EN: (
            "Done: *{ajuste}* went from {anterior} to {nuevo}. "
            "It applies from the next order on, with no restart. "
            "It's on record under your name ({ts})."
        ),
    },
    "codigo.ajuste_no_aplicado": {
        ES: "No apliqué nada: {motivo}.",
        EN: "I applied nothing: {motivo}.",
    },
    "codigo.ajuste_error": {
        ES: "No pude aplicar el cambio en este momento. No cambié nada.",
        EN: "I couldn't apply the change right now. Nothing was changed.",
    },
    "codigo.ajuste_repetido": {
        ES: (
            "Es el mismo cambio que ya estaba esperando:\n{cambio}\n\n"
            "Te reenvié el MISMO código, así que el que ya tenías sigue sirviendo."
        ),
        EN: (
            "That's the same change already waiting:\n{cambio}\n\n"
            "I re-sent the SAME code, so the one you already had still works."
        ),
    },
    # ------------------------------------------- códigos de seis dígitos
    "accion.preparada": {
        ES: (
            "Código para confirmar esta acción sobre {pedido}:\n"
            "{consecuencia}\n\n"
            "Contestá *{codigo}* para que la haga. "
            "Si no contestás, en {minutos} minutos se descarta sola. "
            "Este código confirma sólo esta acción y ninguna otra que tengas "
            "esperando."
        ),
        EN: (
            "Code to confirm this action on {pedido}:\n"
            "{consecuencia}\n\n"
            "Reply *{codigo}* and I'll do it. "
            "If you don't reply, it's discarded on its own in {minutos} minutes. "
            "This code confirms only this action and no other one you may have "
            "waiting."
        ),
    },
    "accion.aplicada": {
        ES: "Hecho: {consecuencia}",
        EN: "Done: {consecuencia}",
    },
    # ------------------------------------------------- errores de código
    "codigo.invalido": {
        ES: "Ese código no es el del cambio pendiente.",
        EN: "That code doesn't match the pending change.",
    },
    "codigo.vencido": {
        ES: "Ese código ya venció. No cambié nada: pedime el cambio de nuevo.",
        EN: "That code has expired. I changed nothing: ask me for the change again.",
    },
    "codigo.sin_pendiente": {
        ES: "No hay ningún cambio esperando confirmación.",
        EN: "There's no change waiting for confirmation.",
    },
    "codigo.otro_numero": {
        ES: "Ese código no es de este número.",
        EN: "That code doesn't belong to this number.",
    },
    "codigo.no_abre_nada": {
        ES: "Ese código no abre nada.",
        EN: "That code doesn't unlock anything.",
    },
    # ------------------------------------------------ estado del sistema
    "sistema.ok": {
        ES: "· {componente}: responde",
        EN: "· {componente}: responding",
    },
    "sistema.caido": {
        ES: "· {componente}: {detalle}",
        EN: "· {componente}: {detalle}",
    },
    "sistema.no_disponible": {
        ES: "no disponible",
        EN: "unavailable",
    },
    "sistema.avisos_fallidos": {
        ES: "{cuantos} aviso(s) que no salieron.",
        EN: "{cuantos} notification(s) that didn't go out.",
    },
    "sistema.sin_avisos_fallidos": {
        ES: "No hay avisos fallidos.",
        EN: "No failed notifications.",
    },
    # ---------------------------------------- stock / precio / entrega
    "stock.no_confiable": {
        ES: "No puedo prometer disponibilidad de {producto} ahora mismo.",
        EN: "I can't promise availability of {producto} right now.",
    },
    "stock.insuficiente": {
        ES: "No me alcanza el stock de {producto} para esa cantidad.",
        EN: "I don't have enough stock of {producto} for that quantity.",
    },
    "precio.a_confirmar": {
        ES: "precio a confirmar",
        EN: "price to be confirmed",
    },
    # ------------------------------------------------ fallback / revisión
    "fallback.error_tecnico": {
        ES: (
            "Tuve un problema técnico con eso. Ya avisé al equipo y te "
            "responden a la brevedad."
        ),
        EN: (
            "I hit a technical problem with that. I've told the team and "
            "they'll get back to you shortly."
        ),
    },
    "fallback.revisa_persona": {
        ES: "Lo está viendo una persona del equipo.",
        EN: "Someone from the team is looking at it.",
    },
    "fallback.sin_permiso": {
        ES: "No tenés permiso para eso.",
        EN: "You don't have permission for that.",
    },
    # ------------------------------------------------------- idioma mismo
    "idioma.cambiado_cliente": {
        ES: "Listo, te respondo en español de ahora en más.",
        EN: "Done, I'll reply in English from now on.",
    },
    "idioma.gerencia_cambio": {
        ES: "*Idioma de gerencia*: {anterior} → {nuevo}",
        EN: "*Manager language*: {anterior} → {nuevo}",
    },
    "idioma.nombre.es": {ES: "español", EN: "Spanish"},
    "idioma.nombre.en": {ES: "inglés", EN: "English"},
}


def nombre(idioma: str, en_idioma: str | None = None) -> str:
    """Cómo se llama ese idioma, dicho en `en_idioma`."""
    return t(f"idioma.nombre.{valido(idioma)}", en_idioma)


def claves_incompletas() -> list[str]:
    """Las claves del catálogo que no tienen los dos idiomas. Para los tests."""
    faltan = []
    for clave, textos in CATALOGO.items():
        for idioma in IDIOMAS:
            if not str(textos.get(idioma, "")).strip():
                faltan.append(f"{clave}:{idioma}")
    return faltan


def t(clave: str, idioma: str | None = None, /, **params: object) -> str:
    """El texto de esa clave en ese idioma. NUNCA levanta.

    Los `params` se interpolan tal cual: un código, un número de pedido, una
    cantidad o una fecha valen lo mismo en los dos idiomas y no se tocan.

    Degradaciones, todas silenciosas menos el log:
      * idioma desconocido      -> el de por defecto
      * clave sin ese idioma    -> el de por defecto
      * clave que no existe     -> la clave misma, para que se vea en un test
      * falta un parámetro      -> el texto sin interpolar, nunca una excepción
    """
    destino = valido(idioma)
    textos = CATALOGO.get(clave)
    if textos is None:
        print(f"[idioma] clave desconocida: {clave!r}")
        return clave
    crudo = str(textos.get(destino) or "").strip()
    if not crudo:
        respaldo = por_defecto()
        crudo = str(textos.get(respaldo) or "").strip()
        if not crudo:
            crudo = str(textos.get(ES) or "").strip()
        print(f"[idioma] falta {clave!r} en {destino!r}; uso {respaldo!r}")
    if not crudo:
        return clave
    if not params:
        return crudo
    try:
        return crudo.format(**params)
    except (KeyError, IndexError, ValueError) as exc:
        # Un texto sin interpolar sigue siendo un texto. Perder el mensaje no.
        print(f"[idioma] no pude interpolar {clave!r} ({type(exc).__name__})")
        return crudo


# ------------------------------------------------ idioma de cada cliente

_PREFIJO_CLIENTE = "plus-agent:idioma-cliente"
# Un año. La preferencia del cliente no es un dato de turno: si pidió inglés en
# marzo, sigue queriendo inglés en abril. Sobrevive a un reinicio de la app
# porque vive en Redis con AOF; si se pierde el Redis se vuelve a espejar el
# idioma del mensaje, que es la degradación correcta y no un error.
TTL_CLIENTE_SEGUNDOS = 365 * 24 * 3600


def _clave_cliente(canonico: str) -> str:
    # Hasheada: el teléfono no aparece nunca en el nombre de una clave.
    return f"{_PREFIJO_CLIENTE}:{hashlib.sha256(canonico.encode()).hexdigest()}"


def recordar_cliente(numero: object, idioma: object) -> bool:
    """Guarda la preferencia de ESE teléfono. Best effort: nunca levanta.

    Devuelve True sólo si quedó guardada. Un teléfono no puede escribir la
    preferencia de otro: la clave sale del número normalizado del webhook, que
    ningún texto del mensaje puede cambiar.
    """
    from app import locks
    from app import telefono as telefono_mod

    canonico = telefono_mod.normalizar(numero)
    elegido = normalizar(idioma)
    if not canonico or not elegido:
        return False
    try:
        locks.conexion().setex(
            _clave_cliente(canonico), TTL_CLIENTE_SEGUNDOS, elegido
        )
        return True
    except Exception as exc:
        print(f"[idioma] no pude guardar el idioma del cliente ({type(exc).__name__})")
        return False


def cliente_guardado(numero: object) -> str | None:
    """La preferencia guardada de ese teléfono, o None. Nunca levanta."""
    from app import locks
    from app import telefono as telefono_mod

    canonico = telefono_mod.normalizar(numero)
    if not canonico:
        return None
    try:
        crudo = locks.conexion().get(_clave_cliente(canonico))
    except Exception as exc:
        print(f"[idioma] no pude leer el idioma del cliente ({type(exc).__name__})")
        return None
    if isinstance(crudo, bytes):
        crudo = crudo.decode()
    return normalizar(crudo)


# Lo que un cliente dice para PEDIR un idioma. Tiene que ser explícito: que el
# mensaje esté escrito en inglés no es lo mismo que pedir que le contesten en
# inglés, y confundir las dos cosas le cambia el idioma a cualquiera que
# escriba una palabra suelta en otro idioma.
_PEDIDOS_EXPLICITOS = (
    ("reply in english", EN),
    ("answer in english", EN),
    ("respond in english", EN),
    ("in english please", EN),
    ("speak english", EN),
    ("hablame en ingles", EN),
    ("contestame en ingles", EN),
    ("responde en ingles", EN),
    ("respondeme en ingles", EN),
    ("en ingles por favor", EN),
    ("respondé en español", ES),
    ("responde en espanol", ES),
    ("respondeme en espanol", ES),
    ("contestame en espanol", ES),
    ("hablame en espanol", ES),
    ("reply in spanish", ES),
    ("answer in spanish", ES),
    ("in spanish please", ES),
    ("speak spanish", ES),
    ("en espanol por favor", ES),
)


def pedido_explicito(texto: object) -> str | None:
    """El idioma que ese mensaje PIDE explícitamente, o None.

    El texto del cliente se mira como DATO: se compara contra una lista fija de
    frases y no se interpreta de ninguna otra forma.
    """
    limpio = _sin_tildes(texto)
    if not limpio:
        return None
    for frase, idioma in _PEDIDOS_EXPLICITOS:
        if _sin_tildes(frase) in limpio:
            return idioma
    return None


def para_cliente(
    numero: object, texto_entrante: object = "", *, recordar: bool = True
) -> str:
    """En qué idioma contestarle a ESTE cliente, ahora.

    El orden no es casual:
      1. Lo que pidió explícitamente en este mensaje (y queda guardado).
      2. Lo que había pedido antes.
      3. Nada guardado: se espeja el idioma del mensaje — el mismo
         comportamiento que ya tenía el sistema.
      4. Si no se puede decidir con seguridad, el idioma por defecto.

    ``recordar=False`` sólo resuelve, sin escribir nada. Es lo que usa todo el
    que necesita saber en qué idioma redactar un aviso: preguntar no puede
    tener el efecto de fijarle el idioma a alguien.
    """
    pedido = pedido_explicito(texto_entrante)
    if pedido:
        if recordar:
            recordar_cliente(numero, pedido)
        return pedido
    guardado = cliente_guardado(numero)
    if guardado:
        return guardado
    return espejo(texto_entrante)


def para_destinatario(numero: object, texto_entrante: object = "") -> str:
    """En qué idioma escribirle a quien tiene ESE número. Sin efectos.

    UN solo lugar decide esto, y por eso está acá: si el número es del equipo
    rige el idioma que fijó el dueño, y si no, el de ese cliente. Repartir esa
    decisión por el código es cómo un aviso termina saliendo en un idioma y el
    siguiente en otro.
    """
    try:
        from app.router import es_equipo

        if es_equipo(numero):
            return gerencia()
    except Exception as exc:  # router sin cargar, número raro: no es fatal
        print(f"[idioma] no pude clasificar el destinatario ({type(exc).__name__})")
    return para_cliente(numero, texto_entrante, recordar=False)


# Palabras cortas y frecuentes que sólo existen en uno de los dos idiomas. No
# es un detector de idiomas de verdad y no pretende serlo: decide entre DOS
# idiomas conocidos y, ante la duda, devuelve el de por defecto.
_PISTAS = {
    EN: (
        "the", "and", "please", "hello", "hi", "order", "want", "need",
        "delivery", "tomorrow", "thanks", "you", "can", "would", "i'd",
        "how", "much", "price", "stock", "for", "with", "my",
    ),
    ES: (
        "hola", "quiero", "necesito", "pedido", "gracias", "por", "favor",
        "manana", "entrega", "precio", "unidades", "para", "con", "que",
        "cuanto", "tenes", "tienen", "buenas", "dame", "mandame",
    ),
}


def espejo(texto: object) -> str:
    """El idioma en que parece estar escrito ese texto, o el de por defecto."""
    limpio = _sin_tildes(texto)
    if not limpio:
        return por_defecto()
    fichas = {f.strip(".,;:!¡?¿()\"'") for f in limpio.split()}
    puntajes = {
        idioma: len(fichas & set(pistas)) for idioma, pistas in _PISTAS.items()
    }
    mejor = max(puntajes, key=lambda k: puntajes[k])
    otro = EN if mejor == ES else ES
    # Empate o nada reconocido: no se adivina.
    if puntajes[mejor] == 0 or puntajes[mejor] == puntajes[otro]:
        return por_defecto()
    return mejor


# ------------------------------------------- la regla que ve el modelo

# El texto EXACTO que tenía el prompt del cliente antes de que existiera este
# módulo. Es el comportamiento por defecto y se conserva palabra por palabra:
# sin preferencia guardada, el agente espeja el idioma del mensaje igual que
# siempre.
REGLA_ESPEJO_CLIENTE = (
    "- Respondé SIEMPRE en el idioma en que te escribió el cliente en su último "
    "mensaje.\n"
    "  Si escribe en español: español rioplatense, con voseo, cordial y breve, como "
    "habla\n"
    "  la gente por WhatsApp. Si escribe en inglés: inglés simple, directo y breve.\n"
    "  Si cambia de idioma, cambiá con él. Nunca mezcles los dos en un mismo mensaje."
)

_REGLA_FIJADA = {
    ES: (
        "- Respondé SIEMPRE en español rioplatense, con voseo, cordial y breve.\n"
        "  Es el idioma que eligió esta persona: no cambies de idioma aunque el\n"
        "  último mensaje venga en otro. Nunca mezcles dos idiomas en un mensaje."
    ),
    EN: (
        "- Always reply in English: simple, direct and brief.\n"
        "  This person chose that language: do not switch languages even if the\n"
        "  last message arrives in another one. Never mix two languages in one message."
    ),
}


def regla_prompt(fijado: str | None, *, espejo_por_defecto: str | None = None) -> str:
    """La instrucción de idioma que se le pone al prompt del sistema.

    Con un idioma elegido, se fija. Sin nada elegido, se devuelve la regla de
    espejo de siempre. El modelo NUNCA decide el idioma de un texto de Python:
    esta regla sólo gobierna lo que redacta él.
    """
    elegido = normalizar(fijado) if fijado else None
    if elegido:
        return _REGLA_FIJADA[elegido]
    if espejo_por_defecto is not None:
        return espejo_por_defecto
    return REGLA_ESPEJO_CLIENTE


# ------------------------------------------------ idioma de la gerencia


def gerencia() -> str:
    """El idioma que fijó el dueño para el agente de gestión.

    NUNCA levanta y NUNCA bloquea una venta: si el almacén no se puede leer o
    se perdió, se contesta en el idioma por defecto. Un idioma no autoriza
    nada, así que no tiene por qué fallar cerrado como un límite.
    """
    from app import limites

    try:
        return limites.idioma_gerencia()
    except Exception as exc:
        print(f"[idioma] no pude leer el idioma de gerencia ({type(exc).__name__})")
        return por_defecto()
