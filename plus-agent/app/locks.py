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


class Lease:
    """El lease tomado, para la única pregunta que un llamador necesita hacerle.

    POR QUÉ EXISTE
    --------------
    Un lease vence solo. Nadie lo renueva —no hay un `extend()` en este repo— y
    adentro de las secciones críticas de este sistema hay hasta cuatro llamadas
    a ERPNext con `timeout` de 20 y 30 s. Cuando el lease se vence a mitad de
    camino, la llamada NO falla: sigue caminando hacia el Submit sin exclusión
    mutua, mientras otro worker toma la llave libre y decide otra cosa sobre el
    mismo pedido.

    `distributed_lock` hacía `yield None`, así que preguntar «¿lo sigo
    teniendo?» era literalmente imposible: los dos lugares que emiten lo dicen
    en un comentario y se conforman con mirar si alguien decidió mientras tanto.
    Eso achica la ventana y no la cierra. Esta clase es la pieza que faltaba.

    Es deliberadamente una sola pregunta y no el `Lock` de redis-py: un llamador
    que pudiera hacer `release()` o `extend()` podría soltar el lease de otro, o
    estirarlo para siempre. Lo único que hace falta del otro lado de un Submit
    es saber si todavía es tuyo.
    """

    __slots__ = ("_lock", "_nombre")

    def __init__(self, lock, nombre: str) -> None:
        self._lock = lock
        self._nombre = nombre

    def sigue_mio(self) -> bool:
        """True SÓLO si Redis confirma que el lease sigue siendo de este proceso.

        **Falla cerrado.** Si Redis no contesta, la respuesta es False: del otro
        lado de esta pregunta hay un efecto irreversible, y «no pude comprobar»
        no es «sí». Una comprobación que falla abierta no comprueba nada, que es
        la misma regla que el tope diario de `app/precios.py`.
        """
        try:
            return bool(self._lock.owned())
        except (RedisError, LockError) as exc:
            print(
                f"[locks] {self._nombre}: no pude comprobar el lease "
                f"({type(exc).__name__}), lo doy por perdido"
            )
            return False


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
) -> Iterator[Lease]:
    """Acquire a named cross-worker lock, failing closed after a short wait.

    Yields a `Lease`, que es lo que le permite al llamador preguntar
    `sigue_mio()` justo antes de hacer algo irreversible. Los llamadores que no
    emiten nada siguen escribiendo `with distributed_lock(...):` sin `as` y no
    cambian en nada.
    """
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
        yield Lease(lock, name)
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
