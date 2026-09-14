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
            except (RedisError, LockError) as exc:
                # SE LOGUEA ACÁ, que es el único lugar donde se puede.
                # Decía «a lost/expired lease is logged by callers» y era
                # falso: ningún llamador lo logueaba y ninguno PODÍA —este
                # `contextmanager` hace `yield None` y nunca expone el lock, ni
                # `owned()`, ni el token—, así que un lease vencido en mitad de
                # una sección crítica era un evento que no dejaba rastro en
                # ninguna parte. Con `LockNotOwnedError` la sección que acaba de
                # terminar corrió SIN exclusión mutua un rato: no se puede
                # deshacer desde acá, pero tiene que poder verse.
                #
                # Sigue sin levantar: un lease perdido no puede convertir un
                # pedido conocido en un resultado incierto para el cliente.
                print(f"[locks] {name}: el lease no estaba al soltarlo ({type(exc).__name__})")
