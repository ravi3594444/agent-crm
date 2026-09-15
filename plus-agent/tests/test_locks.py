"""El lock distribuido: lo que dice cuando algo salió mal, y quién sigue siendo su dueño.

`distributed_lock` hacía `yield None`: no exponía el lock, ni `owned()`, ni el
token. O sea que un lease vencido en mitad de una sección crítica NO ERA
OBSERVABLE desde afuera —el comentario decía que lo logueaban los llamadores;
ninguno lo hacía y ninguno podía— y, peor, tampoco era PREGUNTABLE: los dos
lugares que emiten lo dicen en un comentario y se conforman con mirar si
alguien decidió mientras tanto, que achica la ventana y no la cierra.

Ahora hace `yield Lease(...)`, que contesta una sola pregunta —«¿sigue siendo
mío?»— y la contesta **fallando cerrado**: si Redis no contesta, la respuesta
es que no. Del otro lado de esa pregunta hay el único Submit del sistema.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("ERPNEXT_URL", "http://erpnext.test")
os.environ.setdefault("ERPNEXT_API_KEY", "test-key")
os.environ.setdefault("ERPNEXT_API_SECRET", "test-secret")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test-phone-id")
os.environ.setdefault("WHATSAPP_TOKEN", "test-token")

from app import locks


def test_un_lease_perdido_deja_rastro(capsys) -> None:
    """Perder el lease antes de terminar tiene que poder verse en algún lado.

    Es la mitad invisible del hallazgo de la sección crítica. Que el `finally`
    no levante está bien y se mantiene —un lease perdido no puede convertir un
    pedido conocido en un resultado incierto para el cliente—, pero hasta ahora
    tampoco IMPRIMÍA: la sección que acaba de terminar corrió un rato sin
    exclusión mutua y no quedaba ni una línea en ningún log. Un servidor al que
    le pasa esto todas las noches se ve exactamente igual que uno al que no le
    pasa nunca.

    Se borra la llave desde adentro del `with`, que es lo que hace redis cuando
    el lease vence: al salir, `release` encuentra que el token ya no es suyo y
    levanta `LockNotOwnedError`.

    SE BORRA CON `_redis()` Y NO CON `conexion()`, que es un privado y acá es el
    correcto: `limites_sin_redis` (autouse, en `conftest.py`) le pone un
    `FakeRedis` a `conexion` para que cada test arranque con el almacén de
    límites vacío, y `distributed_lock` no pasa por ahí — toma `_redis()`
    directo, el redis de verdad. Borrar por `conexion()` borra en el doble, el
    lock real sigue entero, y el test queda verde sin haber ejercido nada.

    Mutación dirigida: volver el `print` a `pass`. Mata a este test y a ninguno
    otro.
    """
    nombre = "prueba-lease-perdido"

    with locks.distributed_lock(nombre, lease_seconds=30, wait_seconds=2):
        locks._redis().delete(f"plus-agent:business-lock:{nombre}")

    salida = capsys.readouterr().out
    assert nombre in salida
    assert "lease" in salida


def test_un_lock_que_se_suelta_bien_no_dice_nada(capsys) -> None:
    """La otra mitad: el camino sano es mudo.

    Sin esto, «loguear el lease perdido» se podría escribir como un `print` en
    cada salida del `finally`, y entonces cada barrido de un minuto ensuciaría
    el log con una línea por lock — que es como se consigue que nadie lea el
    log en el que después aparece la línea que importa.
    """
    with locks.distributed_lock("prueba-lease-sano", lease_seconds=30, wait_seconds=2):
        pass

    assert capsys.readouterr().out == ""


def test_el_lock_entrega_su_lease_y_adentro_sigue_siendo_mio() -> None:
    """Lo básico, contra el Redis de verdad: adentro del `with`, es mío."""
    with locks.distributed_lock("prueba-lease-vivo", lease_seconds=30, wait_seconds=2) as lease:
        assert isinstance(lease, locks.Lease)
        assert lease.sigue_mio() is True


def test_un_lease_vencido_ya_no_es_mio() -> None:
    """Es lo que hace redis cuando el lease vence: la llave deja de estar.

    Se borra la llave desde adentro del `with`, igual que hace
    `test_un_lease_perdido_deja_rastro`, y por el mismo motivo se borra con
    `_redis()` y no con `conexion()`: `limites_sin_redis` le pone un `FakeRedis`
    a `conexion`, y `distributed_lock` no pasa por ahí.
    """
    nombre = "prueba-lease-vencido"
    with locks.distributed_lock(nombre, lease_seconds=30, wait_seconds=2) as lease:
        assert lease.sigue_mio() is True
        locks._redis().delete(f"plus-agent:business-lock:{nombre}")
        assert lease.sigue_mio() is False


def test_un_lease_de_otro_no_es_mio() -> None:
    """La llave existe pero con OTRO token: es el caso que de verdad importa.

    Un lease vencido que otro worker ya volvió a tomar deja la llave PUESTA, así
    que preguntar «¿existe la llave?» contestaría que sí. Lo que hay que
    comparar es el token, que es lo que hace `owned()`.
    """
    nombre = "prueba-lease-de-otro"
    llave = f"plus-agent:business-lock:{nombre}"
    try:
        with locks.distributed_lock(nombre, lease_seconds=30, wait_seconds=2) as lease:
            # CON `ex`, y el `try/finally` de abajo ADEMÁS. El `finally` de
            # `distributed_lock` no puede soltar un lease que ya no es suyo, así
            # que el que limpia es este test — y un test que limpia sólo en su
            # camino feliz deja la llave PARA SIEMPRE cuando el assert falla.
            # Pasó: una mutación hizo fallar el assert, la llave quedó sin TTL,
            # y a partir de ahí TODAS las corridas de este archivo fallaban acá
            # —incluso con el código sano— porque el lock ya no se podía tomar.
            # Un test que envenena el Redis compartido cuando falla no falla una
            # vez: rompe el archivo hasta que alguien lo limpia a mano.
            locks._redis().set(llave, "el-token-de-otro", ex=30)
            assert lease.sigue_mio() is False
    finally:
        locks._redis().delete(llave)


def test_si_redis_no_contesta_el_lease_se_da_por_perdido(capsys) -> None:
    """FALLA CERRADO. «No pude comprobar» no es «sí, seguís siendo el dueño».

    Del otro lado de esta pregunta hay un Submit que no se deshace. Una
    comprobación que falla abierta no comprueba nada — la misma regla que el
    tope diario de `app/precios.py`.
    """
    from redis.exceptions import RedisError

    class LockMudo:
        def owned(self):
            raise RedisError("se cayó redis")

    lease = locks.Lease(LockMudo(), "prueba-redis-mudo")

    assert lease.sigue_mio() is False
    # Y deja rastro: si esto pasa todas las noches, tiene que poder verse.
    assert "prueba-redis-mudo" in capsys.readouterr().out
