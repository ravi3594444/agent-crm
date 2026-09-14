"""`calling_agent` para los tests de voz, y por qué no alcanza un `importorskip`.

EL SKIP QUE NO SE VE ES EL QUE IMPORTA.
19 tests del canal de voz necesitan el relay instalado: los que arman el
`AgentDefinition`, los que miran el payload de la sesión y el que corre una
llamada entera. `pytest.importorskip` los saltea cuando el paquete no está, que
es lo correcto en la máquina de alguien que sólo toca WhatsApp — y es
exactamente lo que NO se quiere en CI, donde saltearlos deja el job en verde
afirmando algo que no probó. La cobertura del canal se encogía a la mitad sin
que ningún check se pusiera en rojo.

Es el mismo problema que `REDIS_OBLIGATORIO` ya resuelve para el Redis Stack, y
la solución es la misma forma: una variable que convierte el skip en FALLA
donde el paquete tiene que estar. `VOZ_OBLIGATORIO=1` lo pone el job de tests
junto a `REDIS_OBLIGATORIO`, y `requirements-dev.txt` trae el relay para que
esté.

Lo encontró una sesión que corrió `pytest -rs` y leyó los skips: en local el
repo del relay está al lado y el import anda, así que acá nunca se veía.
"""
import os

import pytest


def relay():
    """Devuelve el módulo `calling_agent`. Saltea afuera de CI, falla adentro.

    La condición es `VOZ_OBLIGATORIO` y no `CI`: que el relay tenga que estar
    es una decisión del job que lo instala, no un hecho de correr en un runner.
    Un job que NO lo instala —el de la imagen, por ejemplo— no tiene por qué
    volverse rojo por una variable que puso otro.
    """
    try:
        import calling_agent
    except ImportError as exc:
        if os.getenv("VOZ_OBLIGATORIO") == "1":
            raise AssertionError(
                "VOZ_OBLIGATORIO=1 y `calling_agent` no está instalado: "
                "los tests del relay se saltearían y el job quedaría verde "
                "sin haberlos corrido. Instalá requirements-dev.txt, que "
                f"incluye requirements-voz.txt. ({exc})"
            ) from exc
        pytest.skip("calling_agent no está instalado (requirements-voz.txt)")
    return calling_agent
