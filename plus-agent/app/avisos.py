"""Durable outbound notices: enqueued once, delivered exactly once.

WHY THIS EXISTS
The customer's order confirmation used to be a sentence the language model was
asked to relay: app/tools/pedidos.py returned a PEDIDO_CONFIRMADO token and the
prompt said "decí confirmado". A model that paraphrased it, dropped it, or
failed after ERPNext had already submitted the order left the customer with no
confirmation at all — and the suppression marker was set unconditionally in the
same breath, so no later path would send one either. The order was real and the
customer never heard it.

The authoritative notice is now DATA, not a prompt. It is built from the
ERPNext document, enqueued atomically under an idempotency key of
(event, order), and delivered by a worker that retries transient failures with
bounded backoff, parks permanent ones in the dead-letter list and opens one
deduplicated ERPNext ToDo. The model may still add conversational text in the
same turn; it is no longer responsible for the fact.

ORDERING OF THE TWO MARKERS
  1. the queue entry and its idempotency key are written in ONE Lua script;
  2. only a caller whose enqueue really committed reports success.
Nothing marks the customer as informed before the notice is durably queued, so
a crash between the two can only ever cause a retry, never a silent hole.

WHAT IS NOT HERE
Delivery to STAFF stays in app/notificar.py: an alert to the owner is a
different problem (several recipients, buttons, per-recipient claims) and it
already fails closed with its own dead-letter entry.
"""

from __future__ import annotations

import json
import os
import threading
import time

from app import erpnext
from app.formato import pesos
from app.outbound_status import (
    cliente as _redis,
)
from app.outbound_status import (
    digest_recipiente,
    has_accepted,
    record_outbound,
    registrar_aviso_fallido,
    window_open,
)

# ZSET: member = the notice as JSON, score = the epoch second it is due.
# One structure gives both the queue and its retry schedule, and a claimed
# entry is re-scored into the future instead of removed, so a worker that dies
# mid-send loses nothing: the lease simply expires and the notice comes back.
COLA = "wa:{inbound}:avisos"
ENCOLADO_TTL_SEGUNDOS = 30 * 24 * 60 * 60
LEASE_SEGUNDOS = 90

EVENTO_CONFIRMACION = "customer_order_confirmation"

# A woken worker sends within milliseconds; the poll interval is only the
# floor for retries and for a process that missed the wake-up.
despertar = threading.Event()

_ENCOLAR_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then
    return 0
end
redis.call('ZADD', KEYS[2], ARGV[1], ARGV[2])
redis.call('SET', KEYS[1], '1', 'EX', tonumber(ARGV[3]))
return 1
"""

# Claim the earliest due notice and lease it, in one step, so two workers
# cannot take the same one.
_RECLAMAR_LUA = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, 1)
if #due == 0 then
    return false
end
redis.call('ZADD', KEYS[1], ARGV[2], due[1])
return due[1]
"""


def _entero(variable: str, defecto: int, minimo: int = 1) -> int:
    try:
        valor = int(os.getenv(variable, str(defecto)))
    except (TypeError, ValueError):
        return defecto
    return max(minimo, valor)


def max_intentos() -> int:
    """How many delivery attempts before a notice is parked for a person."""
    return _entero("AVISOS_MAX_INTENTOS", 8)


def _espera(intentos: int) -> float:
    """Bounded exponential backoff, read fresh so it can be tuned by env."""
    base = float(_entero("AVISOS_REINTENTO_SEGUNDOS", 30))
    techo = float(_entero("AVISOS_REINTENTO_MAXIMO_SEGUNDOS", 3600))
    return min(techo, base * (2 ** max(0, intentos - 1)))


def _clave_encolado(evento: str, pedido: str) -> str:
    """Idempotency key: one notice per (event, order), for ever."""
    return f"wa:{{inbound}}:aviso-encolado:{digest_recipiente(evento + chr(0) + pedido)}"


def _clave_intentos(evento: str, pedido: str) -> str:
    return f"wa:{{inbound}}:aviso-intentos:{digest_recipiente(evento + chr(0) + pedido)}"


def _texto(valor: object, tope: int = 3500) -> str:
    return str(valor or "")[:tope]


def encolar(
    evento: str,
    pedido: str,
    telefono: str,
    texto: str,
    *,
    plantilla_env: str = "",
    parametros: list[str] | None = None,
) -> bool:
    """Queue ONE notice durably. True only when this call committed the entry.

    False means either "already queued or already delivered" or "Redis would
    not take it". The two are different for the caller, so a refused enqueue
    raises instead: a caller must never mark a customer as informed because a
    write it could not perform appeared to be a duplicate.
    """
    if not evento or not pedido or not telefono or not texto:
        raise ValueError("evento, pedido, teléfono y texto son obligatorios")

    try:
        if has_accepted(pedido, evento):
            return False
    except Exception as exc:
        print(f"[avisos] {pedido}: has_accepted falló ({type(exc).__name__})")

    entrada = json.dumps(
        {
            "evento": evento[:80],
            "pedido": pedido,
            "telefono": telefono,
            "texto": _texto(texto),
            "plantilla_env": plantilla_env[:80],
            "parametros": [_texto(p, 512) for p in (parametros or [])],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    nuevo = bool(
        _redis().eval(
            _ENCOLAR_LUA,
            2,
            _clave_encolado(evento, pedido),
            COLA,
            f"{time.time():.3f}",
            entrada,
            ENCOLADO_TTL_SEGUNDOS,
        )
    )
    if nuevo:
        despertar.set()
    return nuevo


def encolar_equipo(evento: str, pedido: str, texto: str) -> bool:
    """Queue one notice per staff phone. True when at least one was queued.

    A decision request must never be delivered inline: the sales turn that
    opened it has to answer the customer now, and a manager notice that Meta
    happens to reject must not take the request down with it. The idempotency
    key carries the recipient tag, so each phone is told exactly once and a
    second staff member is not skipped as a duplicate.
    """
    from app.router import STAFF

    if not STAFF:
        print(f"[avisos] {pedido}: TELEFONOS_EQUIPO vacío, nadie recibe la solicitud")
        return False
    telefonos = sorted(STAFF)
    if os.getenv("NOTIFICAR_SOLO_PRIMERO", "true").strip().lower() == "true":
        telefonos = telefonos[:1]
    encolados = 0
    for telefono in telefonos:
        etiqueta = digest_recipiente(telefono)[:12]
        try:
            if encolar(
                f"{evento}:{etiqueta}",
                pedido,
                telefono,
                texto,
                plantilla_env="WHATSAPP_STAFF_ALERT_TEMPLATE",
                parametros=[pedido, texto[:512]],
            ):
                encolados += 1
        except Exception as exc:
            print(f"[avisos] {pedido}: aviso al equipo no encolado ({type(exc).__name__})")
    return bool(encolados)


def pendientes() -> int:
    """Notices still queued. -1 when Redis cannot answer."""
    try:
        return int(_redis().zcard(COLA))
    except Exception as exc:
        print(f"[avisos] conteo no disponible ({type(exc).__name__})")
        return -1


def _reclamar(ahora: float) -> str | None:
    crudo = _redis().eval(
        _RECLAMAR_LUA,
        1,
        COLA,
        f"{ahora:.3f}",
        f"{ahora + LEASE_SEGUNDOS:.3f}",
    )
    if crudo is None or crudo is False:
        return None
    return crudo.decode() if isinstance(crudo, bytes) else str(crudo)


def _quitar(crudo: str) -> None:
    try:
        _redis().zrem(COLA, crudo)
    except Exception as exc:
        print(f"[avisos] no pude quitar un aviso entregado ({type(exc).__name__})")


def _reprogramar(crudo: str, demora: float) -> None:
    try:
        _redis().zadd(COLA, {crudo: time.time() + demora})
    except Exception as exc:
        print(f"[avisos] no pude reprogramar un aviso ({type(exc).__name__})")


def _sumar_intento(evento: str, pedido: str) -> int:
    """Attempts so far, including this one. 0 when the counter is unreadable.

    0 is below every cap on purpose: an unknown count must never be the reason
    a customer's notice is thrown away.
    """
    try:
        return int(_redis().incr(_clave_intentos(evento, pedido)))
    except Exception as exc:
        print(f"[avisos] contador de intentos no disponible ({type(exc).__name__})")
        return 0


def _wamid(resultado: object) -> str:
    if not isinstance(resultado, dict):
        return ""
    mensajes = resultado.get("messages")
    if not isinstance(mensajes, list) or not mensajes:
        return ""
    primero = mensajes[0]
    wamid = primero.get("id") if isinstance(primero, dict) else None
    return wamid.strip() if isinstance(wamid, str) else ""


def _enviar(entrada: dict) -> str:
    """Free text inside the recipient's own window, an optional template outside.

    Templates are an optional fallback in this pilot: customers always write
    first, so their 24-hour window is open when we answer them. Returns '' when
    there was no legitimate channel at all, which the caller retries.
    """
    from app import whatsapp

    telefono = str(entrada.get("telefono") or "")
    if window_open(telefono):
        return _wamid(whatsapp.enviar_mensaje(telefono, str(entrada.get("texto") or "")))

    plantilla = os.getenv(str(entrada.get("plantilla_env") or ""), "").strip()
    if not plantilla:
        print(
            f"[avisos] {entrada.get('pedido')}: ventana cerrada y sin "
            f"{entrada.get('plantilla_env') or 'plantilla'} configurada"
        )
        return ""
    return _wamid(
        whatsapp.enviar_plantilla(
            telefono,
            plantilla,
            os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "es_AR").strip() or "es_AR",
            list(entrada.get("parametros") or []),
        )
    )


def _parar(entrada: dict, crudo: str, motivo: str) -> None:
    """Give up on one notice: park it and make sure a person gets a task."""
    evento = str(entrada.get("evento") or "aviso")
    pedido = str(entrada.get("pedido") or "")
    _quitar(crudo)
    try:
        registrar_aviso_fallido(
            evento,
            pedido,
            str(entrada.get("texto") or ""),
            digest_recipiente(str(entrada.get("telefono") or ""))[:16],
        )
    except Exception as exc:
        print(f"[avisos] {pedido}: dead-letter falló ({type(exc).__name__})")
    try:
        erpnext.add_comment(
            "Sales Order",
            pedido,
            f"Aviso al cliente ({evento}) NO entregado: {motivo}. "
            "Quedó en la lista de avisos pendientes con una tarea para contactarlo.",
        )
    except Exception as exc:
        print(f"[avisos] {pedido}: comentario de dead-letter falló ({type(exc).__name__})")


def _entregar(crudo: str) -> None:
    """Deliver one claimed notice. Never raises: the queue must keep draining."""
    try:
        entrada = json.loads(crudo)
        if not isinstance(entrada, dict):
            raise ValueError("entrada inválida")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"[avisos] entrada ilegible descartada ({type(exc).__name__})")
        _quitar(crudo)
        return

    evento = str(entrada.get("evento") or "")
    pedido = str(entrada.get("pedido") or "")
    try:
        if has_accepted(pedido, evento):
            _quitar(crudo)
            return
    except Exception as exc:
        print(f"[avisos] {pedido}: has_accepted falló ({type(exc).__name__})")

    permanente = False
    detalle = ""
    try:
        wamid = _enviar(entrada)
    except Exception as exc:
        wamid = ""
        permanente = bool(getattr(exc, "permanent", False))
        detalle = f"{type(exc).__name__}"
        print(
            f"[avisos] {pedido}: envío falló ({detalle}) "
            f"{'permanente' if permanente else 'transitorio'}"
        )

    if wamid:
        try:
            record_outbound(wamid, evento, order_name=pedido)
        except Exception as exc:
            # Meta accepted it. Losing the tracking marker must not cause a
            # second message to the same customer.
            print(f"[avisos] {pedido}: tracking falló ({type(exc).__name__})")
        _quitar(crudo)
        return

    intentos = _sumar_intento(evento, pedido)
    tope = max_intentos()
    if permanente:
        _parar(entrada, crudo, f"Meta lo rechazó definitivamente ({detalle})")
        return
    if intentos >= tope:
        _parar(entrada, crudo, f"{intentos} intentos sin éxito ({detalle or 'sin canal'})")
        return
    _reprogramar(crudo, _espera(intentos))


def procesar(limite: int = 20) -> int:
    """Send every notice that is due. Returns how many were handled.

    Never raises: it runs on a background thread and a Redis hiccup must not
    stop the loop that will retry a minute later.
    """
    hechos = 0
    for _ in range(max(1, limite)):
        try:
            crudo = _reclamar(time.time())
        except Exception as exc:
            print(f"[avisos] no pude leer la cola ({type(exc).__name__})")
            return hechos
        if crudo is None:
            return hechos
        _entregar(crudo)
        hechos += 1
    return hechos


# ---------------------------------------------------------------------------
# The customer's order confirmation: the one notice that used to depend on the
# model saying it.
# ---------------------------------------------------------------------------


def texto_confirmacion_cliente(so: dict) -> str:
    """Order id, lines, total and how it is fulfilled — bilingual.

    Built here rather than by a model: this text IS the confirmation, and it
    has to say the same thing whatever the conversation looked like.
    """
    from app import notificar

    nombre = str(so.get("name") or "").strip()
    renglones = notificar.renglones(so)
    total = f"{pesos(so.get('grand_total'), 2)} {so.get('currency') or ''}".strip()
    direccion = notificar.direccion_de_entrega(so)
    fecha = str(so.get("delivery_date") or "").strip()
    entrega_txt = " — ".join(parte for parte in (direccion, fecha) if parte)
    if not entrega_txt:
        entrega_txt = "a coordinar / to be arranged"
    return "\n".join(
        [
            f"✅ Pedido {nombre} confirmado",
            f"Items: {renglones}",
            f"Total: {total}",
            f"Entrega: {entrega_txt}",
            "",
            f"Order {nombre} is confirmed. Items: {renglones}. "
            f"Total: {total}. Delivery: {entrega_txt}.",
        ]
    )[:3500]


def confirmacion_cliente(so: dict, telefono_conocido: str = "") -> bool:
    """Queue the authoritative confirmation for the order's customer.

    Returns True when THIS call queued it. False means it was already queued or
    already accepted by Meta — both of which mean the customer is covered — or
    that the customer has no phone on record, which is written on the order.
    Raises when NOBODY did it and nobody can: the queue is unavailable, or the
    Customer could not be read and no verified phone was given. A caller must
    be able to tell "someone else did it" from "nobody did it", and an ERPNext
    read that failed is the second thing, never a customer without a phone.

    ``telefono_conocido`` is a phone the caller already verified belongs to this
    order's customer — the one that just accepted the offer under the lock. It
    covers the notice when ERPNext cannot be asked for the Customer right now:
    the order IS confirmed, and a confirmation must not be lost to a re-read.
    """
    from app import telefono as telefonos

    nombre = str(so.get("name") or "").strip()
    if not nombre:
        return False
    leido = _telefono_del_cliente(so)
    telefono = leido or telefonos.normalizar(telefono_conocido) or ""
    if not telefono and leido is None:
        raise erpnext.ERPNextError(
            f"{nombre}: no pude leer el cliente para avisarle la confirmación"
        )
    if not telefono:
        try:
            erpnext.add_comment(
                "Sales Order",
                nombre,
                "Confirmación no avisada al cliente: no tiene teléfono cargado. "
                "Requiere contacto manual.",
            )
        except Exception as exc:
            print(f"[avisos] {nombre}: comentario sin teléfono falló ({type(exc).__name__})")
        return False
    return encolar(
        EVENTO_CONFIRMACION,
        nombre,
        telefono,
        texto_confirmacion_cliente(so),
        plantilla_env="WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE",
        parametros=[nombre, str(so.get("delivery_date") or "a coordinar")],
    )


def _telefono_del_cliente(so: dict) -> str | None:
    """The customer's phone; '' when they have none; None when it is UNKNOWN.

    None is never ''. A document with no customer code, or a Customer ERPNext
    would not hand over, says nothing about whether the customer has a phone —
    and recording "no tiene teléfono cargado" on the order in that state wrote
    a false audit line and dropped the customer's confirmation on the floor.
    """
    from app import telefono as telefonos

    codigo = str(so.get("customer") or "").strip()
    if not codigo:
        return None
    try:
        cliente = erpnext.policy_get_doc("Customer", codigo)
    except Exception as exc:
        print(f"[avisos] no pude leer el cliente ({type(exc).__name__})")
        return None
    return telefonos.normalizar(cliente.get("mobile_no")) or ""
