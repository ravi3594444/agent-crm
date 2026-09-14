"""Los scripts Lua de `agenda` y `avisos`, contra un Redis de VERDAD.

POR QUÉ ESTE ARCHIVO EXISTE APARTE
`tests/fakes.py` no ejecuta Lua: reimplementa en Python la regla que el script
declara. Eso alcanza para que el resto de la suite tenga un caché que se porte,
pero **no puede discrepar con el script**: mutar el Lua entero —invertir la
comparación de sellos, invertir la pertenencia al índice— no rompía un solo
test. Una réplica que sigue a su original no prueba al original.

Lo que este archivo prueba no es «la regla es ésta» sino «el script que Redis
ejecuta hace esto», que es lo único que sostiene la atomicidad: la comparación
y las dos escrituras entran o no entran juntas, y nadie corre en el medio.

Es el mismo patrón que `test_limites.py::test_a_change_really_survives_in_redis`
y usa su mismo interruptor: sin Redis es un skip en una laptop y una FALLA en
CI, donde `REDIS_OBLIGATORIO` está puesto a propósito.
"""

from __future__ import annotations

import json
import os

import pytest

from app import agenda

pytestmark = pytest.mark.idioma("es")

PEDIDO = "SO-REDIS-0001"


def _sin_redis(motivo: str) -> None:
    if os.getenv("REDIS_OBLIGATORIO", "").strip():
        pytest.fail(f"REDIS_OBLIGATORIO está puesto y {motivo}")
    pytest.skip(motivo)


@pytest.fixture
def redis_real(monkeypatch: pytest.MonkeyPatch):
    """El Redis de verdad, con el índice de este archivo y no el de producción."""
    import redis

    from app import outbound_status

    if not os.getenv("REDIS_URL", "").strip():
        _sin_redis("no hay REDIS_URL configurada")
    cliente = redis.Redis.from_url(os.environ["REDIS_URL"])
    try:
        cliente.ping()
    except redis.exceptions.RedisError:
        _sin_redis("Redis no responde")
    indice = "plus-agent:test-agenda:indice"
    monkeypatch.setattr(agenda, "CLAVE_INDICE", indice)
    monkeypatch.setattr(outbound_status, "_client", cliente)
    claves = [indice]

    def clave_cache(sobre, identificador):
        nombre = f"plus-agent:test-agenda:{identificador}"
        claves.append(nombre)
        return nombre

    monkeypatch.setattr(agenda, "_clave_cache", clave_cache)
    try:
        yield cliente, indice
    finally:
        cliente.delete(*dict.fromkeys(claves))


def _fila(estado: str = agenda.PENDIENTE, sello: float = 1000.0) -> agenda.Fila:
    return agenda.Fila(
        id="filaredis0000001",
        sobre=PEDIDO,
        tipo=agenda.SEGUIMIENTO,
        vence=2000.0,
        estado=estado,
        params={"por_que": "x"},
        evento="creada" if estado == agenda.PENDIENTE else "hecha",
        sello=sello,
    )


def _guardada(cliente, fila) -> dict | None:
    crudo = cliente.get(agenda._clave_cache(fila.sobre, fila.id))
    return json.loads(crudo) if crudo else None


def test_el_script_no_deja_que_una_foto_vieja_pise_a_la_nueva(redis_real) -> None:
    """La comparación de sellos, tal como Redis la ejecuta.

    Las dos mitades, porque son dos daños distintos y cada una sola se cumple
    con la otra rota: que el BLOB guardado siga diciendo HECHO, y que la fila no
    vuelva al ÍNDICE — una fila fuera del índice no se despacha aunque su blob
    mienta, y un blob correcto con la fila adentro del índice se despacha igual.
    """
    cliente, indice = redis_real
    hecha = _fila(agenda.HECHO, sello=2000.0)
    agenda._cachear(hecha)
    assert cliente.zscore(indice, agenda._miembro(hecha)) is None

    # El worker lento vuelve con la foto PENDIENTE, más vieja. Misma fila.
    agenda._cachear(_fila(agenda.PENDIENTE, sello=1000.0))

    assert _guardada(cliente, hecha)["estado"] == agenda.HECHO
    assert cliente.zscore(indice, agenda._miembro(hecha)) is None


def test_con_el_mismo_sello_lo_terminal_le_gana_a_lo_pendiente(redis_real) -> None:
    """El empate existe de verdad: `ejecutar_ahora` crea y despacha con un solo
    `ahora`, así que el `creada` y la `hecha` de esa fila llevan el MISMO sello.

    Comparar sólo `>` dejaba pasar al `creada` atrasado, y la fila terminada
    volvía al índice — el mismo daño que el test de arriba, por la puerta que
    el sello no alcanza a cerrar.
    """
    cliente, indice = redis_real
    hecha = _fila(agenda.HECHO, sello=1500.0)
    agenda._cachear(hecha)

    agenda._cachear(_fila(agenda.PENDIENTE, sello=1500.0))

    assert _guardada(cliente, hecha)["estado"] == agenda.HECHO
    assert cliente.zscore(indice, agenda._miembro(hecha)) is None


def test_una_foto_mas_nueva_si_entra_y_pone_la_fila_en_el_indice(redis_real) -> None:
    """La otra mitad de todo lo de arriba: el script no es «nunca escribas».

    Sin esto, un script que devolviera 0 siempre pasaría los dos tests
    anteriores y la agenda no despacharía nada nunca.
    """
    cliente, indice = redis_real
    vieja = _fila(agenda.PENDIENTE, sello=1000.0)
    agenda._cachear(vieja)
    assert cliente.zscore(indice, agenda._miembro(vieja)) == vieja.vence

    agenda._cachear(_fila(agenda.HECHO, sello=3000.0))

    assert _guardada(cliente, vieja)["estado"] == agenda.HECHO
    assert cliente.zscore(indice, agenda._miembro(vieja)) is None


# ===========================================================================
# `avisos`: los dos scripts que sostienen el exactamente-una-vez de la cola
#
# Mismo motivo que arriba y encontrado del mismo modo: `tests/fakes.py`
# reimplementa estos dos en Python, así que ningún test podía discrepar con el
# script. Son los que garantizan que un cliente reciba su confirmación UNA vez
# y que dos workers no se lleven el mismo aviso; medidos contra el doble, esa
# garantía es una afirmación sobre el doble.
# ===========================================================================


@pytest.fixture
def avisos_en_redis(monkeypatch: pytest.MonkeyPatch):
    """`avisos` apuntando a un Redis real, con claves propias de este archivo."""
    import redis

    from app import avisos, outbound_status

    if not os.getenv("REDIS_URL", "").strip():
        _sin_redis("no hay REDIS_URL configurada")
    cliente = redis.Redis.from_url(os.environ["REDIS_URL"])
    try:
        cliente.ping()
    except redis.exceptions.RedisError:
        _sin_redis("Redis no responde")
    cola = "plus-agent:test-avisos:cola"
    monkeypatch.setattr(avisos, "COLA", cola)
    monkeypatch.setattr(outbound_status, "_client", cliente)
    claves = [cola]

    def clave_encolado(evento, pedido):
        nombre = f"plus-agent:test-avisos:enc:{evento}:{pedido}"
        claves.append(nombre)
        return nombre

    monkeypatch.setattr(avisos, "_clave_encolado", clave_encolado)
    monkeypatch.setattr(avisos, "has_accepted", lambda pedido, evento: False)
    try:
        yield avisos, cliente, cola
    finally:
        cliente.delete(*dict.fromkeys(claves))


def test_el_script_encola_una_sola_vez_el_mismo_aviso(avisos_en_redis) -> None:
    """La idempotencia de la cola, tal como Redis la ejecuta.

    Las dos mitades: el PRIMER encolado entra —si el script nunca escribiera,
    el cliente no recibiría nada y el test de abajo pasaría igual— y el segundo
    NO agrega una segunda entrada. El booleano y el contenido de la cola se
    afirman por separado porque son dos cosas distintas: `encolar` puede
    devolver False habiendo escrito, que es el peor de los dos errores.
    """
    avisos, cliente, cola = avisos_en_redis

    primero = avisos.encolar("ev", "SO-1", "5493510000001", "hola")
    segundo = avisos.encolar("ev", "SO-1", "5493510000001", "hola")

    assert primero is True
    assert segundo is False
    assert cliente.zcard(cola) == 1


def test_el_script_no_le_da_el_mismo_aviso_a_dos_workers(avisos_en_redis) -> None:
    """Reclamar es atómico: el segundo worker no se lleva lo mismo que el primero.

    `_RECLAMAR_LUA` no borra la entrada, la re-puntúa al futuro (el lease), así
    que sigue en la cola y lo que tiene que cambiar es a QUIÉN le toca ahora.
    """
    avisos, cliente, cola = avisos_en_redis
    assert avisos.encolar("ev", "SO-1", "5493510000001", "uno") is True
    assert avisos.encolar("ev", "SO-2", "5493510000002", "dos") is True

    ahora = 10_000_000_000.0
    lua = avisos._RECLAMAR_LUA
    primero = cliente.eval(lua, 1, cola, f"{ahora:.3f}", f"{ahora + 90:.3f}")
    segundo = cliente.eval(lua, 1, cola, f"{ahora:.3f}", f"{ahora + 90:.3f}")

    assert primero is not None and segundo is not None
    assert primero != segundo, "dos workers se llevaron el MISMO aviso"
    assert cliente.zcard(cola) == 2, "reclamar no borra: re-puntúa con el lease"
