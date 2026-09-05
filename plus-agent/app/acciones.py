"""La prosa del dueño, convertida en UNA acción que ya existe. Nada más.

EL PROBLEMA
El dueño no escribe comandos. Escribe «cancelá el de la panadería, se
arrepintieron» a las once de la noche. Hasta acá eso llegaba al agente de
gerencia, que no tiene ninguna herramienta capaz de mover un pedido, así que
la respuesta era el resumen de la solicitud y la lista de comandos exactos
(app/main.py::_resumen_de_solicitud). Correcto, y también un callejón: el que
tiene que tipear `cancelar SAL-ORD-2026-00008 se arrepintieron` es él, desde
el teléfono, con el número de pedido bien escrito.

LO QUE ESTA CAPA HACE, Y LO QUE NO
Hace UNA traducción: de lo que el dueño dijo a una acción de la lista blanca
de abajo, que es exactamente la misma lista de siempre —la de los comandos
determinísticos y la de los botones—. NO agrega una capacidad. Cada acción
termina en `app/aprobacion.py::manejar_boton` con el mismo payload que habría
armado el comando escrito a mano, y ahí adentro no cambió nada.

  · Las de SÓLO LECTURA se ejecutan en el momento, con el teléfono verificado.
    Leer un pedido no compromete nada y hacerlo esperar un código sería
    ceremonia sin propósito.

  · Las que ESCRIBEN no se ejecutan. Se guardan como una propuesta durable que
    dice, sin adjetivos, qué acción es, sobre qué pedido, con qué parámetros y
    qué va a pasar cuando se aplique. Python le manda al dueño —a su número, no
    por la respuesta del modelo— un código de SEIS dígitos de un solo uso,
    atado a su teléfono, a la acción, al pedido, a los parámetros y a un
    vencimiento. Cuando lo contesta, el router determinista de app/main.py
    revalida todo de nuevo en Python y recién entonces llama al handler de
    siempre.

POR QUÉ SEIS DÍGITOS Y NO CUATRO
Los cambios de ajustes (app/limites.py) ya usan cuatro. Si los dos códigos
tuvieran el mismo largo, un mensaje de cuatro dígitos con las dos cosas
pendientes sería ambiguo, y la desambiguación tendría que adivinar cuál de las
dos quiso confirmar. Con largos distintos no hay nada que adivinar: el router
mira el largo y sabe. Un pedido que se cancela solo porque el dueño tenía dos
confirmaciones abiertas es exactamente el accidente que esto evita.

LO QUE EL MODELO NO PUEDE HACER
No ve el código (nunca vuelve en el resultado de la herramienta), así que no
puede tomar los dos pasos. No puede inventar una acción: la que no está en la
lista blanca no existe. No puede inventar un pedido: el número tiene que tener
forma de pedido y el pedido tiene que leerse en ERPNext. No puede completar lo
que falta: sin motivo, sin fecha o sin términos no se guarda NADA y se
pregunta. Y lo que escribió un cliente y llegó citado se limpia antes de que
nada de eso se lea como un argumento (app/formato.py::sin_citas).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from redis.exceptions import RedisError

from app import erpnext, locks, notificar, solicitudes
from app import telefono as telefono_mod
from app.formato import pesos, sin_citas
from app.router import es_equipo

CLAVE_PROPUESTA = "plus-agent:acciones:propuesta"
CLAVE_AUDITORIA = "plus-agent:acciones:auditoria"

# Diez minutos, como el código de ajustes. Es lo que tarda una persona en leer
# un WhatsApp y contestarlo, y es poco para que algo cambie abajo.
PROPUESTA_TTL_SEGUNDOS = 600
AUDITORIA_MAXIMA = 500

# La marca del comentario durable en ERPNext. Es el registro de que ALGUIEN
# AUTORIZÓ esto: qué acción, sobre qué pedido, con qué parámetros y desde qué
# número. Lo que efectivamente pasó después lo anota el handler de siempre.
MARCA_DURABLE = "[accion]"


class AccionError(RuntimeError):
    """Lo que se pidió no se puede preparar o no se puede aplicar."""


# --------------------------------------------------------------- lista blanca

# Qué parámetro lleva cada acción, si lleva alguno.
NADA = ""
MOTIVO = "motivo"
TERMINOS = "terminos"
TERMINOS_RETIRO = "terminos_retiro"


@dataclass(frozen=True)
class Accion:
    nombre: str
    # El prefijo que espera app/aprobacion.py::manejar_boton. Es el MISMO que
    # arma el comando escrito a mano en app/main.py; lo verifica un test.
    payload: str
    escritura: bool
    parametro: str
    alias: tuple[str, ...]
    # Qué va a pasar, en la lengua del negocio. Se completa con el estado real
    # del pedido antes de mostrarlo: una consecuencia genérica no sirve para
    # decidir.
    consecuencia: str


ACCIONES: tuple[Accion, ...] = (
    Accion(
        "ver", "ver", False, NADA,
        ("detalle", "detalles", "mostrar"),
        "Te muestro el pedido y, si tiene una solicitud abierta, en qué anda.",
    ),
    Accion(
        "confirmar", "ok", True, NADA,
        ("confirma", "confirmo", "ok", "aprobar", "aproba", "apruebo", "aprobado"),
        "Confirmo el pedido en ERPNext y el cliente recibe su confirmación.",
    ),
    Accion(
        "rechazar", "no", True, MOTIVO,
        ("rechaza", "rechazo", "no"),
        "Rechazo el pedido: queda sin confirmar y se le avisa al cliente.",
    ),
    Accion(
        "preparar", "preparar", True, NADA,
        ("prepara", "preparo"),
        "Preparo el remito EN BORRADOR. No despacha nada todavía.",
    ),
    Accion(
        "despachar", "despachar", True, NADA,
        ("despacha", "despacho"),
        "Despacho el remito ya preparado: eso sí mueve el stock.",
    ),
    Accion(
        "despreparar", "despreparar", True, NADA,
        ("desprepara", "desprepare"),
        "Borro el remito en borrador que preparó el agente. El pedido queda como estaba.",
    ),
    Accion(
        "cancelar", "cancelar", True, MOTIVO,
        ("cancela", "cancelo", "anular", "anula", "anulo"),
        "Cancelo el pedido ya confirmado y se le avisa al cliente.",
    ),
    Accion(
        "contraoferta", "contraoferta", True, TERMINOS,
        ("contraofertar", "contraoferto"),
        "Le ofrezco al cliente otros términos. Todavía tiene que aceptarlos.",
    ),
    Accion(
        "retiro", "retiro", True, TERMINOS_RETIRO,
        ("retirar", "retira"),
        "Le ofrezco retirar por el local, sin cargo. Todavía tiene que aceptar.",
    ),
)

TODAS: dict[str, Accion] = {accion.nombre: accion for accion in ACCIONES}
POR_PALABRA: dict[str, Accion] = {
    palabra: accion
    for accion in ACCIONES
    for palabra in (accion.nombre, *accion.alias)
}
DE_ESCRITURA: tuple[str, ...] = tuple(a.nombre for a in ACCIONES if a.escritura)
DE_LECTURA: tuple[str, ...] = tuple(a.nombre for a in ACCIONES if not a.escritura)

# La forma de un número de pedido. Es la misma de app/main.py::_ORDER_REF, y
# hay un test que compara las dos contra el mismo corpus: si alguien afloja una
# nunca se entera por su lado. Anclada de punta a punta a propósito — sin eso,
# un "pedido" con dos puntos adentro se colaría en el payload y ahí los dos
# puntos separan la acción de sus argumentos.
PEDIDO_RE = re.compile(r"^[A-Za-z]{1,6}(?:-[A-Za-z]{1,6})?-\d[\w-]*$")


def _sin_tildes(texto: object) -> str:
    """Igual que app/main.py::_sin_tildes, más los signos que trae la prosa."""
    plano = "".join(
        char
        for char in unicodedata.normalize("NFD", str(texto or "").lower())
        if unicodedata.category(char) != "Mn"
    )
    return plano.strip().strip(".,;:!¡?¿ ").strip()


def resolver(nombre_o_alias: object) -> Accion:
    """La acción de la lista blanca, o AccionError con la lista entera.

    Sin coincidencias parciales: «confirmá el envío» no es «confirmar» a medias,
    y una acción elegida por parecido es una acción elegida por casualidad.
    """
    palabra = _sin_tildes(nombre_o_alias)
    if not palabra:
        raise AccionError(
            "no me dijiste qué hacer; las acciones que puedo preparar son: "
            + ", ".join(sorted(TODAS))
        )
    accion = POR_PALABRA.get(palabra)
    if accion is None:
        raise AccionError(
            f"«{palabra}» no es una acción que exista. Las que puedo preparar "
            "son: " + ", ".join(sorted(TODAS))
        )
    return accion


def pedido_valido(crudo: object) -> str:
    """El número de pedido en mayúsculas, o AccionError."""
    texto = " ".join(str(crudo or "").split())
    if not texto:
        raise AccionError("falta el número de pedido")
    if not PEDIDO_RE.match(texto):
        raise AccionError(
            f"«{texto[:40]}» no tiene forma de número de pedido "
            "(SAL-ORD-2026-00008). No adivino cuál es"
        )
    return texto.upper()


# ------------------------------------------------------------------ el reloj


def _ahora_texto() -> str:
    zona = os.getenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires").strip()
    try:
        return datetime.now(ZoneInfo(zona)).isoformat(timespec="seconds")
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now().isoformat(timespec="seconds")


def _codigo() -> str:
    """Seis dígitos, siempre seis: el primero nunca es cero."""
    return f"{secrets.randbelow(900000) + 100000}"


def _tag(telefono: object) -> str:
    """Hash corto, para el log. El número entero no va a stdout."""
    return hashlib.sha256(str(telefono or "").encode()).hexdigest()[:10]


def _clave(telefono: object) -> str:
    """La clave de la propuesta, en forma canónica.

    Proponer y confirmar llegan por caminos distintos —la herramienta de
    gerencia con el teléfono del contexto, el router determinista con el del
    webhook—. Si cada uno normalizara distinto, el código correcto no
    encontraría nada que aplicar. Ya pasó una vez, con los límites.
    """
    return f"{CLAVE_PROPUESTA}:{telefono_mod.normalizar(telefono) or telefono}"


def _texto(valor: object) -> str:
    if isinstance(valor, bytes):
        return valor.decode("utf-8", "replace")
    return str(valor or "")


# ------------------------------------------------------- lo que se va a hacer


def _huella(telefono: str, accion: Accion, pedido: str, parametros: dict) -> str:
    """Identidad de la propuesta: mismo pedido, misma acción, mismos datos.

    Sirve para que pedir dos veces lo mismo no genere dos códigos vivos ni dos
    mensajes al dueño. Un modelo que llama a la herramienta dos veces en el
    mismo turno es un accidente común; dos códigos distintos para la misma
    acción son un accidente caro.
    """
    crudo = json.dumps(
        [telefono_mod.normalizar(telefono) or telefono, accion.nombre, pedido, parametros],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(crudo.encode()).hexdigest()[:16]


def _parametros(accion: Accion, detalle: object, pedido: str) -> dict:
    """Valida lo que la acción necesita, o AccionError diciendo qué falta.

    `detalle` viene del modelo, o sea que puede venir de lo que el dueño
    dictó… o de lo que un cliente escribió y él reenvió citado. La cita se
    saca primero, siempre.
    """
    limpio = sin_citas(detalle)

    if accion.parametro == NADA:
        return {}

    if accion.parametro == MOTIVO:
        if len(limpio) < 3:
            que = "el motivo" if accion.nombre == "cancelar" else "por qué"
            raise AccionError(
                f"falta {que}. «{accion.nombre}» se lo dice al cliente, así que "
                "no lo invento: preguntale y volvé a pedírmelo"
            )
        return {"motivo": limpio[:400]}

    con_cargo = accion.parametro == TERMINOS
    terminos = solicitudes.parsear_terminos(limpio, con_cargo=con_cargo)
    if terminos is None:
        raise AccionError(
            "no entendí los términos, y no los invento: son una fecha y un "
            "precio que después hay que cumplir. Necesito "
            + ("qué día, a qué hora y cuánto se cobra" if con_cargo
               else "qué día y a qué hora")
            + ". El día va como «mañana», «jueves», «4/9» o «2026-09-07»; la "
            "hora como 18:00"
            + (" y el cargo como un número (0 es sin cargo)" if con_cargo else "")
            + f". Ejemplo: {solicitudes.como_pedir_los_terminos(pedido, {} if con_cargo else {'metodo': 'retiro'})}"
        )
    return {"crudo": limpio[:200], **terminos}


def payload(accion: Accion, pedido: str, parametros: dict) -> str:
    """El payload exacto que recibe app/aprobacion.py::manejar_boton.

    Byte por byte el mismo que arma el comando escrito a mano. Un test lo
    compara contra app/main.py::_staff_command para cada acción.
    """
    if accion.parametro == MOTIVO:
        motivo = str(parametros.get("motivo") or "")
        return f"{accion.payload}:{pedido}:{motivo}" if motivo else f"{accion.payload}:{pedido}"
    if accion.parametro in (TERMINOS, TERMINOS_RETIRO):
        return f"{accion.payload}:{pedido}:{parametros.get('crudo') or ''}"
    return f"{accion.payload}:{pedido}"


def _consecuencia(accion: Accion, pedido: str, parametros: dict) -> str:
    """Qué va a pasar, leyendo el pedido de verdad. Nunca una promesa genérica.

    Levanta AccionError cuando lo que se pidió no se puede hacer o le falta un
    dato: en los dos casos no se guarda nada y se dice qué falta.
    """
    try:
        so = erpnext.get_doc("Sales Order", pedido)
    except erpnext.ERPNextError as exc:
        raise AccionError(
            f"no pude leer {pedido} en ERPNext, así que no preparé nada"
        ) from exc

    cliente = str(so.get("customer_name") or so.get("customer") or "").strip()
    total = pesos(so.get("grand_total", 0))
    docstatus = so.get("docstatus")
    encabezado = f"{pedido} — {cliente or 'sin cliente'} · {total}"

    solicitud = _solicitud_abierta(pedido)

    if accion.nombre == "confirmar" and solicitud is not None:
        # Con una solicitud abierta, «ok» NO confirma el pedido: aprueba la
        # excepción que pidió el cliente (app/aprobacion.py). Y sólo se puede
        # aprobar lo que el cliente dijo en términos completos: un "ok" no
        # puede fabricar la fecha que nadie dio.
        faltan = solicitudes.terminos_incompletos(solicitud.solicitado)
        if faltan:
            raise AccionError(
                f"{pedido} tiene una solicitud abierta y aprobarla es aprobar lo "
                f"que pidió el cliente, y de eso falta {solicitudes.enumerar(faltan)}. "
                "No cambié nada. Decime los términos completos y preparo una "
                "contraoferta (qué día, a qué hora y cuánto se cobra) o un retiro "
                "(qué día y a qué hora)"
            )
        return (
            f"{encabezado}\nApruebo la excepción que pidió el cliente: "
            f"{solicitudes.terminos_texto(solicitud.solicitado, solicitud.moneda)}. "
            "Se le ofrece y todavía tiene que aceptarla."
        )

    if accion.nombre == "rechazar" and solicitud is not None:
        return (
            f"{encabezado}\nRechazo la excepción que pidió el cliente y se le "
            f"avisa. Motivo: «{parametros.get('motivo')}». El borrador del "
            "pedido queda para que lo revises."
        )

    if accion.nombre in ("contraoferta", "retiro") and solicitud is None:
        raise AccionError(
            f"{pedido} no tiene ninguna solicitud abierta, así que no hay nada "
            "que ofrecerle al cliente. No preparé nada"
        )

    if accion.nombre == "confirmar":
        estado = "Ya está confirmado" if docstatus == 1 else "Está en borrador"
        return (
            f"{encabezado}\n{estado}. Lo confirmo en ERPNext (submit) y el "
            "cliente recibe su confirmación."
        )

    if accion.nombre == "rechazar":
        return (
            f"{encabezado}\nRechazo el pedido: queda sin confirmar, deja de "
            f"comprometer stock y se le avisa al cliente. Motivo: "
            f"«{parametros.get('motivo') or 'sin detalle'}»."
        )

    if accion.nombre == "cancelar":
        from app import decisiones

        horas = decisiones.horas_cancelacion()
        return (
            f"{encabezado}\nCancelo el pedido en ERPNext y se le avisa al "
            f"cliente. Motivo: «{parametros.get('motivo')}». Sólo se puede "
            f"dentro de las {horas:g} h de la confirmación y si no tiene remito "
            "ni factura confirmados."
        )

    if accion.nombre == "contraoferta":
        return (
            f"{encabezado}\nLe ofrezco entrega el {parametros['fecha']} a las "
            f"{parametros['hora']} con un cargo de {pesos(parametros.get('cargo', 0))}. "
            "Todavía tiene que aceptarla."
        )

    if accion.nombre == "retiro":
        return (
            f"{encabezado}\nLe ofrezco retirar por el local el "
            f"{parametros['fecha']} a las {parametros['hora']}, sin cargo. "
            "Todavía tiene que aceptarlo."
        )

    return f"{encabezado}\n{accion.consecuencia}"


def _solicitud_abierta(pedido: str):
    """La solicitud abierta del pedido, o None. Nunca levanta."""
    try:
        solicitud = solicitudes.leer(pedido)
    except Exception as exc:
        print(f"[acciones] {pedido}: solicitud no legible ({type(exc).__name__})")
        return None
    return solicitud if solicitud is not None and solicitud.abierta else None


# ------------------------------------------------------------ sólo lectura


def ejecutar_lectura(nombre_o_alias: object, pedido_crudo: object, telefono: str) -> str:
    """Ejecuta AHORA una acción de sólo lectura. Sin código y sin propuesta.

    El payload lo arma esta función, nunca quien la llama: así no hay ninguna
    forma de que una acción de escritura entre por acá.
    """
    from app import aprobacion

    if not telefono:
        raise AccionError("no sé quién pregunta")
    accion = resolver(nombre_o_alias)
    if accion.escritura:
        raise AccionError(
            f"«{accion.nombre}» cambia algo, así que no se hace de una: "
            "hay que prepararla y confirmarla con el código"
        )
    pedido = pedido_valido(pedido_crudo)
    return str(aprobacion.manejar_boton(payload(accion, pedido, {}), telefono))


# --------------------------------------------------------------- la propuesta


def _leer(telefono: str) -> dict | None:
    try:
        crudo = locks.conexion().get(_clave(telefono))
    except (locks.CoordinationError, RedisError):
        return None
    if not crudo:
        return None
    try:
        propuesta = json.loads(_texto(crudo))
    except ValueError:
        return None
    return propuesta if isinstance(propuesta, dict) else None


def _vencida(propuesta: dict, ahora: float | None = None) -> bool:
    try:
        expira = float(propuesta.get("expira") or 0)
    except (TypeError, ValueError):
        return True
    return expira <= (time.time() if ahora is None else ahora)


def proponer(
    nombre_o_alias: object, pedido_crudo: object, detalle: object, telefono: str
) -> dict:
    """Deja una acción PENDIENTE de confirmación. No cambia nada.

    Devuelve la propuesta sin el código. `repetida` es True cuando ya había una
    idéntica esperando: en ese caso NO se genera otro código ni se manda otro
    mensaje, porque dos códigos vivos para la misma acción son dos formas de
    ejecutarla y una sola de acordarse.
    """
    if not telefono:
        raise AccionError("no sé quién pide la acción")
    accion = resolver(nombre_o_alias)
    if not accion.escritura:
        raise AccionError(
            f"«{accion.nombre}» es de sólo lectura: se hace en el momento, no "
            "se propone"
        )
    pedido = pedido_valido(pedido_crudo)
    parametros = _parametros(accion, detalle, pedido)
    consecuencia = _consecuencia(accion, pedido, parametros)

    huella = _huella(telefono, accion, pedido, parametros)
    anterior = _leer(telefono)
    if anterior and anterior.get("id") == huella and not _vencida(anterior):
        return {**{k: v for k, v in anterior.items() if k != "codigo"}, "repetida": True}

    ahora = time.time()
    propuesta = {
        "id": huella,
        "codigo": _codigo(),
        "accion": accion.nombre,
        "pedido": pedido,
        "parametros": parametros,
        "consecuencia": consecuencia,
        "telefono": telefono,
        "ts": _ahora_texto(),
        "expira": ahora + PROPUESTA_TTL_SEGUNDOS,
    }
    # UNA sola propuesta viva por teléfono, y la nueva pisa a la anterior. El
    # código de la anterior deja de existir en el mismo momento, así que no
    # queda un código viejo capaz de ejecutar algo que ya no es lo último que
    # se pidió — que es la manera en que dos pedidos abiertos a la vez se
    # convierten en el pedido equivocado cancelado.
    try:
        locks.conexion().setex(
            _clave(telefono),
            PROPUESTA_TTL_SEGUNDOS,
            json.dumps(propuesta, ensure_ascii=False),
        )
    except (locks.CoordinationError, RedisError) as exc:
        raise AccionError("no pude registrar la acción para confirmarla") from exc
    return {
        **{k: v for k, v in propuesta.items() if k != "codigo"},
        "repetida": False,
        "reemplazo": bool(anterior and anterior.get("id") != huella and not _vencida(anterior)),
    }


def codigo_de(telefono: str) -> str:
    """El código de la propuesta pendiente. SÓLO para app/tools/gestion.py.

    Existe para una cosa: dárselo a app/notificar.py, que lo manda al número
    del dueño. Nunca vuelve al modelo, nunca vuelve en el resultado de una
    herramienta y no se escribe en ningún log.
    """
    propuesta = _leer(telefono)
    return str((propuesta or {}).get("codigo") or "")


def pendiente(telefono: str) -> dict | None:
    """La acción que ese teléfono dejó esperando confirmación, si hay una.

    La usa el router determinista de app/main.py para saber si un mensaje de
    seis dígitos ES un código. NUNCA devuelve el código, y falla cerrada: si no
    se puede leer, no hay nada pendiente y el mensaje sigue su camino normal.
    """
    if not telefono:
        return None
    propuesta = _leer(telefono)
    if propuesta is None or _vencida(propuesta):
        return None
    return {k: v for k, v in propuesta.items() if k != "codigo"}


def descartar(telefono: str) -> None:
    """Tira la acción pendiente. Para cuando el código no llegó a destino."""
    if not telefono:
        return
    try:
        locks.conexion().delete(_clave(telefono))
    except (locks.CoordinationError, RedisError) as exc:
        print(f"[acciones] no pude descartar la propuesta ({type(exc).__name__})")


# ---------------------------------------------------------------- aplicarla


def aplicar(codigo: object, telefono: str) -> dict:
    """Revalida TODO en Python y recién entonces llama al handler de siempre.

    La propuesta se lee y se borra en la misma operación (GETDEL), así que un
    código sirve UNA vez: un reintento, un mensaje repetido de Meta o dos
    workers a la vez encuentran que ya no hay nada que aplicar. Un código
    equivocado también la consume, a propósito: sin segundo intento no hay
    nada que adivinar, y lo que se pierde es una propuesta que no escribió
    nada.
    """
    from app import aprobacion

    if not telefono:
        raise AccionError("no sé quién confirma la acción")
    # El número puede haber salido de TELEFONOS_EQUIPO entre que propuso y
    # confirmó. Se pregunta de nuevo, acá, con la lista de este momento.
    if not es_equipo(telefono):
        raise AccionError("ese número ya no está autorizado para esto")

    clave = _clave(telefono)
    try:
        crudo = locks.conexion().getdel(clave)
    except (locks.CoordinationError, RedisError) as exc:
        raise AccionError("no pude leer la acción pendiente") from exc
    if not crudo:
        raise AccionError("no hay ninguna acción esperando confirmación")
    try:
        propuesta = json.loads(_texto(crudo))
    except ValueError as exc:
        raise AccionError("la acción pendiente quedó ilegible") from exc
    if not isinstance(propuesta, dict):
        raise AccionError("la acción pendiente quedó ilegible")

    if str(propuesta.get("codigo")) != str(codigo or "").strip():
        raise AccionError(
            "ese código no es el de la acción pendiente. La descarté: no cambié "
            "nada y no queda nada esperando. Pedímela de nuevo si la querés"
        )
    # El vencimiento va adentro de la propuesta y no sólo en el TTL: un TTL que
    # no corrió (un Redis restaurado desde un backup, un reloj movido) no puede
    # revivir un código de ayer.
    if _vencida(propuesta):
        raise AccionError(
            "ese código ya venció. No cambié nada: pedime la acción de nuevo"
        )
    # Atado al teléfono: el código que le llegó a uno no lo puede usar otro,
    # aunque los dos estén en la lista del equipo.
    if telefono_mod.normalizar(propuesta.get("telefono")) != telefono_mod.normalizar(telefono):
        raise AccionError("ese código no es de este número")

    accion = resolver(propuesta.get("accion"))
    if not accion.escritura:
        raise AccionError("la acción pendiente no es de las que se confirman")
    pedido = pedido_valido(propuesta.get("pedido"))
    guardados = propuesta.get("parametros")
    if not isinstance(guardados, dict):
        raise AccionError("los parámetros de la acción pendiente quedaron ilegibles")
    # Se revalidan con los MISMOS validadores de la propuesta, sobre lo que el
    # dueño dictó, no sobre lo ya interpretado: si algo de eso dejó de ser
    # válido, no se ejecuta.
    if accion.parametro == MOTIVO:
        parametros = _parametros(accion, guardados.get("motivo"), pedido)
    elif accion.parametro in (TERMINOS, TERMINOS_RETIRO):
        parametros = _parametros(accion, guardados.get("crudo"), pedido)
    else:
        parametros = {}

    entrada = {
        "ts": _ahora_texto(),
        # Canónico, igual que el de la propuesta: si acá fuera el argumento
        # crudo, el comentario de ERPNext y el historial mostrarían un número
        # escrito distinto del que se autorizó. Es el mismo número; leerlo de
        # dos formas es una pregunta de más para el que audita.
        "telefono": telefono_mod.normalizar(telefono) or telefono,
        "accion": accion.nombre,
        "pedido": pedido,
        "parametros": parametros,
    }
    _auditar_en_erpnext(entrada)

    # Dos confirmaciones sobre el mismo pedido se hacen una detrás de la otra.
    # Los handlers ya releen y son idempotentes; esto les evita cruzarse.
    try:
        with locks.distributed_lock(f"accion:{pedido}", lease_seconds=60, wait_seconds=10):
            detalle = str(aprobacion.manejar_boton(payload(accion, pedido, parametros), telefono))
    except locks.CoordinationError as exc:
        raise AccionError(
            f"no pude coordinar la acción sobre {pedido}; pedímela de nuevo en un momento"
        ) from exc

    _auditar(entrada)
    print(
        f"[acciones] {accion.nombre} {pedido} "
        f"por {_tag(telefono)} ({entrada['ts']})"
    )
    return {**entrada, "detalle": detalle}


def _auditar_en_erpnext(entrada: dict) -> None:
    """Deja constancia de la AUTORIZACIÓN en ERPNext, antes de ejecutar nada.

    Lo que se registra acá es que una persona autorizó esto, con qué código y
    sobre qué parámetros. Lo que efectivamente pasó lo anota después el handler
    de siempre, con sus propias palabras.

    Si no se puede escribir, la acción NO se ejecuta. No es una precaución
    cara: todo lo que viene después habla con el mismo ERPNext, así que un
    ERPNext que no acepta un comentario tampoco va a confirmar un pedido — y
    prefiero no mover un pedido antes que moverlo sin registro.
    """
    detalles = ", ".join(
        f"{k}={v}" for k, v in sorted(entrada["parametros"].items()) if k != "crudo"
    )
    texto = (
        f"{MARCA_DURABLE} {entrada['accion']} autorizado por "
        f"{entrada['telefono']} el {entrada['ts']} con código de un solo uso"
        + (f" · {detalles}" if detalles else "")
    )
    try:
        erpnext.registrar_comentario("Sales Order", entrada["pedido"], texto)
    except erpnext.ERPNextError as exc:
        raise AccionError(
            "no pude registrar la autorización en ERPNext, así que no la ejecuté"
        ) from exc


def _auditar(entrada: dict) -> None:
    """El historial local. Nunca cambia lo que ya pasó, así que no levanta."""
    try:
        cliente = locks.conexion()
        cliente.rpush(CLAVE_AUDITORIA, json.dumps(entrada, ensure_ascii=False))
        cliente.ltrim(CLAVE_AUDITORIA, -AUDITORIA_MAXIMA, -1)
    except (locks.CoordinationError, RedisError) as exc:
        print(f"[acciones] historial no escrito ({type(exc).__name__})")


def auditoria(maximo: int = 10) -> list[dict]:
    """Las últimas acciones aplicadas, de la más nueva a la más vieja."""
    try:
        crudos = locks.conexion().lrange(CLAVE_AUDITORIA, -max(1, maximo), -1)
    except (locks.CoordinationError, RedisError) as exc:
        raise AccionError("no pude leer el historial de acciones") from exc
    entradas = []
    for crudo in reversed(list(crudos or [])):
        try:
            entradas.append(json.loads(_texto(crudo)))
        except ValueError:
            continue
    return entradas


def mandar_codigo(telefono: str, propuesta: dict) -> bool:
    """Le manda el código al dueño, a SU número. Nunca al modelo.

    Es la razón por la que los dos pasos son dos. Un código que volviera en el
    resultado de la herramienta sería un código que el modelo leyó, y un modelo
    que lo leyó puede confirmar la acción él solo en el mismo turno.
    """
    codigo = codigo_de(telefono)
    if not codigo:
        return False
    minutos = PROPUESTA_TTL_SEGUNDOS // 60
    return notificar.pedir_codigo_de_ajuste(
        telefono,
        f"Código para confirmar esta acción:\n{propuesta['consecuencia']}\n\n"
        f"Contestá *{codigo}* para que la haga. "
        f"Si no contestás, en {minutos} minutos se descarta sola. "
        "Si te equivocás de código no la ejecuto y la descarto.",
    )
