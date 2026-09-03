"""Lightweight outbound WhatsApp correlation and status tracking.

This module deliberately does not import the agent graph or webhook app, so
notification jobs can record sends without initializing models/checkpointers.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from typing import Any

import redis

STATE_TTL_SECONDS = 30 * 24 * 60 * 60
_client: Any | None = None

_RECORD_LUA = """
redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
if redis.call('EXISTS', KEYS[2]) == 0 then
    redis.call('SET', KEYS[2], 'accepted_by_meta', 'EX', tonumber(ARGV[2]))
end
if KEYS[3] ~= KEYS[1] then
    redis.call('SET', KEYS[3], '1', 'EX', tonumber(ARGV[2]))
end
if KEYS[4] ~= KEYS[1] then
    redis.call('SET', KEYS[4], ARGV[3], 'EX', tonumber(ARGV[2]))
end
return redis.call('GET', KEYS[2])
"""


def _redis():
    global _client
    if _client is None:
        _client = redis.from_url(
            os.environ["REDIS_URL"],
            socket_connect_timeout=2.0,
            socket_timeout=5.0,
            health_check_interval=30,
            retry_on_timeout=True,
        )
    return _client


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _business_key(order_name: str, purpose: str) -> str:
    correlation = purpose + "\0" + order_name
    return f"wa:{{inbound}}:accepted-business:{_digest(correlation)}"


def record_outbound(
    wamid: str,
    purpose: str,
    order_name: str | None = None,
    inbound_message_id: str | None = None,
) -> None:
    """Record an outbound message after Meta accepts its API request."""
    if not wamid or not purpose:
        raise ValueError("wamid and purpose are required")

    outbound_digest = _digest(wamid)
    metadata = json.dumps(
        {
            "purpose": purpose[:80],
            "order_name": order_name or "",
            "inbound_hash": _digest(inbound_message_id) if inbound_message_id else "",
        },
        separators=(",", ":"),
    )
    mapping_key = f"wa:{{inbound}}:outbound:{outbound_digest}"
    status_key = f"wa:{{inbound}}:status:{outbound_digest}"
    business_key = _business_key(order_name, purpose) if order_name else mapping_key
    inbound_key = (
        f"wa:{{inbound}}:accepted:{_digest(inbound_message_id)}"
        if inbound_message_id
        else mapping_key
    )
    client = _redis()
    current_status = client.eval(
        _RECORD_LUA,
        4,
        mapping_key,
        status_key,
        business_key,
        inbound_key,
        metadata,
        STATE_TTL_SECONDS,
        outbound_digest,
    )
    if isinstance(current_status, bytes):
        current_status = current_status.decode()
    if current_status == "failed":
        _audit_failed(client, outbound_digest, metadata)


# Meta allows free-form messages only inside the 24-hour customer-service
# window opened by the recipient's own last inbound message. The window is
# tracked per hashed phone so alerts can legitimately fall back to free-form
# text when a template is not yet approved.
WINDOW_TTL_SECONDS = 23 * 60 * 60


def _window_key(phone: str) -> str:
    return f"wa:{{inbound}}:window:{_digest(phone.strip().lstrip('+'))}"


def record_inbound_window(phone: str) -> None:
    """Remember that ``phone`` messaged us; opens its free-form window."""
    if not phone or not phone.strip():
        return
    _redis().set(_window_key(phone), "1", ex=WINDOW_TTL_SECONDS)


def window_open(phone: str) -> bool:
    """Whether ``phone`` wrote to us recently enough for a free-form reply.

    Fails closed: any Redis problem reads as "window closed".
    """
    if not phone or not phone.strip():
        return False
    try:
        return _redis().get(_window_key(phone)) is not None
    except Exception:
        return False


def has_accepted(order_name: str, purpose: str) -> bool:
    """Whether Meta already accepted this order/purpose notification."""
    if not order_name or not purpose:
        return False
    return _redis().get(_business_key(order_name, purpose)) is not None


def update_status(
    wamid: str,
    status: str,
    *,
    audit_comment: Callable[[str, str, str], None] | None = None,
) -> None:
    """Persist a Meta status and audit terminal failures for mapped orders."""
    if status not in {"sent", "delivered", "read", "failed"}:
        return
    outbound_digest = _digest(wamid)
    client = _redis()
    mapping_key = f"wa:{{inbound}}:outbound:{outbound_digest}"
    client.set(
        f"wa:{{inbound}}:status:{outbound_digest}",
        status,
        ex=STATE_TTL_SECONDS,
    )

    if status == "failed":
        _audit_failed(client, outbound_digest, client.get(mapping_key), audit_comment)

    print(f"[whatsapp] status outbound={outbound_digest[:10]} state={status}")


def _audit_failed(
    client,
    outbound_digest: str,
    raw_metadata,
    audit_comment: Callable[[str, str, str], None] | None = None,
) -> None:
    """Create one manual-follow-up audit once business mapping is available."""
    if isinstance(raw_metadata, bytes):
        raw_metadata = raw_metadata.decode()
    try:
        metadata = json.loads(raw_metadata) if raw_metadata else {}
    except (json.JSONDecodeError, TypeError):
        metadata = {}
    order_name = metadata.get("order_name")
    audit_key = f"wa:{{inbound}}:failed-audit:{outbound_digest}"
    if not order_name or not client.set(audit_key, "1", nx=True, ex=STATE_TTL_SECONDS):
        return
    if audit_comment is None:
        from app import erpnext

        audit_comment = erpnext.add_comment
    try:
        audit_comment(
            "Sales Order",
            order_name,
            "Meta informó que un mensaje de WhatsApp relacionado "
            "con este pedido no pudo entregarse. Requiere "
            "seguimiento manual.",
        )
    except Exception:
        client.delete(audit_key)
        raise


# ---------------------------------------------------------------------------
# Stage 2e — exactly-once claims, parked notifications, digest counters.
# ---------------------------------------------------------------------------

DEAD_NOTIFY_KEY = "wa:{inbound}:dead-notify"
NOTIFY_TODO_TTL_SECONDS = 24 * 60 * 60


def claim_once(name: str, ttl_seconds: int) -> bool:
    """First caller wins for ``ttl_seconds``; everybody else gets False.

    Raises on a Redis failure: the caller decides whether that means "skip"
    or "notify anyway", and it must never look like a successful claim.
    """
    return bool(
        _redis().set(f"wa:{{inbound}}:once:{_digest(name)}", "1", nx=True, ex=ttl_seconds)
    )


def release_claim(name: str) -> None:
    """Give a claim back when the claimed action did not actually happen."""
    try:
        _redis().delete(f"wa:{{inbound}}:once:{_digest(name)}")
    except Exception as exc:
        print(f"[queue] release claim type={type(exc).__name__}")


def registrar_aviso_fallido(
    purpose: str, order_name: str, resumen: str, destinatario_tag: str = ""
) -> bool:
    """A notification nobody received: park it, and open ONE ERPNext ToDo per
    purpose and order per day so a person follows up.

    Returns True when a ToDo exists for it (fresh or from earlier today), so
    callers can be honest about whether anyone will see the problem. The parked
    entry carries no phone number: only a hashed recipient tag.
    """
    entry = json.dumps(
        {
            "purpose": purpose[:80],
            "order_name": order_name or "",
            "resumen": (resumen or "")[:300],
            "destinatario": destinatario_tag[:16],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    client = None
    try:
        client = _redis()
        client.rpush(DEAD_NOTIFY_KEY, entry)
    except Exception as exc:
        print(f"[notify] dead-letter no disponible type={type(exc).__name__}")

    todo_key = f"wa:{{inbound}}:notify-todo:{_digest(purpose + chr(0) + (order_name or ''))}"
    fresh = True
    if client is not None:
        try:
            fresh = bool(client.set(todo_key, "1", nx=True, ex=NOTIFY_TODO_TTL_SECONDS))
        except Exception as exc:
            print(f"[notify] dedupe no disponible type={type(exc).__name__}")
            fresh = True
    if not fresh:
        return True

    from app import erpnext

    payload: dict = {
        "description": (
            f"[WhatsApp] Aviso no entregado ({purpose})"
            + (f" para {order_name}" if order_name else "")
            + f": {(resumen or '')[:300]}. Nadie lo recibió por WhatsApp; revisar la lista "
            f"{DEAD_NOTIFY_KEY} y contactar manualmente."
        ),
        "priority": "High",
    }
    if order_name:
        payload["reference_type"] = "Sales Order"
        payload["reference_name"] = order_name
    try:
        erpnext.create_doc("ToDo", payload)
        return True
    except Exception as exc:
        print(f"[notify] ToDo de aviso fallido no creado type={type(exc).__name__}")
        if client is not None:
            try:
                client.delete(todo_key)
            except Exception:
                pass
        return False


def contar_pendientes() -> dict:
    """Counts for the daily digest. None means "could not read"."""
    counts: dict = {
        "respuestas_en_dead_letter": None,
        "avisos_en_dead_letter": None,
        "entregas_fallidas": None,
    }
    try:
        client = _redis()
        counts["respuestas_en_dead_letter"] = int(client.llen("wa:{inbound}:dead"))
        counts["avisos_en_dead_letter"] = int(client.llen(DEAD_NOTIFY_KEY))
        fallidas = 0
        for _ in client.scan_iter(match="wa:{inbound}:failed-audit:*", count=500):
            fallidas += 1
        counts["entregas_fallidas"] = fallidas
    except Exception as exc:
        print(f"[notify] contadores no disponibles type={type(exc).__name__}")
    return counts


# ---------------------------------------------------------------------------
# The "customer already read it in the chat" flag, and shared Redis access.
#
# The confirmation TIMESTAMP is NOT here any more. Redis is a cache of it and
# cannot be its source of truth: a flush, an eviction or a restart with a fresh
# volume silently closed a cancellation window the business still had.
# app/confirmacion.py owns the durable ERPNext record and uses this module only
# for the cache.
# ---------------------------------------------------------------------------

CONFIRMACION_TTL_SEGUNDOS = 7 * 24 * 60 * 60


def _clave_cliente_informado(order_name: str) -> str:
    return f"wa:{{inbound}}:cliente-informado:{_digest(order_name)}"


def marcar_cliente_informado(order_name: str) -> None:
    """Say the customer already read the confirmation in the conversation."""
    if not order_name:
        return
    try:
        _redis().set(
            _clave_cliente_informado(order_name), "1", ex=CONFIRMACION_TTL_SEGUNDOS
        )
    except Exception as exc:
        print(f"[notify] marca de cliente informado no guardada type={type(exc).__name__}")


def cliente_informado(order_name: str) -> bool:
    """Whether the customer already received the confirmation in the conversation."""
    if not order_name:
        return False
    try:
        return _redis().get(_clave_cliente_informado(order_name)) is not None
    except Exception:
        return False


def cliente():
    """The same Redis this module uses, for the confirmation cache.

    One client, one place tests patch (``outbound_status._client``), so a test
    can never accidentally reach a real server through a second connection.
    """
    return _redis()


def digest_recipiente(value: str) -> str:
    """Stable non-reversible tag; never store or log a phone number itself."""
    return _digest(value)
