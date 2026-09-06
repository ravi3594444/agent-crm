"""WHEN this system confirmed an order — durably, so a restart cannot lie.

THE BUG THIS FIXES
``cancelar <pedido> <motivo>`` may only cancel within CANCELACION_HORAS of the
confirmation. That deadline used to live in one Redis string with a seven-day
TTL. Redis is the right place for a cache and the wrong place for the fact: a
flush, an eviction, a restart with a fresh volume or simply a different Redis
URL made the timestamp disappear, and the code then read "this system did not
confirm it" and refused a cancellation the business was perfectly entitled to
make. The opposite failure is worse: a Redis somebody repopulated by hand would
have re-opened a window that had closed days earlier.

WHERE THE FACT LIVES NOW
An ERPNext Comment on the Sales Order, carrying MARCA and an explicit UTC
timestamp:

    [confirmado-por-agente] 2026-09-03T11:22:33+00:00 fuente=automática (política)

Comments are append-only in practice and are already this system's audit trail
(app/limites.py records limit changes the same way). ERPNext is the same system
of record that holds the order itself, so the deadline and the document it
applies to can never drift apart, survive any application or Redis restart, and
are visible to a person reading the order.

Redis keeps a cache of the parsed value to avoid a read per cancellation
attempt. It is only ever written AFTER a durable record exists or FROM one, so
it cannot invent a window of its own.

FAIL CLOSED
An order submitted directly in ERPNext has no such record, and neither does one
whose durable write failed. Both answer None, and app/decisiones.py refuses the
WhatsApp cancellation and points to ERPNext. Refusing a legitimate cancellation
costs a person one click; inventing a deadline cancels a delivered order.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime

from app import erpnext
from app.outbound_status import cliente, digest_recipiente

MARCA = "[confirmado-por-agente]"
CACHE_TTL_SEGUNDOS = 30 * 24 * 60 * 60
# Enough to find the first one; the read asks for them oldest-first anyway.
MAX_MARCAS = 20

_SELLO = re.compile(
    re.escape(MARCA) + r"\s*(?P<sello>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.+-][\d:.]+)?)"
)


def _clave_cache(pedido: str) -> str:
    return f"wa:{{inbound}}:confirmado-en:{digest_recipiente(pedido)}"


def _ahora_utc() -> datetime:
    return datetime.now(UTC)


def sello(momento: datetime | None = None) -> str:
    """The timestamp exactly as it is written into the durable record."""
    return (momento or _ahora_utc()).isoformat(timespec="seconds")


def _parsear(texto: str) -> float | None:
    encontrado = _SELLO.search(texto or "")
    if not encontrado:
        return None
    crudo = encontrado.group("sello").replace(" ", "T")
    try:
        momento = datetime.fromisoformat(crudo)
    except ValueError:
        return None
    if momento.tzinfo is None:
        # Written by this module, which always stamps an offset. A naive value
        # is somebody else's text: read it as UTC rather than as local time,
        # which is the direction that expires the window sooner.
        momento = momento.replace(tzinfo=UTC)
    return momento.timestamp()


def registrar(pedido: str, fuente: str) -> bool:
    """Write the durable confirmation record, then cache it. True when durable.

    The durable write comes FIRST and uses erpnext.registrar_comentario, which
    raises instead of logging: a cancellation window that only exists in Redis
    is exactly the bug this module removes. A failure here never undoes the
    confirmation itself — the order is confirmed either way — it only means the
    24-hour WhatsApp cancellation is unavailable and ERPNext is the place to
    cancel.
    """
    pedido = str(pedido or "").strip()
    if not pedido:
        return False
    texto = f"{MARCA} {sello()} fuente={fuente}"
    try:
        erpnext.registrar_comentario("Sales Order", pedido, texto)
    except Exception as exc:
        print(
            f"[confirmacion] {pedido}: no pude registrar la marca durable "
            f"({type(exc).__name__}); la cancelación por WhatsApp queda cerrada"
        )
        return False
    _cachear(pedido, _parsear(texto))
    return True


def _cachear(pedido: str, momento: float | None) -> None:
    if momento is None:
        return
    try:
        # First write wins: a second confirmation must not push the deadline out.
        cliente().set(
            _clave_cache(pedido),
            f"{momento:.3f}",
            nx=True,
            ex=CACHE_TTL_SEGUNDOS,
        )
    except Exception as exc:
        print(f"[confirmacion] {pedido}: caché no guardada ({type(exc).__name__})")


def _desde_cache(pedido: str) -> float | None:
    try:
        valor = cliente().get(_clave_cache(pedido))
    except Exception as exc:
        print(f"[confirmacion] {pedido}: caché no legible ({type(exc).__name__})")
        return None
    if isinstance(valor, bytes):
        valor = valor.decode()
    try:
        return float(valor) if valor else None
    except (TypeError, ValueError):
        return None


def _desde_erpnext(pedido: str) -> float | None:
    """The EARLIEST durable record for the order, or None.

    Earliest, not latest: if an order carries more than one record the first
    confirmation is the one the deadline runs from, which is the direction that
    closes the window sooner.
    """
    try:
        filas = erpnext.policy_get_list(
            "Comment",
            filters=[
                ["reference_doctype", "=", "Sales Order"],
                ["reference_name", "=", pedido],
                ["content", "like", f"%{MARCA}%"],
            ],
            fields=["content", "creation"],
            limit=MAX_MARCAS,
            order_by="creation asc",
        )
    except Exception as exc:
        print(
            f"[confirmacion] {pedido}: no pude leer la marca durable "
            f"({type(exc).__name__})"
        )
        return None
    momentos = [
        parsed
        for fila in filas
        if isinstance(fila, dict)
        for parsed in (_parsear(str(fila.get("content") or "")),)
        if parsed is not None
    ]
    return min(momentos) if momentos else None


def momento(pedido: str) -> float | None:
    """Epoch seconds of the confirmation THIS system performed, or None.

    None means "cannot be proven": no durable record, an unparseable one, or
    ERPNext unreadable right now. Every caller must fail closed on it.
    """
    pedido = str(pedido or "").strip()
    if not pedido:
        return None
    desde_cache = _desde_cache(pedido)
    if desde_cache is not None:
        return desde_cache
    durable = _desde_erpnext(pedido)
    if durable is not None:
        _cachear(pedido, durable)
    return durable


def horas_ventana() -> float:
    """CANCELACION_HORAS, read per call so the owner can change it live."""
    try:
        horas = float(os.getenv("CANCELACION_HORAS", "24"))
    except (TypeError, ValueError):
        return 24.0
    return horas if horas > 0 else 24.0
