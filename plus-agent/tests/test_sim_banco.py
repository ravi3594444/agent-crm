"""El banco de pruebas offline (`make demo`): lo que tiene que DESHACER.

No hay un test por escenario acá —eso es `make demo`, que corre 48 turnos y se
mira—. Lo que se prueba es lo único de `sim_banco.py` que le puede hacer daño a
otra cosa: `levantar()` pisa `os.environ` con credenciales falsas y endpoints de
loopback, y ese proceso después sigue vivo.
"""
from __future__ import annotations

import os

import pytest

import sim_banco


def _banco() -> sim_banco.Banco:
    """Un `Banco` SIN levantar. `bajar()` tiene que andar sobre esto también:
    `__enter__` llama a `levantar()` directo, así que el camino de error pasa
    por `bajar()` con casi todo todavía en None."""
    return sim_banco.Banco([], verbose=False)


def test_bajar_devuelve_el_entorno_que_levantar_piso(monkeypatch) -> None:
    """Las TRES formas de volver, que no son dos.

    Una variable que existía vuelve a su valor viejo; una que NO existía tiene
    que quedar BORRADA, no en "" — un `ERPNEXT_API_KEY=""` no es lo mismo que
    sin variable para `os.getenv(...) or default`—; y la que el banco no tocó
    no se toca.

    MUTACIÓN: en `bajar`, `os.environ.pop(clave, None)` -> `os.environ[clave] =
    ""`. Cae ésta y sólo ésta: es la rama que un `dict.update` de vuelta —el
    arreglo obvio— no cubre.
    """
    monkeypatch.setenv("YA_ESTABA", "el valor de antes")
    monkeypatch.delenv("NO_ESTABA", raising=False)
    monkeypatch.setenv("NI_LA_TOCO", "intacta")

    banco = _banco()
    # Lo que `levantar()` deja anotado antes de pisar: None para la que no
    # existía, que es el dato que se pierde si se guarda con `dict(os.environ)`.
    banco._entorno_previo = {"YA_ESTABA": "el valor de antes", "NO_ESTABA": None}
    os.environ["YA_ESTABA"] = "el del simulador"
    os.environ["NO_ESTABA"] = "inventada por el simulador"

    banco.bajar()

    assert os.environ["YA_ESTABA"] == "el valor de antes"
    assert "NO_ESTABA" not in os.environ
    assert os.environ["NI_LA_TOCO"] == "intacta"


def test_bajar_dos_veces_no_vuelve_a_pisar_nada(monkeypatch) -> None:
    """El segundo `bajar()` —un `__exit__` después de un `bajar()` a mano— no
    puede deshacer lo que pasó DESPUÉS del primero.

    Sin vaciar `_entorno_previo`, la segunda pasada vuelve a escribir los
    valores viejos encima de lo que alguien haya puesto mientras tanto.

    MUTACIÓN: borrar `self._entorno_previo = {}` del final de `bajar`. Cae ésta
    y sólo ésta.
    """
    monkeypatch.setenv("YA_ESTABA", "el valor de antes")
    banco = _banco()
    banco._entorno_previo = {"YA_ESTABA": "el valor de antes"}
    os.environ["YA_ESTABA"] = "el del simulador"

    banco.bajar()
    os.environ["YA_ESTABA"] = "lo que puso el test de después"
    banco.bajar()

    assert os.environ["YA_ESTABA"] == "lo que puso el test de después"


def test_levantar_anota_TODAS_las_variables_que_va_a_pisar() -> None:
    """La mitad que los dos de arriba no pueden ver, y sin la cual se cumplen
    solos.

    Los dos anteriores le PASAN a `bajar()` un `_entorno_previo` escrito a
    mano: si `levantar()` anotara la mitad de las claves —o ninguna— seguirían
    en verde. `levantar()` necesita puertos, Redis y un certificado, así que no
    se puede correr acá; lo que sí se puede es exigir que la foto se saque
    sobre EL MISMO `env` que después se aplica, que es la relación que puede
    romperse.

    MUTACIÓN: `self._entorno_previo = {k: os.environ.get(k) for k in env}` ->
    `... for k in self.extra_env`. Cae ésta y sólo ésta.
    """
    import ast
    import inspect
    import textwrap

    arbol = ast.parse(textwrap.dedent(inspect.getsource(sim_banco.Banco.levantar)))
    foto = aplicado = None
    for nodo in ast.walk(arbol):
        # `self._entorno_previo = {k: os.environ.get(k) for k in <FUENTE>}`
        if (isinstance(nodo, ast.Assign)
                and isinstance(nodo.value, ast.DictComp)
                and any(getattr(t, "attr", "") == "_entorno_previo" for t in nodo.targets)):
            foto = ast.unparse(nodo.value.generators[0].iter)
        # `os.environ.update(<FUENTE>)`
        if (isinstance(nodo, ast.Call)
                and ast.unparse(nodo.func) == "os.environ.update"
                and nodo.args):
            aplicado = ast.unparse(nodo.args[0])

    assert foto is not None, "levantar() ya no saca la foto del entorno"
    assert aplicado is not None, "levantar() ya no aplica el entorno"
    assert foto == aplicado, (
        f"la foto se saca de «{foto}» y se pisa «{aplicado}»: "
        "lo que no esté en la foto no vuelve nunca"
    )


class _HiloFalso:
    """Un hilo que termina o no termina, según se lo pida el test.

    Deriva de lo que RECIBE: guarda el `timeout` con el que lo esperaron, así
    que un `join()` sin límite —o sin `join`— se ve distinto de uno acotado.
    """

    def __init__(self, *, termina: bool) -> None:
        self.termina = termina
        self.esperado = "no lo esperaron"

    def join(self, timeout=None) -> None:
        self.esperado = timeout

    def is_alive(self) -> bool:
        return not self.termina


class _UvicornFalso:
    def __init__(self) -> None:
        self.should_exit = False


def test_bajar_espera_al_hilo_de_uvicorn_con_un_limite(capsys) -> None:
    """El hilo era `Thread(...).start()` y se tiraba.

    Sin guardarlo, `bajar()` sólo podía pedir `should_exit` y dormir un
    segundo: si el apagado tardaba más, el hilo seguía con el puerto tomado y
    el próximo `levantar()` moría en `_verificar_puertos()` acusando a un
    ocupante que éramos nosotros. Se espera, y CON límite: un `join()` sin
    timeout cambia un puerto tomado por un `make demo` colgado.

    MUTACIÓN: `hilo.join(timeout=10)` -> `hilo.join()`. Cae ésta y sólo ésta.
    """
    banco = _banco()
    banco.verbose = True
    banco._uvicorn = _UvicornFalso()
    hilo = _HiloFalso(termina=True)
    banco._hilo_uvicorn = hilo

    banco.bajar()

    assert banco._uvicorn.should_exit is True
    assert hilo.esperado == 10
    # Terminó, así que no hay nada que decir.
    assert "puede seguir tomado" not in capsys.readouterr().out


def test_un_uvicorn_que_no_termina_se_dice_en_vez_de_dejarse_pasar(capsys) -> None:
    """La otra mitad, y la que le sirve a quien lea la corrida siguiente.

    El puerto queda tomado por ESTE proceso. Sin la línea, el próximo
    `levantar()` falla contra ese puerto y parece que el ocupante es otro
    programa: se pierden diez minutos buscando afuera lo que está adentro.

    MUTACIÓN: borrar ese `self._decir(...)`. Cae ésta y sólo ésta.
    """
    banco = _banco()
    banco.verbose = True
    banco._uvicorn = _UvicornFalso()
    banco._hilo_uvicorn = _HiloFalso(termina=False)

    banco.bajar()

    salida = capsys.readouterr().out
    assert "no terminó en 10s" in salida
    assert str(sim_banco.PUERTO_AGENTE) in salida
