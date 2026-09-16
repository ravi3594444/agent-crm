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
    # OJO CON LAS PRIMERAS 24 LETRAS DE ESTOS CUATRO TEXTOS.
    # `demo/piloto.py::_es_disculpa` reconoce una disculpa técnica comparando
    # `texto[:24]` contra el catálogo, así que el arranque —«Perdón, tuve un
    # problema» / «Sorry, I hit a technical»— es un contrato con el banco de
    # pruebas y con los casos escritos a mano de tests/test_demo.py. La COLA se
    # reescribe libremente; el arranque, no, o el banco deja de ver un turno roto.
    # «no pude procesar tu mensaje» era lo único de acá que hablaba de máquinas.
    "fallback.problema_tecnico": {
        ES: (
            "Perdón, tuve un problema técnico y no te pude contestar. "
            "Probá de nuevo en un rato."
        ),
        EN: (
            "Sorry, I hit a technical problem and couldn't get back to you. "
            "Try again in a bit."
        ),
    },
    "fallback.problema_tecnico_avisado": {
        ES: (
            "Perdón, tuve un problema técnico. Ya le avisé al equipo, "
            "te contestan en un rato."
        ),
        EN: (
            "Sorry, I hit a technical problem. I've told the team, they'll get "
            "back to you in a bit."
        ),
    },
    # ------------------------------------------------------ estado de pedido
    # Todos estos los recibe el CLIENTE. Antes iban en los dos idiomas pegados
    # —«outside a model turn the customer's language is unknown», decía el
    # docstring— y justamente eso es lo que dejó de ser cierto.
    # `prompts.py` prohíbe contar lo que el sistema hace por dentro («Nunca
    # cuentes lo que hacés por dentro»), y esa regla ata al MODELO pero no a
    # estas cadenas fijas, así que ésta la rompía sin que nada lo notara:
    # nombraba la consulta al encargado y la re-lectura de stock, dos cosas que
    # al cliente no le sirven de nada y que además prometen un chequeo. Queda
    # sólo lo que necesita saber: quedó anotado, no está confirmado, le
    # contestamos. Sin día, sin hora y sin precio.
    # El mensaje que más clientes leen de todo el producto: sale con CADA pedido
    # que no se auto-confirma. Decía «Te anoté el pedido X. Todavía no está
    # confirmado; te contesto dentro de N h.» — el punto y coma es de un mail,
    # no de un WhatsApp, y «dentro de N h» es de un formulario. Lo que promete
    # es exactamente lo mismo: no está confirmado, y contesto antes de N horas.
    "pedido.pendiente": {
        ES: (
            "Listo, te lo anoté: {pedido}. Todavía no está confirmado, "
            "pero te contesto antes de {horas} h."
        ),
        EN: (
            "Done, I've got it down: {pedido}. It's not confirmed yet, "
            "but I'll get back to you within {horas} h."
        ),
    },
    # La confirmación. La etiqueta «Items:» era una palabra en inglés adentro de
    # un mensaje en español y convertía el recibo en un formulario; los renglones
    # se entienden solos debajo del título. «Total:» y «Entrega:» se quedan: así
    # escribe un remito cualquier almacenero, y son los dos datos que se buscan
    # de un vistazo.
    "pedido.confirmado_cliente": {
        ES: (
            "✅ Pedido {pedido} confirmado\n"
            "{renglones}\nTotal: {total}\nEntrega: {entrega}"
        ),
        EN: (
            "✅ Order {pedido} confirmed\n"
            "{renglones}\nTotal: {total}\nDelivery: {entrega}"
        ),
    },
    "pedido.entrega_a_coordinar": {
        ES: "a coordinar",
        EN: "to be arranged",
    },
    # Sin saludo: estos llegan cuando ya se estuvo hablando, y el saludo va una
    # sola vez por conversación. «Hola!» en la mitad de una charla es lo que
    # delata que del otro lado hay un programa que no leyó lo anterior.
    # «cumplirlo», «En breve» y «Perdón por la molestia» son de una carta
    # documento. Lo que dice no cambia: no se hace, alguien del equipo escribe,
    # y hubo una sola disculpa.
    "pedido.rechazado": {
        ES: (
            "Sobre tu pedido {pedido}: no lo vamos a poder hacer{motivo}. "
            "Ya te escribe alguien del equipo. Perdón."
        ),
        EN: (
            "About your order {pedido}: we can't do it{motivo}. "
            "Someone from the team is writing to you now. Sorry about that."
        ),
    },
    "pedido.cancelado": {
        ES: (
            "Tu pedido {pedido} quedó cancelado ({motivo}). Si fue un error, "
            "escribime y lo vemos."
        ),
        EN: (
            "Your order {pedido} is cancelled ({motivo}). If that's a mistake, "
            "message me and we'll sort it out."
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
        ES: "No me llegó bien tu respuesta. Mandámela de nuevo en un minuto.",
        EN: "Your reply didn't come through. Send it to me again in a minute.",
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
    # «está mirándolo una persona» es otra de las frases que
    # tests/test_solicitudes.py usa para reconocer ESTE mensaje: queda igual, y
    # lo que se arregla es el «Sobre {pedido} está mirándolo…» de adelante.
    "oferta.en_revision": {
        ES: (
            "El {pedido} está mirándolo una persona antes de cerrarlo. Te "
            "contesto en cuanto lo vea."
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
    # «a la brevedad» es de una nota de la municipalidad. «Te contesto en un
    # rato» dice lo mismo y lo dice una persona.
    # «necesito revisarlo con una persona» y «necesito que lo vea una persona»
    # (abajo) NO son sólo redacción: tests/test_solicitudes.py las usa para
    # distinguir ESTE mensaje del de al lado, incluso con un `not in`. Se
    # conservan tal cual; lo que se arregla es el marco que las rodeaba.
    "oferta.a_revision": {
        ES: (
            "Gracias por confirmar. Antes de cerrar el {pedido} necesito "
            "revisarlo con una persona: cambió algo desde la oferta. Te "
            "contesto en un rato."
        ),
        EN: (
            "Thanks for confirming. Someone has to look at {pedido} with me before "
            "we close it: something moved since the offer. I'll get back to you "
            "in a bit."
        ),
    },
    "oferta.revision_sin_registro": {
        ES: (
            "Gracias por confirmar. Con el {pedido} se me complicó algo y "
            "necesito que lo vea una persona. No queda nada confirmado a tu "
            "nombre. Te contesto en un rato."
        ),
        EN: (
            "Thanks for confirming. Something went wrong with {pedido} on my side "
            "and someone has to look at it. Nothing is closed in your name. I'll "
            "get back to you in a bit."
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
            "confirmed. If it is still doable, it has to be redone with today's "
            "stock and prices."
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
    # EL MISMO MENSAJE CUANDO LA VENTANA NO EXISTE.
    #
    # `confirmacion.registrar` escribe la marca durable que ABRE la cancelación
    # por WhatsApp, y puede fallar sola: el pedido queda confirmado igual —eso
    # es irreversible— pero la ventana no se abre. El mensaje de arriba la
    # promete siempre, así que en ese caso le decía al encargado que podía
    # anular algo que el sistema iba a rechazar. Es la clase de mentira que
    # `app/confirmacion.py` existe para que este sistema no pueda contar.
    #
    # Cambia SÓLO la última línea. Las otras siete son las mismas a propósito:
    # lo que pasó es lo mismo, y lo único distinto es lo que se puede hacer
    # después.
    "gerencia.confirmado_sin_ventana": {
        ES: (
            "✅ Pedido {pedido} confirmado\nCliente: {cliente}\nItems: {detalle}\n"
            "Total: {total}\nEntrega: {entrega}\nOrigen: {fuente}\n"
            "Confirmado: {momento}\n"
            "Informativo: no hace falta responder, el pedido queda confirmado.\n"
            "No pude dejar el registro de la confirmación, así que la anulación "
            "por WhatsApp no está disponible: si hay que anularlo, hacelo en ERPNext."
        ),
        EN: (
            "✅ Order {pedido} confirmed\nCustomer: {cliente}\nItems: {detalle}\n"
            "Total: {total}\nDelivery: {entrega}\nSource: {fuente}\n"
            "Confirmed: {momento}\n"
            "For your information: no reply needed, the order is confirmed.\n"
            "I could not write the confirmation record, so voiding it over "
            "WhatsApp is unavailable: if it has to be voided, do it in ERPNext."
        ),
    },
    # DE DÓNDE SALIÓ UNA CONFIRMACIÓN, que es el campo «Origen:» del mensaje de
    # arriba. Se armaba como literal en los tres que confirman —«automática
    # (política)», «manual (confirmación humana)», «solicitud aprobada y
    # aceptada»— y el mismo string se usaba para DOS cosas: el aviso que lee el
    # dueño y el registro durable que abre la ventana de anulación.
    #
    # Acá está sólo la mitad que se lee. El registro durable sigue guardando su
    # texto en español, como todo rastro de auditoría (sección 6 del allowlist):
    # es un valor que ya está escrito en los ERPNext de los despliegues.
    "gerencia.fuente_automatica": {
        ES: "automática (política)",
        EN: "automatic (policy)",
    },
    "gerencia.fuente_manual": {
        ES: "manual (confirmación humana)",
        EN: "manual (human confirmation)",
    },
    "gerencia.fuente_solicitud": {
        ES: "solicitud aprobada y aceptada",
        EN: "request approved and accepted",
    },
    "gerencia.escalamiento_asunto": {
        ES: "🙋 Un cliente necesita una persona",
        EN: "🙋 A customer needs a person",
    },
    "gerencia.escalamiento_cuerpo": {
        ES: "Cliente: {cliente}\nTel: {telefono}\nMotivo: {motivo}",
        EN: "Customer: {cliente}\nPhone: {telefono}\nReason: {motivo}",
    },
    # LA MISMA DERIVACIÓN, CUANDO LA PIDE ALGUIEN DEL EQUIPO. No es cosmética:
    # con las de arriba, un miembro del equipo cuya herramienta falló recibía
    # «🙋 Un cliente necesita una persona / Cliente: cuenta no registrada / Tel:
    # <su propio número>» —él, anunciado como un cliente desconocido, avisándose
    # a sí mismo—. `customer_code` está vacío para el equipo por construcción,
    # así que el hueco no se llenaba con un dato ausente sino con la etiqueta
    # equivocada.
    "gerencia.escalamiento_asunto_equipo": {
        ES: "🙋 Alguien del equipo necesita una mano",
        EN: "🙋 Someone on the team needs a hand",
    },
    "gerencia.escalamiento_cuerpo_equipo": {
        ES: "Del equipo: {telefono}\nMotivo: {motivo}",
        EN: "From the team: {telefono}\nReason: {motivo}",
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
        ES: "Ese día no salimos a repartir, así que eso lo decide el encargado.",
        EN: "We don't run deliveries that day, so the manager decides that one.",
    },
    "entrega.fuera_de_zona": {
        ES: "No repartimos en esa zona por ahora.",
        EN: "We don't deliver to that area right now.",
    },
    "entrega.solicitud_vencida": {
        ES: (
            "Sobre tu pedido {pedido}: se me pasó el tiempo y no pude ofrecerte "
            "otra cosa. Lo está viendo el encargado."
        ),
        EN: (
            "About your order {pedido}: time ran out and I couldn't offer you "
            "anything else. The manager is looking at it."
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
    # «no llegué a tener UNA respuesta» separa este mensaje de `entrega.respaldo`
    # («la respuesta»), y tests/test_solicitudes.py se apoya en esa diferencia de
    # un artículo, con un `in` de un lado y un `not in` del otro. No se toca.
    "entrega.vencida": {
        ES: (
            "Sobre tu pedido {pedido}: no llegué a tener una respuesta del "
            "encargado, así que no te lo puedo dar por confirmado. Escribime y "
            "lo vemos de nuevo con lo que haya."
        ),
        EN: (
            "About your order {pedido}: the manager didn't get back to me in "
            "time, so I can't call it confirmed. Message me and we'll look at it "
            "again with whatever we have."
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
    # El inglés decía «nothing is charged» —«no se te cobra nada»— donde el
    # español dice «no queda nada a tu nombre». No son lo mismo: uno habla de
    # plata y el otro de que no hay pedido agendado, y la promesa que importa es
    # la segunda. Se corrige junto con el tono.
    "entrega.revision_vencida": {
        ES: (
            "Sobre tu pedido {pedido}: te dije que lo miraba una persona y no "
            "llegamos a hacerlo, así que no lo dejo agendado y no queda nada a "
            "tu nombre. Perdón. Cuando quieras lo armamos de nuevo con lo que "
            "haya."
        ),
        EN: (
            "About your order {pedido}: I said someone would look at it and we "
            "didn't get to it. Nothing is booked in your name. Sorry. Message me "
            "whenever you like and we'll put it together again with whatever we "
            "have."
        ),
    },
    "entrega.respaldo_vencido": {
        ES: (
            "Sobre tu pedido {pedido}: se venció el plazo de esa opción "
            "({terminos}), así que no queda agendada y no hay nada confirmado a "
            "tu nombre. Cuando quieras escribime y lo armamos con lo que haya."
        ),
        EN: (
            "About your order {pedido}: that option has run out, so it isn't "
            "booked and nothing is confirmed in your name ({terminos}). Message "
            "me whenever you like and we'll put it together with whatever we "
            "have."
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
    # El gemelo de `codigo.ajuste_no_aplicado` para el código de SEIS dígitos
    # (una acción sobre un pedido). Estas dos estaban escritas a mano en
    # `app/main.py`, así que el dueño con el sistema en inglés recibía la
    # negativa en castellano justo en el camino del código de confirmación.
    "codigo.accion_no_aplicada": {
        ES: "No hice nada: {motivo}.",
        EN: "I did nothing: {motivo}.",
    },
    "codigo.accion_error": {
        ES: "No pude hacer esa acción en este momento. No cambié nada.",
        EN: "I could not carry out that action right now. I changed nothing.",
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
    # El «no pude preguntarle a ERPNext si esto ya se configuró antes». Es el
    # único LimiteError que no nace de lo que tecleó el dueño, y llega igual a su
    # pantalla: `proponer` -> `vigente` -> `_almacen` -> `_hubo_cambios_durables`,
    # y de ahí a `ajustes.preparar`, que lo mete adentro de un mensaje traducido.
    "limite.marca_no_verificable": {
        ES: "no pude verificar en ERPNext si los límites se configuraron antes",
        EN: "I couldn't check in ERPNext whether the limits were configured before",
    },
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
    "limite.texto_vacio": {
        ES: "«{ajuste}» no puede quedar vacío",
        EN: "«{ajuste}» cannot be left empty",
    },
    "limite.texto_largo": {
        ES: "«{ajuste}» entra en {tope} caracteres y escribiste {largo}",
        EN: "«{ajuste}» fits in {tope} characters and you wrote {largo}",
    },
    "limite.plantilla_invalida": {
        ES: (
            "«{ajuste}» tiene que ser el nombre de una plantilla de Meta "
            "—minúsculas, números y guión bajo—, no {valor}"
        ),
        EN: (
            "«{ajuste}» has to be the name of a Meta template —lowercase, "
            "digits and underscores—, not {valor}"
        ),
    },
    "limite.idioma_plantilla_invalido": {
        ES: (
            "«{ajuste}» es el idioma en que registraste la plantilla en Meta, "
            "como «es_AR» o «en_US», no {valor}"
        ),
        EN: (
            "«{ajuste}» is the language you registered the template in on Meta, "
            "like «es_AR» or «en_US», not {valor}"
        ),
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
    # La rama de «no autorizado» de los dos informes. Era una constante de
    # módulo en app/tools/operaciones.py —evaluada al importar, o sea antes de
    # que hubiera un idioma que consultar— y por eso era el único string de ese
    # archivo sin clave.
    "sistema.sin_permiso": {
        ES: (
            "Ese número no está autorizado para ver el estado del sistema. No "
            "consulté nada."
        ),
        EN: (
            "That number is not authorized to see the system status. I checked "
            "nothing."
        ),
    },
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
    # Los tres restos del informe de avisos caídos. Los dos primeros se alcanzan
    # en la forma más normal de una respuesta fallida a un cliente: no tiene
    # pedido. El tercero salía en TODA entrada con tag, no sólo en ésas.
    "sistema.sin_pedido": {ES: "sin pedido", EN: "no order"},
    "sistema.sin_proposito": {ES: "sin propósito", EN: "no purpose"},
    "sistema.destinatario": {
        ES: " — destinatario {tag}…",
        EN: " — recipient {tag}…",
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
        ES: "Ahora mismo no te puedo asegurar que tengamos {producto}.",
        EN: "Right now I can't promise we have {producto}.",
    },
    # «el stock de {producto}» y no «el {producto}»: el nombre del catálogo lleva
    # su propio género («Leche entera 1 L» es femenino, «Queso cremoso»
    # masculino) y ninguna plantilla puede concordar con los dos. «Stock» es
    # palabra de mostrador acá, así que no suena a sistema.
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
    # Los dos del mensaje al cliente. «Mandarlo»/«No mandarlo» y no
    # «Sí»/«No»: el botón se ve solo, sin la pregunta, cuando llega una
    # notificación al celular.
    "boton.mandar": {ES: "Mandarlo", EN: "Send it"},
    "boton.no_mandar": {ES: "No mandarlo", EN: "Do not send"},
    # La negativa GENÉRICA de una herramienta de gerencia. Hasta acá era sólo
    # la constante `runtime_context.SIN_PERMISO`, en castellano y sin gemelo en
    # inglés: el dueño que habla inglés recibía su única negativa en castellano.
    # El ES de acá tiene que seguir siendo igual a esa constante, y hay un test
    # que lo afirma — son dos literales en dos archivos distintos, así que
    # cambiar uno no mueve al otro y el assert sirve de verdad.
    "permiso.sin_autorizacion": {
        ES: "Ese número no está autorizado para esto. No consulté ni cambié nada.",
        EN: "That number is not authorized for this. I checked nothing and "
            "changed nothing.",
    },
    # ------------------------------------------------ la memoria del negocio
    # Las preguntas que el agente le hace al dueño cuando le falta un dato
    # del negocio. Viajaban interpoladas SIN TRADUCIR adentro de
    # `memoria.falta`, que sí estaba traducida: media frase en cada idioma,
    # el mismo defecto que `AccionError`. El ES es el de `HUECOS`, palabra
    # por palabra — la tupla lo sigue llevando y es lo que ve el prompt.
    "memoria.hueco.reparto_costo": {
        ES: '¿El reparto se lo cobrás aparte al cliente o ya va incluido en el precio?',
        EN: 'Do you charge delivery separately, or is it already in the price?',
    },
    "memoria.hueco.pedido_minimo": {
        ES: '¿Tenés un mínimo de compra para salir a repartir?',
        EN: "Do you have a minimum order before you'll make a delivery run?",
    },
    "memoria.hueco.formas_de_pago": {
        ES: '¿Cómo te suelen pagar: efectivo contra entrega, transferencia, cuenta corriente?',
        EN: 'How do they usually pay you: cash on delivery, transfer, credit account?',
    },
    "memoria.hueco.cuenta_corriente": {
        ES: '¿A quiénes les das cuenta corriente y a cuántos días?',
        EN: 'Who do you give a credit account to, and on what terms?',
    },
    "memoria.hueco.envases": {
        ES: '¿Los cajones y los envases vuelven, o se los cobrás?',
        EN: 'Do the crates and containers come back, or do you charge for them?',
    },
    "memoria.hueco.horario_corte": {
        ES: '¿Hasta qué hora te pueden pedir para que salga en el reparto del otro día?',
        EN: "How late can they order and still make the next day's delivery run?",
    },
    "memoria.hueco.producto_clave": {
        ES: '¿Cuál es el producto que no te puede faltar nunca?',
        EN: 'Which product can you never afford to run out of?',
    },
    "memoria.hueco.faltante": {
        ES: 'Cuando te falta un producto, ¿qué le ofrecés al cliente en su lugar?',
        EN: "When you're out of something, what do you offer the customer instead?",
    },
    "memoria.hueco.devoluciones": {
        ES: 'Si a un cliente le llega algo en mal estado, ¿qué hacés?',
        EN: 'If something arrives damaged, what do you do?',
    },
    "memoria.hueco.frio": {
        ES: 'En verano, ¿qué le contestás al que pregunta cómo le llega la mercadería?',
        EN: 'In summer, what do you tell someone who asks how the goods stay cold?',
    },
    "memoria.hueco.temporada": {
        ES: '¿Hay alguna época del año en que se te dispare o se te caiga la venta?',
        EN: 'Is there a time of year when your sales spike or drop off?',
    },
    "memoria.hueco.clientes_delicados": {
        ES: '¿Hay algún cliente al que convenga no dejarle acumular deuda?',
        EN: "Is there a customer you'd rather not let run up a balance?",
    },
    # Los cuatro que se cierran con un AJUSTE y no con una nota. El ES es el de
    # `HUECOS`, palabra por palabra, como los doce de arriba.
    "memoria.hueco.nombre_del_negocio": {
        ES: "¿Cómo se llama tu negocio, tal cual querés que se lo diga a un cliente?",
        EN: "What is your business called, exactly as you want a customer to hear it?",
    },
    "memoria.hueco.rubro_del_negocio": {
        ES: "¿A qué se dedica tu negocio? Con dos o tres palabras alcanza.",
        EN: "What does your business do? Two or three words is enough.",
    },
    "memoria.hueco.horario_de_atencion": {
        ES: "¿En qué horario atendés? Es lo que le voy a contestar al que pregunte.",
        EN: "What are your opening hours? That is what I will tell anyone who asks.",
    },
    "memoria.hueco.localidades_de_reparto": {
        ES: (
            "¿A qué localidades repartís? Sin esa lista no puedo confirmar "
            "ninguna entrega sola."
        ),
        EN: (
            "Which towns do you deliver to? Without that list I cannot confirm "
            "any delivery on my own."
        ),
    },
    "memoria.no_pude_leer": {
        ES: "No pude leer los datos del negocio: {error}.",
        EN: "I could not read your business notes: {error}.",
    },
    "memoria.olvidado": {
        ES: "Listo, me olvido de esto: «{texto}».",
        EN: "Done, I am forgetting this: \u201c{texto}\u201d.",
    },
    "memoria.no_anote": {
        ES: "No anoté nada: {error}.",
        EN: "I wrote nothing down: {error}.",
    },
    "memoria.anotado": {
        ES: "Anotado: «{texto}». Si está mal, decímelo y lo corrijo.",
        EN: "Noted: \u201c{texto}\u201d. If it is wrong, tell me and I will fix it.",
    },
    "memoria.nada_falta": {
        ES: "Por ahora no me falta nada importante. Si hay algo del negocio que "
            "querés que tenga presente, decímelo y lo anoto.",
        EN: "Nothing important is missing right now. If there is anything about "
            "the business you want me to keep in mind, tell me and I will note it.",
    },
    "memoria.falta": {
        ES: "Me falta esto: {pregunta}\n(cuando me conteste, lo anoto con "
            "sobre=\"{clave}\")",
        EN: "This is what I am missing: {pregunta}\n(when they answer, I note it "
            "with sobre=\"{clave}\")",
    },
    "memoria.sin_datos": {
        ES: "Todavía no tengo ningún dato tuyo anotado. Decime cualquier cosa que "
            "quieras que tenga presente —cómo te paga un cliente, qué no puede "
            "faltar, qué hacés con una devolución— y la anoto.",
        EN: "I have nothing of yours noted yet. Tell me anything you want me to "
            "keep in mind \u2014how a customer pays you, what must never run out, "
            "what you do with a return\u2014 and I will note it.",
    },
    "memoria.linea": {ES: "· {texto}  ({clave})", EN: "· {texto}  ({clave})"},
    "memoria.listado": {
        ES: "Tengo {total} datos tuyos anotados:",
        EN: "I have {total} of your notes:",
    },
    "memoria.listado_pie": {
        ES: "Para cambiar uno, decime el nuevo con la misma palabra entre "
            "paréntesis; para borrarlo, decime que me olvide de eso.",
        EN: "To change one, tell me the new version using the same word in "
            "parentheses; to delete it, tell me to forget it.",
    },
    # Las DOS que faltaban. `app/tools/memoria.py` pasaba por el catálogo en
    # todos sus caminos menos éstos dos, así que un dueño que puso el sistema en
    # inglés recibía inglés para «lo anoté» y castellano para «no había nada» y
    # para la confirmación de un cambio — en la misma conversación.
    "memoria.no_habia": {
        ES: "No tenía ningún dato guardado sobre «{sobre}», así que no borré nada.",
        EN: "I had nothing stored about “{sobre}”, so I deleted nothing.",
    },
    "memoria.cambiado": {
        ES: "Cambiado. Antes tenía: «{antes}».\nAhora: «{ahora}».",
        EN: "Changed. It used to say: “{antes}”.\nNow: “{ahora}”.",
    },
    # ------------------------------------------------ los informes de gerencia
    # app/tools/operaciones.py traducía las 40 cosas que devuelve y
    # app/tools/gerencia.py ninguna de las suyas, y los dos devuelven bloques
    # que el modelo relata casi textuales. Para un dueño que habla inglés, «¿de
    # qué estoy corto?» volvía con las etiquetas en castellano. No era un
    # criterio distinto: era que a este archivo nadie lo había traducido.
    "gerencia.reporte_vacio": {
        ES: "El reporte «{reporte}» no devolvió filas.",
        EN: "Report \u201c{reporte}\u201d returned no rows.",
    },
    "gerencia.reporte_encabezado": {
        ES: "Reporte «{reporte}» ({total} filas, muestro {muestro}):",
        EN: "Report \u201c{reporte}\u201d ({total} rows, showing {muestro}):",
    },
    "gerencia.autonomia_fallo": {
        ES: "No pude armar el resumen de autonomía: {error}.",
        EN: "I could not build the autonomy summary: {error}.",
    },
    "gerencia.sin_pendientes": {
        ES: "No hay pedidos pendientes de confirmación.",
        EN: "No orders are waiting to be confirmed.",
    },
    "gerencia.pendientes_encabezado": {
        ES: "{total} pedidos pendientes de confirmar:",
        EN: "{total} orders waiting to be confirmed:",
    },
    # La antigüedad es con lo que decide a cuál atender primero.
    "gerencia.antiguedad": {ES: " · hace {horas} h", EN: " · {horas} h ago"},
    "gerencia.linea_pendiente": {
        ES: "- {pedido} · {quien} · {monto} · entrega {fecha}{antiguedad}",
        EN: "- {pedido} · {quien} · {monto} · delivery {fecha}{antiguedad}",
    },
    "gerencia.ventas_resumen": {
        ES: "Últimos {dias} días: {total} pedidos confirmados, total {monto}. "
            "Promedio {promedio} por pedido.",
        EN: "Last {dias} days: {total} confirmed orders, {monto} in total. "
            "Average {promedio} per order.",
    },
    "gerencia.ventas_vacio": {
        ES: "Sin pedidos confirmados en los últimos {dias} días.",
        EN: "No confirmed orders in the last {dias} days.",
    },
    "gerencia.stock_bajo_titulo": {ES: "Stock bajo:", EN: "Low stock:"},
    "gerencia.stock_sin_alertas": {
        ES: "Sin alertas de stock.",
        EN: "No stock alerts.",
    },
    "gerencia.linea_stock": {
        ES: "- {item}: {cantidad} (mínimo {minimo})",
        EN: "- {item}: {cantidad} (minimum {minimo})",
    },
    "gerencia.sin_cobranzas": {
        ES: "No hay saldos pendientes de cobro.",
        EN: "There are no outstanding balances.",
    },
    "gerencia.cobranzas_encabezado": {
        ES: "Total a cobrar {monto} en {total} facturas:",
        EN: "{monto} outstanding across {total} invoices:",
    },
    "gerencia.linea_cobranza": {
        ES: "- {cliente}: {monto}",
        EN: "- {cliente}: {monto}",
    },
    "gerencia.cliente_no_encontrado": {
        ES: "No encontré un cliente que coincida con «{quien}».",
        EN: "I found no customer matching \u201c{quien}\u201d.",
    },
    "gerencia.ficha_encabezado": {
        ES: "{nombre} ({codigo}) · {grupo} · {telefono}\nÚltimos pedidos:",
        EN: "{nombre} ({codigo}) · {grupo} · {telefono}\nLatest orders:",
    },
    "gerencia.linea_pedido_cliente": {
        ES: "  · {fecha} {pedido} {monto} ({estado})",
        EN: "  · {fecha} {pedido} {monto} ({estado})",
    },
    "gerencia.ficha_sin_pedidos": {
        ES: "  · sin pedidos confirmados",
        EN: "  · no confirmed orders",
    },
    # Va adentro del encabezado de la ficha, donde iría el teléfono.
    "gerencia.sin_telefono_corto": {ES: "s/tel", EN: "no phone"},
    # ------------------------------------------------ mensajes a un cliente
    # Lo que el dueño ve antes de aprobar. El texto va ENTRE COMILLAS y
    # completo: lo que aprueba es exactamente lo que va a salir, y si se
    # resumiera estaría aprobando otra cosa.
    "salida.pedir_visto_bueno": {
        ES: "Para {cliente} ({telefono}):\n\n«{texto}»\n\n¿Se lo mando?",
        EN: "To {cliente} ({telefono}):\n\n\u201c{texto}\u201d\n\nShall I send it?",
    },
    "salida.mandado": {
        ES: "Listo, se lo mandé a {cliente}.",
        EN: "Done, I sent it to {cliente}.",
    },
    "salida.descartado": {
        ES: "Listo, no se lo mando.",
        EN: "Fine, I will not send it.",
    },
    # Venció o ya se usó. NO se distinguen los dos casos a propósito: el
    # remedio del dueño es el mismo —pedirlo de nuevo— y decirle «ya se mandó»
    # cuando en realidad venció sería decirle que el cliente fue avisado.
    "salida.ya_no_esta": {
        ES: "Ese mensaje ya no está para mandar: o salió, o pasó más de una "
            "hora. Pedímelo de nuevo si querés.",
        EN: "That message is no longer waiting: it either went out or it is "
            "over an hour old. Ask me again if you still want it.",
    },
    "salida.sin_telefono": {
        ES: "{cliente} no tiene teléfono cargado en el sistema, así que no le "
            "puedo escribir. Cargáselo en ERPNext y volvé a pedírmelo.",
        EN: "{cliente} has no phone number on file, so I cannot write to them. "
            "Add it in ERPNext and ask me again.",
    },
    # La ventana de 24 h de Meta. Se le explica en sus términos —«hace más de
    # un día que no te escribe»— y no con la palabra «ventana», que no
    # significa nada para él.
    # Cuando la ventana se cierra ENTRE que se propuso y que el dueño aprobó.
    # No es la misma que `fuera_de_ventana`: acá él ya tocó el botón, así que
    # lo que hay que decirle es que NO salió, no que no se puede pedir.
    "salida.se_cerro_la_ventana": {
        ES: "No salió: se cerró la ventana de 24 h de {cliente} mientras esperaba tu visto bueno. Escribile vos, o esperá a que te escriba y pedímelo de nuevo.",
        EN: "It did not go out: {cliente}'s 24 h window closed while it was waiting for your approval. Write to them yourself, or wait for them to write and ask me again.",
    },
    "salida.fuera_de_ventana": {
        ES: "Hace más de un día que {cliente} no te escribe, y WhatsApp no deja "
            "escribirle primero salvo con un mensaje ya aprobado por Meta, que "
            "para esto no hay. Te queda escribirle vos desde tu WhatsApp.",
        EN: "{cliente} has not written to you in over a day, and WhatsApp only "
            "allows starting a conversation with a message Meta approved in "
            "advance, and there is none for this. You would have to write from "
            "your own WhatsApp.",
    },
    # Igual que `crm.cliente_ambiguo` pero del lado de MANDAR: acá elegir solo
    # no le cambia los datos al cliente equivocado, le CUENTA algo al comercio
    # de al lado.
    "salida.cliente_ambiguo": {
        ES: "«{quien}» le queda a más de un cliente: {cuales}. No mandé nada — decime cuál con el código.",
        EN: "«{quien}» matches more than one customer: {cuales}. I sent nothing — tell me which one, by code.",
    },
    "salida.no_encontre": {
        ES: "No encontré a «{quien}» en el sistema.",
        EN: "I could not find \u201c{quien}\u201d in the system.",
    },
    # Lo que la herramienta le devuelve AL MODELO. Dice explícitamente que no
    # salió nada, porque el fallo natural del modelo acá es contestarle al
    # dueño «ya le avisé» cuando todavía no tocó el botón.
    "salida.esperando_visto_bueno": {
        ES: "Le mandé el mensaje al dueño con un botón para aprobarlo. TODAVÍA "
            "NO SALIÓ: no digas que el cliente fue avisado. Decile en una línea "
            "que se lo pasaste para que lo apruebe.",
        EN: "I sent the owner the message with a button to approve it. IT HAS "
            "NOT GONE OUT YET: do not say the customer was told. Say in one "
            "line that you passed it to them to approve.",
    },
    "salida.no_pude_pedir": {
        ES: "No pude mandarle el mensaje al dueño para que lo apruebe, así que "
            "no quedó nada pendiente. Decíselo y que le escriba él.",
        EN: "I could not send the owner the message to approve, so nothing is "
            "pending. Tell them, and that they should write to the customer.",
    },
    "salida.no_salio": {
        ES: "Aprobaste el mensaje pero no lo pude poner en la cola de salida, "
            "así que NO salió. Escribíle vos.",
        EN: "You approved the message but I could not queue it, so it did NOT "
            "go out. Write to them yourself.",
    },
    # Qué le falta a unos términos para ser una oferta. Son las piezas de una
    # frase («de eso falta qué día y a qué hora»), así que son prosa: antes
    # viajaban como literales adentro de `TERMINOS_DE_UNA_OFERTA`.
    "terminos.falta_fecha": {ES: "qué día", EN: "what day"},
    "terminos.falta_hora": {ES: "a qué hora", EN: "what time"},
    "terminos.falta_cargo": {ES: "cuánto se cobra", EN: "what you charge"},
    # La conjunción de una enumeración. Es prosa y no un separador: una lista
    # traducida terminaba con un «y» en el medio de una frase en inglés. Va sin
    # espacios porque `t` los recorta: los pone quien la usa.
    "terminos.y": {ES: "y", EN: "and"},
    # ------------------------------------------ el desglose de frenos
    # CÓMO SE LLAMA CADA CUBETA del resumen de autonomía. La tabla `_GRUPOS` de
    # app/autonomia.py sigue teniendo los nombres en español y ésos son la
    # CLAVE: se cuentan, se suman entre fuentes y se ordenan por cuenta, así que
    # traducirlos ahí partiría una cubeta en dos el día que cambie el idioma.
    # Acá está sólo cómo se muestran, igual que con los días de reparto.
    #
    # Es la mitad visible del bug que abrió el issue #9: `_linea_grupos`
    # formateaba estos nombres tal cual dentro de un resumen en inglés, y el
    # detector no los veía porque `sin` no estaba en la lista de palabras.
    "grupo.auto_apagada": {
        ES: "auto-confirmación apagada",
        EN: "auto-confirm off",
    },
    "grupo.limites_ilegibles": {ES: "límites ilegibles", EN: "limits unreadable"},
    "grupo.inventario_apagado": {ES: "inventario apagado", EN: "inventory off"},
    "grupo.tope_del_pedido": {ES: "tope del pedido", EN: "order ceiling"},
    "grupo.cliente_nuevo": {ES: "cliente nuevo", EN: "new customer"},
    "grupo.sobre_el_promedio": {
        ES: "muy por encima de su promedio",
        EN: "well above their average",
    },
    "grupo.historial_ilegible": {ES: "historial ilegible", EN: "history unreadable"},
    "grupo.deuda_vencida": {ES: "deuda vencida", EN: "overdue debt"},
    "grupo.sin_conteo": {ES: "sin conteo de stock", EN: "no stock count"},
    "grupo.sin_stock": {ES: "sin stock", EN: "out of stock"},
    "grupo.zona_de_entrega": {ES: "zona de entrega", EN: "delivery area"},
    "grupo.fecha_de_entrega": {ES: "fecha de entrega", EN: "delivery date"},
    "grupo.cantidad_por_producto": {
        ES: "cantidad por producto",
        EN: "quantity per item",
    },
    "grupo.descuento": {ES: "descuento", EN: "discount"},
    "grupo.lista_o_moneda": {ES: "lista o moneda", EN: "price list or currency"},
    "grupo.pedido_incompleto": {ES: "pedido incompleto", EN: "incomplete order"},
    "grupo.otros": {ES: "otros", EN: "other"},
    # Las dos puertas de postura (app/policy.py). Son tokens cortos y estables
    # que se cuentan, no prosa interpolada, y por eso también se agrupan.
    "grupo.postura_tope": {ES: "tope", EN: "ceiling"},
    "grupo.postura_stock": {ES: "stock apagado", EN: "stock off"},
    # El separador del conteo de frescura: «5 de 6». Es el `de` que motivó el
    # issue: se armaba en Python y salía igual en un resumen en inglés.
    "gerencia.autonomia_conteos": {
        ES: "{frescos} de {mirados}",
        EN: "{frescos} of {mirados}",
    },
    # -------------------------------------------- los avisos al equipo
    # LOS MENSAJES QUE EL BOT LE MANDA AL EQUIPO SOLO, sobre una solicitud que
    # nadie contestó, un cliente que contestó tarde o un borrador que no se pudo
    # cerrar. Salen por `solicitudes._avisar_equipo` -> `avisos.encolar_equipo`
    # -> `whatsapp.enviar_mensaje`, así que son texto que LEE UNA PERSONA en
    # WhatsApp, no un log ni un comentario de ERPNext.
    #
    # No eran una excepción documentada: el allowlist no tiene ninguna entrada
    # que diga «los avisos al equipo quedan en español», y los avisos al equipo
    # SÍ están migrados en todo lo demás —`pendientes.recordatorio_dueno`,
    # `pendientes.pendiente_cerrado_equipo` y los cuatro de `notificar.*`—. Eran
    # el resto sin migrar de una superficie que la migración cubre entera.
    #
    # LO QUE VA EN `{detalle}` ES OTRA COSA, y sigue en español a propósito: es
    # el motivo que se escribe en el registro durable y en el comentario de
    # ERPNext, o sea el rastro de auditoría, y traducirlo ahí sería traducir la
    # auditoría. Ver la entrada de `tests/idioma_allowlist.py`.
    "equipo.vencida_con_respaldo": {
        ES: (
            "⏰ {pedido}: la solicitud {solicitud} venció sin respuesta. "
            "{detalle}.\n"
            "Le ofrecí automáticamente lo que ya estaba configurado: {terminos} "
            "(solicitud {nueva}, vence {vence} UTC).\n"
            "Nada está confirmado hasta que el cliente acepte, y ahí se "
            "revalida todo."
        ),
        EN: (
            "⏰ {pedido}: request {solicitud} expired with no answer. "
            "{detalle}.\n"
            "I automatically offered them what was already configured: "
            "{terminos} (request {nueva}, expires {vence} UTC).\n"
            "Nothing is confirmed until the customer accepts, and everything is "
            "re-checked then."
        ),
    },
    "equipo.vencida_sin_respaldo": {
        ES: (
            "⏰ {pedido}: la solicitud venció sin respuesta.\n"
            "{detalle}.\n"
            "No pude ofrecerle nada concreto en su lugar: {porque}.\n"
            "Si querés hacerlo igual, reabrí el pedido en ERPNext y confirmalo."
        ),
        EN: (
            "⏰ {pedido}: the request expired with no answer.\n"
            "{detalle}.\n"
            "I couldn't offer them anything concrete instead: {porque}.\n"
            "If you want to do it anyway, reopen the order in ERPNext and "
            "confirm it."
        ),
    },
    "equipo.revision_vencida": {
        ES: (
            "⏰ {pedido}: la revisión {solicitud} venció sin que nadie la mirara "
            "({plazo} h).\n"
            "Motivo original: {motivo}.\n"
            "{detalle}.\n"
            "Le avisé al cliente que no avanza. Si todavía se puede, hay que "
            "rehacerlo con los datos del momento."
        ),
        EN: (
            "⏰ {pedido}: review {solicitud} expired with nobody looking at it "
            "({plazo} h).\n"
            "Original reason: {motivo}.\n"
            "{detalle}.\n"
            "I told the customer it isn't going ahead. If it still can be done, "
            "it has to be redone with today's data."
        ),
    },
    "equipo.cierro_por_persona": {
        ES: (
            "✅ {pedido}: cierro {que} {solicitud} porque el pedido ya {estado}. "
            "El borrador ya no retiene stock."
        ),
        EN: (
            "✅ {pedido}: I'm closing {que} {solicitud} because the order is "
            "already {estado}. The draft no longer holds stock."
        ),
    },
    "equipo.trabada": {
        ES: (
            "🚨 {pedido}: venció {que} {solicitud} y NO pude cerrar el borrador "
            "— {detalle}. Sigue reservando stock, así que lo dejo con plazo y "
            "reintento (intento {intentos}, próximo en {espera} min). Cerralo o "
            "confirmalo a mano en ERPNext."
        ),
        EN: (
            "🚨 {pedido}: {que} {solicitud} expired and I could NOT close the "
            "draft — {detalle}. It's still reserving stock, so I'm leaving it "
            "with a deadline and retrying (attempt {intentos}, next in {espera} "
            "min). Close it or confirm it by hand in ERPNext."
        ),
    },
    "equipo.cliente_rechazo": {
        ES: "🙅 {pedido}: el cliente no aceptó la oferta ({terminos}). {detalle}.",
        EN: "🙅 {pedido}: the customer didn't accept the offer ({terminos}). {detalle}.",
    },
    "equipo.acepto_tarde_trabado": {
        ES: (
            "⏰ {pedido}: el cliente aceptó después del vencimiento y NO pude "
            "cerrar el borrador — {detalle}. No lo confirmé. Sigue reservando "
            "stock y el barrido lo reintenta. Si todavía se puede, hay que "
            "rehacerlo con los datos del momento."
        ),
        EN: (
            "⏰ {pedido}: the customer accepted after the deadline and I could "
            "NOT close the draft — {detalle}. I didn't confirm it. It's still "
            "reserving stock and the sweep will retry. If it still can be done, "
            "it has to be redone with today's data."
        ),
    },
    "equipo.acepto_tarde": {
        ES: (
            "⏰ {pedido}: el cliente aceptó después del vencimiento. No lo "
            "confirmé; {detalle}. Si todavía se puede, hay que rehacerlo con los "
            "datos del momento."
        ),
        EN: (
            "⏰ {pedido}: the customer accepted after the deadline. I didn't "
            "confirm it; {detalle}. If it still can be done, it has to be "
            "redone with today's data."
        ),
    },
    # El comando `confirmar <pedido>` NO se traduce: es el payload que parsea el
    # router determinista, igual que en el aviso de pedido pendiente.
    "equipo.a_revision": {
        ES: (
            "⚠️ {pedido}: el cliente aceptó la oferta pero NO lo confirmé. "
            "Cambió algo desde la decisión: {detalle}. El pedido sigue en "
            "borrador; revisalo y, si corresponde, confirmalo con 'confirmar "
            "{pedido}'.\nTenés {horas} h: pasado ese plazo cierro el borrador "
            "para que deje de retener stock, y le aviso al cliente."
        ),
        EN: (
            "⚠️ {pedido}: the customer accepted the offer but I did NOT confirm "
            "it. Something changed since the decision: {detalle}. The order is "
            "still a draft; check it and, if it holds, confirm it with "
            "'confirmar {pedido}'.\nYou have {horas} h: after that I close the "
            "draft so it stops holding stock, and I tell the customer."
        ),
    },
    "equipo.revision_sin_registro": {
        ES: (
            "🚨 {pedido}: el cliente aceptó, algo había cambiado ({detalle}) y "
            "NO pude registrar la revisión en ERPNext. Cerré el borrador para "
            "que no retenga stock sin plazo: {como}. Está sin confirmar y sin "
            "revisión abierta — miralo a mano."
        ),
        EN: (
            "🚨 {pedido}: the customer accepted, something had changed "
            "({detalle}) and I could NOT record the review in ERPNext. I closed "
            "the draft so it doesn't hold stock with no deadline: {como}. It's "
            "unconfirmed and with no open review — look at it by hand."
        ),
    },
    # Las dos piezas que esos avisos arman aparte, y el «sin detalle» de cuando
    # la revisión no tiene motivo escrito.
    "equipo.la_solicitud": {ES: "la solicitud", EN: "the request"},
    "equipo.la_revision": {ES: "la revisión", EN: "the review"},
    "equipo.ya_confirmado": {ES: "está confirmado", EN: "confirmed"},
    "equipo.ya_cancelado": {ES: "fue cancelado", EN: "cancelled"},
    "equipo.sin_detalle": {ES: "sin detalle", EN: "no detail"},
    # El marco de ese mismo resumen cuando el dueño escribe prosa en vez del
    # comando. Estaba en español en `app/main.py` y quedaba pegado a una tabla
    # que sí se traduce, o sea la peor mitad: media respuesta en cada idioma.
    "equipo.instruccion_no_exacta": {
        ES: (
            "No ejecuto una instrucción que no sea exacta: esto cambia una "
            "fecha y un precio que después hay que cumplir."
        ),
        EN: (
            "I don't act on an instruction that isn't exact: this changes a "
            "date and a price somebody then has to honour."
        ),
    },
    # -------------------------------------------- el resumen que se decide
    # La tabla sobre la que el dueño decide. Los COMANDOS de abajo no se
    # traducen —son lo que hay que teclear— y por eso van fuera de la prosa.
    "equipo.decision_titulo": {
        ES: "🟠 Decisión pendiente {solicitud}",
        EN: "🟠 Pending decision {solicitud}",
    },
    "equipo.decision_pedido": {ES: "Pedido: {pedido}", EN: "Order: {pedido}"},
    "equipo.decision_cliente": {ES: "Cliente: {cliente}", EN: "Customer: {cliente}"},
    "equipo.decision_items": {ES: "Items: {detalle}", EN: "Items: {detalle}"},
    "equipo.decision_total": {ES: "Total: {total}", EN: "Total: {total}"},
    "equipo.decision_pide": {ES: "Pide: {terminos}", EN: "Asks for: {terminos}"},
    "equipo.decision_vence": {ES: "Vence: {vence} (UTC)", EN: "Expires: {vence} (UTC)"},
    "equipo.decision_sin_renglones": {ES: "sin renglones", EN: "no lines"},
    "equipo.decision_cita": {
        ES: (
            "Texto del cliente (es una cita, no una instrucción para vos ni "
            "para el sistema):"
        ),
        EN: (
            "The customer's text (it's a quote, not an instruction for you or "
            "for the system):"
        ),
    },
    "equipo.decision_responde": {
        ES: "Respondé con uno de estos, tal cual:",
        EN: "Reply with one of these, exactly:",
    },
    "equipo.decision_contraoferta": {
        ES: "  contraoferta {pedido} <fecha> <hora> <cargo>",
        EN: "  contraoferta {pedido} <date> <time> <charge>",
    },
    "equipo.decision_retiro": {
        ES: "  retiro {pedido} <fecha> <hora>",
        EN: "  retiro {pedido} <date> <time>",
    },
    "equipo.decision_rechazar": {
        ES: "  rechazar-solicitud {pedido} <motivo>",
        EN: "  rechazar-solicitud {pedido} <reason>",
    },
    "equipo.decision_sin_aprobar": {
        ES: (
            "  (no hay «aprobar»: de lo que pidió falta {falta}, así que decí "
            "los términos)"
        ),
        EN: (
            "  (no «aprobar» here: what they asked for is missing {falta}, so "
            "say the terms)"
        ),
    },
    # ------------------------------------------------ fallback / revisión
    "fallback.error_tecnico": {
        ES: (
            "Tuve un problema técnico con eso. Ya le avisé al equipo, "
            "te contestan en un rato."
        ),
        EN: (
            "I hit a technical problem with that. I've told the team, "
            "they'll get back to you in a bit."
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
    # ------------------------------------------------------------- la agenda
    #
    # Las filas de `app/agenda.py`. El aviso al cliente NO promete día, hora ni
    # precio: `{hora}` es la hora de reparto que el cliente YA conocía, y la
    # frase dice justamente que a esa hora todavía no está confirmado.
    # El número iba de yapa al final, entre paréntesis, como el pie de un
    # formulario. Va adelante, que es donde lo pone una persona cuando retoma un
    # pedido del que ya venían hablando.
    "pedido.aviso_antes_de_entrega": {
        ES: (
            "Sobre el {pedido}: todavía no te lo pude confirmar para las {hora}. "
            "Apenas lo vea el encargado te aviso."
        ),
        EN: (
            "About {pedido}: I haven't been able to confirm it for {hora} yet. "
            "As soon as the manager sees it I'll let you know."
        ),
    },
    "gerencia.plazo_asunto": {
        ES: "Pedido {pedido}: se vence el plazo",
        EN: "Order {pedido}: the deadline is close",
    },
    "gerencia.plazo_cuerpo": {
        ES: "Necesita respuesta antes de {hora} o no llega. Pedido {pedido}.",
        EN: "Needs an answer before {hora} or it won't make it. Order {pedido}.",
    },
    "gerencia.seguimiento": {
        ES: "Recordatorio del pedido {pedido}: {motivo}",
        EN: "Follow-up on order {pedido}: {motivo}",
    },
    # La línea que el aviso de pedido pendiente le agrega al dueño cuando hay
    # un plazo real. Va aparte y no dentro de `gerencia.cuerpo_pedido` para que
    # el aviso sin plazo no cambie ni una palabra.
    "gerencia.responder_antes_de": {
        ES: "⏳ Necesita respuesta antes de {hora} o no llega.",
        EN: "⏳ Needs an answer before {hora} or it won't make it.",
    },
    # La baja que pide el propio cliente (`app/agenda.py`). Las dos primeras son
    # un HECHO ya consumado: cuando salen, la reserva está suelta y comprobada,
    # así que no preguntan nada ni piden que nadie haga nada.
    "gerencia.baja_asunto": {
        ES: "Pedido {pedido}: lo dio de baja el cliente",
        EN: "Order {pedido}: the customer took it back",
    },
    "gerencia.baja_cuerpo": {
        ES: "{cliente} dio de baja el pedido {pedido}. No se prepara y ya no toma stock.",
        EN: "{cliente} took order {pedido} back. It will not be prepared and no longer holds stock.",
    },
    # Y éstas dos son lo contrario: al cliente se le dijo que su pedido quedaba
    # dado de baja y NO quedó. Es lo único de esta función que necesita a una
    # persona, y por eso lo dice con el motivo que contestó ERPNext.
    "gerencia.baja_trabada_asunto": {
        ES: "Pedido {pedido}: no pude darlo de baja",
        EN: "Order {pedido}: I could not take it back",
    },
    "gerencia.baja_trabada_cuerpo": {
        ES: (
            "Al cliente le dije que el pedido {pedido} quedaba dado de baja y no "
            "quedó: {motivo}. Sigue tomando stock hasta que lo cierres a mano."
        ),
        EN: (
            "I told the customer order {pedido} was taken back and it was not: "
            "{motivo}. It keeps holding stock until you close it by hand."
        ),
    },
    # ------------------------------------------------------------------
    # Las cadenas fijas de las herramientas de gerencia. El prompt ya
    # respetaba el idioma del dueño; estas lo esquivaban, así que un dueño
    # con el sistema en inglés recibía castellano en cuanto una herramienta
    # contestaba algo que no fuera un número.
    "ajustes.cliente_nuevo_sin_efecto": {
        ES: 'ℹ️ todavía sin efecto: hasta que el sistema verifique la dirección y la zona de entrega, un cliente nuevo siempre espera a una persona',
        EN: 'ℹ️ no effect yet: until the system checks the address and the delivery zone, a new customer always waits for a person',
    },
    "ajustes.cliente_nuevo_zona": {
        ES: 'ℹ️ sólo cuando la dirección del pedido cae en una zona de reparto configurada (o ya se le entregó ahí antes); si no, el pedido queda en borrador igual',
        EN: "ℹ️ only when the order's address falls inside a configured delivery zone (or you have delivered there before); otherwise the order stays a draft anyway",
    },
    "ajustes.cuenta_cargo": {
        ES: 'Cuenta contable del cargo: {cuenta} (se configura en el servidor).',
        EN: 'Accounting head for the fee: {cuenta} (it is set on the server).',
    },
    "ajustes.entrega_ilegible": {
        ES: 'No pude leer las reglas de entrega ({motivo}). Mientras no se puedan leer, no se ofrece ninguna entrega fuera de día ni retiro.',
        EN: "I couldn't read the delivery rules ({motivo}). While they can't be read, nothing is offered off-schedule and nothing for pickup.",
    },
    "ajustes.entrega_perdida": {
        ES: '⚠️ Se perdieron tus reglas de entrega: el almacén está vacío y ERPNext tiene cambios tuyos registrados. Los valores del servidor NO rigen. Hasta que las vuelvas a fijar no se ofrece reparto, entrega fuera de día ni retiro: decime cada regla con su valor y te pido confirmación.',
        EN: "⚠️ Your delivery rules were lost: the store is empty and ERPNext has changes of yours on record. The server values are NOT in force. Until you set them again there is no round, nothing off-schedule and no pickup: tell me each rule with its value and I'll ask you to confirm.",
    },
    "ajustes.entrega_pie": {
        ES: 'Para cambiar una, decime cuál y el valor nuevo. Te pido confirmación antes de aplicarla.',
        EN: "To change one, tell me which and the new value. I'll ask you to confirm before applying it.",
    },
    "ajustes.entrega_titulo": {
        ES: 'Reglas de entrega:',
        EN: 'Delivery rules:',
    },
    "ajustes.historial_ilegible": {
        ES: 'No pude leer el historial ({motivo}).',
        EN: "I couldn't read the history ({motivo}).",
    },
    "ajustes.historial_titulo": {
        ES: 'Últimos cambios de límites:',
        EN: 'Latest changes to your limits:',
    },
    "ajustes.historial_vacio": {
        ES: 'Todavía nadie cambió un límite; están todos en su valor inicial.',
        EN: 'Nobody has changed a limit yet; they are all at their initial value.',
    },
    "ajustes.limites_ilegibles": {
        ES: 'No pude leer los límites ({motivo}). Mientras no se puedan leer, ningún pedido se auto-confirma: todos quedan pendientes.',
        EN: "I couldn't read the limits ({motivo}). While they can't be read, nothing confirms on its own: every order waits for you.",
    },
    "ajustes.limites_pie": {
        ES: 'Para cambiar uno, decime cuál y el valor nuevo. Te pido confirmación antes de aplicarlo.',
        EN: "To change one, tell me which and the new value. I'll ask you to confirm before applying it.",
    },
    "ajustes.limites_titulo": {
        ES: 'Límites de auto-confirmación:',
        EN: 'Auto-confirm limits:',
    },
    "ajustes.linea_historial": {
        ES: '· {ts} — {ajuste}: {anterior} → {nuevo} (desde {telefono})',
        EN: '· {ts} — {ajuste}: {anterior} → {nuevo} (from {telefono})',
    },
    "ajustes.mal_configurado": {
        ES: '⚠️ mal configurado: {problema}',
        EN: '⚠️ misconfigured: {problema}',
    },
    "ajustes.no": {
        ES: 'no',
        EN: 'no',
    },
    "ajustes.origen_arranque": {
        ES: 'valor de arranque',
        EN: 'start-up value',
    },
    "ajustes.origen_default": {
        ES: 'default del sistema',
        EN: 'system default',
    },
    "ajustes.origen_dueno": {
        ES: 'lo fijaste vos',
        EN: 'you set it',
    },
    "ajustes.origen_perdido": {
        ES: 'se perdió del almacén',
        EN: 'lost from the store',
    },
    "ajustes.si": {
        ES: 'sí',
        EN: 'yes',
    },
    "ajustes.sin_configurar": {
        ES: 'sin configurar',
        EN: 'not set',
    },
    "ajustes.sin_cuenta_cargo": {
        ES: '⚠️ Sin cuenta contable configurada: un cargo de envío no se escribe en el pedido y queda para que lo agregue una persona.',
        EN: '⚠️ No accounting head set: a delivery fee is not written into the order and a person has to add it.',
    },
    "ajustes.sin_permiso": {
        ES: 'Ese número no está autorizado para ver ni cambiar los límites. No cambié nada.',
        EN: 'That number is not authorized to see or change the limits. I changed nothing.',
    },
    "ajustes.sin_valor_vigente": {
        ES: 'sin valor vigente',
        EN: 'no value in effect',
    },
    "captura.conteo_sin_autenticar": {
        ES: 'No pude autenticar quién cuenta; no cargué el conteo.',
        EN: 'I could not verify who is doing the count, so I did not record it.',
    },
    # LO QUE SE DICE CUANDO EL CONTEO NO SE PUDO ESCRIBIR. Existe porque sin
    # ella la falla llegaba al modelo como «esa herramienta falló» a secas, y el
    # modelo completaba el hueco con lo que sonaba bien: «ya te anoté los 5 kg de
    # leche», sobre una escritura que ERPNext había rechazado. Las dos mitades
    # que importan son «NO se guardó nada» y el motivo, que es lo único que
    # convierte una disculpa en algo que el dueño puede ir a arreglar.
    # LO QUE CONTESTA EL BOTÓN DE CONFIRMAR UN CONTEO. Vivían escritas a mano en
    # `app/decisiones.py`, o sea en castellano para todo el mundo: ese módulo no
    # es una herramienta —lo llama el router determinista de `app/main.py`— y el
    # audit que hace fallar el build ante castellano a mano deriva su lista de
    # módulos de `graph.TOOLS_GERENCIA`. Sin herramienta, sin audit. El dueño lo
    # vio al tocar el botón: toda la conversación en inglés y la confirmación en
    # castellano.
    "conteo.confirmado": {
        ES: (
            "Conteo {nombre} confirmado. Desde ahora el bot puede hablar de "
            "stock de esos productos."
        ),
        EN: (
            "Done — count {nombre} is applied. I can tell customers what's in "
            "stock now."
        ),
    },
    "conteo.ya_confirmado": {
        ES: "El conteo {nombre} ya estaba confirmado.",
        EN: "Count {nombre} was already applied.",
    },
    "conteo.cancelado": {
        ES: "El conteo {nombre} está cancelado; cargá uno nuevo.",
        EN: "Count {nombre} was cancelled. Send me a new one.",
    },
    "conteo.no_pude_abrir": {
        ES: "No pude abrir el conteo {nombre}. Revisalo en ERPNext.",
        EN: "I couldn't open count {nombre}. Have a look at it in ERPNext.",
    },
    "conteo.no_pude_confirmar": {
        ES: (
            "No pude confirmar el conteo {nombre}. Confirmalo en ERPNext o "
            "volvé a intentar."
        ),
        EN: (
            "I couldn't apply count {nombre}. Try again, or apply it in "
            "ERPNext."
        ),
    },
    "captura.conteo_rechazado": {
        ES: (
            "NO se guardó nada: ERPNext rechazó el conteo de {item_code} en "
            "{dep}. Motivo: {motivo}. Decíselo así —que no quedó registrado— y "
            "no digas que lo anotaste."
        ),
        EN: (
            "NOTHING was saved: ERPNext rejected the count of {item_code} in "
            "{dep}. Reason: {motivo}. Tell him exactly that — it was not "
            "recorded — and do not say you noted it down."
        ),
    },
    "captura.conteo_sin_diferencia": {
        ES: 'El sistema ya tiene {sistema} de {item_code} en {dep}; no hace falta ningún ajuste.',
        EN: 'The system already has {sistema} of {item_code} in {dep}; no adjustment is needed.',
    },
    "captura.pedido_en_borrador": {
        ES: 'El pedido {numero_pedido} todavía está en borrador. Hay que confirmarlo antes de marcarlo entregado.',
        EN: 'Order {numero_pedido} is still a draft. It has to be confirmed before it can be marked as delivered.',
    },
    "captura.pedido_no_encontrado": {
        ES: 'No encontré el pedido {numero_pedido}.',
        EN: 'I could not find order {numero_pedido}.',
    },
    "captura.remito_creado": {
        ES: 'Remito {remito} creado en borrador para {cliente}. Confirmalo y baja el stock.',
        EN: 'Delivery note {remito} created in draft for {cliente}. Confirm it and the stock comes off.',
    },
    "captura.venta_cargada": {
        ES: 'Cargado como {factura} en borrador ({detalle}) para {cliente}. Confirmalo en el sistema y se descuenta del stock.',
        EN: 'Saved as {factura} in draft ({detalle}) for {cliente}. Confirm it in the system and it comes off stock.',
    },
    "captura.venta_sin_lineas": {
        ES: 'Necesito saber qué productos se vendieron.',
        EN: 'I need to know which products were sold.',
    },
    "crm.borrador_sin_cambios": {
        ES: 'No me dijiste qué cambiarle: los renglones o la fecha.',
        EN: 'You did not tell me what to change: the lines or the date.',
    },
    "crm.cambio_entrega": {
        ES: 'entrega {fecha_entrega}',
        EN: 'delivery {fecha_entrega}',
    },
    "crm.cambio_renglones": {
        ES: '{renglones} renglón/es',
        EN: '{renglones} line(s)',
    },
    "crm.cliente_ambiguo": {
        ES: '«{nombre_o_codigo}» le queda a más de un cliente: {cuales} y puede que más. Pasame el código exacto — no quiero escribirle al equivocado.',
        EN: '“{nombre_o_codigo}” fits more than one customer: {cuales}, and there may be more. Send me the exact code — I do not want to write to the wrong one.',
    },
    "crm.cliente_error": {
        ES: 'No pude cambiar la ficha de {cliente}: {exc}',
        EN: 'I could not update the record for {cliente}: {exc}',
    },
    "crm.cliente_listo": {
        ES: 'Listo. {cliente}: {detalle}.',
        EN: 'Done. {cliente}: {detalle}.',
    },
    "crm.cliente_no_encontrado": {
        ES: 'No encontré ningún cliente que se llame o se codifique «{nombre_o_codigo}».',
        EN: 'I could not find any customer named or coded “{nombre_o_codigo}”.',
    },
    "crm.cliente_sin_cambios": {
        ES: 'No me dijiste qué cambiarle. Decime el grupo o la condición de pago.',
        EN: 'You did not tell me what to change. Give me the group or the payment terms.',
    },
    "crm.estado_cancelado": {
        ES: 'cancelado',
        EN: 'cancelled',
    },
    "crm.estado_confirmado": {
        ES: 'confirmado',
        EN: 'confirmed',
    },
    "crm.hecho_descripcion": {
        ES: 'descripción',
        EN: 'description',
    },
    "crm.hecho_reposicion": {
        ES: 'punto de reposición en {deposito} = {punto_de_reposicion}',
        EN: 'reorder level in {deposito} = {punto_de_reposicion}',
    },
    "crm.lo_hecho": {
        ES: 'Quedó cambiado: {hecho}. Pero:',
        EN: 'This much did change: {hecho}. But:',
    },
    "crm.nota_error": {
        ES: 'No pude dejar la nota en {cual}: {exc}',
        EN: 'I could not leave the note on {cual}: {exc}',
    },
    "crm.nota_hecha": {
        ES: 'Anotado en {cual}.',
        EN: 'Noted on {cual}.',
    },
    "crm.nota_vacia": {
        ES: 'No me dijiste qué anotar.',
        EN: 'You did not tell me what to write down.',
    },
    "crm.nota_y_tarea": {
        ES: 'Anotado en {cual}, y le queda la tarea a {recordarle_a}.',
        EN: 'Noted on {cual}, and the task is now with {recordarle_a}.',
    },
    "crm.pedido_actualizado": {
        ES: 'Pedido {pedido} actualizado ({dicho}). Sigue en BORRADOR: hay que confirmarlo para que salga.',
        EN: 'Order {pedido} updated ({dicho}). Still a DRAFT: it has to be confirmed before it goes out.',
    },
    "crm.pedido_error": {
        ES: 'No pude cambiar el pedido {pedido}: {exc}',
        EN: 'I could not change order {pedido}: {exc}',
    },
    "crm.pedido_no_borrador": {
        ES: 'El pedido {pedido} ya está {cual}, así que no lo toco. Un pedido confirmado se cambia por el camino de siempre, con tu código.',
        EN: 'Order {pedido} is already {cual}, so I am not touching it. A confirmed order gets changed the usual way, with your code.',
    },
    "crm.pedido_no_leido": {
        ES: 'No pude leer el pedido {pedido}: {exc}',
        EN: 'I could not read order {pedido}: {exc}',
    },
    "crm.presupuesto_error": {
        ES: 'No pude armar el presupuesto: {exc}',
        EN: 'I could not put the quote together: {exc}',
    },
    "crm.presupuesto_listo": {
        ES: 'Presupuesto {presupuesto} en borrador para {cliente}, con {renglones} renglón/es. Queda sin emitir: miralo antes de mandarlo.',
        EN: 'Quote {presupuesto} drafted for {cliente}, with {renglones} line(s). It stays unsubmitted: have a look before you send it.',
    },
    "crm.presupuesto_vacio": {
        ES: 'Un presupuesto vacío no sirve. Decime al menos un producto.',
        EN: 'An empty quote is no use. Give me at least one product.',
    },
    "crm.producto_descripcion_error": {
        ES: 'No pude cambiar la descripción de {item_code}: {exc}',
        EN: 'I could not change the description of {item_code}: {exc}',
    },
    "crm.producto_listo": {
        ES: '{item_code}: {hecho}.',
        EN: '{item_code}: {hecho}.',
    },
    "crm.producto_sin_cambios": {
        ES: 'No me dijiste qué cambiarle: la descripción o el punto de reposición.',
        EN: 'You did not tell me what to change: the description or the reorder level.',
    },
    # LOS PRECIOS. La negativa siempre dice QUÉ falta y cómo se destraba: un
    # «no puedo» sin la salida deja al dueño esperando a que alguien confirme
    # algo que nadie le va a confirmar.
    "crm.precio_banda_cerrada": {
        ES: 'Todavía no puedo cambiar precios solo. Decime cuánto lo dejo mover '
            'de una vez —por ejemplo «banda de precio 15%»— y te mando el código. '
            'Después de eso los cambio sin preguntarte más.',
        EN: 'I cannot change prices on my own yet. Tell me how much I may move '
            'one at a time — say "price band 15%" — and I will send you the code. '
            'After that I change them without asking you again.',
    },
    "crm.precio_sin_lista": {
        ES: 'No tengo configurada la lista de precios y la moneda con las que '
            'trabaja la confirmación automática. Si escribo el precio así, queda '
            'puesto y no lo mira nadie. Eso se arregla en el .env del servidor.',
        EN: 'The price list and currency that automatic confirmation works with '
            'are not configured. If I write the price like that it lands and '
            'nothing ever looks at it. That is fixed in the server .env.',
    },
    "crm.precio_sin_producto": {
        ES: 'Decime el código del producto.',
        EN: 'Tell me the product code.',
    },
    "crm.precio_sin_unidad": {
        ES: '{item_code} no tiene unidad de stock cargada, así que el precio '
            'quedaría sin unidad y la confirmación automática no lo encontraría.',
        EN: '{item_code} has no stock UOM, so the price would land without a unit '
            'and automatic confirmation would never find it.',
    },
    "crm.precio_sin_anterior": {
        ES: '{item_code} no tiene precio todavía. El primero no lo pongo solo: '
            'sin uno anterior no hay contra qué medir cuánto se mueve.',
        EN: '{item_code} has no price yet. I do not set the first one on my own: '
            'with nothing before it there is no way to measure the move.',
    },
    "crm.precio_fuera_de_banda": {
        ES: 'No lo cambié. {item_code} está en {antes} y me pedís {ahora}: es '
            '{movimiento}% y me dejaste mover hasta {banda}%. Si va en serio, '
            'subime la banda y lo hago.',
        EN: 'I did not change it. {item_code} is at {antes} and you asked for '
            '{ahora}: that is {movimiento}% and you let me move up to {banda}%. '
            'If you mean it, raise the band and I will.',
    },
    "crm.precio_no_verificado": {
        ES: 'Mandé el precio de {item_code} pero al releerlo no me quedó el que '
            'mandé. No te digo que está puesto sin haberlo visto: miralo en ERPNext.',
        EN: 'I sent the price for {item_code} but on reading it back it was not '
            'the one I sent. I will not tell you it is set without seeing it: '
            'check it in ERPNext.',
    },
    "crm.precio_hecho": {
        ES: '{item_code}: de {antes} a {ahora} por {unidad}. Releído y confirmado.',
        EN: '{item_code}: from {antes} to {ahora} per {unidad}. Read back and confirmed.',
    },
    "crm.precio_ya_hoy": {
        ES: 'A {item_code} ya le cambié el precio hoy. Uno por día por producto: '
            'así una seguidilla de cambios chicos no termina siendo uno grande '
            'sin que lo veas. Mañana lo muevo de nuevo.',
        EN: "I already changed {item_code}'s price today. One per product per day, "
            "so a run of small changes does not add up to a big one behind your "
            "back. I can move it again tomorrow.",
    },
    "crm.precio_error": {
        ES: 'No pude con el precio: {exc}',
        EN: 'I could not do the price: {exc}',
    },
    "crm.reposicion_error": {
        ES: 'No pude cambiar el punto de reposición de {item_code}: {exc}',
        EN: 'I could not change the reorder level for {item_code}: {exc}',
    },
    "crm.reposicion_no_leida": {
        ES: 'No pude leer el punto de reposición de {item_code}: {exc}',
        EN: 'I could not read the reorder level for {item_code}: {exc}',
    },
    "crm.reposicion_sin_deposito": {
        ES: 'Para el punto de reposición necesito el depósito: el mismo producto puede tener uno distinto en cada uno.',
        EN: 'For the reorder level I need the warehouse: the same product can have a different one in each.',
    },
    "crm.reposicion_sin_regla": {
        ES: '{item_code} no tiene una regla de reposición en {deposito} todavía. Esa se crea en ERPNext una vez, y después la puedo ajustar.',
        EN: '{item_code} does not have a reorder rule in {deposito} yet. That one is set up in ERPNext once, and after that I can adjust it.',
    },
    "crm.tarea_error": {
        ES: 'La nota quedó en {cual}, pero no pude crearle la tarea a {recordarle_a}: {exc}',
        EN: 'The note is on {cual}, but I could not create the task for {recordarle_a}: {exc}',
    },
    "gestion.no_prepare_nada": {
        ES: 'No preparé nada y no cambié nada: {exc}.',
        EN: 'I prepared nothing and changed nothing: {exc}.',
    },
    "gestion.no_pude_mostrar": {
        ES: 'No pude mostrarte el pedido: {exc}.',
        EN: "I couldn't show you that order: {exc}.",
    },
    "gestion.preparada": {
        ES: '{reemplazo}Preparada, todavía sin hacer:\n{consecuencia}\n\nTe mandé el código de confirmación por separado: contestá con esos seis dígitos y la hago. Yo no lo veo y no la puedo aplicar por vos. Si no contestás, se descarta sola.',
        EN: "{reemplazo}Prepared, not done yet:\n{consecuencia}\n\nI sent you the confirmation code separately: reply with those six digits and I'll do it. I don't see it and I can't apply it for you. If you don't reply, it's discarded on its own.",
    },
    "gestion.reemplazo": {
        ES: 'Reemplacé lo que tenías esperando sobre {pedido}: ese código anterior ya no sirve. Lo que hayas preparado sobre otro pedido sigue esperando igual.',
        EN: 'I replaced what you had waiting on {pedido}: that earlier code no longer works. Whatever you prepared on another order keeps waiting just the same.',
    },
    "gestion.repetida": {
        ES: 'Esto ya estaba preparado y sigue esperando tu confirmación:\n{consecuencia}\n\nEl código ya te lo mandé; contestá esos seis dígitos. No preparé nada nuevo ni cambié nada.',
        EN: 'This was already prepared and is still waiting for your confirmation:\n{consecuencia}\n\nI already sent you the code; reply with those six digits. I prepared nothing new and changed nothing.',
    },
    "gestion.sin_codigo": {
        ES: 'Preparé la acción ({accion} {pedido}) pero NO pude mandarte el código de confirmación, así que la descarté. No cambié nada. Probá de nuevo.',
        EN: 'I prepared the action ({accion} {pedido}) but could NOT send you the confirmation code, so I discarded it. Nothing was changed. Try again.',
    },
    # ------------------------------------------------------------------
    # Los motivos de `AccionError` (app/acciones.py). Viajaban como texto
    # en castellano adentro de un mensaje ya traducido: media frase en
    # cada idioma. Ahora la excepción lleva la clave y el motivo se arma
    # cuando se lee — ver `idioma.motivo_de`.
    "accion.cambia_algo": {
        ES: '«{accion}» cambia algo, así que no se hace de una: hay que prepararla y confirmarla con el código',
        EN: '«{accion}» changes something, so it is not done in one step: it has to be prepared and confirmed with the code',
    },
    "accion.cargo_como_numero": {
        ES: ' y el cargo como un número (0 es sin cargo)',
        EN: ' and the charge as a number (0 means no charge)',
    },
    "accion.codigo_de_otro_numero": {
        ES: 'ese código no es de este número',
        EN: 'that code does not belong to this number',
    },
    "accion.codigo_mal_formado": {
        ES: 'eso no tiene forma de código de confirmación',
        EN: 'that is not shaped like a confirmation code',
    },
    "accion.codigo_vencido": {
        ES: 'ese código ya venció. No cambié nada: pedime la acción de nuevo',
        EN: 'that code has expired. I changed nothing: ask me for the action again',
    },
    # Las tres de `_codigo_que_no_abre_nada`, que DEVUELVE la excepción en vez
    # de levantarla — por eso el barrido de `raise AccionError(` no las vio.
    "accion.falta_el_motivo": {
        ES: 'el motivo',
        EN: 'the reason',
    },
    "accion.falta_por_que": {
        ES: 'por qué',
        EN: 'why',
    },
    "accion.codigo_sin_nada_esperando": {
        ES: 'no hay ninguna acción esperando confirmación',
        EN: 'there is no action waiting to be confirmed',
    },
    "accion.codigo_racha_agotada": {
        ES: 'ese código no confirma nada, y van {intentos} seguidos: descarté lo que quedaba esperando ({cuantas}). No cambié nada — pedime de nuevo lo que querías',
        EN: 'that code confirms nothing, and that is {intentos} in a row: I discarded what was waiting ({cuantas}). I changed nothing — ask me again for what you wanted',
    },
    "accion.codigo_no_es_tuyo": {
        ES: 'ese código no confirma ninguna acción tuya. No cambié nada, y lo que tenías esperando sigue esperando: fijate el mensaje del código y contestá esos seis dígitos',
        EN: 'that code confirms none of your actions. I changed nothing, and what you had waiting is still waiting: check the message with the code and reply with those six digits',
    },
    "accion.falta_dato": {
        ES: 'falta {que}. «{accion}» se lo dice al cliente, así que no lo invento: preguntale y volvé a pedírmelo',
        EN: '{que} is missing. «{accion}» tells the customer, so I do not invent it: ask them and come back to me',
    },
    "accion.falta_pedido": {
        ES: 'falta el número de pedido',
        EN: 'the order number is missing',
    },
    "accion.necesito_con_cargo": {
        ES: 'qué día, a qué hora y cuánto se cobra',
        EN: 'what day, what time and how much is charged',
    },
    "accion.necesito_sin_cargo": {
        ES: 'qué día y a qué hora',
        EN: 'what day and what time',
    },
    "accion.no_pude_coordinar": {
        ES: 'no pude coordinar la acción sobre {pedido}; pedímela de nuevo en un momento',
        EN: 'I could not coordinate the action on {pedido}; ask me for it again in a moment',
    },
    "accion.no_pude_leer_historial": {
        ES: 'no pude leer el historial de acciones',
        EN: 'I could not read the action history',
    },
    "accion.no_pude_leer_pedido": {
        ES: 'no pude leer {pedido} en ERPNext, así que no preparé nada',
        EN: 'I could not read {pedido} in ERPNext, so I prepared nothing',
    },
    "accion.no_pude_leer_pendiente": {
        ES: 'no pude leer la acción pendiente',
        EN: 'I could not read the pending action',
    },
    "accion.no_registre": {
        ES: 'no pude registrar la acción para confirmarla',
        EN: 'I could not record the action so you could confirm it',
    },
    "accion.no_registre_autorizacion": {
        ES: 'no pude registrar la autorización en ERPNext, así que no la ejecuté',
        EN: 'I could not record the authorization in ERPNext, so I did not carry it out',
    },
    "accion.numero_no_autorizado": {
        ES: 'ese número ya no está autorizado para esto',
        EN: 'that number is no longer authorized for this',
    },
    "accion.parametros_ilegibles": {
        ES: 'los parámetros de la acción pendiente quedaron ilegibles',
        EN: "the pending action's parameters came back unreadable",
    },
    "accion.pedido_mal_formado": {
        ES: '«{texto}» no tiene forma de número de pedido (SAL-ORD-2026-00008). No adivino cuál es',
        EN: '«{texto}» is not shaped like an order number (SAL-ORD-2026-00008). I am not going to guess which one it is',
    },
    "accion.pendiente_ilegible": {
        ES: 'la acción pendiente quedó ilegible',
        EN: 'the pending action came back unreadable',
    },
    "accion.pendiente_no_confirmable": {
        ES: 'la acción pendiente no es de las que se confirman',
        EN: 'the pending action is not one of the ones that get confirmed',
    },
    "accion.sin_quien_confirma": {
        ES: 'no sé quién confirma la acción',
        EN: 'I do not know who is confirming the action',
    },
    "accion.sin_quien_pide": {
        ES: 'no sé quién pide la acción',
        EN: 'I do not know who is asking for the action',
    },
    "accion.sin_quien_pregunta": {
        ES: 'no sé quién pregunta',
        EN: 'I do not know who is asking',
    },
    "accion.sin_solicitud_abierta": {
        ES: '{pedido} no tiene ninguna solicitud abierta, así que no hay nada que ofrecerle al cliente. No preparé nada',
        EN: '{pedido} has no open request, so there is nothing to offer the customer. I prepared nothing',
    },
    "accion.sin_verbo": {
        ES: 'no me dijiste qué hacer; las acciones que puedo preparar son: {acciones}',
        EN: 'you did not tell me what to do; the actions I can prepare are: {acciones}',
    },
    "accion.solicitud_abierta_faltan_terminos": {
        ES: '{pedido} tiene una solicitud abierta y aprobarla es aprobar lo que pidió el cliente, y de eso falta {faltan}. No cambié nada. Decime los términos completos y preparo una contraoferta (qué día, a qué hora y cuánto se cobra) o un retiro (qué día y a qué hora)',
        EN: '{pedido} has an open request, and approving it means approving what the customer asked for — and {faltan} is missing from that. I changed nothing. Give me the full terms and I prepare a counter-offer (what day, what time and how much is charged) or a pickup (what day and what time)',
    },
    "accion.solo_lectura": {
        ES: '«{accion}» es de sólo lectura: se hace en el momento, no se propone',
        EN: '«{accion}» is read-only: it happens right away, it is not proposed',
    },
    "accion.terminos_no_entendidos": {
        ES: 'no entendí los términos, y no los invento: son una fecha y un precio que después hay que cumplir. Necesito {necesito}. El día va como «mañana», «jueves», «4/9» o «2026-09-07»; la hora como 18:00{extra}. Ejemplo: {ejemplo}',
        EN: 'I did not understand the terms, and I do not invent them: they are a date and a price that have to be honoured afterwards. I need {necesito}. The day goes like «tomorrow», «Thursday», «4/9» or «2026-09-07»; the time like 18:00{extra}. Example: {ejemplo}',
    },
    "accion.verbo_desconocido": {
        ES: '«{palabra}» no es una acción que exista. Las que puedo preparar son: {acciones}',
        EN: '«{palabra}» is not an action that exists. The ones I can prepare are: {acciones}',
    },
    # Los consejos al dueño (app/consejos.py). Los `TODO(idioma)` de ese
    # archivo nombraban estas claves: el módulo se escribió mientras otra
    # rama editaba el catálogo, y quedó pendiente. Es texto que lee el dueño.
    "consejo.sin_costo": {
        ES: 'no pude verificar el costo',
        EN: 'I could not verify the cost',
    },
    "consejo.perdida.titulo": {
        ES: 'Una venta por debajo del costo',
        EN: 'A sale below cost',
    },
    "consejo.perdida.cuerpo": {
        ES: 'El pedido {pedido} de {cliente} salió por debajo del costo: {perdida} contra la lista «{lista}».\n{detalle}',
        EN: 'Order {pedido} for {cliente} went out below cost: {perdida} against price list «{lista}».\n{detalle}',
    },
    "consejo.perdida.renglon": {
        ES: '· {item_name} — {qty} {uom} a {rate} y cuesta {costo} — {perdida}',
        EN: '· {item_name} — {qty} {uom} at {rate} and costs {costo} — {perdida}',
    },
    "consejo.perdida.piso": {
        ES: '\n({sin_costo} de: {faltantes}, así que la pérdida es un piso)',
        EN: '\n({sin_costo} for: {faltantes}, so the loss is a floor)',
    },
    "consejo.dormido.titulo": {
        ES: 'Un cliente dejó de comprar',
        EN: 'A customer stopped buying',
    },
    "consejo.dormido.cuerpo": {
        ES: '{nombre} compraba cada {ritmo} días y hace {silencio} que no pide (último: {ultimo}). Son unos {faltantes} pedidos de menos, cerca de {estimado} a su promedio de {promedio}.',
        EN: '{nombre} used to buy every {ritmo} days and has not ordered for {silencio} (last one: {ultimo}). That is about {faltantes} orders fewer, around {estimado} at their average of {promedio}.',
    },
    "consejo.deuda.titulo": {
        ES: 'Deuda que está envejeciendo',
        EN: 'A balance that is ageing',
    },
    "consejo.deuda.cuerpo": {
        ES: '{nombre} debe {total} en {facturas} factura(s), la más vieja vencida hace {atraso} días (tolerás {tolerancia}).',
        EN: '{nombre} owes {total} across {facturas} invoice(s), the oldest overdue by {atraso} days (you tolerate {tolerancia}).',
    },
    "consejo.quiebre.titulo": {
        ES: 'Un producto no llega al próximo reparto',
        EN: 'A product will not last until the next delivery run',
    },
    "consejo.quiebre.cuerpo": {
        ES: '{item_code}: quedan {hay} y el mínimo es {nivel}. Se venden {por_dia} por día y el próximo reparto es el {proximo} ({dias} día(s)): llegás con {proyectado}.',
        EN: '{item_code}: {hay} left and the minimum is {nivel}. {por_dia} sell per day and the next delivery run is {proximo} ({dias} day(s)): you arrive with {proyectado}.',
    },
    "consejo.quiebre.ya_abajo": {
        ES: ' Ya está por debajo del mínimo.',
        EN: ' It is already below the minimum.',
    },
    "consejo.quiebre.supuesto": {
        ES: '\n(Asumo el stock del sistema: {supuesto}.)',
        EN: "\n(I am assuming the system's stock figure: {supuesto}.)",
    },
    # ------------------------------------- las condiciones de entrega, al cliente
    # Lo que devuelve `app/tools/entrega.py::condiciones_de_entrega`. Son los
    # doce ajustes del grupo ENTREGA de app/limites.py dichos en el mostrador:
    # el cliente los pregunta todo el tiempo y hasta ahora el agente no tenía de
    # dónde sacarlos.
    #
    # DOS COSAS QUE SE LEEN JUNTAS Y NO SE PUEDEN SEPARAR: el DATO y la
    # instrucción de qué se puede hacer con él. Un ajuste que falta sale como
    # `condiciones.sin_dato` y `condiciones.instruccion` es lo que impide que
    # eso se convierta en un «no repartimos» — igual que el «No confirmes
    # disponibilidad.» con el que termina cada rama de `consultar_stock`.
    "condiciones.titulo": {
        ES: 'Lo que hacemos EN GENERAL con la entrega (no es una promesa sobre un pedido):',
        EN: 'What we do IN GENERAL about delivery (this is not a promise about any order):',
    },
    "condiciones.sin_dato": {
        ES: 'no lo tengo configurado',
        EN: "I don't have it set",
    },
    "condiciones.dias": {
        ES: 'Días de reparto: {valor}',
        EN: 'Delivery days: {valor}',
    },
    "condiciones.hora": {
        ES: 'Hora del reparto: {valor}',
        EN: 'Delivery time: {valor}',
    },
    "condiciones.localidades": {
        ES: 'Localidades donde repartimos: {valor}',
        EN: 'Towns we deliver to: {valor}',
    },
    "condiciones.cp": {
        ES: 'Códigos postales donde repartimos: {valor}',
        EN: 'Postcodes we deliver to: {valor}',
    },
    "condiciones.retiro": {
        ES: 'Se puede pasar a buscar el pedido por el local: {valor}',
        EN: 'Orders can be collected at the shop: {valor}',
    },
    "condiciones.retiro_dias": {
        ES: 'Días para pasar a buscarlo: {valor}',
        EN: 'Days to come and collect it: {valor}',
    },
    "condiciones.retiro_hora": {
        ES: 'Hora para pasar a buscarlo: {valor}',
        EN: 'Time to come and collect it: {valor}',
    },
    "condiciones.fuera_de_dia": {
        ES: 'Entregamos fuera de los días de reparto: {valor}',
        EN: 'We deliver outside the delivery days: {valor}',
    },
    "condiciones.fuera_de_dia_dias": {
        ES: 'Días en que se puede entregar fuera de reparto: {valor}',
        EN: 'Days an off-day delivery can happen: {valor}',
    },
    "condiciones.fuera_de_dia_hora": {
        ES: 'Hora de una entrega fuera de reparto: {valor}',
        EN: 'Time of an off-day delivery: {valor}',
    },
    "condiciones.cargo": {
        ES: 'Cargo por entregar fuera de los días de reparto: {valor}',
        EN: 'Charge for delivering outside the delivery days: {valor}',
    },
    "condiciones.minimo": {
        ES: 'Pedido mínimo para entregar fuera de día: {valor}',
        EN: 'Minimum order for an off-day delivery: {valor}',
    },
    "condiciones.zona_dentro": {
        ES: (
            '{zona}: entra en la zona de reparto. No se lo prometas para este '
            'pedido igual: la dirección completa la mira una persona cuando el '
            'pedido ya está cargado.'
        ),
        EN: (
            '{zona}: that falls inside the delivery area. Do not promise it for '
            'this order anyway: the full address is checked by a person once the '
            'order is in.'
        ),
    },
    "condiciones.zona_fuera": {
        ES: (
            '{zona}: {frase} Decíselo así y en la misma línea ofrecele lo que sí '
            'hay: que lo pase a buscar por el local si el retiro está activo, o '
            'que se lo pasás al encargado.'
        ),
        EN: (
            '{zona}: {frase} Say it like that and in the same line offer what '
            'there IS: collecting it at the shop if pickup is on, or that you '
            'are passing it to the manager.'
        ),
    },
    "condiciones.zona_sin_listas": {
        ES: (
            '{zona}: no tengo cargadas las zonas de reparto, así que no sé si '
            'llegamos. No le digas que no: decile que eso te lo confirma el '
            'encargado.'
        ),
        EN: (
            "{zona}: I don't have the delivery areas loaded, so I don't know "
            'whether we reach it. Do not tell them no: tell them the manager '
            'confirms that one.'
        ),
    },
    "condiciones.zona_pedir_cp": {
        ES: (
            '{zona}: tengo cargados los códigos postales y no las localidades, '
            'así que por el nombre no lo puedo comprobar. Pedile el código '
            'postal; no le digas que no llegamos.'
        ),
        EN: (
            "{zona}: I have the postcodes loaded but not the town names, so I "
            'cannot check it by name. Ask them for the postcode; do not tell '
            'them we do not reach it.'
        ),
    },
    "condiciones.zona_pedir_localidad": {
        ES: (
            '{zona}: tengo cargadas las localidades y no los códigos postales, '
            'así que por el número no lo puedo comprobar. Pedile la localidad; '
            'no le digas que no llegamos.'
        ),
        EN: (
            "{zona}: I have the town names loaded but not the postcodes, so I "
            'cannot check it by number. Ask them for the town; do not tell them '
            'we do not reach it.'
        ),
    },
    "condiciones.instruccion": {
        ES: (
            'Contestá SÓLO lo que preguntó y con tus palabras: no le leas esta '
            'lista. Lo que diga «{sin_dato}» no es un no —no lo inventes y no lo '
            'niegues—: decile que eso te lo confirma el encargado. Y nada de '
            'esto confirma la entrega de un pedido: el día y la dirección de '
            'ESTE pedido los decide una persona, y para entregar fuera de los '
            'días de reparto usá pedir_excepcion_de_entrega.'
        ),
        EN: (
            'Answer ONLY what they asked, in your own words: do not read this '
            'list back to them. Anything that says «{sin_dato}» is not a no — do '
            'not invent it and do not deny it: tell them the manager confirms '
            'that one. And none of this confirms the delivery of an order: the '
            'day and the address of THIS order are decided by a person, and to '
            'deliver outside the delivery days use pedir_excepcion_de_entrega.'
        ),
    },
    "condiciones.no_pude": {
        ES: (
            'No pude mirar las condiciones de entrega ahora. No inventes días, '
            'zonas, horarios ni cargos de envío, y no digas que no repartimos: '
            'decile que eso te lo confirma el encargado.'
        ),
        EN: (
            'I could not look up the delivery terms right now. Do not invent '
            'days, areas, times or delivery charges, and do not say we do not '
            'deliver: tell them the manager confirms that one.'
        ),
    },
    # Las dos que hacen VISIBLE que una nota pasó a ser pública. Van separadas
    # de `memoria.anotado` y `memoria.linea` en vez de llevar un `{marca}`
    # vacío: una marca que casi siempre es cadena vacía deja una frase con dos
    # espacios seguidos, y peor, hace que el caso importante se escriba igual
    # que el normal.
    "memoria.anotado_publico": {
        ES: "Anotado: «{texto}». Y esto se lo cuento a los clientes cuando "
            "pregunten. Si preferís que me lo guarde, decímelo.",
        EN: "Noted: \u201c{texto}\u201d. And I will tell customers this when "
            "they ask. If you would rather I keep it to myself, tell me.",
    },
    "memoria.linea_publica": {
        ES: "· {texto}  ({clave}) — esto lo saben los clientes",
        EN: "· {texto}  ({clave}) — customers are told this",
    },
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


def motivo_de(exc: Exception, lengua: str | None = None) -> str:
    """El texto de una excepción que lleva `clave`, en el idioma del que lee.

    Vive acá y no en cada módulo que tiene su propia excepción porque ya había
    DOS copias —`limites.motivo` y la que iba a necesitar `acciones`— y dos
    copias de una regla son dos reglas: la segunda se olvida el día que la
    primera aprende algo. `limites.motivo` ahora llama a ésta y conserva su
    nombre para los cinco lugares que ya lo usaban.

    Sin clave cae a `str(exc)`, que es lo que hacía antes: una excepción de otro
    módulo, o una vieja, sigue saliendo y nunca vacía.

    UN DATO PUEDE SER LLAMABLE, y ésa es la parte que no tenía `limites.motivo`.
    Casi todos los datos valen igual en los dos idiomas —un número de pedido, un
    monto, un código—, pero algunos NO: «falta el día y la hora» es una lista de
    nombres que hay que traducir, y quien levanta la excepción no sabe quién la
    va a leer. Un `lambda lengua: ...` difiere esa parte hasta acá, que es el
    único lugar donde el idioma ya se conoce.
    """
    clave = str(getattr(exc, "clave", "") or "")
    if not clave:
        return str(exc)
    crudos = getattr(exc, "datos", None) or {}
    datos = {k: (v(lengua) if callable(v) else v) for k, v in crudos.items()}
    return t(clave, lengua, **datos)


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
