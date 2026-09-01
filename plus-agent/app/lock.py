"""Un lock chico para cerrar la ventana entre "verifiqué stock" y "confirmé".

EL PROBLEMA CONCRETO
policy.evaluar() lee el stock y decide; después pedidos.crear_pedido() hace
el submit. Entre esas dos cosas pasan cientos de milisegundos de llamadas
REST. Dos clientes que piden los últimos 10 litros al mismo tiempo pasan los
dos la verificación y se confirman los dos. Vendiste 20 litros que no tenés.

Peor: en ERPNext los BORRADORES NO reservan stock. `Bin.reserved_qty` sube
cuando el Sales Order se CONFIRMA. Así que hasta el submit, el stock que
promete un borrador es invisible para el siguiente chequeo.

LA SOLUCIÓN, DEL TAMAÑO DEL PROBLEMA
Un lock global sobre "evaluar + confirmar". Una lechería hace decenas de
pedidos por día, no miles por segundo: serializar la auto-confirmación no
cuesta nada y elimina la carrera por completo. Si el lock no se consigue, el
pedido no se auto-confirma — va a revisión humana, que es el lado seguro.
"""

from __future__ import annotations

import contextlib
import os
import uuid

import redis

from app import log

_log = log.get("lock")

_TTL = int(os.getenv("LOCK_TTL_SEGUNDOS", "30"))
_ESPERA = float(os.getenv("LOCK_ESPERA_SEGUNDOS", "5"))

_r: redis.Redis | None = None


def _cliente() -> redis.Redis:
    global _r
    if _r is None:
        _r = redis.from_url(os.environ["REDIS_URL"])
    return _r


@contextlib.contextmanager
def tomar(nombre: str, ttl: int | None = None, espera: float | None = None):
    """Context manager que rinde True si consiguió el lock, False si no.

    NUNCA levanta excepción por no conseguirlo ni por Redis caído: el
    llamador decide qué hacer, y en este sistema "no conseguí el lock"
    siempre significa "que lo revise un humano".
    """
    clave = f"lock:{nombre}"
    token = uuid.uuid4().hex
    conseguido = False
    try:
        conseguido = bool(_cliente().set(clave, token, nx=True, ex=ttl or _TTL))
        if not conseguido:
            # Un solo reintento corto: si otro pedido está confirmándose,
            # normalmente termina en menos de un segundo.
            deadline = espera if espera is not None else _ESPERA
            paso = 0.25
            esperado = 0.0
            while esperado < deadline and not conseguido:
                _cliente().ping()  # falla rápido si Redis se cayó
                import time

                time.sleep(paso)
                esperado += paso
                conseguido = bool(_cliente().set(clave, token, nx=True, ex=ttl or _TTL))
        if not conseguido:
            _log.warning("no conseguí el lock %s en %ss", nombre, espera or _ESPERA)
        yield conseguido
    except Exception as e:
        _log.error("Redis no disponible para el lock %s: %s", nombre, e)
        yield False
    finally:
        if conseguido:
            try:
                # Solo borra si el token sigue siendo el nuestro: si el TTL
                # venció y otro lo tomó, no le pisamos el lock.
                actual = _cliente().get(clave)
                if actual and actual.decode() == token:
                    _cliente().delete(clave)
            except Exception as e:
                _log.warning("no pude liberar el lock %s: %s", nombre, e)


def reset() -> None:
    """Para los tests."""
    global _r
    _r = None
