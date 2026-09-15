"""El mensaje al cliente que espera el visto bueno del dueño.

Lo que se prueba acá es lo que puede salir mal DOS VECES o NUNCA: que un
segundo toque del botón no mande el mensaje otra vez, y que un fallo al
guardar no se trague, porque un botón que al tocarlo no encuentra nada es peor
que no mandar el botón.
"""
from __future__ import annotations

import dataclasses
import time
from unittest.mock import Mock

import pytest

from app import locks, salidas


# La suite le pone un FakeRedis a `locks.conexion` para TODOS los tests
# (`limites_sin_redis`, autouse, ver docs/MAPA.md trampa 11). Alcanza para el
# comportamiento —guardar, leer, que el segundo consumo no encuentre nada— y NO
# alcanza para dos cosas que dependen del servidor de verdad: que `GETDEL` sea
# una sola operación, y que la clave venza sola. El doble implementa `getdel`
# como un get y un delete de Python, así que no puede estar en desacuerdo con
# el código sobre la atomicidad, y ni siquiera tiene `ttl`.
#
# Así que los dos tests que afirman semántica de servidor piden `redis_real`,
# que deshace el doble. Los demás corren contra el doble, que es más rápido.
@pytest.fixture
def redis_real(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(locks, "conexion", locks._redis)
    cliente = locks._redis()
    yield cliente
    for clave in cliente.scan_iter(f"{salidas.PREFIJO}*"):
        cliente.delete(clave)


def _proponer(texto: str = "Llegó el queso cremoso, ¿te mando 2 hormas?"):
    return salidas.proponer(
        cliente="Panadería San José", telefono="5493516667777",
        texto=texto, pedida_por="5493511234567",
    )


def test_lo_guardado_vuelve_entero() -> None:
    salida = _proponer()

    leida = salidas.leer(salida.id)

    assert leida is not None
    assert leida.texto == "Llegó el queso cremoso, ¿te mando 2 hormas?"
    assert leida.telefono == "5493516667777"
    assert leida.cliente == "Panadería San José"
    # Quién lo pidió queda escrito: el botón lo aprueba cualquiera del equipo,
    # así que el registro de quién lo redactó es lo único que los separa.
    assert leida.pedida_por == "5493511234567"


def test_el_segundo_toque_del_boton_no_encuentra_nada(redis_real) -> None:
    """La mitad que importa. Un dedo impaciente toca dos veces y el cliente
    recibe el mismo mensaje dos veces — y no es un aviso interno, es una
    promesa comercial repetida.

    Contra Redis de verdad: el doble haría pasar esto aunque `consumir` fuera
    un `get` y un `delete` separados, que es la versión que dos toques
    simultáneos sí atraviesan."""
    salida = _proponer()

    primero = salidas.consumir(salida.id)
    segundo = salidas.consumir(salida.id)

    assert primero is not None
    assert primero.texto == salida.texto
    assert segundo is None


def test_mirar_no_consume() -> None:
    """`leer` es para mostrar. Si consumiera, mostrar el mensaje en el panel lo
    dejaría sin poder mandar."""
    salida = _proponer()

    assert salidas.leer(salida.id) is not None
    assert salidas.leer(salida.id) is not None
    assert salidas.consumir(salida.id) is not None


def test_un_id_que_no_existe_no_explota() -> None:
    assert salidas.leer("no-existe") is None
    assert salidas.consumir("no-existe") is None
    assert salidas.leer("") is None


def test_dos_mensajes_seguidos_no_comparten_id() -> None:
    """El id viaja en el payload de un botón. Si fuera el nombre del cliente o
    un contador, un número del equipo podría aprobar el mensaje de otro sin
    haberlo visto nunca."""
    ids = {_proponer(f"mensaje {i}").id for i in range(20)}

    assert len(ids) == 20
    assert all(len(i) >= 16 for i in ids)


def test_un_texto_vacio_se_rechaza_antes_de_guardar() -> None:
    for vacio in ("", "   ", "\n"):
        with pytest.raises(salidas.SalidaError):
            salidas.proponer("X", "549351", vacio, "549351")


def test_sin_telefono_no_hay_salida() -> None:
    with pytest.raises(salidas.SalidaError):
        salidas.proponer("X", "", "hola", "549351")


def test_si_redis_no_toma_el_mensaje_LEVANTA_en_vez_de_devolver_un_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Devolver un id que no quedó guardado es mandarle al dueño un botón que
    al tocarlo no hace nada, y dejarlo creyendo que el cliente fue avisado."""
    caido = Mock()
    caido.set.side_effect = ConnectionError("redis no está")
    monkeypatch.setattr(locks, "conexion", Mock(return_value=caido))

    with pytest.raises(salidas.SalidaError):
        _proponer()


def test_el_texto_se_recorta_al_largo_que_meta_acepta() -> None:
    salida = _proponer("x" * 5000)

    assert len(salida.texto) == salidas.LARGO_MAXIMO
    assert len(salidas.leer(salida.id).texto) == salidas.LARGO_MAXIMO


def test_el_mensaje_vence_solo(redis_real) -> None:
    """«Ya llegó el queso» deja de ser cierto. Si no lo aprobó en una hora, que
    el agente lo vuelva a redactar con lo que sepa entonces."""
    salida = _proponer()

    ttl = redis_real.ttl(f"{salidas.PREFIJO}{salida.id}")

    assert 0 < ttl <= salidas.TTL_SEGUNDOS
    assert salida.creada <= time.time()


# ------------------------------------------- devolver lo que se consumió y no salió

def test_devolver_la_repone_con_lo_que_le_QUEDABA_de_vida(redis_real) -> None:
    """El vencimiento vuelve a ser el original, no una hora nueva.

    Un TTL fresco convierte cada fallo de la cola en una hora más de vida para
    una promesa que el cliente todavía no recibió. La que se aprobó hace 50
    minutos tiene que seguir venciendo a los 60, no a los 110.

    Mata a este test cambiar `ex=restante` por `ex=TTL_SEGUNDOS`.
    """
    salida = _proponer()
    consumida = salidas.consumir(salida.id)
    vieja = dataclasses.replace(consumida, creada=time.time() - 3000)

    assert salidas.devolver(vieja) is True
    restante = redis_real.ttl(salidas._clave(salida.id))
    assert 500 < restante <= 700


def test_una_salida_ya_vencida_no_revive(redis_real) -> None:
    """Una promesa vieja que sale tarde es peor que una que no sale."""
    salida = _proponer()
    consumida = salidas.consumir(salida.id)
    vencida = dataclasses.replace(
        consumida, creada=time.time() - salidas.TTL_SEGUNDOS - 10
    )

    assert salidas.devolver(vencida) is False
    assert salidas.leer(salida.id) is None


def test_lo_devuelto_es_el_mismo_texto_y_el_mismo_id(redis_real) -> None:
    """El id es la clave de idempotencia de la cola: si cambiara al devolver, el
    reintento dejaría de estar deduplicado contra el intento que quizá encoló."""
    salida = _proponer("Llegó el queso, ¿te mando 2?")
    consumida = salidas.consumir(salida.id)

    salidas.devolver(consumida)
    repuesta = salidas.leer(salida.id)

    assert repuesta is not None
    assert repuesta.id == salida.id
    assert repuesta.texto == salida.texto
    assert repuesta.telefono == salida.telefono
