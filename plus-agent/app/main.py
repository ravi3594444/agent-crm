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
import threading
import time
import uuid
from contextlib import asynccontextmanager

import redis
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response

from app import erpnext
from app.aprobacion import manejar_boton
from app.graph import responder_cliente, responder_gerencia
from app.outbound_status import record_outbound, update_status
from app.router import es_equipo
from app.whatsapp import enviar_mensaje

APP_SECRET = os.environ["META_APP_SECRET"]
VERIFY_TOKEN = os.environ["META_VERIFY_TOKEN"]

ACK_TEXT = "Recibido, dame un momento mientras lo verifico."
TEXT_REQUIRED = (
    "Por ahora necesito que me escribas el pedido en texto para poder ayudarte."
)
TECHNICAL_ERROR = (
    "Perdón, tuve un problema técnico. Ya avisé al equipo y te responden en un rato."
)

MAX_WEBHOOK_BYTES = max(1_024, int(os.getenv("WHATSAPP_WEBHOOK_MAX_BYTES", "1048576")))
_STATE_TTL_SECONDS = 30 * 24 * 60 * 60
_WORKER_LOCK_TTL_SECONDS = 90
_ITEM_LEASE_TTL_SECONDS = 90
_WORKER_POLL_SECONDS = 1.0
_RETRY_SECONDS = 2.0
_ACK_CLAIM_TTL_SECONDS = 30
_ACK_WAIT_SECONDS = 16

# All queue keys share a Redis Cluster hash slot. This deployment uses a
# single container, but keeping the transaction cluster-safe costs nothing.
_QUEUE_KEY = "wa:{inbound}:queue"
_PROCESSING_KEY = "wa:{inbound}:processing"
_WORKER_LOCK_KEY = "wa:{inbound}:worker-lock"

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


def _alternate_phone_numbers(telefono: str) -> list[str]:
    normalized = telefono.strip()
    without_plus = normalized.lstrip("+")
    candidates = [normalized]
    alternate = without_plus if normalized.startswith("+") else f"+{without_plus}"
    if alternate and alternate not in candidates:
        candidates.append(alternate)
    return candidates


def _contexto(telefono: str) -> tuple[str, str]:
    """Resolve authorization internally; identifiers never enter the prompt."""
    for candidate in _alternate_phone_numbers(telefono):
        clientes = erpnext.get_list(
            "Customer",
            filters=[["mobile_no", "=", candidate]],
            fields=["name"],
            limit=1,
        )
        if clientes:
            return str(clientes[0]["name"]), (
                "Cliente registrado y validado por el servidor. "
                "Podés ayudarlo con su pedido."
            )
    return "", (
        "Cliente no registrado todavía. Si hace un pedido, "
        "registralo primero con crear_lead."
    )


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
            return str(
                responder_gerencia(data, thread_id=thread_tag, usuario=thread_tag)
            )

        customer_code, contexto = _contexto(telefono)
        return str(
            responder_cliente(
                data,
                thread_id=thread_tag,
                contexto_cliente=contexto,
                customer_code=customer_code,
                inbound_message_id=message_id,
                actor_phone=telefono,
            )
        )
    except Exception as error:  # noqa: BLE001
        print(
            f"[agent] error msg={_correlation(message_id)} "
            f"phone={_correlation(telefono)} type={_error_name(error)}"
        )
        return TECHNICAL_ERROR


def _acknowledge_once(telefono: str, message_id: str) -> None:
    """Best-effort normal WhatsApp acknowledgement, deduped across workers."""
    key = _message_key("ack", message_id)
    claim = uuid.uuid4().hex
    try:
        claimed = bool(r.set(key, claim, nx=True, ex=_ACK_CLAIM_TTL_SECONDS))
    except Exception as error:  # noqa: BLE001
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
            except Exception:  # noqa: BLE001
                return
            if state == "accepted_by_meta":
                return
            if state is None:
                return _acknowledge_once(telefono, message_id)
            time.sleep(0.05)
        return

    try:
        enviar_mensaje(telefono, ACK_TEXT)
    except Exception as error:  # noqa: BLE001
        print(
            f"[whatsapp] ack failed phone={_correlation(telefono)} "
            f"type={_error_name(error)}"
        )
        try:
            r.eval(_DELETE_IF_VALUE_LUA, 1, key, claim)
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        r.set(key, "accepted_by_meta", ex=_STATE_TTL_SECONDS)
    except Exception as error:  # noqa: BLE001
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
        except Exception as error:  # noqa: BLE001
            # Before the outbound API call the worker performs a direct
            # ownership read. A transient timeout is not proof of lock loss.
            print(f"[queue] lock refresh type={_error_name(error)}")


def _owns_worker_lock(ownership: _Ownership) -> bool:
    if ownership.lost.is_set():
        return False
    try:
        owns = _as_text(r.get(_WORKER_LOCK_KEY)) == ownership.token
    except Exception as error:  # noqa: BLE001
        print(f"[queue] lock check type={_error_name(error)}")
        return False
    if not owns:
        ownership.lost.set()
    return owns


def _release_owned(key: str, token: str) -> None:
    try:
        r.eval(_DELETE_IF_VALUE_LUA, 1, key, token)
    except Exception as error:  # noqa: BLE001
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
    except Exception as error:  # noqa: BLE001
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
    except Exception as error:  # noqa: BLE001
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
    except Exception as error:  # noqa: BLE001
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
    except Exception as error:  # noqa: BLE001
        print(f"[queue] completion type={_error_name(error)}")
        return False
    return result == 1


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
    except Exception:  # noqa: BLE001
        return "retry"

    if accepted:
        return "done" if _complete_pending(raw, lease_key, ownership) else "retry"

    try:
        response, response_is_durable = _cached_result(message_id)
    except Exception:  # noqa: BLE001
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
        except Exception as error:  # noqa: BLE001
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
        except Exception as error:  # noqa: BLE001
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
    except Exception as error:  # noqa: BLE001
        print(
            f"[whatsapp] final pending msg={_correlation(message_id)} "
            f"phone={_correlation(telefono)} type={_error_name(error)}"
        )
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
    except Exception as error:  # noqa: BLE001
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
            except Exception as error:  # noqa: BLE001
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
            stop.wait(_RETRY_SECONDS)
        else:
            _worker_wake.wait(_WORKER_POLL_SECONDS)


@asynccontextmanager
async def _lifespan(application: FastAPI):
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
                except Exception as error:  # noqa: BLE001
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
                except Exception as error:  # noqa: BLE001
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

                print(
                    f"[webhook] type={kind} "
                    f"msg={_correlation(message_id)} "
                    f"phone={_correlation(telefono)}"
                )
                if kind == "text":
                    background.add_task(_acknowledge_once, telefono, message_id)
                _worker_wake.set()

    return {"status": "ok"}
