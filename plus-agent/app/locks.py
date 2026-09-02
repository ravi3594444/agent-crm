"""Small, bounded Redis locks for cross-worker business invariants.

Webhook ordering and business idempotency are separate concerns.  These locks
protect the two critical sections which must not run concurrently across app
workers: creating an order for one inbound Meta message and the final
stock-check/submit transition.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import redis
from redis.exceptions import LockError, RedisError


class CoordinationError(RuntimeError):
    """Redis could not safely coordinate a business-critical operation."""


_client: redis.Redis | None = None


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        url = os.getenv("REDIS_URL", "").strip()
        if not url:
            raise CoordinationError("REDIS_URL no configurado")
        _client = redis.Redis.from_url(
            url,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
            retry_on_timeout=False,
        )
    return _client


def conexion() -> redis.Redis:
    """The same Redis the business locks use.

    app/limites.py stores the owner's auto-confirmation limits here on purpose:
    a limits read and a submit lock then fail closed together, instead of the
    policy trusting numbers it could not verify while the lock was unavailable.
    """
    return _redis()


@contextmanager
def distributed_lock(
    name: str,
    *,
    lease_seconds: int = 60,
    wait_seconds: int = 5,
) -> Iterator[None]:
    """Acquire a named cross-worker lock, failing closed after a short wait."""
    lock = _redis().lock(
        f"plus-agent:business-lock:{name}",
        timeout=lease_seconds,
        blocking_timeout=wait_seconds,
        thread_local=False,
    )
    acquired = False
    try:
        acquired = bool(lock.acquire(blocking=True))
        if not acquired:
            raise CoordinationError("no se pudo adquirir el lock distribuido")
        yield
    except (RedisError, LockError) as exc:
        raise CoordinationError("falló la coordinación distribuida") from exc
    finally:
        if acquired:
            try:
                lock.release()
            except (RedisError, LockError):
                # The operation has already ended.  A lost/expired lease is
                # logged by callers and must never turn a known order into an
                # unknown customer-facing result.
                pass
