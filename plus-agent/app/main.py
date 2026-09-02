"""Durable WhatsApp webhook -> FIFO worker -> agent -> reply.

The webhook only verifies and durably enqueues an event before returning 200.
A persistent worker owns one global Redis lock, moves (rather than removes)
the FIFO head into a processing list, and removes it only after Meta accepts
the final WhatsApp send. Generated responses are cached before outbound API
calls, so a send retry never reruns the agent or its order tools.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import threading
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager

import httpx
import redis
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response

from app import erpnext, notificar
from app import whatsapp as whatsapp_client
from app.aprobacion import manejar_boton
from app.graph import responder_cliente, responder_gerencia
from app.outbound_status import record_inbound_window, record_outbound, update_status
from app.router import es_equipo
from app.whatsapp import enviar_mensaje

APP_SECRET = os.environ["META_APP_SECRET"]
VERIFY_TOKEN = os.environ["META_VERIFY_TOKEN"]

ACK_TEXT = "Recibido, dame un momento mientras lo verifico. / Got it, give me a moment to check."
TEXT_REQUIRED = (
    "Por ahora necesito que me escribas el pedido en texto para poder ayudarte."
)
# Two variants so the customer is never told the team was alerted unless an
# ERPNext task actually exists.
TECHNICAL_ERROR = (
    "Perdón, tuve un problema técnico y no pude procesar tu mensaje. "
    "Probá de nuevo en unos minutos."
)
TECHNICAL_ERROR_ALERTED = (
    "Perdón, tuve un problema técnico. Ya avisé al equipo y te responden en un rato."
)
# A model turn can end with no text at all (a provider may return a content
# list whose only blocks are non-text). Meta rejects an empty body, so the item
# would retry forever and the customer would hear nothing.
EMPTY_REPLY_FALLBACK = (
    "Perdón, no pude armar la respuesta. ¿Me lo escribís de nuevo? "
    "/ Sorry, I could not put together a reply. Could you send that again?"
)

MAX_WEBHOOK_BYTES = max(1_024, int(os.getenv("WHATSAPP_WEBHOOK_MAX_BYTES", "1048576")))
_STATE_TTL_SECONDS = 30 * 24 * 60 * 60
_WORKER_LOCK_TTL_SECONDS = 90
_ITEM_LEASE_TTL_SECONDS = 90
_WORKER_POLL_SECONDS = 1.0
_RETRY_SECONDS = 2.0
_ACK_CLAIM_TTL_SECONDS = 30
_ACK_WAIT_SECONDS = 16
_TECH_ALERT_TTL_SECONDS = 60 * 60
# A final send Meta rejects must not block every other customer behind it.
# Permanent rejections (expired token 190, recipient not allowed 131030, closed
# 24-hour window 131047, template errors...) are parked in the dead-letter list
# on the FIRST attempt. Only timeouts, HTTP 429 and 5xx are retried, with
# exponential backoff from _RETRY_SECONDS up to _RETRY_MAX_SECONDS, at most
# _SEND_MAX_ATTEMPTS times. Redis failures never count towards that limit.
_SEND_MAX_ATTEMPTS = max(1, int(os.getenv("WHATSAPP_SEND_MAX_ATTEMPTS", "10")))
_RETRY_MAX_SECONDS = max(
    _RETRY_SECONDS, float(os.getenv("WHATSAPP_RETRY_MAX_SECONDS", "60"))
)
# Meta codes that mean "try again later" although they arrive as HTTP 400.
_TRANSIENT_META_CODES = frozenset({4, 80007, 130429, 131000, 131016, 131048, 131056})
_retry_hint_guard = threading.Lock()
_retry_hint: float | None = None

# All queue keys share a Redis Cluster hash slot. This deployment uses a
# single container, but keeping the transaction cluster-safe costs nothing.
_QUEUE_KEY = "wa:{inbound}:queue"
_PROCESSING_KEY = "wa:{inbound}:processing"
_WORKER_LOCK_KEY = "wa:{inbound}:worker-lock"
_DEAD_KEY = "wa:{inbound}:dead"

r = redis.from_url(
    os.environ["REDIS_URL"],
    socket_connect_timeout=2.0,
    socket_timeout=5.0,
    health_check_interval=30,
    retry_on_timeout=True,
)

_worker_wake = threading.Event()
_volatile_results: dict[str, str] = {}
_volatile_accepted: dict[str, str | None] = {}
_volatile_guard = threading.Lock()


_ENQUEUE_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then
    return 0
end
redis.call('RPUSH', KEYS[2], ARGV[1])
redis.call('SET', KEYS[1], '1', 'EX', tonumber(ARGV[2]))
return 1
"""

_REFRESH_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
    return 1
end
return 0
"""

_DELETE_IF_VALUE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

# API acceptance is persisted before this script runs. Requiring the worker token
# fences an old worker whose lock expired from removing another worker's item.
_COMPLETE_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return -1
end
local removed = redis.call('LREM', KEYS[2], 1, ARGV[2])
if redis.call('GET', KEYS[3]) == ARGV[1] then
    redis.call('DEL', KEYS[3])
end
return removed
"""


_DEAD_LETTER_LUA = """
-- dead-letter: park an item whose final send Meta keeps rejecting
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return -1
end
local removed = redis.call('LREM', KEYS[2], 1, ARGV[2])
if removed == 1 then
    redis.call('RPUSH', KEYS[4], ARGV[2])
end
if redis.call('GET', KEYS[3]) == ARGV[1] then
    redis.call('DEL', KEYS[3])
end
return removed
"""


def _message_key(namespace: str, message_id: str) -> str:
    digest = hashlib.sha256(message_id.encode()).hexdigest()
    return f"wa:{{inbound}}:{namespace}:{digest}"


def _correlation(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:10]


def _thread_tag(telefono: str) -> str:
    return f"wa:{hashlib.sha256(telefono.encode()).hexdigest()}"


def _error_name(error: Exception) -> str:
    return type(error).__name__


async def _run_sync(function, *args):
    """Keep blocking Redis/worker operations off FastAPI's event loop."""
    return await asyncio.to_thread(function, *args)


def _as_text(value: bytes | str | None) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


def _enqueue_message(telefono: str, message_id: str, kind: str, data: str) -> bool:
    """Atomically deduplicate and append to the durable global FIFO."""
    item = json.dumps(
        {
            "message_id": message_id,
            "telefono": telefono,
            "kind": kind,
            "data": data,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return bool(
        r.eval(
            _ENQUEUE_LUA,
            2,
            _message_key("seen", message_id),
            _QUEUE_KEY,
            item,
            _STATE_TTL_SECONDS,
        )
    )


def _contexto(telefono: str) -> tuple[str, str]:
    """Resolve authorization internally; identifiers never enter the prompt.

    The lookup tolerates hand-typed mobile_no formats (+54 9 351 123-4567,
    0351 15 123-4567, ...) by matching in canonical form; see app/clientes.py.
    """
    # Local import: keeps main.py's import block untouched for this concern.
    from app import clientes

    cliente = clientes.buscar_por_telefono(telefono, get_list=erpnext.get_list)
    if cliente:
        return str(cliente["name"]), (
            "Cliente registrado y validado por el servidor. "
            "Podés ayudarlo con su pedido."
        )
    return "", (
        "Cliente no registrado todavía. Si hace un pedido, "
        "registralo primero con crear_lead."
    )


def _non_empty(respuesta: object, message_id: str) -> str:
    """Never hand Meta an empty body: it 400s and the item retries forever."""
    texto = str(respuesta or "")
    if texto.strip():
        return texto
    print(f"[agent] respuesta vacía msg={_correlation(message_id)}")
    return EMPTY_REPLY_FALLBACK


def _sin_tildes(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", text.lower())
        if unicodedata.category(char) != "Mn"
    ).strip()


# Deterministic manager commands: "confirmar SAL-ORD-2026-00008", "ver SO-0042".
# They reuse the same authorized button handler, so the manager can approve an
# order even when no WhatsApp template is approved yet or the LLM is down.
_ORDER_REF = r"(?P<order>[A-Za-z]{1,6}(?:-[A-Za-z]{1,6})?-\d[\w-]*)"
_STAFF_COMMAND_RE = re.compile(
    r"^\s*(?P<verb>[^\W\d_]+)\s+" + _ORDER_REF + r"\s*[.!]*\s*$"
)
_STAFF_ACTIONS = {
    "ok": "ok",
    "confirmar": "ok",
    "confirma": "ok",
    "confirmo": "ok",
    "aprobar": "ok",
    "apruebo": "ok",
    "aprobado": "ok",
    "ver": "ver",
    "detalle": "ver",
    "detalles": "ver",
    "mostrar": "ver",
}
_ORDER_IN_TEXT = re.compile(r"\b[A-Z]{2,6}(?:-[A-Z]{2,6})?-\d{2,}[0-9-]*\b")


def _staff_command(text: str) -> str | None:
    """Map a short manager message to a button payload, or None."""
    match = _STAFF_COMMAND_RE.match(text or "")
    if not match:
        return None
    action = _STAFF_ACTIONS.get(_sin_tildes(match.group("verb")))
    if not action:
        return None
    return f"{action}:{match.group('order').upper()}"


def _alert_technical_failure(error: Exception, telefono: str, data: object) -> bool:
    """Make "ya avisé al equipo" true, once per failure type per hour.

    Two channels, independent of each other: a deduplicated ERPNext ToDo (the
    durable record) and the WhatsApp exception alert (the one somebody sees
    tonight). Returns True only when at least one of them really happened, or
    a recent one exists; when both fail the hourly marker is released so the
    next failure tries again instead of hiding behind a claim.
    """
    name = _error_name(error)
    key = f"wa:{{inbound}}:tech-alert:{hashlib.sha256(name.encode()).hexdigest()[:16]}"
    try:
        fresh = bool(r.set(key, "1", nx=True, ex=_TECH_ALERT_TTL_SECONDS))
    except Exception as redis_error:
        # Cannot deduplicate: alert anyway rather than stay silent.
        print(f"[agent] tech-alert coordination type={_error_name(redis_error)}")
        fresh, key = True, ""
    if not fresh:
        return True

    alerted = False
    try:
        erpnext.create_doc(
            "ToDo",
            {
                "description": (
                    "[WhatsApp] Falla técnica atendiendo mensajes de clientes: "
                    f"{name}. Los clientes reciben una disculpa automática. "
                    "Revisar el log del agente (cuota del modelo, ERPNext, Redis)."
                ),
                "priority": "High",
            },
        )
        alerted = True
    except Exception as todo_error:
        print(f"[agent] tech-alert ToDo failed type={_error_name(todo_error)}")
    try:
        alerted = bool(notificar.avisar_falla_tecnica(telefono, str(data), name)) or alerted
    except Exception as alert_error:
        print(f"[agent] alerta al equipo falló type={_error_name(alert_error)}")

    if not alerted and key:
        try:
            r.eval(_DELETE_IF_VALUE_LUA, 1, key, "1")
        except Exception:
            pass
    return alerted


def _generate_response(item: dict) -> str:
    telefono = item["telefono"]
    message_id = item["message_id"]
    kind = item["kind"]
    data = item.get("data", "")
    thread_tag = _thread_tag(telefono)

    try:
        if kind in {"interactive", "button"}:
            return str(manejar_boton(data, telefono))
        if kind != "text":
            return TEXT_REQUIRED
        if es_equipo(telefono):
            command = _staff_command(data)
            if command:
                return str(manejar_boton(command, telefono))
            return _non_empty(
                responder_gerencia(data, thread_id=thread_tag, usuario=thread_tag),
                message_id,
            )

        customer_code, contexto = _contexto(telefono)
        return _non_empty(
            responder_cliente(
                data,
                thread_id=thread_tag,
                contexto_cliente=contexto,
                customer_code=customer_code,
                inbound_message_id=message_id,
                actor_phone=telefono,
            ),
            message_id,
        )
    except Exception as error:
        print(
            f"[agent] error msg={_correlation(message_id)} "
            f"phone={_correlation(telefono)} type={_error_name(error)}"
        )
        # TECHNICAL_ERROR_ALERTED promises the customer that the team was told.
        # Only say so when a ToDo or a WhatsApp alert really exists.
        if _alert_technical_failure(error, telefono, data):
            return TECHNICAL_ERROR_ALERTED
        return TECHNICAL_ERROR


def _acknowledge_once(telefono: str, message_id: str) -> None:
    """Best-effort normal WhatsApp acknowledgement, deduped across workers."""
    key = _message_key("ack", message_id)
    claim = uuid.uuid4().hex
    try:
        claimed = bool(r.set(key, claim, nx=True, ex=_ACK_CLAIM_TTL_SECONDS))
    except Exception as error:
        print(
            f"[queue] ack coordination phone={_correlation(telefono)} "
            f"type={_error_name(error)}"
        )
        return

    if not claimed:
        # The webhook background task and durable worker may race. Ensure a
        # fast agent cannot send its final before the in-flight ack finishes.
        deadline = time.monotonic() + _ACK_WAIT_SECONDS
        while time.monotonic() < deadline:
            try:
                state = _as_text(r.get(key))
            except Exception:
                return
            if state == "accepted_by_meta":
                return
            if state is None:
                return _acknowledge_once(telefono, message_id)
            time.sleep(0.05)
        return

    try:
        enviar_mensaje(telefono, ACK_TEXT)
    except Exception as error:
        print(
            f"[whatsapp] ack failed phone={_correlation(telefono)} "
            f"type={_error_name(error)}"
        )
        try:
            r.eval(_DELETE_IF_VALUE_LUA, 1, key, claim)
        except Exception:
            pass
        return

    try:
        r.set(key, "accepted_by_meta", ex=_STATE_TTL_SECONDS)
    except Exception as error:
        print(
            f"[queue] ack state phone={_correlation(telefono)} "
            f"type={_error_name(error)}"
        )


class _Ownership:
    """Mutable ownership shared with the lock heartbeat."""

    def __init__(self, token: str):
        self.token = token
        self.lost = threading.Event()
        self._guard = threading.Lock()
        self._item_lease: str | None = None

    def set_item_lease(self, key: str | None) -> None:
        with self._guard:
            self._item_lease = key

    def item_lease(self) -> str | None:
        with self._guard:
            return self._item_lease


def _heartbeat(ownership: _Ownership, stop: threading.Event) -> None:
    interval = max(1, _WORKER_LOCK_TTL_SECONDS // 3)
    while not stop.wait(interval):
        try:
            if not r.eval(
                _REFRESH_LUA,
                1,
                _WORKER_LOCK_KEY,
                ownership.token,
                _WORKER_LOCK_TTL_SECONDS,
            ):
                ownership.lost.set()
                return

            lease_key = ownership.item_lease()
            if lease_key and not r.eval(
                _REFRESH_LUA,
                1,
                lease_key,
                ownership.token,
                _ITEM_LEASE_TTL_SECONDS,
            ):
                ownership.lost.set()
                return
        except Exception as error:
            # Before the outbound API call the worker performs a direct
            # ownership read. A transient timeout is not proof of lock loss.
            print(f"[queue] lock refresh type={_error_name(error)}")


def _owns_worker_lock(ownership: _Ownership) -> bool:
    if ownership.lost.is_set():
        return False
    try:
        owns = _as_text(r.get(_WORKER_LOCK_KEY)) == ownership.token
    except Exception as error:
        print(f"[queue] lock check type={_error_name(error)}")
        return False
    if not owns:
        ownership.lost.set()
    return owns


def _release_owned(key: str, token: str) -> None:
    try:
        r.eval(_DELETE_IF_VALUE_LUA, 1, key, token)
    except Exception as error:
        print(f"[queue] lock release type={_error_name(error)}")


def _claim_pending() -> bytes | str | None:
    """Recover a crash-pending item first, otherwise atomically move FIFO head."""
    pending = r.lindex(_PROCESSING_KEY, 0)
    if pending is not None:
        return pending
    return r.lmove(_QUEUE_KEY, _PROCESSING_KEY, "LEFT", "RIGHT")


def _cached_result(message_id: str) -> tuple[str | None, bool]:
    """Return (response, durably_cached).

    A Redis read failure is allowed to bubble up: treating it as a cache miss
    could rerun an agent whose response is already stored.
    """
    cached = _as_text(r.get(_message_key("final", message_id)))
    if cached is not None:
        return cached, True
    with _volatile_guard:
        return _volatile_results.get(message_id), False


def _cache_result(message_id: str, response: str) -> bool:
    with _volatile_guard:
        _volatile_results[message_id] = response
    try:
        r.set(
            _message_key("final", message_id),
            response,
            ex=_STATE_TTL_SECONDS,
        )
        return True
    except Exception as error:
        print(
            f"[queue] final cache msg={_correlation(message_id)} "
            f"type={_error_name(error)}"
        )
        return False


def _outbound_id(send_result: object) -> str | None:
    if not isinstance(send_result, dict):
        return None
    messages = send_result.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    first = messages[0]
    if not isinstance(first, dict):
        return None
    outbound_id = first.get("id")
    return outbound_id if isinstance(outbound_id, str) and outbound_id else None


def _persist_accepted(message_id: str, outbound_id: str | None) -> bool:
    try:
        if outbound_id:
            record_outbound(
                outbound_id,
                "agent_final",
                inbound_message_id=message_id,
            )
        else:
            r.set(
                _message_key("accepted", message_id),
                "accepted_by_meta",
                ex=_STATE_TTL_SECONDS,
            )
        return True
    except Exception as error:
        print(
            f"[queue] acceptance state msg={_correlation(message_id)} "
            f"type={_error_name(error)}"
        )
        return False


def _is_accepted(message_id: str) -> bool:
    with _volatile_guard:
        local_outbound_id = _volatile_accepted.get(message_id)
        locally_accepted = message_id in _volatile_accepted
    if locally_accepted:
        # A previous API send succeeded but persisting its marker failed.
        # Persist that fact before allowing the pending entry to be removed.
        if not _persist_accepted(message_id, local_outbound_id):
            raise RuntimeError("acceptance marker unavailable")
        return True

    try:
        return r.get(_message_key("accepted", message_id)) is not None
    except Exception as error:
        print(
            f"[queue] acceptance read msg={_correlation(message_id)} "
            f"type={_error_name(error)}"
        )
        raise


def _record_accepted(message_id: str, send_result: object) -> bool:
    outbound_id = _outbound_id(send_result)
    with _volatile_guard:
        _volatile_accepted[message_id] = outbound_id
    return _persist_accepted(message_id, outbound_id)


def _complete_pending(
    raw: bytes | str,
    lease_key: str,
    ownership: _Ownership,
) -> bool:
    try:
        result = int(
            r.eval(
                _COMPLETE_LUA,
                3,
                _WORKER_LOCK_KEY,
                _PROCESSING_KEY,
                lease_key,
                ownership.token,
                raw,
            )
        )
    except Exception as error:
        print(f"[queue] completion type={_error_name(error)}")
        return False
    return result == 1


def _send_error_is_permanent(error: Exception) -> bool:
    """Decide whether retrying the very same send could ever succeed.

    ``app.whatsapp`` already classifies its own errors (``permanent``); the
    fallbacks cover raw httpx errors and unknown exceptions, which are retried
    within the bounded budget rather than dropped.
    """
    flagged = getattr(error, "permanent", None)
    if isinstance(flagged, bool):
        return flagged
    if isinstance(error, (httpx.TimeoutException, httpx.TransportError)):
        return False
    status = getattr(error, "status_code", None)
    if not isinstance(status, int):
        status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int):
        if status == 429 or status >= 500:
            return False
        return getattr(error, "error_code", None) not in _TRANSIENT_META_CODES
    return False


def _send_error_detail(error: Exception) -> str:
    """Status and Meta code only: never recipient data or response bodies."""
    status = getattr(error, "status_code", None)
    if not isinstance(status, int):
        status = getattr(getattr(error, "response", None), "status_code", None)
    code = getattr(error, "error_code", None)
    if isinstance(status, int):
        return f"HTTP {status}, código {code if code is not None else 'desconocido'}"
    return _error_name(error)


def _retry_delay_seconds(error: Exception, attempts: int) -> float:
    """Bounded exponential backoff; a Retry-After hint can only lengthen it."""
    delay = min(_RETRY_MAX_SECONDS, _RETRY_SECONDS * (2 ** max(0, attempts - 1)))
    retry_after = getattr(error, "retry_after", None)
    try:
        if retry_after is not None:
            delay = min(_RETRY_MAX_SECONDS, max(delay, float(retry_after)))
    except (TypeError, ValueError):
        pass
    return delay


def _set_retry_hint(seconds: float) -> None:
    global _retry_hint
    with _retry_hint_guard:
        _retry_hint = seconds


def _take_retry_hint() -> float:
    """Delay before the next worker cycle after a retryable outcome."""
    global _retry_hint
    with _retry_hint_guard:
        hint, _retry_hint = _retry_hint, None
    return hint if hint is not None else _RETRY_SECONDS


def _note_send_failure(message_id: str) -> int:
    """Count consecutive rejected final sends; the worker lock serializes it."""
    key = _message_key("send-attempts", message_id)
    try:
        attempts = int(_as_text(r.get(key)) or 0) + 1
        r.set(key, str(attempts), ex=_STATE_TTL_SECONDS)
        return attempts
    except Exception as error:
        print(
            f"[queue] attempt counter msg={_correlation(message_id)} "
            f"type={_error_name(error)}"
        )
        # An unknown count must never dead-letter a message: 0 is below any cap.
        return 0


def _audit_undelivered(response: str, reason: str) -> None:
    """If the undeliverable reply named an ERPNext order, leave a trace on it."""
    for name in sorted(set(_ORDER_IN_TEXT.findall(response or ""))):
        try:
            erpnext.add_comment(
                "Sales Order",
                name,
                f"WhatsApp no aceptó la respuesta al cliente ({reason}); "
                "el cliente NO recibió el número de pedido por este canal. "
                "Requiere contacto manual.",
            )
        except Exception as error:
            print(f"[queue] dead-letter audit type={_error_name(error)}")


def _dead_letter(
    raw: bytes | str,
    lease_key: str,
    ownership: _Ownership,
    message_id: str,
    telefono: str,
    response: str,
    attempts: int,
    reason: str,
) -> bool:
    """Atomically move the head item to the dead-letter list. True if parked."""
    try:
        result = int(
            r.eval(
                _DEAD_LETTER_LUA,
                4,
                _WORKER_LOCK_KEY,
                _PROCESSING_KEY,
                lease_key,
                _DEAD_KEY,
                ownership.token,
                raw,
            )
        )
    except Exception as error:
        print(f"[queue] dead-letter type={_error_name(error)}")
        return False
    if result != 1:
        return False
    with _volatile_guard:
        _volatile_results.pop(message_id, None)
        _volatile_accepted.pop(message_id, None)
    print(
        f"[queue] dead-letter msg={_correlation(message_id)} "
        f"phone={_correlation(telefono)} attempts={attempts} motivo={reason}; "
        f"la respuesta quedó en {_DEAD_KEY} para revisión manual"
    )
    _audit_undelivered(response, reason)
    return True


def _parse_item(raw: bytes | str) -> dict | None:
    try:
        item = json.loads(raw)
        for field in ("message_id", "telefono", "kind"):
            if not isinstance(item.get(field), str) or not item[field]:
                raise ValueError(f"missing {field}")
        return item
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        # Only our atomic enqueue writes this list. Keep corrupt data pending
        # for an operator instead of destructively discarding an unknown event.
        print(f"[queue] invalid item type={_error_name(error)} retained")
        return None


def _handle_pending(
    raw: bytes | str,
    ownership: _Ownership,
    stop: threading.Event,
) -> str:
    """Handle one pending entry. Returns done/retry/blocked/lost."""
    item = _parse_item(raw)
    if item is None:
        return "blocked"

    message_id = item["message_id"]
    telefono = item["telefono"]
    lease_key = _message_key("lease", message_id)

    try:
        accepted = _is_accepted(message_id)
    except Exception:
        return "retry"

    if accepted:
        return "done" if _complete_pending(raw, lease_key, ownership) else "retry"

    try:
        response, response_is_durable = _cached_result(message_id)
    except Exception:
        return "retry"

    if response is None:
        try:
            claimed = bool(
                r.set(
                    lease_key,
                    ownership.token,
                    nx=True,
                    ex=_ITEM_LEASE_TTL_SECONDS,
                )
            )
        except Exception as error:
            print(
                f"[queue] item claim msg={_correlation(message_id)} "
                f"type={_error_name(error)}"
            )
            return "retry"
        if not claimed:
            return "blocked"

        ownership.set_item_lease(lease_key)
        if stop.is_set() or not _owns_worker_lock(ownership):
            _release_owned(lease_key, ownership.token)
            ownership.set_item_lease(None)
            return "lost"

        if item["kind"] == "text":
            _acknowledge_once(telefono, message_id)
        response = _generate_response(item)

        # Persist the final before any send. On a lock loss, its successor can
        # send this exact response without invoking the agent again.
        if not _cache_result(message_id, response):
            _release_owned(lease_key, ownership.token)
            ownership.set_item_lease(None)
            return "retry"
    else:
        if not response_is_durable and not _cache_result(message_id, response):
            return "retry"
        # A cached final fences agent execution. Still respect the item lease:
        # an old worker may have lost its global lock while its Meta POST is
        # already in flight. Waiting for that lease prevents a duplicate send.
        try:
            claimed = bool(
                r.set(
                    lease_key,
                    ownership.token,
                    nx=True,
                    ex=_ITEM_LEASE_TTL_SECONDS,
                )
            )
        except Exception as error:
            print(
                f"[queue] item lease msg={_correlation(message_id)} "
                f"type={_error_name(error)}"
            )
            return "retry"
        if not claimed:
            return "blocked"
        ownership.set_item_lease(lease_key)
        if item["kind"] == "text":
            _acknowledge_once(telefono, message_id)

    if stop.is_set() or not _owns_worker_lock(ownership):
        _release_owned(lease_key, ownership.token)
        ownership.set_item_lease(None)
        return "lost"

    try:
        send_result = enviar_mensaje(telefono, response)
    except Exception as error:
        permanent = _send_error_is_permanent(error)
        attempts = _note_send_failure(message_id)
        print(
            f"[whatsapp] final pending msg={_correlation(message_id)} "
            f"phone={_correlation(telefono)} type={_error_name(error)} "
            f"{'permanente' if permanent else 'transitorio'} "
            f"attempt={attempts}/{_SEND_MAX_ATTEMPTS}"
        )
        if permanent:
            reason = f"rechazo definitivo de Meta: {_send_error_detail(error)}"
        elif attempts >= _SEND_MAX_ATTEMPTS:
            reason = (
                f"{attempts} intentos con reintentos agotados: "
                f"{_send_error_detail(error)}"
            )
        else:
            reason = None
        if reason and _dead_letter(
            raw, lease_key, ownership, message_id, telefono, response, attempts, reason
        ):
            ownership.set_item_lease(None)
            return "done"
        _set_retry_hint(_retry_delay_seconds(error, attempts))
        _release_owned(lease_key, ownership.token)
        ownership.set_item_lease(None)
        return "retry"

    # A successful API response means Meta accepted the send; actual delivery
    # arrives later as a webhook status. Retain the item until this acceptance
    # marker is durable. Its local copy prevents an API resend in this process.
    if not _record_accepted(message_id, send_result):
        _release_owned(lease_key, ownership.token)
        ownership.set_item_lease(None)
        return "retry"

    completed = _complete_pending(raw, lease_key, ownership)
    ownership.set_item_lease(None)
    if completed:
        with _volatile_guard:
            _volatile_results.pop(message_id, None)
            _volatile_accepted.pop(message_id, None)
        print(
            f"[agent] accepted msg={_correlation(message_id)} "
            f"phone={_correlation(telefono)}"
        )
        return "done"
    return "retry"


def _worker_cycle(stop: threading.Event | None = None) -> str:
    """Acquire the global worker lease and drain in strict FIFO order."""
    stop = stop or threading.Event()
    token = uuid.uuid4().hex
    try:
        acquired = bool(
            r.set(
                _WORKER_LOCK_KEY,
                token,
                nx=True,
                ex=_WORKER_LOCK_TTL_SECONDS,
            )
        )
    except Exception as error:
        print(f"[queue] worker lock type={_error_name(error)}")
        return "retry"
    if not acquired:
        return "busy"

    ownership = _Ownership(token)
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat,
        args=(ownership, heartbeat_stop),
        daemon=True,
        name="whatsapp-worker-heartbeat",
    )
    heartbeat.start()

    outcome = "idle"
    try:
        while not stop.is_set() and not ownership.lost.is_set():
            try:
                raw = _claim_pending()
            except Exception as error:
                print(f"[queue] claim type={_error_name(error)}")
                return "retry"
            if raw is None:
                return outcome

            result = _handle_pending(raw, ownership, stop)
            if result != "done":
                return result
            outcome = "worked"
        return "lost" if ownership.lost.is_set() else outcome
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=1)
        lease_key = ownership.item_lease()
        if lease_key:
            _release_owned(lease_key, token)
        _release_owned(_WORKER_LOCK_KEY, token)


def _worker_supervisor(stop: threading.Event) -> None:
    """Persistent crash-recovery loop started by the FastAPI lifespan."""
    while not stop.is_set():
        _worker_wake.clear()
        outcome = _worker_cycle(stop)
        if stop.is_set():
            break
        if outcome in {"retry", "blocked", "lost"}:
            stop.wait(_take_retry_hint())
        else:
            _worker_wake.wait(_WORKER_POLL_SECONDS)


def _config_warnings() -> list[str]:
    """Configuration gaps that silently disable part of the order flow."""
    warnings: list[str] = []
    if not os.getenv("TELEFONOS_EQUIPO", "").strip():
        warnings.append(
            "TELEFONOS_EQUIPO vacío: no hay agente de gestión, nadie recibe "
            "alertas y nadie puede confirmar pedidos por WhatsApp"
        )
    for variable, efecto in (
        ("WHATSAPP_STAFF_PENDING_TEMPLATE", "la alerta de pedido pendiente"),
        ("WHATSAPP_STAFF_CONFIRMED_TEMPLATE", "la alerta de pedido confirmado"),
        ("WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE", "el aviso de confirmación al cliente"),
    ):
        if not os.getenv(variable, "").strip():
            warnings.append(
                f"{variable} vacío: {efecto} solo sale como mensaje libre si el "
                "destinatario escribió en las últimas 24 h"
            )
    if (
        not os.getenv("AUTO_CONFIRM_PRICE_LIST", "").strip()
        or not os.getenv("AUTO_CONFIRM_CURRENCY", "").strip()
    ):
        warnings.append(
            "AUTO_CONFIRM_PRICE_LIST/AUTO_CONFIRM_CURRENCY vacíos: el agente "
            "responde 'precio a confirmar' a toda consulta de precio"
        )
    return warnings


def _startup_checks() -> None:
    """Log, never raise: a misconfigured agent must still answer customers."""
    for warning in _config_warnings():
        print(f"[config] WARN {warning}")
    verify = getattr(whatsapp_client, "verificar_credenciales", None)
    if verify is None:
        return
    try:
        ok, detail = verify()
    except Exception as error:
        print(f"[whatsapp] verificación de credenciales falló type={_error_name(error)}")
        return
    print(f"[whatsapp] {'OK' if ok else 'ERROR'} {detail}")


@asynccontextmanager
async def _lifespan(application: FastAPI):
    threading.Thread(target=_startup_checks, daemon=True, name="startup-checks").start()
    stop = threading.Event()
    worker = threading.Thread(
        target=_worker_supervisor,
        args=(stop,),
        daemon=True,
        name="whatsapp-durable-worker",
    )
    application.state.worker_stop = stop
    application.state.worker_thread = worker
    worker.start()
    _worker_wake.set()
    try:
        yield
    finally:
        stop.set()
        _worker_wake.set()
        await _run_sync(worker.join, 5)


app = FastAPI(title="Plus Agent", lifespan=_lifespan)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/webhook/whatsapp")
def verify(request: Request):
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == VERIFY_TOKEN
    ):
        return Response(content=params.get("hub.challenge"), media_type="text/plain")
    raise HTTPException(403, "verify token mismatch")


def _valid_signature(body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


async def _limited_body(request: Request) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "webhook body too large")
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/webhook/whatsapp")
async def inbound(request: Request, background: BackgroundTasks):
    body = await _limited_body(request)
    if not _valid_signature(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(403, "bad signature")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(400, "invalid json") from error
    if not isinstance(payload, dict):
        raise HTTPException(400, "invalid payload")

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for status_event in value.get("statuses", []):
                outbound_id = status_event.get("id", "")
                outbound_status = status_event.get("status", "")
                if not outbound_id or not outbound_status:
                    continue
                try:
                    await _run_sync(
                        update_status,
                        outbound_id,
                        outbound_status,
                    )
                except Exception as error:
                    print("[queue] status persistence " f"type={_error_name(error)}")
                    raise HTTPException(503, "queue unavailable") from error

            for msg in value.get("messages", []):
                message_id = msg.get("id", "")
                telefono = msg.get("from", "")
                if not message_id or not telefono:
                    print("[webhook] mensaje sin id o remitente ignorado")
                    continue

                tipo = msg.get("type")
                if tipo == "interactive":
                    kind = "interactive"
                    data = (
                        msg.get("interactive", {}).get("button_reply", {}).get("id", "")
                    )
                elif tipo == "button":
                    kind = "button"
                    data = msg.get("button", {}).get("payload", "")
                elif tipo == "text" and msg.get("text", {}).get("body"):
                    kind = "text"
                    data = msg["text"]["body"]
                else:
                    kind = "unsupported"
                    data = tipo or "unknown"

                try:
                    accepted = await _run_sync(
                        _enqueue_message,
                        telefono,
                        message_id,
                        kind,
                        data,
                    )
                except Exception as error:
                    # Nothing was acknowledged unless the atomic script fully
                    # committed; Meta can safely retry the whole webhook.
                    print(
                        f"[queue] enqueue msg={_correlation(message_id)} "
                        f"type={_error_name(error)}"
                    )
                    raise HTTPException(503, "queue unavailable") from error

                if not accepted:
                    print(f"[webhook] duplicate msg={_correlation(message_id)}")
                    continue

                # Any inbound opens the sender's 24-hour free-form window.
                try:
                    await _run_sync(record_inbound_window, telefono)
                except Exception as error:
                    print(
                        f"[queue] window marker phone={_correlation(telefono)} "
                        f"type={_error_name(error)}"
                    )

                print(
                    f"[webhook] type={kind} "
                    f"msg={_correlation(message_id)} "
                    f"phone={_correlation(telefono)}"
                )
                if kind == "text":
                    background.add_task(_acknowledge_once, telefono, message_id)
                _worker_wake.set()

    return {"status": "ok"}
