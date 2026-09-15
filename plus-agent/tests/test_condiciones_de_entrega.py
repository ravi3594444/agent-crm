"""Las condiciones de entrega, contestadas SIN prometer una entrega.

QUÉ PROTEGE ESTE ARCHIVO
El dueño configura doce hechos de entrega por WhatsApp y hasta ahora ninguno
llegaba al cliente que los pregunta. `condiciones_de_entrega` los lee, y lo que
se prueba acá es la mitad peligrosa de esa lectura:

  * un ajuste que el dueño FIJÓ se contesta con su valor;
  * un ajuste que FALTA —o que se PERDIÓ del almacén— sale como faltante y
    nunca como un «no»: «no lo tengo configurado» no es «no repartimos», y
    confundirlos convierte un agujero de configuración en una promesa (o en
    una puerta en la cara);
  * una lectura que no contesta tampoco es «no hay nada configurado»;
  * la plata pasa por `app/formato.pesos`, así que un cargo se lee como lo lee
    quien mira;
  * la respuesta entera cambia de idioma con el cliente, y ninguna mitad sale
    como una clave cruda del catálogo (`idioma.t` devuelve la clave cuando la
    fila no existe, así que «salió en inglés» no prueba que esté traducido);
  * y nada de esto escribe nada, en ningún lado.

CÓMO SE ELIGIERON LAS MUTACIONES
Cada test nombra en su docstring UNA mutación que lo mata a él y a ningún otro,
medida de verdad (editar, correr la suite entera, revertir). Se eligieron contra
lo que el test NO dice, y donde un valor derivado tiene dos consumidores se
mutó cada consumidor por separado: `localidades`/`codigos` alimentan las líneas
del listado Y la respuesta de zona —y cada mutación mata un test distinto—, y
`sin_dato` alimenta cada línea faltante Y la instrucción final.

Los números de cada corrida se escriben tal como salieron, rojos ajenos
incluidos: mientras se midió, otras ramas estaban editando `test_mcp_cliente.py`,
`test_memoria*.py`, `test_prompts.py` y `test_sim_banco.py`, y esos rojos
aparecen y desaparecen entre corridas. Lo que se afirma es el DELTA contra la
corrida de referencia inmediata, y en las catorce mediciones el delta fue
siempre un solo test de este archivo.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import FakeRedis

from app import idioma, limites, locks
from app.tools.entrega import condiciones_de_entrega

# Este archivo afirma texto en español y montos con la forma es_AR, así que
# declara los dos en vez de heredarlos del entorno. Ver tests/conftest.py.
pytestmark = [pytest.mark.idioma("es"), pytest.mark.locale("es_AR")]

ES = idioma.ES
EN = idioma.EN

CLIENTE = "5493510000000"
OTRO_CLIENTE = "5493510000001"

# Las doce variables de arranque del grupo ENTREGA. `limites_sin_redis` (el
# fixture autouse de conftest) limpia las de LIMITES y no éstas, así que un
# `.env` de desarrollo con días de reparto cargados haría que los tests de «no
# está configurado» probaran otra cosa. El archivo declara lo que supone.
DEL_ENTORNO = tuple(limites.ENTREGA)


@pytest.fixture
def almacen(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """Un almacén vacío que conserva lo que se le escribe, y sin entorno."""
    for nombre in DEL_ENTORNO:
        monkeypatch.delenv(nombre, raising=False)
    falso = FakeRedis()
    monkeypatch.setattr(locks, "conexion", lambda: falso)
    return falso


def _fijar(almacen: FakeRedis, **valores: str) -> None:
    """Lo que el dueño dejó fijado, en forma normal, como lo guarda `aplicar`."""
    almacen.hashes.setdefault(limites.CLAVE_VALORES, {}).update(valores)


def _config(telefono: str = CLIENTE) -> dict:
    return {
        "configurable": {
            "thread_id": "cli:hilo",
            "actor_scope": "customer",
            "customer_code": "CUST-001",
            "actor_phone": telefono,
            "inbound_message_id": "wamid.cli-001",
        }
    }


def _responder(zona: str = "", telefono: str = CLIENTE) -> str:
    return condiciones_de_entrega.invoke({"zona": zona}, config=_config(telefono))


def _dice(respuesta: str, linea: str) -> bool:
    """¿Está esa línea ENTERA en la respuesta?

    `in respuesta` no alcanza y el motivo es concreto: «...por el local: no» es
    un prefijo de «...por el local: no lo tengo configurado», así que un
    ajuste FALTANTE pasaba por un «no» —justo lo que estos tests separan— sin
    que ninguna aserción se enterara.
    """
    return linea in respuesta.splitlines()


# --------------------------------------------------------------- lo que SÍ hay


def test_los_dias_y_la_hora_del_reparto_configurados_se_contestan(
    almacen: FakeRedis,
) -> None:
    """Lo que el dueño fijó llega al cliente, con los días como los dice una persona.

    MUTACIÓN MEDIDA: en `app/tools/entrega.py::_condiciones`,
    `poner("condiciones.hora", _valor("ENTREGA_HORA"))` ->
    `poner("condiciones.hora", _valor("RETIRO_LOCAL_HORA"))` — la línea lee la
    hora del RETIRO en vez de la del reparto. Es lo que este test no dice: dice
    que la hora se contesta, no de qué ajuste sale. Resultado: muere sólo éste
    (corrida entera 1 failed / 3402 passed).
    """
    _fijar(almacen, ENTREGA_DIAS="lunes,viernes", ENTREGA_HORA="09:00")

    respuesta = _responder()

    assert _dice(respuesta, "Días de reparto: lunes, viernes")
    assert _dice(respuesta, "Hora del reparto: 09:00")


def test_el_retiro_encendido_dice_cuando_se_puede_pasar_a_buscarlo(
    almacen: FakeRedis,
) -> None:
    """El retiro está construido y configurado; era invisible para quien lo pedía.

    MUTACIÓN MEDIDA: en `_condiciones`,
    `poner("condiciones.retiro_dias", _dias("RETIRO_LOCAL_DIAS", lengua))` ->
    `poner("condiciones.retiro_dias", _dias("ENTREGA_DIAS", lengua))` — los días
    de retiro salen de los días del reparto. El test dice que el retiro
    encendido trae sus días; no dice de qué ajuste. Resultado: muere sólo éste
    (corrida entera 4 failed / 3399 passed; los otros tres rojos son de
    `test_mcp_cliente.py` y `test_sim_banco.py`, que otras ramas estaban
    editando en paralelo, y se mueven de corrida en corrida).
    """
    _fijar(
        almacen,
        RETIRO_LOCAL_ACTIVO="true",
        RETIRO_LOCAL_DIAS="martes,jueves",
        RETIRO_LOCAL_HORA="10:00",
    )

    respuesta = _responder()

    assert _dice(respuesta, "Se puede pasar a buscar el pedido por el local: sí")
    assert _dice(respuesta, "Días para pasar a buscarlo: martes, jueves")
    assert _dice(respuesta, "Hora para pasar a buscarlo: 10:00")


def test_el_retiro_apagado_se_contesta_que_no_y_no_inventa_dias(
    almacen: FakeRedis,
) -> None:
    """Apagado es un «no» de verdad: el ajuste existe, tiene default y rige.

    Es la otra mitad del anterior, y la que distingue «no lo ofrecemos» de «no
    lo sé»: con `RETIRO_LOCAL_ACTIVO` en su default, el sistema no ofrece
    retiro, y decirlo es correcto.

    MUTACIÓN MEDIDA: en `_condiciones`,
    `{"true": si, "false": no}.get(retiro, "")` ->
    `{"true": si, "false": si}.get(retiro, "")` en la línea del retiro — el
    retiro apagado se contesta que sí. Resultado: muere sólo éste (corrida
    entera 2 failed / 3401 passed; el otro rojo es de otra rama en paralelo).
    """
    respuesta = _responder()

    assert _dice(respuesta, "Se puede pasar a buscar el pedido por el local: no")
    assert "Días para pasar a buscarlo" not in respuesta
    assert "Hora para pasar a buscarlo" not in respuesta


def test_el_cargo_y_el_minimo_de_una_entrega_fuera_de_dia_pasan_por_pesos(
    almacen: FakeRedis,
) -> None:
    """Un monto se lee como lo lee quien mira: `$1.500`, nunca `1500.0`.

    `pesos` deriva la forma del número de LOCALE (app/formato.py), que este
    archivo declara. Sin él, un cargo salía como el texto crudo del almacén.

    `_plata` tiene DOS consumidores —el cargo y el mínimo—, así que se afirman
    los dos y se mutó cada uno por separado.

    MUTACIÓN MEDIDA: en `_condiciones`,
    `poner("condiciones.minimo", _plata(_valor("ENTREGA_EXCEPCION_MIN_TOTAL")))`
    -> `poner("condiciones.minimo", _valor("ENTREGA_EXCEPCION_MIN_TOTAL"))` —
    el mínimo se escribe crudo. Resultado: muere sólo éste (corrida entera
    1 failed / 3405 passed). El otro consumidor se midió aparte —la misma
    mutación sobre `condiciones.cargo`— y también mata sólo a éste (2 failed /
    3406 passed, el segundo rojo de otra rama en paralelo).
    """
    _fijar(
        almacen,
        ENTREGA_EXCEPCION_ACTIVA="true",
        ENTREGA_EXCEPCION_CARGO="1500",
        ENTREGA_EXCEPCION_MIN_TOTAL="8000",
    )

    respuesta = _responder()

    assert _dice(respuesta, "Cargo por entregar fuera de los días de reparto: $1.500")
    assert _dice(respuesta, "Pedido mínimo para entregar fuera de día: $8.000")
    assert "1500" not in respuesta
    assert "8000" not in respuesta


# ------------------------------------------- lo que NO hay, que no es un «no»


def test_un_ajuste_que_falta_sale_como_faltante_y_la_instruccion_lo_desarma(
    almacen: FakeRedis,
) -> None:
    """«No lo tengo configurado» sólo sirve si viene con qué hacer con eso.

    Las dos mitades tienen que hablar de lo mismo: la línea marca el hueco y la
    instrucción final dice, CITANDO ESA MISMA FRASE, que eso no es un «no». Con
    dos frases distintas el modelo recibe una regla sobre un texto que no
    aparece en ninguna parte, que es lo mismo que no recibir ninguna.

    `sin_dato` tiene DOS consumidores —cada línea faltante y la instrucción—, y
    la mutación va contra el segundo, que es el que ningún test miraba.

    MUTACIÓN MEDIDA: en `_condiciones`,
    `idioma.t("condiciones.instruccion", lengua, sin_dato=sin_dato)` ->
    `idioma.t("condiciones.instruccion", lengua, sin_dato=idioma.t("ajustes.sin_configurar", lengua))`
    — la instrucción cita «sin configurar» mientras las líneas dicen «no lo
    tengo configurado». Resultado: muere sólo éste (corrida entera 2 failed /
    3406 passed; el otro rojo es de otra rama en paralelo).
    """
    sin_dato = idioma.t("condiciones.sin_dato", ES)

    respuesta = _responder()

    assert _dice(respuesta, idioma.t("condiciones.dias", ES, valor=sin_dato))
    instruccion = respuesta.splitlines()[-1]
    assert sin_dato in instruccion
    assert "no es un no" in instruccion


def test_una_regla_perdida_no_se_contesta_ni_que_si_ni_que_no(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Almacén vacío + cambios registrados en ERPNext = se perdió, no «está apagado».

    En ese estado `limites.entrega()` no ofrece NADA —tampoco lo del .env—, así
    que contestar «no se puede pasar a buscarlo» sería afirmar una política que
    no rige, y contestar los días del entorno sería prometer una ronda que el
    sistema no va a hacer.

    MUTACIÓN MEDIDA: en `_condiciones`,
    `{"true": si, "false": no}.get(retiro, "")` ->
    `{"true": si, "false": no}.get(retiro, no)` en la línea del retiro — un
    valor perdido cae en «no». Los dos tests del retiro (encendido y apagado)
    no se enteran: ninguno pasa por ese default. Resultado: muere sólo éste
    (corrida entera 2 failed / 3401 passed; el otro rojo es de otra rama).
    """
    monkeypatch.setattr(limites, "_hubo_cambios_durables_entrega", lambda: True)
    monkeypatch.setenv("ENTREGA_DIAS", "lunes,martes,miercoles,jueves,viernes")
    monkeypatch.setenv("RETIRO_LOCAL_ACTIVO", "true")

    sin_dato = idioma.t("condiciones.sin_dato", ES)
    respuesta = _responder()

    assert _dice(respuesta, idioma.t("condiciones.retiro", ES, valor=sin_dato))
    assert not _dice(
        respuesta, idioma.t("condiciones.retiro", ES, valor=idioma.t("ajustes.no", ES))
    )
    # Y el .env tampoco contesta por él: los días perdidos no son los de arranque.
    assert _dice(respuesta, idioma.t("condiciones.dias", ES, valor=sin_dato))
    assert "lunes" not in respuesta


def test_una_lectura_que_no_contesta_no_es_no_hay_nada_configurado(
    almacen: FakeRedis,
) -> None:
    """Redis caído no es una respuesta sobre la política del negocio.

    Misma postura que `consultar_stock` cuando no puede ver el depósito: se
    dice que no se pudo mirar y se manda a una persona, en vez de devolver una
    ficha entera de huecos que el modelo va a leer como «no tenemos nada».

    MUTACIÓN MEDIDA: en `app/tools/entrega.py::_valor`, envolver la lectura —
    `try: crudo = limites.vigente(nombre) / except limites.LimiteError: return ""`
    — así el fallo se traga y la herramienta contesta la ficha completa en
    blanco. Resultado: muere sólo éste (corrida entera 4 failed / 3404 passed;
    los otros tres rojos son de `test_mcp_cliente.py` y `test_memoria*.py`, que
    otras ramas estaban editando en paralelo).
    """
    almacen.caido = True

    respuesta = _responder("Alta Córdoba")

    assert respuesta == idioma.t("condiciones.no_pude", ES)
    assert idioma.t("condiciones.sin_dato", ES) not in respuesta
    assert "Días de reparto" not in respuesta


# ------------------------------------------------------------------ las zonas


def test_una_zona_de_la_lista_no_promete_la_entrega_de_este_pedido(
    almacen: FakeRedis,
) -> None:
    """Estar en la lista es la regla general; ESTE pedido lo mira una persona.

    `localidades` y `codigos` tienen DOS consumidores —las líneas del listado y
    la respuesta de zona—, así que se afirman los dos acá y se mutó cada
    consumidor por separado. Cada mutación mata un test DISTINTO, y eso es el
    resultado: la del veredicto muere en el test de la zona FUERA de la lista y
    ésta acá.

    MUTACIÓN MEDIDA: en `_condiciones`,
    `poner("condiciones.localidades", localidades)` ->
    `poner("condiciones.localidades", codigos)` — la línea del listado imprime
    la otra lista. Resultado: muere sólo éste (corrida entera 2 failed /
    3406 passed; el otro rojo es de otra rama en paralelo).

    Y LA QUE SOBREVIVIÓ, dicha en voz alta porque es la mitad que enseña algo:
    cruzar los argumentos de `_respuesta_de_zona` NO mata este test. Con las dos
    listas dadas vuelta «villa  allende» vuelve a matchear, esta vez como si
    fuera un código postal —`normalizar_cp` le saca el espacio y lo pone en
    mayúsculas—, así que el veredicto sigue siendo «entra». Por eso esa mutación
    se mide contra el test de la zona fuera de la lista, donde sí cambia la
    respuesta.
    """
    _fijar(
        almacen,
        ZONAS_ENTREGA_LOCALIDADES="Villa Allende, Córdoba",
        ZONAS_ENTREGA_CP="5000, X5105ABC",
    )

    respuesta = _responder("villa  allende")

    assert _dice(respuesta, "Localidades donde repartimos: Villa Allende, Córdoba")
    assert _dice(respuesta, "Códigos postales donde repartimos: 5000, X5105ABC")
    assert "villa  allende: entra en la zona de reparto" in respuesta
    assert "No se lo prometas para este pedido" in respuesta


def test_una_zona_fuera_de_la_lista_usa_la_fila_del_catalogo_y_no_la_deja_sola(
    almacen: FakeRedis,
) -> None:
    """`entrega.fuera_de_zona` existía sin un solo llamador. Acá lo tiene.

    Y no viaja sola: un «no» pelado es una puerta en la cara (CÓMO HABLÁS, en
    app/prompts.py), así que la misma línea trae qué SÍ hay.

    MUTACIÓN MEDIDA: en `_condiciones`,
    `_respuesta_de_zona(limpia, localidades, codigos, lengua)` ->
    `_respuesta_de_zona(limpia, codigos, localidades, lengua)` — las dos listas
    cruzadas. Es lo que este test no dice: dice qué se contesta cuando la zona
    no está, no de qué lista sale ese «no está». Con las listas dadas vuelta la
    localidad se busca contra una lista de códigos postales, no hay con qué
    contestarle, y la respuesta pasa a ser «pedile el código postal». Es el
    consumidor VEREDICTO de un valor que también alimenta las líneas del
    listado; el otro consumidor se mide en el test de la zona que SÍ está.
    Resultado: muere sólo éste (corrida entera 2 failed / 3406 passed; el otro
    rojo es de otra rama en paralelo).
    """
    _fijar(almacen, ZONAS_ENTREGA_LOCALIDADES="Villa Allende, Córdoba")

    respuesta = _responder("Alta Córdoba")

    assert idioma.t("entrega.fuera_de_zona", ES) in respuesta
    assert "que lo pase a buscar por el local" in respuesta
    assert "se lo pasás al encargado" in respuesta


def test_sin_zonas_cargadas_no_se_dice_que_no_repartimos(
    almacen: FakeRedis,
) -> None:
    """Sin listas no hay zona que contestar, y eso NO es «no llegamos».

    Es el mismo estado en que `app/entrega.py` contesta SIN_ZONAS y nada se
    entrega solo: falta la configuración, no la voluntad de repartir.

    MUTACIÓN MEDIDA: en `_respuesta_de_zona`,
    `if not nombres and not postales:` -> `if False:` — sin listas se cae a la
    rama de «pedile la localidad», que tampoco es un «no» pero deja de decir lo
    único que hay que decir acá. Los dos tests de zona de arriba tienen listas
    y no pasan por esa guarda. Resultado: muere sólo éste (corrida entera
    3 failed / 3405 passed; los otros dos rojos son de otras ramas).
    """
    respuesta = _responder("Alta Córdoba")

    assert _dice(respuesta, idioma.t("condiciones.zona_sin_listas", ES, zona="Alta Córdoba"))
    assert idioma.t("entrega.fuera_de_zona", ES) not in respuesta


# ------------------------------------------------------------------ el idioma


def test_la_respuesta_entera_cambia_de_idioma_y_ninguna_mitad_es_una_clave(
    almacen: FakeRedis,
) -> None:
    """El idioma sale del TELÉFONO que recibe la herramienta, no de un default.

    Los dos clientes de este test difieren en una sola cosa —uno tiene su
    idioma guardado— y la respuesta tiene que diferir por eso y no por otra
    cosa. Y se afirma que ninguna mitad salió como clave cruda: `idioma.t`
    devuelve la clave cuando la fila falta, así que «vino en inglés» no prueba
    que esté traducido.

    MUTACIÓN MEDIDA: en `app/tools/entrega.py::_dias`,
    `limites.mostrar(nombre, valor, lengua)` -> `limites.mostrar(nombre, valor)`
    — las etiquetas siguen traducidas y los días vuelven al idioma por defecto,
    o sea inglés con nombres de días en castellano. Resultado: muere sólo éste
    (corrida entera 1 failed / 3409 passed).
    """
    _fijar(almacen, ENTREGA_DIAS="lunes,viernes")
    assert idioma.recordar_cliente(OTRO_CLIENTE, EN)

    en_espanol = _responder(telefono=CLIENTE)
    en_ingles = _responder(telefono=OTRO_CLIENTE)

    assert en_espanol != en_ingles
    assert _dice(en_espanol, "Días de reparto: lunes, viernes")
    assert _dice(en_ingles, "Delivery days: Monday, Friday")
    for respuesta in (en_espanol, en_ingles):
        assert "condiciones." not in respuesta
        assert "entrega.fuera_de_zona" not in respuesta
        assert "ajustes." not in respuesta


# ------------------------------------------- alcanzable, y sin escribir nada


def test_la_herramienta_la_tiene_el_cliente_y_no_gerencia(almacen: FakeRedis) -> None:
    """Definir el `@tool` NO lo hace alcanzable: el agente se arma de una lista.

    Todos los tests de arriba llaman a la función directamente, así que todos
    pueden estar verdes con la herramienta sin registrar y el cliente
    escuchando «esa herramienta no existe». Se afirma el REGISTRO.

    MUTACIÓN MEDIDA: en `app/graph.py`, sacar `condiciones_de_entrega` de
    `TOOLS_CLIENTES` (dejando el import). Resultado: muere sólo éste (corrida
    entera 4 failed / 3406 passed; los otros tres rojos son de
    `test_memoria_cableado.py`, que otra rama estaba editando en paralelo).
    """
    from app import graph

    assert "condiciones_de_entrega" in {h.name for h in graph.TOOLS_CLIENTES}
    # Fuera de gerencia a propósito: el dueño ya lee y CAMBIA estos doce
    # ajustes con `ver_ajustes` y `proponer_limite`, que además dicen de dónde
    # sale cada valor. Una segunda puerta a lo mismo es solapamiento, que es lo
    # que degrada la elección del modelo.
    assert "condiciones_de_entrega" not in {h.name for h in graph.TOOLS_GERENCIA}


def test_contestar_las_condiciones_no_escribe_nada(almacen: FakeRedis) -> None:
    """Sólo lectura, y se mide: el almacén queda idéntico a como estaba.

    MUTACIÓN MEDIDA: en `app/tools/entrega.py::_valor`, agregar
    `locks.conexion().hset(limites.CLAVE_VALORES, nombre, crudo)` antes del
    `return` — una sola escritura, del valor que acaba de leer, o sea la que
    menos se nota: fija en el almacén lo que hasta entonces venía del entorno.
    Resultado: muere sólo éste (corrida entera 2 failed / 3408 passed; el otro
    rojo es de otra rama en paralelo).
    """
    _fijar(
        almacen,
        ENTREGA_DIAS="lunes,viernes",
        ZONAS_ENTREGA_LOCALIDADES="Villa Allende",
        RETIRO_LOCAL_ACTIVO="true",
    )
    antes = (
        copy.deepcopy(almacen.hashes),
        copy.deepcopy(almacen.strings),
        copy.deepcopy(almacen.lists),
        copy.deepcopy(almacen.zsets),
    )

    _responder("Alta Córdoba")

    assert (
        copy.deepcopy(almacen.hashes),
        copy.deepcopy(almacen.strings),
        copy.deepcopy(almacen.lists),
        copy.deepcopy(almacen.zsets),
    ) == antes
