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
import re

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
    # ------------------------------------------------------- aviso de avance
    # NO es un acuse de recibo. Sale sólo cuando el modelo eligió una
    # herramienta y ésta ya está corriendo hace unos segundos (app/progreso.py):
    # recién entonces es verdad que se está consultando algo. Una respuesta
    # directa del modelo no manda esto, tarde lo que tarde.
    # La clave se llama «consultando» por historia; el texto ya no nombra
    # ningún sistema. Nadie le dice a un cliente que está consultando un
    # sistema: le dice que le da un segundo.
    "progreso.consultando": {
        ES: "Dame un segundo que lo miro.",
        EN: "One sec, let me check.",
    },
    # Lo que llega y no es texto. Se dice QUÉ llegó: contestarle «escribime el
    # pedido» a alguien que mandó su ubicación, o «no puedo ver fotos» a un
    # audio, es la clase de respuesta que sólo puede haber escrito un programa.
    # Un almacén argentino manda audios todo el día, así que ésta es una de las
    # respuestas más leídas del sistema. Transcribirlos todavía no está hecho
    # (ver «Not done yet» en el README): esto es lo que se dice mientras no lo
    # esté, y por eso dice «todavía».
    "ack.solo_texto": {
        ES: "Uy, eso no lo puedo abrir. ¿Me lo escribís? Aunque sea cortito.",
        EN: "Sorry, I can't open that. Could you type it? Even a short line.",
    },
    "ack.audio": {
        ES: (
            "Uy, los audios todavía no los puedo escuchar. ¿Me lo escribís? "
            "Aunque sea cortito."
        ),
        EN: (
            "Sorry, I can't listen to voice notes yet. Could you type it? Even a "
            "short line."
        ),
    },
    "ack.imagen": {
        ES: "Uy, las fotos todavía no las puedo ver. ¿Me lo escribís?",
        EN: "Sorry, I can't see photos yet. Could you type it?",
    },
    "ack.video": {
        ES: "Uy, los videos todavía no los puedo ver. ¿Me lo escribís?",
        EN: "Sorry, I can't watch videos yet. Could you type it?",
    },
    "ack.archivo": {
        ES: "Uy, los archivos todavía no los puedo abrir. ¿Me lo escribís?",
        EN: "Sorry, I can't open files yet. Could you type it?",
    },
    "ack.ubicacion": {
        ES: (
            "Uy, la ubicación no la puedo abrir. Si es para la entrega, pasame la "
            "calle y el número."
        ),
        EN: (
            "Sorry, I can't open a dropped pin. If it's for the delivery, send me "
            "the street and number."
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
    # Sin saludo: estos llegan cuando ya se estuvo hablando, y el saludo va una
    # sola vez por conversación. «Hola!» en la mitad de una charla es lo que
    # delata que del otro lado hay un programa que no leyó lo anterior.
    "pedido.rechazado": {
        ES: (
            "Sobre tu pedido {pedido}: no vamos a poder cumplirlo{motivo}. "
            "En breve te escribe alguien del equipo. Perdón por la molestia."
        ),
        EN: (
            "About your order {pedido}: we won't be able to fulfil it{motivo}. "
            "Someone from our team will message you shortly. Sorry about that."
        ),
    },
    "pedido.cancelado": {
        ES: (
            "Tu pedido {pedido} quedó cancelado ({motivo}). Si fue un error, "
            "escribinos y lo revisamos."
        ),
        EN: (
            "Your order {pedido} has been cancelled ({motivo}). If this is a "
            "mistake, message us and we will sort it out."
        ),
    },
    "pedido.sin_confirmar": {
        ES: "Tu pedido {pedido} sigue sin confirmar.",
        EN: "Your order {pedido} is still unconfirmed.",
    },
    # El recordatorio de app/pendientes.py, cuando pasaron las horas que fijó
    # el dueño y nadie decidió nada. NO lleva la edad del pedido: al cliente no
    # le sirve saber que hace cinco horas que espera, le sirve saber que no nos
    # olvidamos. Sin día y sin hora, porque no hay nada confirmado que prometer.
    "pedido.recordatorio": {
        ES: (
            "Sobre tu pedido {pedido}: todavía no te lo pude confirmar. "
            "Apenas lo vea el encargado te aviso."
        ),
        EN: (
            "About your order {pedido}: I haven't been able to confirm it yet. "
            "As soon as someone on the team looks at it, I'll let you know."
        ),
    },
    # Y cuando venció el plazo de cierre: se dice sin vueltas, porque el cliente
    # tiene que poder ir a comprarlo a otro lado en vez de seguir esperando.
    "pedido.cerrado_sin_confirmar": {
        ES: (
            "Sobre tu pedido {pedido}: no llegamos a confirmarlo, así que no "
            "queda nada agendado a tu nombre. Perdón. Si lo seguís necesitando, "
            "escribime y lo armamos de nuevo con lo que haya hoy."
        ),
        EN: (
            "About your order {pedido}: we didn't manage to confirm it, so "
            "nothing is booked in your name. Sorry about that. If you still "
            "need it, message me and we'll put it together again with what we "
            "have today."
        ),
    },
    # --------------------------------------- la oferta de entrega, del lado del cliente
    # Estos los manda Python cuando el cliente contesta «acepto» / «no acepto»:
    # se resuelven ANTES de que ningún modelo vea el mensaje (app/main.py), así
    # que son texto de acá y no del modelo. Tres de ellos salían en los dos
    # idiomas pegados con un salto de línea —el mismo parche que este catálogo
    # existe para no tener— y el resto sólo en español, así que un cliente que
    # había pedido inglés escribía «accept» y recibía español.
    "terminos.sin_cambios": {ES: "sin cambios", EN: "no changes"},
    "terminos.retiro": {ES: "retiro en el local", EN: "pickup at the shop"},
    "terminos.a_las": {ES: "a las {hora}", EN: "at {hora}"},
    "terminos.cargo": {ES: "cargo de envío {monto}", EN: "delivery fee {monto}"},
    "terminos.descuento": {ES: "descuento {pct}%", EN: "{pct}% discount"},
    "oferta.no_hay_tuya": {
        ES: "No encontré una oferta tuya pendiente.",
        EN: "I don't have an open offer for you.",
    },
    "oferta.no_registre": {
        ES: "No me quedó registrada tu respuesta. Escribime de nuevo en un momento.",
        EN: "Your reply did not get saved. Message me again in a moment.",
    },
    "oferta.procesando": {
        ES: "Justo estoy con algo de este pedido. Escribime en un minuto.",
        EN: "I'm on something for that order right now. Message me in a minute.",
    },
    "oferta.rechazada": {
        ES: (
            "Listo, no avanzo con {pedido}. Si querés, lo dejamos para un día de "
            "reparto normal."
        ),
        EN: (
            "Alright, I won't go ahead with {pedido}. If you like, we can leave it "
            "for a normal delivery day."
        ),
    },
    "oferta.sin_pendiente": {
        ES: "No tengo una oferta pendiente para {pedido}.",
        EN: "I don't have an open offer for {pedido}.",
    },
    "oferta.esperando_encargado": {
        ES: (
            "Todavía no tengo la respuesta del encargado sobre {pedido}. Te "
            "escribo en cuanto la tenga."
        ),
        EN: (
            "I still don't have the manager's answer on {pedido}. I'll write as "
            "soon as I do."
        ),
    },
    "oferta.ya_confirmado": {
        ES: "{pedido} ya quedó confirmado con lo que acordamos.",
        EN: "{pedido} is already closed on what we agreed.",
    },
    "oferta.en_revision": {
        ES: (
            "Sobre {pedido} está mirándolo una persona antes de cerrarlo. Te "
            "contestamos en cuanto lo revise."
        ),
        EN: (
            "Someone is looking at {pedido} before we close it. We'll get back to "
            "you as soon as they do."
        ),
    },
    "oferta.cerrada": {
        ES: (
            "Sobre {pedido} ya no tengo nada pendiente para cerrar. Si lo querés "
            "igual, escribime y lo armamos con el stock del momento."
        ),
        EN: (
            "There's nothing left open on {pedido}. If you still want it, message "
            "me and we'll put it together with what's in stock."
        ),
    },
    "oferta.nada_pendiente": {
        ES: (
            "Sobre {pedido} no me quedó nada pendiente de tu parte. Si necesitás "
            "algo más, decime."
        ),
        EN: (
            "There's nothing waiting on you for {pedido}. If you need anything "
            "else, tell me."
        ),
    },
    "oferta.tarde": {
        ES: (
            "Pasó el plazo de la oferta de {pedido}, así que no la puedo cerrar. "
            "Escribime y lo vemos de nuevo con el stock de ahora."
        ),
        EN: (
            "That offer on {pedido} has run out, so I can't close it. Message me "
            "and we'll look at it again with what's in stock now."
        ),
    },
    "oferta.no_verificable": {
        ES: "Ahora no lo pude mirar. Escribime en un rato y lo vemos.",
        EN: "I couldn't look at it just now. Message me shortly and we'll sort it.",
    },
    "oferta.confirmado_por_encargado": {
        ES: (
            "El encargado ya confirmó {pedido} por su cuenta, así que no hay nada "
            "más que cerrar de tu lado. Cualquier duda, escribime."
        ),
        EN: (
            "The manager already closed {pedido} himself, so there's nothing left "
            "on your side. Any questions, message me."
        ),
    },
    "oferta.cancelado_por_encargado": {
        ES: (
            "{pedido} fue cancelado por el encargado. Si lo querés igual, "
            "escribime y lo armamos de nuevo con el stock de ahora."
        ),
        EN: (
            "The manager cancelled {pedido}. If you still want it, message me and "
            "we'll put it together again with what's in stock now."
        ),
    },
    "oferta.aceptada": {
        ES: (
            "¡Listo! {pedido} quedó confirmado con lo que acordamos: {terminos}. "
            "Te mando el detalle enseguida."
        ),
        EN: (
            "Done! {pedido} is confirmed on what we agreed: {terminos}. I'll send "
            "you the details right away."
        ),
    },
    "oferta.a_revision": {
        ES: (
            "Gracias por confirmar. Sobre {pedido} necesito revisarlo con una "
            "persona antes de cerrarlo: cambió algo desde la oferta. Te "
            "contestamos a la brevedad."
        ),
        EN: (
            "Thanks for confirming. Someone has to look at {pedido} with me before "
            "we close it: something moved since the offer. We'll get back to you "
            "shortly."
        ),
    },
    "oferta.revision_sin_registro": {
        ES: (
            "Gracias por confirmar. Sobre {pedido} se me complicó algo y necesito "
            "que lo vea una persona. No queda nada confirmado a tu nombre; te "
            "contestamos a la brevedad."
        ),
        EN: (
            "Thanks for confirming. Something went wrong with {pedido} on my side "
            "and a person has to look at it. Nothing is closed in your name; we'll "
            "get back to you shortly."
        ),
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
    # Los borradores que esperan una decisión (app/pendientes.py). Van al dueño
    # una vez por día, no una vez por ronda de 60 s. Acá sí va la edad: es
    # exactamente el dato con el que él decide a cuál atender primero.
    "gerencia.pendientes_asunto": {
        ES: "🟡 {cuantos} pedido(s) esperando tu confirmación",
        EN: "🟡 {cuantos} order(s) waiting for you to confirm",
    },
    "gerencia.pendientes_cuerpo": {
        ES: (
            "Hace rato que estos {cuantos} esperan y ya le avisé al cliente que "
            "todavía no está confirmado:\n{lineas}\n"
            "Respondé «confirmar <pedido>», «rechazar <pedido>» o «ver <pedido>»."
        ),
        EN: (
            "These {cuantos} have been waiting a while and I have told the "
            "customer it is not confirmed yet:\n{lineas}\n"
            "Reply «confirmar <order>», «rechazar <order>» or «ver <order>»."
        ),
    },
    "gerencia.pendiente_cerrado": {
        ES: (
            "🔒 {pedido}: pasaron {horas} h sin decisión, así que lo cerré para "
            "que deje de reservar stock y le avisé al cliente que no se "
            "confirmó. Si todavía se puede, hay que rehacerlo con lo de hoy."
        ),
        EN: (
            "🔒 {pedido}: {horas} h went by with no decision, so I closed it to "
            "stop it reserving stock and told the customer it was not "
            "confirmed. If it is still doable, it has to be redone with today's."
        ),
    },
    # El resumen de autonomía (app/autonomia.py). Cada número puede ser «no
    # pude leer»: informar una autonomía de cero que nadie midió es peor, porque
    # con eso el dueño baja un límite que no hacía falta bajar.
    "gerencia.autonomia_ilegible": {ES: "no pude leer", EN: "could not read"},
    "gerencia.autonomia_truncado": {
        ES: (
            "⚠️ Hubo más movimiento del que pude leer de una vez: estos números "
            "son un piso, no el total. Pedí menos días para verlos completos."
        ),
        EN: (
            "⚠️ There was more activity than I could read in one go: these "
            "numbers are a floor, not the total. Ask for fewer days to see "
            "them in full."
        ),
    },
    # El desglose de frenos se arma sumando DOS fuentes (los registros de
    # sombra y la prosa de las revisiones humanas). Si una no se pudo leer, lo
    # que queda es real pero incompleto, y un desglose corto se lee como
    # «quedan menos frenos de los que creía» — la dirección que hace subir un
    # límite sobre evidencia que no está. Necesita su propia frase: reusar
    # «no pude leer» dejaría al dueño leyendo un desglose sin saber que le
    # falta la mitad.
    "gerencia.autonomia_frenos_incompletos": {
        ES: (
            "⚠️ No pude leer todas las fuentes de frenos, así que el desglose "
            "de «frenados por reglas» está incompleto: puede haber frenos que "
            "no figuran ahí."
        ),
        EN: (
            "⚠️ I could not read every source of blockers, so the \"held back "
            "by rules\" breakdown is incomplete: there may be blockers it "
            "does not list."
        ),
    },
    # Y el desglose tiene además un techo de LEGIBILIDAD: entran las cubetas
    # más grandes y el resto se resume. Las que se caen son siempre las más
    # chicas, así que el error está acotado — pero informar menos frenos de
    # los que hay es la misma dirección peligrosa, así que se dice cuántas
    # quedaron afuera en vez de cortar en silencio.
    # Sin plural en la frase a propósito: «y 1 más» y «y 3 más» necesitarían
    # dos filas por idioma para no quedar mal escritas, y esto va en el medio
    # de una línea que ya es densa.
    "gerencia.autonomia_grupos_mas": {
        ES: "+{cuantos} sin mostrar",
        EN: "+{cuantos} not shown",
    },
    # El techo de la cuenta de borradores vivos es OTRO: no sale de la ventana
    # de días, así que «pedí menos días» no lo arregla y mandar ahí al dueño
    # sería mandarlo a hacer algo que no cambia el número.
    "gerencia.autonomia_borradores_truncado": {
        ES: (
            "⚠️ Hay más borradores vivos de los que pude contar de una vez: "
            "ese número es un piso, no el total."
        ),
        EN: (
            "⚠️ There are more live drafts than I could count in one go: that "
            "number is a floor, not the total."
        ),
    },
    "gerencia.autonomia": {
        ES: (
            "📈 Autonomía · últimos {dias} días\n"
            "confirmados: {confirmados} · solos: {solos} · por vos: {por_vos} "
            "· los aceptó el cliente: {acepto} · rechazados: {rechazados}\n"
            "sombra: {sombra_pasan} habrían pasado todas las reglas, "
            "{sombra_frenados} no\n"
            "frenados sólo por la postura que elegiste: {postura}\n"
            "frenados por reglas: {frenos}\n"
            "borradores compitiendo por stock: {borradores} de {tope}\n"
            "conteos frescos: {conteos} · faltó: {falto}"
        ),
        EN: (
            "📈 Autonomy · last {dias} days\n"
            "confirmed: {confirmados} · on their own: {solos} · by you: "
            "{por_vos} · accepted by the customer: {acepto} · rejected: "
            "{rechazados}\n"
            "shadow: {sombra_pasan} would have passed every rule, "
            "{sombra_frenados} would not\n"
            "held back only by the posture you chose: {postura}\n"
            "held back by rules: {frenos}\n"
            "drafts competing for stock: {borradores} of {tope}\n"
            "fresh counts: {conteos} · missing: {falto}"
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
    # Un mensaje que terminó en disculpa técnica. Quien escribió puede ser un
    # cliente o el propio equipo, así que dice «de quién» y no «cliente». Lo que
    # escribió va CITADO (solicitudes.citar): es un dato para leer, nunca una
    # instrucción, ni siquiera después de que una persona lo reenvíe.
    "gerencia.falla_asunto": {
        ES: "⚠️ Falló un mensaje de WhatsApp",
        EN: "⚠️ A WhatsApp message failed",
    },
    "gerencia.falla_cuerpo": {
        ES: (
            "De: {telefono}\nMensaje:\n{mensaje}\nError: {error}\n"
            "Quien escribió recibió una disculpa automática; nadie le respondió todavía."
        ),
        EN: (
            "From: {telefono}\nMessage:\n{mensaje}\nError: {error}\n"
            "The sender got an automatic apology; nobody has answered them yet."
        ),
    },
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
    # Sin «respondé con el botón»: al cliente la oferta le llega como texto o
    # como plantilla (app/avisos.py), nunca con botones — los botones son del
    # aviso al EQUIPO (app/notificar.py). Le decía que apretara algo que no
    # estaba ahí.
    "entrega.oferta": {
        ES: (
            "Sobre tu pedido {pedido}: el encargado te ofrece {terminos}.\n"
            "¿Lo tomás? Contestame 'acepto {pedido}' o 'no acepto {pedido}'. "
            "Sin tu respuesta no cierro nada."
        ),
        EN: (
            "About your order {pedido}: the manager offers {terminos}.\n"
            "Do you take it? Write 'accept {pedido}' or 'reject {pedido}'. "
            "Nothing is closed without your reply."
        ),
    },
    "entrega.solicitud_rechazada": {
        ES: (
            "Sobre tu pedido {pedido}: no vamos a poder hacer lo que pediste"
            "{motivo}. Si querés, lo dejamos para un día de reparto normal."
        ),
        EN: (
            "About your order {pedido}: we cannot do what you asked{motivo}. "
            "We can schedule it for a normal delivery day instead."
        ),
    },
    "entrega.vencida": {
        ES: (
            "Sobre tu pedido {pedido}: no llegué a tener una respuesta del "
            "encargado, así que por ahora no queda confirmado. Escribime y lo "
            "volvemos a ver con el stock del momento."
        ),
        EN: (
            "About your order {pedido}: I did not get an answer in time, so it "
            "is not confirmed. Message me and we will look at it again with "
            "current stock."
        ),
    },
    "entrega.respaldo_retiro": {
        ES: "podés pasar a buscarlo por el local",
        EN: "you can pick it up at the shop",
    },
    "entrega.respaldo_reparto": {
        ES: "te lo puedo llevar en el próximo reparto normal",
        EN: "I can bring it on the next normal delivery round",
    },
    "entrega.respaldo": {
        ES: (
            "Sobre tu pedido {pedido}: no llegué a tener la respuesta del "
            "encargado sobre lo que pediste, así que eso queda sin efecto. "
            "Perdón por la espera.\n"
            "Lo que sí {puede}: {terminos}.\n"
            "¿Lo tomás? Respondé 'acepto {pedido}' o 'no acepto {pedido}'. Sin "
            "tu respuesta no cierro nada, y cuando aceptes vuelvo a chequear el "
            "stock antes de confirmarlo."
        ),
        EN: (
            "About your order {pedido}: I did not get the manager's answer about "
            "what you asked for, so that is off. Sorry for the wait.\n"
            "What I can do: {puede} — {terminos}.\n"
            "Do you take it? Reply 'accept {pedido}' or 'reject {pedido}'. "
            "Nothing is closed without your reply, and I re-check stock before "
            "confirming."
        ),
    },
    "entrega.revision_vencida": {
        ES: (
            "Sobre tu pedido {pedido}: te había dicho que lo revisaba una "
            "persona y no llegamos a hacerlo, así que no lo dejo agendado y no "
            "queda nada a tu nombre. Perdón. Cuando quieras lo armamos de nuevo "
            "con el stock del momento."
        ),
        EN: (
            "About your order {pedido}: I said a person would review it and we "
            "did not get to it, so it is not scheduled and nothing is charged. "
            "Sorry. Message me and we will put it together again with current "
            "stock."
        ),
    },
    "entrega.respaldo_vencido": {
        ES: (
            "Sobre tu pedido {pedido}: se venció el plazo de esa opción "
            "({terminos}), así que no queda agendada y no hay nada confirmado a "
            "tu nombre. Cuando quieras, escribime y lo armamos con el stock del "
            "momento."
        ),
        EN: (
            "About your order {pedido}: that option has run out, so it is not "
            "scheduled and nothing is confirmed in your name ({terminos}). "
            "Message me whenever you like and we will put it together with "
            "current stock."
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
    "codigo.ajuste_no_preparado": {
        ES: "No cambié nada: {motivo}.",
        EN: "I changed nothing: {motivo}.",
    },
    "codigo.ajuste_error": {
        ES: "No pude aplicar el cambio en este momento. No cambié nada.",
        EN: "I couldn't apply the change right now. Nothing was changed.",
    },
    "codigo.ajuste_sin_codigo": {
        ES: (
            "Preparé el cambio ({cambio}) pero NO pude mandarte el código de "
            "confirmación, así que lo descarté. No cambié nada. Probá de nuevo."
        ),
        EN: (
            "I prepared the change ({cambio}) but could NOT send you the "
            "confirmation code, so I discarded it. Nothing was changed. Try again."
        ),
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
    "codigo.ya_confirmado": {
        ES: "Ese cambio ya se confirmó.",
        EN: "That change was already confirmed.",
    },
    "codigo.pendiente_no_legible": {
        ES: "No pude leer el cambio pendiente.",
        EN: "I couldn't read the pending change.",
    },
    "codigo.pendiente_ilegible": {
        ES: "El cambio pendiente quedó ilegible.",
        EN: "The pending change is unreadable.",
    },
    "codigo.ajuste_inexistente": {
        ES: "El cambio pendiente apunta a un ajuste que no existe.",
        EN: "The pending change points at a setting that doesn't exist.",
    },
    # ------------------------------------------------ por qué no se pudo
    # EL MOTIVO DE UN LÍMITE QUE NO SE PUDO CAMBIAR, y por qué son claves y no
    # el texto de la excepción.
    #
    # `app/ajustes.py` y `app/main.py` meten este motivo adentro de un mensaje
    # que ya sale traducido: «I changed nothing: {motivo}». Con el motivo en
    # español, el dueño que lee en inglés recibía media frase en cada idioma —
    # «I changed nothing: «monto maximo» no es un número: 'abc'.» La sección 9
    # del allowlist permitía excepciones en español SÓLO mientras no las leyera
    # una persona, y nombraba el arreglo: «hay que darle una clave del
    # catálogo; LimiteError ya soporta `clave` justamente para eso».
    #
    # LO QUE VA ENTRE «» ES UN DATO Y NO SE TRADUCE: el alias del ajuste es lo
    # que el dueño teclea («monto maximo»), o sea un comando, y lo que él
    # escribió se cita tal cual. Igual que el número de pedido o el código.
    "limite.no_pude_leer": {
        ES: "no pude leer los límites configurados",
        EN: "I couldn't read the configured limits",
    },
    "limite.perdidos": {
        ES: (
            "los límites que configuró el dueño no están en el almacén, y "
            "ERPNext tiene cambios registrados: hay que restaurarlos antes de "
            "que algo se confirme solo"
        ),
        EN: (
            "the limits the owner configured are not in the store, and ERPNext "
            "has changes on record: they have to be restored before anything "
            "confirms on its own"
        ),
    },
    "limite.no_es_numero": {
        ES: "«{ajuste}» no es un número: {valor}",
        EN: "«{ajuste}» is not a number: {valor}",
    },
    "limite.no_es_numero_usable": {
        ES: "«{ajuste}» no es un número usable: {valor}",
        EN: "«{ajuste}» is not a usable number: {valor}",
    },
    "limite.minimo": {
        ES: "«{ajuste}» no puede ser menor que {minimo}",
        EN: "«{ajuste}» cannot be lower than {minimo}",
    },
    "limite.maximo": {
        ES: "«{ajuste}» {valor} es imposible: el máximo es {maximo}",
        EN: "«{ajuste}» {valor} is impossible: the maximum is {maximo}",
    },
    "limite.si_o_no": {
        ES: "«{ajuste}» tiene que ser sí o no, no {valor}",
        EN: "«{ajuste}» has to be yes or no, not {valor}",
    },
    "limite.dias_vacio": {
        ES: "«{ajuste}» está vacío: decime qué días",
        EN: "«{ajuste}» is empty: tell me which days",
    },
    # Cada idioma nombra las formas que DE VERDAD parsean en él. Las dos listas
    # las acepta `limites._dias` desde el PR de inglés visible, y
    # `test_limites.py` cruza esta lista contra el parser para que no se
    # separen: un error que enumera días que no se pueden teclear es peor que
    # no enumerar ninguno.
    "limite.dia_desconocido": {
        ES: (
            "«{valor}» no es un día de la semana. Van así: lunes, martes, "
            "miercoles, jueves, viernes, sabado, domingo"
        ),
        EN: (
            "«{valor}» is not a weekday. They go like this: monday, tuesday, "
            "wednesday, thursday, friday, saturday, sunday"
        ),
    },
    "limite.localidades_vacio": {
        ES: "«{ajuste}» está vacío: decime en qué localidades repartís",
        EN: "«{ajuste}» is empty: tell me which towns you deliver to",
    },
    "limite.no_es_localidad": {
        ES: "«{valor}» no es una localidad: no tiene ni una letra ni un número",
        EN: "«{valor}» is not a town: it has neither a letter nor a digit",
    },
    "limite.cp_vacio": {
        ES: "«{ajuste}» está vacío: decime qué códigos postales",
        EN: "«{ajuste}» is empty: tell me which postcodes",
    },
    "limite.no_es_cp": {
        ES: "«{valor}» no es un código postal",
        EN: "«{valor}» is not a postcode",
    },
    "limite.hora_invalida": {
        ES: "«{ajuste}» tiene que ser una hora tipo 08:00, no {valor}",
        EN: "«{ajuste}» has to be a time like 08:00, not {valor}",
    },
    "limite.idioma_invalido": {
        ES: "«{ajuste}» sólo puede ser español o inglés, no {valor}",
        EN: "«{ajuste}» can only be Spanish or English, not {valor}",
    },
    "limite.cual": {
        ES: "no me dijiste qué límite",
        EN: "you didn't tell me which limit",
    },
    "limite.ambiguo": {
        ES: "«{valor}» puede ser varias cosas: {opciones}. Decime cuál",
        EN: "«{valor}» could be several things: {opciones}. Tell me which one",
    },
    # `{conocidos}` es la lista de alias, y queda en español en los dos idiomas
    # a propósito: son los comandos que el dueño teclea (sección 4 del
    # allowlist), no prosa. Los alias en inglés son otro trabajo.
    "limite.ajuste_desconocido": {
        ES: "no conozco el ajuste «{valor}». Hay: {conocidos}",
        EN: "I don't know the setting «{valor}». There are: {conocidos}",
    },
    "limite.sin_quien_pide": {
        ES: "no sé quién pide el cambio",
        EN: "I don't know who is asking for the change",
    },
    "limite.sin_quien_confirma": {
        ES: "no sé quién confirma el cambio",
        EN: "I don't know who is confirming the change",
    },
    "limite.no_registre_propuesta": {
        ES: "no pude registrar el cambio para confirmarlo",
        EN: "I couldn't record the change to confirm it",
    },
    "limite.no_pude_guardar": {
        ES: "no pude guardar el cambio",
        EN: "I couldn't save the change",
    },
    "limite.no_registre_en_erpnext": {
        ES: "no pude registrar el cambio en ERPNext, así que no lo apliqué",
        EN: "I couldn't record the change in ERPNext, so I didn't apply it",
    },
    "limite.no_pude_leer_historial": {
        ES: "no pude leer el historial de cambios",
        EN: "I couldn't read the change history",
    },
    # ------------------------------------------------ estado del sistema
    # El informe de estado. Los NOMBRES de los componentes (Redis, ERPNext,
    # WhatsApp) son propios y no se traducen; sí la prosa alrededor.
    "sistema.titulo": {ES: "Estado del sistema:", EN: "System status:"},
    "sistema.responde": {ES: "responde", EN: "responding"},
    "sistema.no_disponible": {ES: "NO DISPONIBLE", EN: "UNAVAILABLE"},
    "sistema.desconocido": {ES: "DESCONOCIDO", EN: "UNKNOWN"},
    "sistema.componente_ok": {
        ES: "· {componente}: {estado}",
        EN: "· {componente}: {estado}",
    },
    "sistema.componente_caido": {
        ES: "· {componente}: {estado} ({error})",
        EN: "· {componente}: {estado} ({error})",
    },
    "sistema.cargada": {ES: "cargada ({n} caracteres)", EN: "loaded ({n} characters)"},
    "sistema.vacia": {ES: "VACÍA", EN: "EMPTY"},
    "sistema.clave_vacia": {ES: "clave VACÍA", EN: "key EMPTY"},
    "sistema.modelos": {
        ES: (
            "· Modelos: proveedor {proveedor} — ventas {ventas}, gerencia "
            "{gerencia}; {estado}"
        ),
        EN: (
            "· Models: provider {proveedor} — sales {ventas}, management "
            "{gerencia}; {estado}"
        ),
    },
    "sistema.whatsapp": {
        ES: (
            "· WhatsApp: número {numero}, token {token}; {rechazadas} entrega(s) "
            "que Meta rechazó, {sin_entregar} respuesta(s) a clientes sin entregar"
        ),
        EN: (
            "· WhatsApp: number {numero}, token {token}; {rechazadas} delivery(ies) "
            "Meta rejected, {sin_entregar} customer reply(ies) undelivered"
        ),
    },
    "sistema.colas": {
        ES: "· Cola de avisos al cliente: {espera} en espera, {caidos} caído(s)",
        EN: "· Customer notice queue: {espera} waiting, {caidos} failed",
    },
    "sistema.avisos_fallidos_lista": {
        ES: "Avisos que no salieron ({cuantos}):",
        EN: "Notifications that did not go out ({cuantos}):",
    },
    "sistema.sin_avisos_fallidos": {
        ES: "No hay avisos fallidos.",
        EN: "There are no failed notifications.",
    },
    "sistema.indice_pendiente": {
        ES: "reconstrucción PENDIENTE",
        EN: "rebuild PENDING",
    },
    "sistema.indice_completo": {ES: "índice completo", EN: "index complete"},
    "sistema.indice_desconocido": {
        ES: "índice {estado} ({error})",
        EN: "index {estado} ({error})",
    },
    "sistema.decisiones": {
        ES: "· Decisiones: {indice}; borradores trabados {trabadas}{extra}",
        EN: "· Decisions: {indice}; stuck drafts {trabadas}{extra}",
    },
    "sistema.decisiones_reservan": {
        ES: " (siguen reservando stock; hay un ToDo por cada uno)",
        EN: " (they still reserve stock; there is a ToDo for each)",
    },
    "sistema.fallidos_titulo": {
        ES: "Comunicación que no llegó:",
        EN: "Communication that did not arrive:",
    },
    "sistema.fallidos_avisos": {
        ES: "· Avisos sin entregar: {n}",
        EN: "· Undelivered notifications: {n}",
    },
    "sistema.fallidos_respuestas": {
        ES: (
            "· Respuestas a clientes sin entregar: {n} (no se listan: cada una "
            "lleva el mensaje del cliente)"
        ),
        EN: (
            "· Undelivered customer replies: {n} (not listed: each one carries "
            "the customer's own message)"
        ),
    },
    "sistema.fallidos_rechazadas": {
        ES: "· Entregas que Meta rechazó: {n}",
        EN: "· Deliveries Meta rejected: {n}",
    },
    "sistema.fallidos_ultimos": {
        ES: "\nÚltimos {n} aviso(s) caído(s), del más nuevo:",
        EN: "\nLast {n} failed notification(s), newest first:",
    },
    "sistema.fallidos_ninguno": {
        ES: "\nNo hay avisos caídos registrados.",
        EN: "\nThere are no failed notifications on record.",
    },
    "sistema.fallidos_pie": {
        ES: (
            "\nCada uno tiene una tarea en ERPNext para contactarlo a mano. "
            "Desde acá no se reintenta nada."
        ),
        EN: (
            "\nEach one has an ERPNext task to contact them by hand. Nothing is "
            "retried from here."
        ),
    },
    "sistema.ilegibles": {
        ES: "{n} entrada(s) ilegible(s) omitida(s)",
        EN: "{n} unreadable entry(ies) skipped",
    },
    "sistema.lista_no_disponible": {
        ES: "la lista de avisos caídos está {estado} ({error})",
        EN: "the failed-notification list is {estado} ({error})",
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
    # El conteo físico que el dueño manda por WhatsApp. Va en DOS filas
    # enteras y no en una con un fragmento «faltan»/«sobran» pegado: en
    # inglés la diferencia va del otro lado del número («2 short», no
    # «short 2»), y armar la oración con pedazos es cómo se produce el
    # español a medio traducir que este catálogo existe para no tener.
    "stock.conteo_faltan": {
        ES: (
            "Conteo de {producto} ({ajuste}): el sistema decía {sistema}, "
            "vos contaste {contado} — faltan {diferencia}."
        ),
        EN: (
            "Count of {producto} ({ajuste}): the system said {sistema}, "
            "you counted {contado} — {diferencia} short."
        ),
    },
    "stock.conteo_sobran": {
        ES: (
            "Conteo de {producto} ({ajuste}): el sistema decía {sistema}, "
            "vos contaste {contado} — sobran {diferencia}."
        ),
        EN: (
            "Count of {producto} ({ajuste}): the system said {sistema}, "
            "you counted {contado} — {diferencia} over."
        ),
    },
    # El cuerpo del botón. Éste es el que le llega al dueño por WhatsApp, así
    # que es el que la auditoría estática marcaba.
    "stock.conteo_confirmar": {
        ES: "¿Confirmo el ajuste?",
        EN: "Shall I apply the adjustment?",
    },
    # Las dos respuestas al modelo. El NOMBRE del botón se INTERPOLA, no se
    # escribe: lo que va acá tiene que ser la etiqueta que el dueño ve en la
    # pantalla, y ahora esa etiqueta la decide `boton.confirmar_conteo`. Escrita
    # a mano, el día que se tradujo el botón esto lo habría mandado a buscar uno
    # que no existe — que era exactamente el riesgo que el texto viejo nombraba,
    # con la etiqueta en español dentro de la versión en inglés.
    "stock.conteo_boton_enviado": {
        ES: (
            "{resumen} Le mandé el botón *{boton}*. Hasta que lo "
            "toque, el conteo es un borrador y el bot no promete stock de "
            "{producto}."
        ),
        EN: (
            "{resumen} I sent them the *{boton}* button. Until they "
            "tap it the count is a draft and the bot promises no stock of "
            "{producto}."
        ),
    },
    "stock.conteo_sin_boton": {
        ES: (
            "{resumen} No pude mandarle el botón de confirmación: tiene que "
            "confirmar {ajuste} en ERPNext. Hasta entonces el bot no promete "
            "stock de {producto}."
        ),
        EN: (
            "{resumen} I could not send them the confirmation button: they "
            "have to confirm {ajuste} in ERPNext. Until then the bot promises "
            "no stock of {producto}."
        ),
    },
    "precio.a_confirmar": {
        ES: "precio a confirmar",
        EN: "price to be confirmed",
    },
    # ------------------------------------------------ los días de la semana
    # EL VALOR GUARDADO ES EL ESPAÑOL, siempre: "lunes,viernes" es lo que hay en
    # el almacén de cada despliegue, lo que parsea app/excepciones.py y lo que
    # escribió la auditoría durable. Estas claves son SÓLO para mostrarlo —
    # `limites.mostrar()`—, nunca para guardarlo. Un día traducido que volviera
    # al almacén dejaría de matchear, que es la mitad de la regla que hace que
    # aceptar «monday» no cueste una migración.
    "dia.lunes": {ES: "lunes", EN: "Monday"},
    "dia.martes": {ES: "martes", EN: "Tuesday"},
    "dia.miercoles": {ES: "miércoles", EN: "Wednesday"},
    "dia.jueves": {ES: "jueves", EN: "Thursday"},
    "dia.viernes": {ES: "viernes", EN: "Friday"},
    "dia.sabado": {ES: "sábado", EN: "Saturday"},
    "dia.domingo": {ES: "domingo", EN: "Sunday"},
    # ---------------------------------------------- las etiquetas de botones
    # LO ÚNICO QUE LEE UNA PERSONA SIN HABER ESCRITO NADA. El aviso de un pedido
    # pendiente se lo manda el bot al dueño solo, y hasta este PR el cuerpo salía
    # en su idioma y los botones en español: la única pantalla del producto donde
    # él no puede haber elegido el idioma con lo que tecleó era justo la que no lo
    # respetaba.
    #
    # CORTAS A PROPÓSITO. `whatsapp.enviar_botones` trunca el título en 20
    # caracteres SIN AVISAR, así que una etiqueta larga no falla: llega cortada a
    # la pantalla del dueño. «View details» y «Confirm count» entran con lugar de
    # sobra, y `tests/test_idioma_salida.py` afirma el largo para que una
    # traducción futura no lo descubra en vivo.
    "boton.confirmar": {
        ES: "Confirmar",
        EN: "Confirm",
    },
    "boton.ver_detalle": {
        ES: "Ver detalle",
        EN: "View details",
    },
    "boton.confirmar_conteo": {
        ES: "Confirmar conteo",
        EN: "Confirm count",
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
# Una frase pedida EN el idioma que pide es segura de guardar: quien escribe en
# inglés y pide inglés ya iba a recibir inglés por espejo. Al revés no: «¿hablás
# inglés?» escrito en español es una pregunta por lo que sabemos hacer, no un
# pedido de cambiar el idioma de la atención, y guardarla un año dejaría a un
# cliente que escribe en español recibiendo inglés. Por eso los pedidos escritos
# en el OTRO idioma sólo entran acá en forma imperativa («hablame en inglés»).
_PEDIDOS_EXPLICITOS = (
    ("reply in english", EN),
    ("answer in english", EN),
    ("respond in english", EN),
    ("in english please", EN),
    ("english please", EN),
    ("speak english", EN),
    ("speak in english", EN),
    ("talk in english", EN),
    ("talk english", EN),
    ("talk to me in english", EN),
    ("write in english", EN),
    ("write to me in english", EN),
    ("answer me in english", EN),
    ("reply to me in english", EN),
    ("message me in english", EN),
    ("switch to english", EN),
    ("prefer english", EN),
    ("hablame en ingles", EN),
    ("hablar en ingles", EN),
    ("escribime en ingles", EN),
    ("contestame en ingles", EN),
    ("contesta en ingles", EN),
    ("responde en ingles", EN),
    ("respondeme en ingles", EN),
    ("en ingles por favor", EN),
    ("respondé en español", ES),
    ("responde en espanol", ES),
    ("respondeme en espanol", ES),
    ("contestame en espanol", ES),
    ("contesta en espanol", ES),
    ("hablame en espanol", ES),
    ("hablar en espanol", ES),
    ("escribime en espanol", ES),
    ("reply in spanish", ES),
    ("answer in spanish", ES),
    ("respond in spanish", ES),
    ("in spanish please", ES),
    ("spanish please", ES),
    ("speak spanish", ES),
    ("speak in spanish", ES),
    ("talk in spanish", ES),
    ("talk spanish", ES),
    ("talk to me in spanish", ES),
    ("write in spanish", ES),
    ("write to me in spanish", ES),
    ("answer me in spanish", ES),
    ("reply to me in spanish", ES),
    ("message me in spanish", ES),
    ("switch to spanish", ES),
    ("prefer spanish", ES),
    ("en espanol por favor", ES),
)


# Lo que convierte una frase de pedido en NO-pedido cuando está justo antes,
# en la misma cláusula: una negación («don't reply in english», «no, en inglés
# por favor no»), o un verbo que la cita en vez de pedirla («you said 'reply in
# english'», «el cartel decía respondé en español»). Palabras sueltas y
# frecuentes, sin tildes, en los dos idiomas.
_NEGACIONES = frozenset(
    ["no", "not", "dont", "don't", "never", "nunca", "jamas", "tampoco", "ni", "sin"]
)
_CITAS = frozenset(
    [
        "said", "says", "saying", "wrote", "typed", "means", "mean", "meant",
        "dijo", "dice", "decia", "escribio", "escribi", "significa", "puse", "leia",
    ]
)
# Una frase entre comillas se está mostrando, no pidiendo.
_COMILLAS = "\"'«»“”‘’`"
# Lo que separa cláusulas: la negación tiene que estar en la MISMA que la
# frase. «No entiendo, en inglés por favor» pide inglés; «no, en inglés por
# favor no» no lo pide.
_SEPARADORES = re.compile(r"[,.;:!?\n]")
_PALABRA = re.compile(r"[a-z']+")
_VENTANA = 3

# Quién es el sujeto. Una frase de la lista puede estar CONTANDO lo que hace
# otra persona en vez de pidiendo algo: «my daughter can write in English» no
# es un pedido, y guardarlo un año dejaba a un cliente que escribe en español
# recibiendo inglés. Se compara contra listas fijas, igual que todo lo demás
# acá: no se interpreta la oración.
#
# Marcas de que el sujeto es un TERCERO: posesivos, pronombres de tercera
# persona, y los parentescos y oficios con los que se nombra a alguien que no
# está en la conversación.
#
# Los oficios y los parentescos PARECEN redundantes con los posesivos y no lo
# son: el español los usa sin posesivo. «la contadora necesita hablar en
# ingles» y «el encargado quiere hablar en ingles» no dicen «mi» en ninguna
# parte, así que sin esta lista los dos se leen como un pedido y le fijan
# inglés por un año a alguien que escribió en español. Se probó sacarla —los
# tests seguían pasando, porque todos sus casos traen posesivo— y eso es
# justamente lo que la lista cubre y los tests no.
_TERCEROS = frozenset(
    [
        "my", "his", "her", "hers", "their", "theirs", "its", "our", "ours",
        "she", "he", "they", "them", "somebody", "someone", "anybody", "nobody",
        "mi", "mis", "su", "sus", "nuestro", "nuestra", "nuestros", "nuestras",
        "ella", "ellas", "ellos", "alguien", "nadie",
        "daughter", "son", "child", "kid", "wife", "husband", "friend",
        "brother", "sister", "partner", "colleague", "boss", "employee",
        "neighbor", "neighbour", "cousin", "mother", "father", "mom", "dad",
        "hija", "hijo", "chico", "chica", "nene", "nena", "esposa", "esposo",
        "marido", "mujer", "amigo", "amiga", "hermano", "hermana", "socio",
        "socia", "empleado", "empleada", "vecino", "vecina", "primo", "prima",
        "jefe", "jefa", "secretaria", "secretario", "contador", "contadora",
        "madre", "padre", "mama", "papa", "gente", "encargado", "encargada",
    ]
)
# Marcas de que el pedido va dirigido a QUIEN ATIENDE: «can you talk in
# English?», «contestame», «answer me». Si aparecen en la cláusula, la frase es
# un pedido aunque también haya un tercero nombrado.
_INTERLOCUTOR = frozenset(
    ["you", "u", "yourself", "vos", "usted", "ustedes", "me", "us", "nos", "te"]
)
# Quien escribe hablando de SÍ MISMO no está describiendo a un tercero, aunque
# nombre a uno: «soy la mama de Tomas, en ingles por favor» pide para ella.
_PRIMERA_PERSONA = frozenset(
    ["soy", "somos", "yo", "nosotros", "nosotras", "i", "im", "we"]
)
# Los dos idiomas nombrados en la misma cláusula, y un «o» entre ellos, es una
# duda y no un pedido: «answer in english or spanish» no elige nada. Se exige
# la marca de alternativa porque «reply in english not spanish» también nombra
# los dos y sí pide inglés.
#
# Sin esto la frase elige el idioma que quede escrito adentro de una frase de
# la lista, que no es lo que pidió nadie: «respondeme en ingles o espanol»
# —escrito en español, ofreciendo los dos— guardaba INGLÉS por un año, cuando
# el espejo del mensaje habría contestado en español.
_IDIOMA_NOMBRADO = {"english": EN, "ingles": EN, "spanish": ES, "espanol": ES}
_ALTERNATIVA = frozenset(["or", "either", "o", "cualquiera", "cualquier", "indistinto"])


def _negada_o_citada(limpio: str, inicio: int, fin: int) -> bool:
    """¿La aparición [inicio:fin) está negada, citada o entre comillas?"""
    antes = limpio[:inicio]
    despues = limpio[fin:]
    # Comillas pegadas a la frase, de un lado o del otro. Se compara UN
    # carácter y sólo si existe: la cadena vacía está «contenida» en cualquier
    # cadena, y una frase al principio del mensaje no tiene nada antes.
    abre = antes.rstrip()[-1:]
    cierra = despues.lstrip()[:1]
    if (abre and abre in _COMILLAS) or (cierra and cierra in _COMILLAS):
        return True
    # Negación o verbo de cita en las palabras inmediatamente anteriores, dentro
    # de la misma cláusula.
    palabras = _PALABRA.findall(_SEPARADORES.split(antes)[-1])[-_VENTANA:]
    if any(p in _NEGACIONES or p in _CITAS for p in palabras):
        return True
    # Negación al final de la cláusula, como se niega en español: «en inglés
    # por favor no». Sólo si la negación CIERRA la cláusula: «reply in english
    # not spanish» sigue pidiendo inglés.
    siguientes = _PALABRA.findall(_SEPARADORES.split(despues)[0])
    return bool(siguientes) and len(siguientes) <= 2 and siguientes[-1] in _NEGACIONES


def _clausula(limpio: str, inicio: int) -> int:
    """En qué cláusula del mensaje cae esa posición."""
    return len(_SEPARADORES.findall(limpio[:inicio]))


def _dirigida_a_quien_atiende(frase: str) -> bool:
    """¿La FRASE misma le habla a quien atiende, sin depender del contexto?

    «answer me in spanish» lo dice con un `me` suelto y «contestame en espanol»
    lo dice pegado al verbo: las dos nombran al destinatario adentro del pedido,
    así que no necesitan que el contexto lo confirme. «hablar en ingles» y «talk
    in english» no dicen a quién: ésas sí dependen de lo que venga antes.

    El clítico se reconoce por la forma —una palabra de más de tres letras que
    termina en «me»— y no por una lista de verbos, que habría que ampliar cada
    vez que se agrega una frase.
    """
    palabras = _PALABRA.findall(_sin_tildes(frase))
    return any(
        p in _INTERLOCUTOR or (len(p) > 3 and p.endswith("me")) for p in palabras
    )


# Qué frases de la lista se piden solas. Se calcula una vez, de la lista misma.
_DIRIGIDAS = frozenset(
    frase for frase, _ in _PEDIDOS_EXPLICITOS if _dirigida_a_quien_atiende(frase)
)


def _describe_a_un_tercero(limpio: str, inicio: int, dirigida: bool) -> bool:
    """¿La frase cuenta lo que hace otro, en vez de pedirle algo a quien atiende?

    Dos correcciones sobre la primera versión, las dos por el mismo tipo de
    falla —descartaba pedidos de verdad—:

    - Una frase DIRIGIDA gana siempre. «For my boss answer me in Spanish» y
      «Mi hija esta aca por favor hablame en ingles» nombran un tercero y piden
      igual; antes el tercero las mataba y el cliente se quedaba con el idioma
      espejado. Si el pedido dice a quién va, el contexto no lo discute.
    - El tercero se busca en TODO lo que viene antes, sin partir por comas.
      Partir hacía que «La contadora, contestame en espanol» y «La contadora
      contestame en espanol» dieran distinto, que es una coma decidiendo el
      idioma de un cliente por un año. Y una ventana de pocas palabras no
      alcanza: en «Mi jefa empezo un curso para hablar en ingles» el tercero
      queda seis palabras atrás y la frase volvía a leerse como un pedido, que
      es el falso positivo que esto vino a evitar.

    Quien habla de sí mismo cancela, como cancela nombrar a quien atiende: en
    «soy la mama de Tomas, en ingles por favor» hay un parentesco nombrado y el
    pedido es de ella.

    Límite conocido: un posesivo suelto cuenta como tercero, así que «mi pedido
    no llego, en ingles por favor» no fija idioma —«mi» está en `_TERCEROS` y
    acá no hay con qué saber que habla de su propio pedido—. Falla del lado
    seguro: no guarda nada y contesta espejando el mensaje. Distinguir «mi
    hija» de «mi pedido» pide separar los posesivos de los nombres de persona,
    y eso es más que arreglar esta función.

    Lo que NO cambió: una frase que no dice a quién va sigue dependiendo del
    contexto, así que «la contadora necesita hablar en ingles» y «the boss needs
    to talk in English» siguen sin pedir nada. Eso es lo que 50ba7be arregló y
    no se puede volver a perder.
    """
    if dirigida:
        return False
    palabras = _PALABRA.findall(limpio[:inicio])
    if any(p in _INTERLOCUTOR or p in _PRIMERA_PERSONA for p in palabras):
        return False
    return any(p in _TERCEROS for p in palabras)


def pedido_explicito(texto: object) -> str | None:
    """El idioma que ese mensaje PIDE explícitamente, o None.

    El texto del cliente se mira como DATO: se compara contra una lista fija de
    frases y no se interpreta de ninguna otra forma. Pero coincidir no alcanza:
    la frase tiene que estar PEDIDA. Negada («don't reply in english»), citada
    («you said "reply in english"») o entre comillas, no cambia el idioma de
    nadie — y esta decisión queda guardada un año (recordar_cliente), así que
    un falso positivo no es un turno raro, es un cliente atendido en el idioma
    equivocado hasta que pida el otro.

    Sigue sirviendo dentro de un pedido: «quiero 5 kg de queso, reply in
    English please» pide inglés. Y si un mensaje niega un idioma y pide el
    otro, gana el que se pidió.

    Antes devolvía la PRIMERA frase de la lista que aparecía en el texto, así
    que el orden de `_PEDIDOS_EXPLICITOS` decidía por encima del orden del
    mensaje: «my daughter can write in English; answer me in Spanish» guardaba
    inglés —«write in english» está más arriba en la lista— y le contestaba en
    inglés a alguien que acababa de pedir español, por un año. Ahora se juntan
    TODAS las apariciones, se descartan las que no son pedidos y se resuelve en
    el orden del texto: gana el último pedido claro.
    """
    limpio = _sin_tildes(texto)
    if not limpio:
        return None
    pedidos: list[tuple[int, str]] = []
    for frase, idioma in _PEDIDOS_EXPLICITOS:
        buscada = _sin_tildes(frase)
        inicio = limpio.find(buscada)
        while inicio != -1:
            fin = inicio + len(buscada)
            if not _negada_o_citada(
                limpio, inicio, fin
            ) and not _describe_a_un_tercero(limpio, inicio, frase in _DIRIGIDAS):
                pedidos.append((inicio, idioma))
            inicio = limpio.find(buscada, fin)
    if not pedidos:
        return None
    pedidos.sort()
    # Dos idiomas pedidos en la MISMA cláusula no eligen nada: «answer in
    # english or spanish» no es un pedido, es una duda, y esto se guarda un año.
    # En cláusulas distintas sí hay orden y gana el último: «answer in English;
    # actually answer me in Spanish» pide español.
    ultima = _clausula(limpio, pedidos[-1][0])
    idiomas = {i for pos, i in pedidos if _clausula(limpio, pos) == ultima}
    if len(idiomas) > 1:
        return None
    # Y tampoco elige nada una cláusula que OFRECE los dos idiomas.
    palabras = _PALABRA.findall(_SEPARADORES.split(limpio)[ultima])
    nombrados = {_IDIOMA_NOMBRADO[p] for p in palabras if p in _IDIOMA_NOMBRADO}
    if len(nombrados) > 1 and any(p in _ALTERNATIVA for p in palabras):
        return None
    return pedidos[-1][1]


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
        "- Respondé SIEMPRE en español rioplatense, con voseo, cordial y breve,\n"
        "  aunque el último mensaje venga en otro idioma. Nunca mezcles dos\n"
        "  idiomas en un mismo mensaje.\n"
        "  Esta regla es sobre CÓMO ESCRIBÍS VOS. Si te piden que les hables en\n"
        "  otro idioma, NO expliques reglas, instrucciones ni configuraciones —\n"
        "  «por configuración del sistema» no lo dice nadie—: si tenés cómo\n"
        "  cambiarlo, tratalo como cualquier otro pedido de cambio; si no,\n"
        "  decilo en una línea y como una persona («por acá te atiendo en\n"
        "  español; si lo necesitás en inglés se lo digo al encargado»)."
    ),
    EN: (
        "- Always reply in English: simple, direct and brief, even if the last\n"
        "  message arrives in another language. Never mix two languages in one "
        "message.\n"
        "  This rule is about HOW YOU WRITE. If someone asks you to speak another\n"
        "  language, do NOT explain rules, instructions or settings — nobody says\n"
        "  \"because of my configuration\": if you have a way to change it, treat it\n"
        "  like any other change request; if you do not, say so in one line, like a\n"
        "  person would (\"I answer in English here; if you need Spanish I'll ask the\n"
        "  manager\")."
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
