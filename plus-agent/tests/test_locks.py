"""El lock distribuido, y lo único que puede decir cuando algo salió mal.

`distributed_lock` es un `contextmanager` que hace `yield None`: no expone el
lock, ni `owned()`, ni el token. O sea que un lease vencido en mitad de una
sección crítica NO ES OBSERVABLE desde afuera, y el único lugar del programa
donde se puede ver es adentro de esta función. El comentario decía que lo
logueaban los llamadores; ninguno lo hacía y ninguno podía.
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
