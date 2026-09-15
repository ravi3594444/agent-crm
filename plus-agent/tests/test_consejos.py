"""El detector de consejos: Python decide SI y A QUIÉN, el modelo sólo CÓMO.

QUÉ VIGILA ESTE ARCHIVO, por encima de los cuatro detectores

1. **Un valor derivado con varios consumidores se muta por separado.** `tick`
   saca `dia` de `momento` UNA vez y se lo da a `restante`, a `detectar` y a
   `reclamar`; el momento entero se lo da además a `en_silencio`. Es la forma
   exacta del defecto que este repo ya se comió: `digest.enviar()` derivaba
   `dia`, se lo pasaba a dos callees, un test mutaba uno de los dos y los 2481
   tests quedaban en verde con la composición fechada mal. Cada consumidor tiene
   su test y su mutación, y están anotadas al lado.

2. **Un doble deriva de lo que RECIBE.** El doble de `detectar` fabrica la clave
   del consejo a partir del `dia` que le pasan, así que no puede estar de acuerdo
   con el código por construcción; el de `en_silencio` guarda el momento que
   recibió; el de `inventario.confiable` (el de `tests/conftest.py`) honra
   `ignorar_postura`, que es lo que hace visible la mutación de la postura de
   stock.

3. **La trampa de los tres campos del Item Price.** `price_list`, `currency` y
   `uom` se vuelven a comprobar en Python después de haberlos filtrado en la
   consulta, igual que `policy._precio_estandar`. Los tests de `_costo_de` le
   pasan filas a mano —sin pasar por el filtro— porque si no el filtro del doble
   taparía la comprobación y borrar cualquiera de las tres líneas no mataría
   ningún test. Ésa es la mitad que importa: la comprobación existe justamente
   para no confiar en el filtro.

4. **Nada de esto puede escribir.** `test_un_consejo_no_puede_escribir_nada` lee
   el AST de `app/consejos.py` y exige que cada `modulo.funcion` que el archivo
   nombra esté en una lista blanca de LECTURAS.
"""
from __future__ import annotations

import ast
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import RelojDePrueba, inventario_confiable

from app import consejos, erpnext, excepciones, notificar, pendientes, router
from tests import fakes

pytestmark = [pytest.mark.idioma("es"), pytest.mark.locale("es_AR")]

# El día que este archivo nombra. Lunes, que es lo que usan los tests de reparto.
RELOJ = RelojDePrueba("2026-09-14")
HOY = RELOJ.hoy

DUENO = "5493519999999"  # ordena DESPUÉS del empleado, como en tests/test_digest.py
EMPLEADO = "5493511111111"

LISTA_COSTO = "Costo Test"
DEPOSITO = "Principal - LT"  # el de tests/conftest.py
EMPRESA = "Lacteos Test SA"


# ---------------------------------------------------------------- el doble ERP


class FalsoERP:
    """Un ERPNext de mentira que HONRA los filtros, el orden y el techo.

    Delega en `tests/fakes.listar`, que es el doble de consulta del repo, y le
    agrega dos cosas que ese doble no hace y que acá hacen falta para que el test
    pueda discrepar con el código:

      * los operadores de RANGO (`>=`, `<=`), que `fakes._coincide` deja pasar
        siempre. Sin ellos la ventana de días de cada detector sería invisible y
        una ventana cambiada no rompería nada.
      * la PROYECCIÓN a `fields`. Frappe devuelve sólo las columnas pedidas, y un
        doble que devuelve la fila entera hace que pedir mal los campos no tenga
        consecuencia: sacar `stock_qty` de la lista de `_demanda_diaria` no
        mataba un solo test mientras el doble regalaba el campo igual.
    """

    def __init__(self) -> None:
        self.tablas: dict[str, list[dict]] = {
            "Sales Order": [],
            "Sales Order Item": [],
            "Item Price": [],
            "Item Reorder": [],
            "Bin": [],
        }
        self.cobranzas: list[dict] = []
        self.leidas: list[str] = []
        self.rompe: set[str] = set()

    @staticmethod
    def _rango(filas: list[dict], filtros) -> tuple[list[dict], list]:
        resto = []
        for filtro in filtros or []:
            try:
                campo, operador, esperado = filtro[0], str(filtro[1]), filtro[2]
            except (IndexError, TypeError):
                resto.append(filtro)
                continue
            if operador == ">=":
                filas = [f for f in filas if str(f.get(campo) or "") >= str(esperado)]
            elif operador == "<=":
                filas = [f for f in filas if str(f.get(campo) or "") <= str(esperado)]
            else:
                resto.append(filtro)
        return filas, resto

    def get_list(
        self,
        doctype,
        filters=None,
        fields=None,
        limit=20,
        parent=None,
        order_by=None,
        start=0,
        timeout=None,
    ):
        self.leidas.append(doctype)
        if doctype in self.rompe:
            raise erpnext.ERPNextError(f"{doctype} no contesta")
        filas, resto = self._rango(self.tablas.get(doctype, []), filters)
        elegidas = fakes.listar(filas, resto, limit=limit, order_by=order_by, start=start)
        if not fields:
            return elegidas
        return [{c: f[c] for c in fields if c in f} for f in elegidas]

    def run_report(self, nombre, filtros=None):
        self.leidas.append(f"report:{nombre}")
        if "report" in self.rompe:
            raise erpnext.ERPNextError("el reporte no contesta")
        return list(self.cobranzas)


@pytest.fixture
def erp(monkeypatch):
    falso = FalsoERP()
    monkeypatch.setattr(erpnext, "policy_get_list", falso.get_list)
    monkeypatch.setattr(erpnext, "policy_run_report", falso.run_report)
    return falso


@pytest.fixture
def dueno(monkeypatch):
    """Un dueño resoluble: TELEFONO_DUENO dentro de TELEFONOS_EQUIPO."""
    monkeypatch.setattr(router, "STAFF", [EMPLEADO, DUENO])
    monkeypatch.setenv("TELEFONO_DUENO", DUENO)
    return DUENO


@pytest.fixture
def despierto(monkeypatch):
    """Ni horas de silencio ni interruptor apagado: los frenos que no se prueban acá."""
    monkeypatch.setenv("CONSEJOS_ACTIVO", "true")
    monkeypatch.setattr(pendientes, "en_silencio", lambda momento=None: False)


def un_consejo(clave: str, *, clase: str = consejos.PERDIDA, peso: float = 1.0) -> consejos.Consejo:
    return consejos.Consejo(
        clase=clase,
        clave=clave,
        sobre=clave,
        titulo="t",
        cuerpo="c",
        peso=peso,
    )


def soltar_barrido(redis) -> None:
    """Devolver el turno de barrido, para un test que llama `tick` dos veces."""
    redis.delete(consejos._clave_barrido())


# ===========================================================================
# 1. El reclamo y el presupuesto
# ===========================================================================


def test_un_consejo_repetido_no_se_repite_ni_gasta_presupuesto(limites_sin_redis):
    """DOS MUTACIONES DIRIGIDAS, y cada una mata sólo este test:

      * `set(..., nx=True)` -> `set(...)`: sin el NX el mismo consejo se reclama
        dos veces, o sea que el dueño lo escucha dos veces. Es el `SET NX EX` de
        `digest.reclamar`, donde reclamar y marcar son una sola operación.
      * mover el `INCR` del contador ARRIBA del `SET NX`: el consejo repetido
        —que no se va a mandar— pasa a comerse el cupo del día, y tres repetidos
        dejan al dueño sin enterarse de lo que sí es nuevo.

    Van juntas en un test porque son las dos mitades de la misma línea: qué pasa
    cuando el mismo hecho vuelve a detectarse mañana. Separarlas en dos tests
    hacía que cada mutación matara los dos y no midiera nada.
    """
    primero = un_consejo("perdida:SO-1")
    assert consejos.reclamar(primero, HOY) == consejos.NUEVO
    assert consejos.reclamar(primero, HOY) == consejos.REPETIDO
    assert consejos.reclamar(primero, HOY) == consejos.REPETIDO

    # El repetido no gastó: todavía entran los otros dos del día.
    assert consejos.reclamar(un_consejo("perdida:SO-2"), HOY) == consejos.NUEVO
    assert consejos.reclamar(un_consejo("perdida:SO-3"), HOY) == consejos.NUEVO
    assert consejos.reclamar(un_consejo("perdida:SO-4"), HOY) == consejos.SIN_PRESUPUESTO
    assert consejos.restante(HOY) == 0


def test_el_consejo_que_no_entro_en_el_presupuesto_queda_libre_para_manana(limites_sin_redis):
    """MUTACIÓN: borrar el `devolver(consejo)` de la rama de sin presupuesto.

    Mata sólo este test. Sin eso, el consejo número cuatro queda marcado como
    dicho sin haberse dicho nunca: el dueño no se entera hoy porque no hay cupo
    y no se entera mañana porque la clave ya está tomada.
    """
    for i in range(consejos.MAX_CONSEJOS_POR_DIA):
        assert consejos.reclamar(un_consejo(f"perdida:SO-{i}"), HOY) == consejos.NUEVO
    cuarto = un_consejo("perdida:SO-tarde")
    assert consejos.reclamar(cuarto, HOY) == consejos.SIN_PRESUPUESTO
    assert limites_sin_redis.get(consejos._clave_consejo(cuarto.clave)) is None

    # Mañana, con el contador del día nuevo en cero, sí entra.
    assert consejos.reclamar(cuarto, HOY + timedelta(days=1)) == consejos.NUEVO


def test_sin_redis_el_presupuesto_no_es_cero_es_desconocido(limites_sin_redis):
    """MUTACIÓN: en el `except` de `restante`, `return -1` -> `return 0`.

    Mata sólo este test. Los dos frenan, pero no son lo mismo: 0 es «ya hablé
    tres veces hoy» y -1 es «no sé cuántas veces hablé», y con lo segundo no se
    puede prometer «una sola vez». Aplastarlos deja al operador sin poder
    distinguir un día lleno de un Redis caído.
    """
    assert consejos.restante(HOY) == consejos.MAX_CONSEJOS_POR_DIA
    limites_sin_redis.caido = True
    assert consejos.restante(HOY) == -1


def test_si_el_presupuesto_falla_el_consejo_no_queda_dicho_sin_decirse(
    limites_sin_redis, monkeypatch
):
    """El `SET NX` YA GANÓ cuando el contador falla, y eso deja un rastro.

    `reclamar` marca el consejo y recién después toca el presupuesto. Si el
    `INCR` o el `EXPIRE` levantan, se vuelve con `SIN_REDIS` — y `tick` no
    devuelve ese estado, porque no es uno de los que mira. Así que la clave del
    reclamo se queda puesta sus 24 horas: el consejo figura como dicho sin que
    nadie lo haya oído, y no se reintenta en todo el día.

    El contador NO se baja, a propósito: puede haber sido el `EXPIRE` el que
    falló y el `INCR` haber pasado, y ahí un decremento le regalaría presupuesto
    a un consejo que sí lo gastó. De más se corrige con el TTL; de menos, no.

    MUTACIÓN: sacar el `devolver(consejo)` del `except` interno de `reclamar`.
    Cae este test y sólo éste.
    """
    consejo = un_consejo("perdida:SO-9")
    monkeypatch.setattr(
        limites_sin_redis, "incr", Mock(side_effect=ConnectionError("redis"))
    )

    assert consejos.reclamar(consejo, HOY) == consejos.SIN_REDIS
    assert limites_sin_redis.get(consejos._clave_consejo(consejo.clave)) is None


def test_un_reclamo_sin_redis_no_se_confunde_con_uno_repetido(limites_sin_redis):
    """MUTACIÓN: en el `except` de `reclamar`, `return SIN_REDIS` -> `return REPETIDO`.

    Mata sólo este test. `REPETIDO` no corta el recorrido (el siguiente consejo
    puede ser nuevo) y `SIN_REDIS` sí: con la mutación, un Redis caído haría que
    `tick` siguiera preguntando por los veinte consejos de la lista en vez de
    parar en el primero.
    """
    limites_sin_redis.caido = True
    assert consejos.reclamar(un_consejo("deuda:C-1:30", clase=consejos.DEUDA), HOY) == (
        consejos.SIN_REDIS
    )
    assert consejos.SIN_REDIS in consejos.CORTAN
    assert consejos.REPETIDO not in consejos.CORTAN


# ===========================================================================
# 2. tick: los frenos, y el día derivado una vez con varios consumidores
# ===========================================================================


@pytest.fixture
def detector_que_deriva(monkeypatch):
    """Un doble de `detectar` que FABRICA la clave con el día que recibe.

    No puede estar de acuerdo con el código por construcción: si `tick` le pasa
    otro día, la clave del consejo que vuelve lo dice. Es el arreglo del defecto
    que `tests/test_digest.py::mundo` documenta, una capa más abajo.
    """
    vistos: list[date] = []

    def falso(dia: date) -> list[consejos.Consejo]:
        vistos.append(dia)
        return [un_consejo(f"perdida:{dia.isoformat()}")]

    monkeypatch.setattr(consejos, "detectar", falso)
    return vistos


def test_tick_detecta_con_el_dia_del_momento_que_recibio(
    limites_sin_redis, erp, dueno, despierto, detector_que_deriva
):
    """MUTACIÓN 1 de 3 sobre el `dia` derivado: `detectar(dia - timedelta(days=1))`.

    Mata sólo este test. El doble fecha la clave con lo que le pasan, así que
    componer el consejo contra el día de ayer se ve acá y en ningún otro lado —
    exactamente el agujero por el que `digest.enviar` compuso el resumen del día
    equivocado con 2481 tests en verde.
    """
    salida = consejos.tick(ahora=RELOJ.a_las(10))
    assert detector_que_deriva == [HOY]
    assert [c.clave for c in salida] == [f"perdida:{HOY.isoformat()}"]


def test_tick_gasta_el_presupuesto_del_dia_que_esta_corriendo(
    limites_sin_redis, erp, dueno, despierto, monkeypatch
):
    """MUTACIÓN 2 de 3 sobre el `dia` derivado: `reclamar(consejo, dia - timedelta(days=1))`.

    Mata sólo este test. El contador que se incrementa es el de OTRO día, así que
    el de hoy queda en cero: el segundo `tick` del mismo día volvería a mandar
    tres consejos más, y el tope diario sería un tope de nada.
    """
    monkeypatch.setattr(
        consejos,
        "detectar",
        lambda dia: [un_consejo(f"perdida:SO-{i}", peso=float(10 - i)) for i in range(5)],
    )
    primera = consejos.tick(ahora=RELOJ.a_las(10))
    assert len(primera) == consejos.MAX_CONSEJOS_POR_DIA
    assert limites_sin_redis.get(consejos._clave_presupuesto(HOY)) is not None
    assert consejos.restante(HOY) == 0

    soltar_barrido(limites_sin_redis)
    assert consejos.tick(ahora=RELOJ.a_las(11)) == []


def test_tick_no_lee_erpnext_cuando_el_presupuesto_del_dia_ya_se_gasto(
    limites_sin_redis, erp, dueno, despierto, detector_que_deriva
):
    """MUTACIÓN 3 de 3 sobre el `dia` derivado: `restante(dia - timedelta(days=1))`.

    Mata sólo este test. Con la mutación el contador consultado es el de ayer
    —vacío—, así que el freno no frena y el barrido sale a leer ERPNext igual.
    Es el freno que evita seis consultas por minuto en un día que ya habló.
    """
    limites_sin_redis.strings[consejos._clave_presupuesto(HOY)] = str(
        consejos.MAX_CONSEJOS_POR_DIA
    )
    assert consejos.tick(ahora=RELOJ.a_las(10)) == []
    assert detector_que_deriva == []


def test_a_las_tres_de_la_manana_no_se_lee_nada_y_el_momento_es_el_recibido(
    limites_sin_redis, erp, dueno, monkeypatch, detector_que_deriva
):
    """MUTACIÓN: `pendientes.en_silencio(momento + timedelta(hours=12))`.

    Mata sólo este test. Los demás le ponen a `en_silencio` un doble que contesta
    False pase lo que pase, así que correrles el momento no los mueve; acá el
    doble decide CON el momento que recibe y además lo guarda, que son las dos
    mitades: que la ventana nocturna frene, y que la frene con el momento que
    `tick` recibió y no con otro.

    Las horas de silencio no se re-implementan acá: `pendientes.en_silencio` es
    la única que sabe que la ventana cruza la medianoche.
    """
    monkeypatch.setenv("CONSEJOS_ACTIVO", "true")
    recibidos: list[datetime] = []

    def falso(momento=None):
        recibidos.append(momento)
        return momento.hour >= 22 or momento.hour < 7

    monkeypatch.setattr(pendientes, "en_silencio", falso)

    madrugada = RELOJ.a_las(3)
    assert consejos.tick(ahora=madrugada) == []
    assert recibidos == [madrugada]
    assert detector_que_deriva == []


def test_sin_dueno_resoluble_no_se_detecta_nada(
    limites_sin_redis, erp, despierto, detector_que_deriva, monkeypatch
):
    """MUTACIÓN: borrar el `if not destinatario(): return []` de `tick`.

    Mata sólo este test. Un consejo que no sabe a quién va no es un consejo, y
    además leer ERPNext para no decirle nada a nadie es seis consultas tiradas.

    Acá `destinatario` va como doble a propósito: QUIÉN es el dueño es una
    pregunta aparte, y la prueba de que la contesta `TELEFONO_DUENO` y no el
    primero del equipo vive en `test_el_destinatario_es_el_dueno_...`. Con la
    función real, una mutación de la ruta mataba los dos tests y dejaba de medir
    cuál de las dos cosas protege cada uno.
    """
    monkeypatch.setattr(consejos, "destinatario", lambda: "")
    assert consejos.tick(ahora=RELOJ.a_las(10)) == []
    assert detector_que_deriva == []


def test_el_destinatario_es_el_dueno_y_no_el_primero_del_equipo(monkeypatch):
    """MUTACIÓN: en `destinatario()`, devolver el primer teléfono del equipo
    cuando `telefono_dueno()` contesta algo (o sea, conservar el «hay dueño» y
    cambiar CUÁL es).

    Mata sólo este test. `EMPLEADO` ordena antes que `DUENO`, así que la ruta
    genérica del equipo devuelve el empleado; el consejo es para el dueño y
    `TELEFONO_DUENO` es lo único que lo dice. Es el mismo par de números que usa
    `tests/test_digest.py` por el mismo motivo.
    """
    monkeypatch.setattr(router, "STAFF", [EMPLEADO, DUENO])
    monkeypatch.setenv("TELEFONO_DUENO", DUENO)
    assert consejos.destinatario() == DUENO
    assert notificar._destinatarios(router)[0] == EMPLEADO


def test_apagado_de_fabrica(limites_sin_redis, erp, dueno, detector_que_deriva, monkeypatch):
    """MUTACIÓN: el default de `CONSEJOS_ACTIVO` pasa de `"false"` a `"true"`.

    Mata sólo este test. Nada que le hable al dueño por su cuenta se enciende
    solo: es la misma postura que `AUTO_CONFIRM_MAX=0`, y dos semanas antes de
    que Meta empiece a cobrar por mensaje no es un detalle de estilo.
    """
    monkeypatch.delenv("CONSEJOS_ACTIVO", raising=False)
    assert consejos.activo() is False
    assert consejos.tick(ahora=RELOJ.a_las(10)) == []
    assert detector_que_deriva == []


def test_un_solo_barrido_de_erpnext_por_hora(
    limites_sin_redis, erp, dueno, despierto, detector_que_deriva
):
    """MUTACIÓN: borrar el `if not _puedo_barrer(): return []` de `tick`.

    Mata sólo este test. El barrido corre en el hilo que late cada 60 s; sin este
    freno serían 8.640 lecturas de ERPNext por día para encontrar como mucho
    tres cosas.
    """
    assert len(consejos.tick(ahora=RELOJ.a_las(10))) == 1
    assert consejos.tick(ahora=RELOJ.a_las(10, 1)) == []
    # Cuántas veces se barrió, no CON QUÉ DÍA: el día es de
    # `test_tick_detecta_con_el_dia_del_momento_que_recibio`, y afirmarlo también
    # acá haría que una mutación del día matara los dos y no midiera nada.
    assert len(detector_que_deriva) == 1


def test_el_presupuesto_se_lleva_los_mejores_rankeados(
    limites_sin_redis, erp, dueno, despierto, monkeypatch
):
    """MUTACIÓN: en `detectar`, `return ordenar(encontrados)` -> `return encontrados`.

    Mata sólo este test. El tope diario sólo sirve si lo que entra es lo que más
    importa; sin el orden, los tres que salen son los tres que ERPNext devolvió
    primero. (El orden en sí lo prueba `test_el_ranking_es_clase_peso_y_clave`,
    que llama a `ordenar` directo y no se muere con esta mutación.)
    """
    detectores = (
        lambda dia: [un_consejo("dormido:C-9", clase=consejos.DORMIDO, peso=999.0)],
        lambda dia: [
            un_consejo("perdida:SO-1", peso=10.0),
            un_consejo("perdida:SO-2", peso=50.0),
        ],
        lambda dia: [un_consejo("deuda:C-1:30", clase=consejos.DEUDA, peso=1.0)],
    )
    monkeypatch.setattr(consejos, "DETECTORES", detectores)
    salida = consejos.tick(ahora=RELOJ.a_las(10))
    assert [c.clave for c in salida] == ["perdida:SO-2", "perdida:SO-1", "deuda:C-1:30"]


def test_el_ranking_es_clase_peso_y_clave():
    """MUTACIÓN: en `ordenar`, el desempate `c.clave` -> `""`.

    Mata sólo este test. Sin desempate, dos consejos con el mismo peso salen en
    el orden en que los devolvió ERPNext, así que cuál de los dos entra en el
    cupo de tres por día depende de la base de datos y no de una regla — y el
    mismo par puede salir al revés mañana.

    (La mutación obvia —dar vuelta `-c.peso`— mata también
    `test_el_presupuesto_se_lleva_los_mejores_rankeados`: mide que los dos tests
    tocan el mismo `sorted`, no lo que cada uno protege.)
    """
    desordenados = [
        un_consejo("dormido:C-1", clase=consejos.DORMIDO, peso=100.0),
        un_consejo("perdida:SO-b", peso=5.0),
        un_consejo("perdida:SO-a", peso=5.0),
        un_consejo("perdida:SO-c", peso=80.0),
        un_consejo("quiebre:X", clase=consejos.QUIEBRE, peso=1.0),
        un_consejo("deuda:C-2:60", clase=consejos.DEUDA, peso=2.0),
    ]
    assert [c.clave for c in consejos.ordenar(desordenados)] == [
        "perdida:SO-c",
        "perdida:SO-a",
        "perdida:SO-b",
        "deuda:C-2:60",
        "quiebre:X",
        "dormido:C-1",
    ]


def test_un_detector_caido_no_se_lleva_a_los_otros(monkeypatch):
    """MUTACIÓN: sacar el `try/except` de adentro del `for` de `detectar`.

    Mata sólo este test. Es la misma forma que el barrido de `app/main.py`, que
    envuelve `solicitudes.tick()`, `pendientes.tick()` y `agenda.tick()` cada uno
    en su propio `except`: que las cobranzas no contesten no puede tapar una
    venta por debajo del costo.
    """

    def explota(dia):
        raise erpnext.ERPNextError("ERPNext no contesta")

    monkeypatch.setattr(
        consejos,
        "DETECTORES",
        (explota, lambda dia: [un_consejo("perdida:SO-1")]),
    )
    assert [c.clave for c in consejos.detectar(HOY)] == ["perdida:SO-1"]


# ===========================================================================
# 3. El costo: los tres campos del Item Price
# ===========================================================================
#
# Estos llaman a `_costo_de` DIRECTO, con filas a mano. Si pasaran por el doble
# de consulta, el filtro descartaría las filas antes de llegar acá y borrar
# cualquiera de las tres comprobaciones no mataría un solo test — que es
# justamente lo que la comprobación existe para no permitir.


def precio(**campos) -> dict:
    base = {
        "item_code": "LECHE-1L",
        # `buying` va en el FILTRO de la consulta y no se vuelve a comprobar en
        # Python, igual que `selling` en `policy._precio_estandar`: el doble de
        # consulta honra el filtro, así que una fila sin esto no llega nunca.
        "buying": 1,
        "price_list_rate": 120.0,
        "price_list": LISTA_COSTO,
        "currency": "ARS",
        "uom": "Litro",
        "valid_from": "",
        "valid_upto": "",
    }
    base.update(campos)
    return base


def test_un_costo_en_otra_unidad_no_se_usa():
    """MUTACIÓN: borrar la comparación de `uom` en `_costo_de`.

    Mata sólo este test. Es la mitad que hace que la resta signifique algo: el
    renglón cobra por litro y el costo del cajón es doce veces más, así que sin
    esta línea el consejo diría «perdiste $1.100» sobre una venta con margen.
    `policy._precio_estandar` hace exactamente lo mismo, y el `.env` de este
    proyecto ya produjo dos veces un catálogo sin `uom`.
    """
    filas = [precio(uom="Cajon", price_list_rate=1200.0)]
    assert _costo(filas, "Litro") is None
    assert _costo([precio(uom="")], "Litro") is None


def test_un_costo_de_otra_lista_no_se_usa():
    """MUTACIÓN: borrar la comparación de `price_list` en `_costo_de`.

    Mata sólo este test. El consejo NOMBRA la lista contra la que comparó, así
    que comparar contra otra lo vuelve una afirmación falsa firmada.
    """
    assert _costo([precio(price_list="Otra Lista")], "Litro") is None
    assert _costo([precio(price_list="")], "Litro") is None


def test_un_costo_en_otra_moneda_no_se_usa():
    """MUTACIÓN: borrar la comparación de `currency` en `_costo_de`.

    Mata sólo este test. 120 dólares y 120 pesos son el mismo número y no el
    mismo costo; sin esta línea un costo en USD contra un precio en ARS siempre
    parece una pérdida enorme.
    """
    assert _costo([precio(currency="USD")], "Litro") is None
    assert _costo([precio(currency="")], "Litro") is None


def test_un_costo_vencido_no_se_usa():
    """MUTACIÓN: borrar la comprobación `desde <= dia <= hasta` de `_costo_de`.

    Mata sólo este test. Un costo con vigencia cerrada antes del día del pedido
    es historia, y el margen de una venta se mide contra lo que costaba ese día.
    """
    ayer = (HOY - timedelta(days=1)).isoformat()
    manana = (HOY + timedelta(days=1)).isoformat()
    assert _costo([precio(valid_upto=ayer)], "Litro") is None
    assert _costo([precio(valid_from=manana)], "Litro") is None
    assert _costo([precio(valid_from=ayer, valid_upto=manana)], "Litro") == 120.0


def test_entre_dos_costos_vigentes_gana_el_mas_nuevo():
    """MUTACIÓN: `max(desde for desde, _ in candidatos)` -> `min(...)`.

    Mata sólo este test. Con dos precios vigentes, el que vale es el que el dueño
    cargó último; el viejo describe lo que costaba antes.
    """
    viejo = precio(valid_from=(HOY - timedelta(days=90)).isoformat(), price_list_rate=90.0)
    nuevo = precio(valid_from=(HOY - timedelta(days=2)).isoformat(), price_list_rate=130.0)
    assert _costo([viejo, nuevo], "Litro") == 130.0


def test_un_empate_de_costos_se_resuelve_por_el_mas_barato():
    """MUTACIÓN: `min(tarifa for ...)` -> `max(tarifa for ...)`.

    Mata sólo este test. Con dos costos igual de vigentes el consejo se calla
    antes de acusar: el costo más bajo es el que produce la pérdida más chica, o
    ninguna. Un consejo que dice «perdiste plata» tiene que equivocarse para el
    lado del silencio.
    """
    desde = (HOY - timedelta(days=5)).isoformat()
    filas = [
        precio(valid_from=desde, price_list_rate=90.0),
        precio(valid_from=desde, price_list_rate=130.0),
    ]
    assert _costo(filas, "Litro") == 90.0


def _costo(filas: list[dict], uom: str) -> float | None:
    return consejos._costo_de(filas, "LECHE-1L", uom, LISTA_COSTO, "ARS", HOY)


# ===========================================================================
# 4. Detector 1: la venta por debajo del costo
# ===========================================================================


def pedido(nombre: str, *, dia: date | None = None, moneda: str = "ARS") -> dict:
    return {
        "name": nombre,
        "customer": "C-1",
        "customer_name": "Almacén Doña Rosa",
        "currency": moneda,
        "transaction_date": (dia or HOY).isoformat(),
        "docstatus": 1,
        "company": EMPRESA,
    }


def renglon(padre: str, code: str, *, qty: float, rate: float, uom: str = "Litro") -> dict:
    return {
        "parent": padre,
        "item_code": code,
        "item_name": code,
        "qty": qty,
        "uom": uom,
        "rate": rate,
        "docstatus": 1,
    }


@pytest.fixture
def con_lista_de_costo(monkeypatch):
    monkeypatch.setenv("CONSEJOS_LISTA_COSTO", LISTA_COSTO)


def test_una_venta_por_debajo_del_costo_es_un_consejo(erp, con_lista_de_costo):
    """MUTACIÓN: `peso=float(entrada["perdida"])` -> `peso=0.0` en el `Consejo`.

    Mata sólo este test, que es el único que afirma cuánta plata dice el consejo.
    No es cosmética: el `peso` es lo que ordena dentro de la clase, así que una
    pérdida de $200.000 con peso 0 sale ÚLTIMA y la desplaza del cupo de tres por
    día una pérdida de $2.

    (La mutación obvia —dar vuelta el signo de `(costo - rate)`— mata TRES tests:
    mide el acoplamiento incidental de los tres tests de pérdidas contra la misma
    resta, no lo que este test protege. Queda anotada como lo que es.)
    """
    erp.tablas["Sales Order"] = [pedido("SO-1")]
    erp.tablas["Sales Order Item"] = [renglon("SO-1", "LECHE-1L", qty=10, rate=100.0)]
    erp.tablas["Item Price"] = [precio()]

    salida = consejos.perdidas(HOY)
    assert len(salida) == 1
    solo = salida[0]
    assert solo.clase == consejos.PERDIDA
    assert solo.clave == "perdida:SO-1"
    assert solo.sobre == "SO-1"
    assert solo.peso == pytest.approx(200.0)
    assert solo.datos["lista_costo"] == LISTA_COSTO
    assert solo.datos["renglones"][0]["costo"] == 120.0
    assert solo.datos["sin_costo"] == []
    assert LISTA_COSTO in solo.cuerpo


def test_el_costo_se_mide_contra_el_dia_del_pedido_y_no_contra_hoy(erp, con_lista_de_costo):
    """MUTACIÓN: en `perdidas`, `_costo_de(..., dia_pedido)` -> `_costo_de(..., dia)`.

    Mata sólo este test. El margen de una venta de hace cinco días se mide contra
    lo que costaba ESE día; con el costo de hoy, una lista que se renovó
    anteayer hace desaparecer todas las pérdidas de la semana pasada sin un error
    en ninguna parte. Los otros tests usan precios sin vigencia, así que no se
    mueven con esta mutación.
    """
    hace_cinco = HOY - timedelta(days=5)
    erp.tablas["Sales Order"] = [pedido("SO-2", dia=hace_cinco)]
    erp.tablas["Sales Order Item"] = [renglon("SO-2", "LECHE-1L", qty=10, rate=100.0)]
    erp.tablas["Item Price"] = [
        precio(
            valid_from=(HOY - timedelta(days=30)).isoformat(),
            valid_upto=(HOY - timedelta(days=2)).isoformat(),
        )
    ]
    salida = consejos.perdidas(HOY)
    assert [c.clave for c in salida] == ["perdida:SO-2"]


def test_el_total_de_la_perdida_dice_en_que_moneda_es(erp, con_lista_de_costo):
    """MUTACIÓN: `perdida=total` -> `perdida=pesos(entrada["perdida"], 2)`.

    Cae éste y sólo éste. `pesos` escribe el símbolo que elige `LOCALE` y nunca
    un código, así que un pedido en INR y uno en ARS le llegaban al dueño
    idénticos —«$4.800,00» los dos— y la moneda quedaba sólo en `datos`, que él
    no lee. Es el mismo defecto que `notificar.pedir_confirmacion` ya había
    arreglado del lado de la autorización.

    Se prueban las DOS monedas sobre la misma pérdida: con una sola, un código
    pegado a mano en la plantilla pasaría igual.

    Y LOS PEDIDOS NO SE LLAMAN `SO-INR` NI `SO-ARS`, que es como estaba escrito
    y por qué la mutación SOBREVIVIÓ la primera vez que se corrió: el cuerpo
    interpola `{pedido}`, así que «INR in cuerpo» lo cumplía el NÚMERO DE
    PEDIDO y el assert no miraba la moneda en absoluto. Con `SO-UNO` y `SO-DOS`
    el único lugar de donde puede salir «INR» es el código del pedido.
    """
    erp.tablas["Sales Order"] = [
        pedido("SO-UNO", moneda="INR"),
        pedido("SO-DOS", moneda="ARS"),
    ]
    erp.tablas["Sales Order Item"] = [
        renglon("SO-UNO", "LECHE-1L", qty=10, rate=100.0),
        renglon("SO-DOS", "LECHE-1L", qty=10, rate=100.0),
    ]
    erp.tablas["Item Price"] = [precio(currency="INR"), precio(currency="ARS")]

    por_pedido = {c.sobre: c for c in consejos.perdidas(HOY)}

    assert sorted(por_pedido) == ["SO-DOS", "SO-UNO"]
    assert "INR" in por_pedido["SO-UNO"].cuerpo
    assert "ARS" in por_pedido["SO-DOS"].cuerpo
    # Y cada uno dice SÓLO la suya: un código fijo en la plantilla diría las dos.
    assert "ARS" not in por_pedido["SO-UNO"].cuerpo
    assert "INR" not in por_pedido["SO-DOS"].cuerpo


# ---------------------------------------------- la fecha, por los dos bordes
#
# `perdidas` tenía SÓLO piso (`transaction_date >= desde`) y, adentro del bucle,
# `_dia(...) or dia`. Son dos guardias que protegen la misma cosa por caminos
# distintos, así que hay un test por guardia: el que mira el borde del servidor
# hace que la página se llene de futuros, y el que mira el guardia de este lado
# le saca el borde al servidor. Un solo test no podría distinguirlos — con los
# dos guardias puestos, ninguna mutación sola cambia la respuesta.


@pytest.fixture
def servidor_sin_borde_de_arriba(erp, monkeypatch):
    """Un ERPNext que IGNORA el `transaction_date <=`, que es cuando importa.

    El borde de arriba lo pone la consulta, y el guardia de Python existe para
    lo que llega igual: una fecha que el servidor ordenó pero que Python no sabe
    leer, o un servidor que no aplicó el filtro. Con el doble honrando los dos,
    sacarle el `if fecha > dia` al código no cambiaría ninguna respuesta y el
    test no probaría nada.
    """
    original = erpnext.policy_get_list

    def get_list(doctype, filters=None, **kw):
        if doctype == "Sales Order":
            filters = [
                f for f in (filters or [])
                if not (len(f) >= 2 and f[0] == "transaction_date" and f[1] == "<=")
            ]
        return original(doctype, filters=filters, **kw)

    monkeypatch.setattr(erpnext, "policy_get_list", get_list)
    return erp


def test_una_venta_con_fecha_de_la_semana_que_viene_no_es_una_perdida_de_ayer(
    servidor_sin_borde_de_arriba, con_lista_de_costo, capsys
):
    """MUTACIÓN: en `perdidas`, `if fecha is None or fecha > dia` -> `if fecha is None`.

    Cae éste y sólo éste. ERPNext deja emitir un pedido con fecha futura, y el
    consejo se lo presenta al dueño como una venta que YA pasó y ya perdió
    plata: le pide que actúe sobre algo que todavía no ocurrió. El pedido de
    control, con fecha de ayer y exactamente los mismos renglones, está para que
    el assert no se pueda cumplir por la vía de no devolver nunca nada.
    """
    erp = servidor_sin_borde_de_arriba
    erp.tablas["Sales Order"] = [
        pedido("SO-FUTURO", dia=HOY + timedelta(days=7)),
        pedido("SO-AYER", dia=HOY - timedelta(days=1)),
    ]
    erp.tablas["Sales Order Item"] = [
        renglon("SO-FUTURO", "LECHE-1L", qty=10, rate=100.0),
        renglon("SO-AYER", "LECHE-1L", qty=10, rate=100.0),
    ]
    erp.tablas["Item Price"] = [precio()]

    salida = consejos.perdidas(HOY)

    assert [c.sobre for c in salida] == ["SO-AYER"]
    # Y no se descarta en silencio: el que despliega tiene de dónde enterarse.
    assert "posterior a hoy" in capsys.readouterr().out


def test_una_fecha_que_no_se_puede_leer_no_se_convierte_en_hoy(
    servidor_sin_borde_de_arriba, con_lista_de_costo
):
    """MUTACIÓN: volver a `dia_pedido = _dia(cabecera.get("transaction_date")) or dia`
    (y sacar el `fecha is None` de arriba, que es la misma decisión).

    Cae éste y sólo éste. `_dia` devuelve None a propósito —una fecha ilegible
    tiene que dejar la fila afuera, no ponerla en un extremo—, y ese `or dia` la
    ponía en el extremo de hoy: el costo salía de la lista vigente HOY y el
    pedido quedaba acusado de vender por debajo de un costo que no era el suyo.

    Los dos pedidos tienen el MISMO renglón y `precio()` deja `valid_from` y
    `valid_upto` VACÍOS, o sea un precio sin vigencia acotada: vale para
    cualquier fecha. Eso es justamente lo que expone la mutación — con la
    fecha ilegible convertida en hoy, `SO-ILEGIBLE` igual consigue un costo,
    aparece en la salida y rompe la lista exacta. Ponerle `valid_upto` al
    fixture haría que la fecha forzada se quedara SIN costo y la mutación
    pasaría sin que nadie se entere.
    """
    erp = servidor_sin_borde_de_arriba
    ilegible = pedido("SO-ILEGIBLE", dia=HOY - timedelta(days=5))
    ilegible["transaction_date"] = "2026-09-0X"  # pasa el `>=` de texto, no parsea
    erp.tablas["Sales Order"] = [ilegible, pedido("SO-BUENO", dia=HOY - timedelta(days=5))]
    erp.tablas["Sales Order Item"] = [
        renglon("SO-ILEGIBLE", "LECHE-1L", qty=10, rate=100.0),
        renglon("SO-BUENO", "LECHE-1L", qty=10, rate=100.0),
    ]
    erp.tablas["Item Price"] = [precio()]

    salida = consejos.perdidas(HOY)

    assert [c.sobre for c in salida] == ["SO-BUENO"]


def test_las_ventas_del_futuro_no_le_comen_la_pagina_a_las_de_verdad(
    erp, con_lista_de_costo, monkeypatch
):
    """MUTACIÓN: sacar `["transaction_date", "<=", dia.isoformat()]` de los filtros.

    Cae éste y sólo éste, y es lo que el guardia de Python NO puede arreglar: la
    página se pide `desc`, así que sin borde de arriba las fechas futuras son las
    más nuevas y se llevan los lugares. Descartarlas después de leerlas llega
    tarde — el pedido de verdad ya se quedó afuera de la consulta.
    """
    monkeypatch.setattr(consejos, "MAX_PEDIDOS_PERDIDA", 2)
    erp.tablas["Sales Order"] = [
        pedido("SO-F1", dia=HOY + timedelta(days=10)),
        pedido("SO-F2", dia=HOY + timedelta(days=9)),
        pedido("SO-F3", dia=HOY + timedelta(days=8)),
        pedido("SO-REAL", dia=HOY - timedelta(days=1)),
    ]
    erp.tablas["Sales Order Item"] = [
        renglon("SO-REAL", "LECHE-1L", qty=10, rate=100.0),
    ]
    erp.tablas["Item Price"] = [precio()]

    assert [c.sobre for c in consejos.perdidas(HOY)] == ["SO-REAL"]


def test_un_renglon_sin_costo_verificable_se_dice_y_no_se_adivina(erp, con_lista_de_costo):
    """MUTACIÓN: en `perdidas`, cambiar `entrada["sin_costo"].append(code)` por
    `continue` a secas (o sea, tirar la lista de lo que no se pudo verificar).

    Mata sólo este test. La pérdida informada es un PISO y el consejo tiene que
    decirlo con todas las letras: sin eso el dueño lee «$200» como el total
    cuando el queso de la otra línea puede estar peor.
    """
    erp.tablas["Sales Order"] = [pedido("SO-3")]
    erp.tablas["Sales Order Item"] = [
        renglon("SO-3", "LECHE-1L", qty=10, rate=100.0),
        renglon("SO-3", "QUESO-1K", qty=2, rate=900.0, uom="Kg"),
    ]
    erp.tablas["Item Price"] = [precio()]

    salida = consejos.perdidas(HOY)
    assert len(salida) == 1
    assert salida[0].datos["sin_costo"] == ["QUESO-1K"]
    assert consejos.SIN_COSTO in salida[0].cuerpo
    assert "QUESO-1K" in salida[0].cuerpo


def test_sin_ningun_costo_verificable_no_hay_consejo(erp, con_lista_de_costo):
    """MUTACIÓN: emitir igual el `Consejo` cuando `renglones` está vacío pero
    `sin_costo` no (o sea, avisar «no pude verificar nada»).

    Mata sólo este test. «No pude mirar» no es una noticia para el dueño, es
    ruido que le gasta uno de los tres mensajes del día; el que tiene que
    enterarse de que falta cargar la lista de costo es quien despliega, y se
    entera por el log.
    """
    erp.tablas["Sales Order"] = [pedido("SO-4")]
    erp.tablas["Sales Order Item"] = [renglon("SO-4", "QUESO-1K", qty=2, rate=900.0, uom="Kg")]
    erp.tablas["Item Price"] = []
    assert consejos.perdidas(HOY) == []


def test_sin_lista_de_costo_configurada_el_detector_se_calla(erp, monkeypatch):
    """MUTACIÓN: el default de `CONSEJOS_LISTA_COSTO` pasa de `""` a `"Standard Buying"`.

    Mata sólo este test. Adivinar contra qué lista comparar es exactamente lo que
    el detector no puede hacer: el consejo nombra la lista, y nombrar una que el
    dueño nunca cargó es firmar una cuenta que nadie hizo.
    """
    monkeypatch.delenv("CONSEJOS_LISTA_COSTO", raising=False)
    erp.tablas["Sales Order"] = [pedido("SO-5")]
    erp.tablas["Sales Order Item"] = [renglon("SO-5", "LECHE-1L", qty=10, rate=100.0)]
    erp.tablas["Item Price"] = [precio()]
    assert consejos.perdidas(HOY) == []
    assert erp.leidas == []


def test_una_perdida_de_centavos_no_es_un_mensaje(erp, con_lista_de_costo):
    """MUTACIÓN: `if diferencia < UMBRAL_PERDIDA` -> `if diferencia <= 0`.

    Mata sólo este test. Con tres mensajes por día, un redondeo de $0,50 se lleva
    el cupo de la deuda de $80.000 que envejece.
    """
    erp.tablas["Sales Order"] = [pedido("SO-6")]
    erp.tablas["Sales Order Item"] = [renglon("SO-6", "LECHE-1L", qty=1, rate=119.5)]
    erp.tablas["Item Price"] = [precio()]
    assert consejos.perdidas(HOY) == []


def test_un_lote_de_renglones_truncado_no_informa_media_verdad(erp, con_lista_de_costo, monkeypatch):
    """MUTACIÓN: borrar el `raise` de `_renglones` cuando el techo se llena.

    Mata sólo este test. Un techo lleno significa que faltan renglones, y un
    renglón que falta es una pérdida que no se vio: informar el subtotal como si
    fuera el total es el defecto que `app/digest.py` ya documentó. Mejor callarse
    y reintentar en el barrido siguiente.
    """
    monkeypatch.setattr(consejos, "MAX_RENGLONES_POR_PEDIDO", 2)
    erp.tablas["Sales Order"] = [pedido("SO-7")]
    erp.tablas["Sales Order Item"] = [
        renglon("SO-7", "LECHE-1L", qty=10, rate=100.0),
        renglon("SO-7", "LECHE-2L", qty=10, rate=100.0),
        renglon("SO-7", "LECHE-3L", qty=10, rate=100.0),
    ]
    erp.tablas["Item Price"] = [
        precio(),
        precio(item_code="LECHE-2L"),
        precio(item_code="LECHE-3L"),
    ]
    assert consejos.perdidas(HOY) == []


# ===========================================================================
# 5. Detector 2: el cliente que dejó de comprar
# ===========================================================================


def compra(cliente: str, dia_: date, *, total: float = 1000.0, nombre: str = "") -> dict:
    return {
        "name": f"SO-{cliente}-{dia_.isoformat()}",
        "customer": cliente,
        "customer_name": nombre or f"Cliente {cliente}",
        "transaction_date": dia_.isoformat(),
        "grand_total": total,
        "docstatus": 1,
        "company": EMPRESA,
    }


def test_el_ritmo_sale_de_la_historia_de_cada_cliente_y_no_de_una_constante(erp):
    """MUTACIÓN: `umbral = max(MIN_DIAS_DORMIDO, FACTOR_DORMIDO * ritmo)` ->
    `umbral = float(MIN_DIAS_DORMIDO)`.

    Mata sólo este test. Es la afirmación entera del brief: «regularmente» sale
    de la historia del propio cliente. RAPIDO compra cada 2 días y hace 8 que no
    aparece (dormido); LENTO compra cada 30 y hace 20 que no (normal, todavía no
    le toca). Con un umbral global los dos son lo mismo, y el dueño recibe un
    aviso por cada mayorista que compra una vez por mes.
    """
    filas = []
    for atras in (14, 12, 10, 8):
        filas.append(compra("RAPIDO", HOY - timedelta(days=atras)))
    for atras in (110, 80, 50, 20):
        filas.append(compra("LENTO", HOY - timedelta(days=atras)))
    erp.tablas["Sales Order"] = filas

    salida = consejos.dormidos(HOY)
    assert [c.sobre for c in salida] == ["RAPIDO"]
    assert salida[0].datos["ritmo_dias"] == 2
    assert salida[0].datos["silencio_dias"] == 8


def test_un_cliente_diario_que_falto_tres_dias_no_esta_dormido(erp):
    """MUTACIÓN: `max(float(MIN_DIAS_DORMIDO), FACTOR_DORMIDO * ritmo)` ->
    `FACTOR_DORMIDO * ritmo`.

    Mata sólo este test. Sin el piso, un panadero que compra todos los días
    dispara un consejo cada vez que se toma un fin de semana largo: 3 días ya son
    más de 2,5 veces su ritmo. El piso es lo que hace que el consejo signifique
    «se fue» y no «hoy no vino».
    """
    erp.tablas["Sales Order"] = [
        compra("PANADERIA", HOY - timedelta(days=atras)) for atras in (6, 5, 4, 3)
    ]
    assert consejos.dormidos(HOY) == []


def test_sin_suficiente_historia_no_se_le_inventa_un_ritmo(erp):
    """MUTACIÓN: `MIN_PEDIDOS_PARA_RITMO = 4` -> `3`.

    Mata sólo este test. Con tres compras hay dos intervalos, y dos intervalos no
    son un hábito: cualquiera que probó el producto tres veces y no volvió sería
    «un cliente que dejó de comprar». Los otros clientes de este archivo tienen
    cuatro o cinco compras, así que no se mueven.
    """
    erp.tablas["Sales Order"] = [
        compra("NUEVO", HOY - timedelta(days=atras)) for atras in (60, 50, 40)
    ]
    assert consejos.dormidos(HOY) == []


def test_el_ritmo_es_la_mediana_y_no_el_promedio(erp):
    """MUTACIÓN: `statistics.median(huecos)` -> `statistics.mean(huecos)`.

    Mata sólo este test. Los intervalos de este cliente son 2, 2, 2 y 60: la
    mediana dice 2 (compra cada dos días, con un parate de verano en el medio) y
    el promedio dice 16,5, que no describe a nadie. Con el promedio el umbral
    trepa a 41 días y un cliente de todos los días puede estar un mes y medio sin
    aparecer sin que nadie se entere. Los demás clientes del archivo tienen
    intervalos constantes, donde mediana y promedio coinciden.
    """
    base = HOY - timedelta(days=76)
    erp.tablas["Sales Order"] = [
        compra("ESTACIONAL", base + timedelta(days=corrimiento))
        for corrimiento in (0, 2, 4, 6, 66)
    ]
    salida = consejos.dormidos(HOY)
    assert [c.sobre for c in salida] == ["ESTACIONAL"]
    assert salida[0].datos["ritmo_dias"] == 2
    assert salida[0].datos["silencio_dias"] == 10


def test_la_historia_se_pide_de_lo_mas_nuevo_a_lo_mas_viejo(erp, monkeypatch):
    """MUTACIÓN: en `dormidos`, `order_by="transaction_date desc"` -> `"asc"`.

    Mata sólo este test. Es el único error que este detector no puede cometer:
    con `asc`, truncar la página pierde las compras NUEVAS y el consejo diría
    «dejó de comprar» de un cliente que compró ayer. Con `desc` lo que se pierde
    es historia vieja, y eso sólo puede hacer que el ritmo no se pueda calcular —
    que termina en silencio, no en una acusación falsa.
    """
    monkeypatch.setattr(consejos, "MAX_PEDIDOS_HISTORIA", 4)
    # Cuatro compras viejas de VIEJO (que llenarían la página con `asc`) y las
    # cuatro recientes de ACTIVO, incluida la de ayer.
    erp.tablas["Sales Order"] = [
        *[compra("VIEJO", HOY - timedelta(days=atras)) for atras in (170, 160, 150, 140)],
        *[compra("ACTIVO", HOY - timedelta(days=atras)) for atras in (7, 5, 3, 1)],
    ]
    assert [c.sobre for c in consejos.dormidos(HOY)] == []


# --------------------------------------------------- la página que no entera
#
# `dormidos` pide MAX_PEDIDOS_HISTORIA + 1 justamente para poder DARSE CUENTA
# de que había más, y de ahí salen DOS consumidores de la misma condición: el
# aviso en el log y el recorte. Van en dos tests porque se mutan por separado
# —borrar el `print` no toca el recorte, y borrar el recorte no toca el log—,
# y un solo test que los mirara juntos no distinguiría cuál de los dos se cayó.


def _seis_pedidos() -> list[dict]:
    """Una página que se pasa del techo por uno, con el cliente en el borde.

    Ordenada de lo más nuevo a lo más viejo, que es como la pide `dormidos`:

        OTRO      HOY-1     <- entra siempre
        DORMIDO   HOY-54    <- las tres que entran con el techo en 4
        DORMIDO   HOY-56
        DORMIDO   HOY-58
        DORMIDO   HOY-60    <- LA CUARTA: la que decide si tiene ritmo
        OTRO      HOY-100   <- no llega ni al `limit`

    DORMIDO compra cada dos días y hace 54 que no aparece, así que con sus
    cuatro compras adentro es un consejo y con tres es silencio: la fila que el
    recorte saca es exactamente la que cambia la respuesta.
    """
    return [
        compra("OTRO", HOY - timedelta(days=1)),
        *[compra("DORMIDO", HOY - timedelta(days=atras)) for atras in (54, 56, 58, 60)],
        compra("OTRO", HOY - timedelta(days=100)),
    ]


def test_la_pagina_llena_se_recorta_al_techo_y_no_al_techo_mas_uno(erp, monkeypatch):
    """MUTACIÓN: borrar `pedidos = pedidos[:MAX_PEDIDOS_HISTORIA]`.

    Cae éste y sólo éste. El `limit` es techo+1 —hay que pedir una de más para
    saber que faltan—, así que sin el recorte el detector trabaja con una fila
    que decidió no tener, y esa fila acá es la que hace aparecer el consejo.

    Las dos mitades no se leen del catálogo ni de la constante: una corrida con
    el techo en 4 y otra con el techo en 5, sobre la MISMA historia. Un test que
    sólo pidiera `== []` pasaría también si `dormidos` devolviera siempre nada.
    """
    erp.tablas["Sales Order"] = _seis_pedidos()

    # Techo 4: se piden 5, vuelven 5, se recorta a 4 y DORMIDO queda con tres
    # compras — menos de MIN_PEDIDOS_PARA_RITMO, así que no tiene ritmo propio
    # y no se lo juzga.
    monkeypatch.setattr(consejos, "MAX_PEDIDOS_HISTORIA", 4)
    assert [c.sobre for c in consejos.dormidos(HOY)] == []

    # Techo 5: la misma historia entra entera y el mismo cliente SÍ sale.
    monkeypatch.setattr(consejos, "MAX_PEDIDOS_HISTORIA", 5)
    salida = consejos.dormidos(HOY)
    assert [c.sobre for c in salida] == ["DORMIDO"]
    assert salida[0].datos["ritmo_dias"] == 2


def test_una_historia_recortada_se_dice_en_el_log(erp, monkeypatch, capsys):
    """MUTACIÓN: borrar el `print` de adentro del `if`.

    Cae éste y sólo éste. Recortar en silencio es el peor de los dos modos de
    fallar: el consejo sale igual, con menos historia de la que dice tener, y
    nadie tiene de dónde enterarse. La corrida sin recorte está para que el
    assert no pueda cumplirse por la vía de no imprimir nunca nada.
    """
    monkeypatch.setattr(consejos, "MAX_PEDIDOS_HISTORIA", 4)
    erp.tablas["Sales Order"] = _seis_pedidos()

    consejos.dormidos(HOY)

    anuncio = capsys.readouterr().out
    assert "más de 4 pedidos" in anuncio

    # Y la historia que entra entera no dice nada: un aviso que sale siempre no
    # es un aviso.
    erp.tablas["Sales Order"] = _seis_pedidos()[:4]
    consejos.dormidos(HOY)
    assert "más de" not in capsys.readouterr().out


# ===========================================================================
# 6. Detector 3: la deuda que envejece
# ===========================================================================


def factura(cliente: str, *, monto: float, atraso: int, nombre: str = "") -> dict:
    return {
        "party": cliente,
        "customer_name": nombre or f"Cliente {cliente}",
        "outstanding_amount": monto,
        "due_date": (HOY - timedelta(days=atraso)).isoformat(),
    }


@pytest.fixture
def tolerancia_30(monkeypatch):
    monkeypatch.setenv("CONSEJOS_DEUDA_DIAS", "30")
    monkeypatch.delenv("CONSEJOS_DEUDA_MINIMA", raising=False)


def test_la_ventana_tolerada_es_estricta(erp, tolerancia_30):
    """MUTACIÓN: `if atraso <= tolerancia: continue` -> `if atraso < tolerancia`.

    Mata sólo este test. El dueño dijo «30 días»: a los 30 días todavía está
    dentro de lo que tolera, y avisarle ahí es un mensaje que él no pidió. A los
    31 ya no.
    """
    erp.cobranzas = [
        factura("JUSTO", monto=50_000.0, atraso=30),
        factura("PASADO", monto=10_000.0, atraso=31),
    ]
    salida = consejos.deudas(HOY)
    assert [c.sobre for c in salida] == ["PASADO"]
    assert salida[0].datos["atraso_dias"] == 31


def test_una_fila_sin_vencimiento_calla_el_reporte_entero(erp, tolerancia_30):
    """MUTACIÓN: `return []` -> `continue` en la rama de la fila sin `due_date`.

    Mata sólo este test. Es la misma postura que `policy._saldo_vencido`, que
    levanta en ese caso: una fila sin vencimiento significa que el reporte no es
    el que creemos, y media respuesta de un reporte de cobranzas —un total que
    parece el total y no lo es— es peor que ninguna.
    """
    erp.cobranzas = [
        factura("BUENO", monto=90_000.0, atraso=45),
        {"party": "RARO", "outstanding_amount": 70_000.0, "due_date": ""},
    ]
    assert consejos.deudas(HOY) == []


def test_la_misma_deuda_se_dice_una_vez_por_tramo_y_no_una_vez_por_semana(erp, tolerancia_30):
    """MUTACIÓN: `clave=f"{DEUDA}:{cliente}:{tramo}"` -> `clave=f"{DEUDA}:{cliente}"`.

    Mata sólo este test. La clave del reclamo es lo que decide qué es «el mismo
    hecho»: sin el tramo, una deuda que pasó de 35 a 95 días nunca se vuelve a
    mencionar porque la clave ya está tomada; con el tramo, el dueño escucha una
    vez por cada empeoramiento real y no una vez por semana por lo mismo.
    """
    erp.cobranzas = [factura("MOROSO", monto=80_000.0, atraso=35)]
    primero = consejos.deudas(HOY)
    assert [c.clave for c in primero] == ["deuda:MOROSO:30"]

    erp.cobranzas = [factura("MOROSO", monto=80_000.0, atraso=95)]
    despues = consejos.deudas(HOY)
    assert [c.clave for c in despues] == ["deuda:MOROSO:90"]


def test_con_una_tolerancia_corta_llegar_a_30_sigue_siendo_una_noticia(erp, monkeypatch):
    """MUTACIÓN: en `_tramo`, `elegido = 0` -> `elegido = TRAMOS_DEUDA[0]`.

    Cae éste y sólo éste, porque es el único que corre con una tolerancia POR
    DEBAJO del primer tramo — los demás usan `tolerancia_30`, donde nada se
    emite antes de los 30 y la diferencia no se ve.

    El dueño puede poner `CONSEJOS_DEUDA_DIAS` en 7. Con el piso viejo, el
    primer aviso a los 10 días ya reclamaba `deuda:<cliente>:30`, así que
    veinte días después —cuando la deuda de verdad cruza los 30— la clave
    estaba tomada, el TTL de 14 días todavía no había vencido y el
    empeoramiento real no avisaba nada. El tramo tiene que nombrar el escalón
    que el atraso YA cruzó.
    """
    monkeypatch.setenv("CONSEJOS_DEUDA_DIAS", "7")
    monkeypatch.delenv("CONSEJOS_DEUDA_MINIMA", raising=False)

    erp.cobranzas = [factura("MOROSO", monto=80_000.0, atraso=10)]
    temprano = consejos.deudas(HOY)

    erp.cobranzas = [factura("MOROSO", monto=80_000.0, atraso=31)]
    a_los_treinta = consejos.deudas(HOY)

    # Los dos salen —la tolerancia es 7— y con claves DISTINTAS, que es lo que
    # hace que el segundo se pueda reclamar aunque el primero siga vivo.
    assert [c.clave for c in temprano] == ["deuda:MOROSO:0"]
    assert [c.clave for c in a_los_treinta] == ["deuda:MOROSO:30"]


def test_una_tolerancia_ilegible_apaga_el_detector(erp, monkeypatch):
    """MUTACIÓN: en `dias_tolerados`, devolver `DEUDA_DIAS_DEFAULT` en el `except`.

    Mata sólo este test. Cuántos días de atraso tolera es una decisión del dueño;
    un valor que nadie puede leer no se reemplaza por un número «razonable» que
    él no eligió — se apaga y se dice en el log, que es lo que hace
    `inventario.horas_de_validez` con el suyo.
    """
    monkeypatch.setenv("CONSEJOS_DEUDA_DIAS", "un mes y medio")
    erp.cobranzas = [factura("MOROSO", monto=80_000.0, atraso=95)]
    assert consejos.dias_tolerados() == 0.0
    assert consejos.deudas(HOY) == []


def test_una_deuda_por_debajo_del_piso_no_es_un_mensaje(erp, monkeypatch):
    """MUTACIÓN: en `deudas`, borrar el `if entrada["total"] < piso: continue`.

    Mata sólo este test. Los $120 de un kiosco valen menos que el mensaje que los
    reclama, y el cupo del día es de tres.
    """
    monkeypatch.setenv("CONSEJOS_DEUDA_DIAS", "30")
    monkeypatch.setenv("CONSEJOS_DEUDA_MINIMA", "5000")
    erp.cobranzas = [
        factura("CHICO", monto=120.0, atraso=90),
        factura("GRANDE", monto=60_000.0, atraso=40),
    ]
    assert [c.sobre for c in consejos.deudas(HOY)] == ["GRANDE"]


# ===========================================================================
# 7. Detector 4: el producto que no llega al próximo reparto
# ===========================================================================


@pytest.fixture
def reparto_semanal(monkeypatch):
    """El camión sale los lunes, y HOY es lunes: el próximo reparto es en 7 días."""
    monkeypatch.setattr(excepciones, "dias_reparto", lambda: [HOY.weekday()])


def poner_stock(erp, code: str, *, hay: float, minimo: float) -> None:
    erp.tablas["Item Reorder"].append(
        {"parent": code, "warehouse": DEPOSITO, "warehouse_reorder_level": minimo}
    )
    erp.tablas["Bin"].append(
        {"item_code": code, "warehouse": DEPOSITO, "actual_qty": hay, "reserved_qty": 0.0}
    )


def poner_venta(erp, code: str, *, qty: float, stock_qty: float, atras: int = 5) -> None:
    nombre = f"SO-V-{code}-{atras}"
    erp.tablas["Sales Order"].append(pedido(nombre, dia=HOY - timedelta(days=atras)))
    erp.tablas["Sales Order Item"].append(
        {
            "parent": nombre,
            "item_code": code,
            "warehouse": DEPOSITO,
            "qty": qty,
            "stock_qty": stock_qty,
            "docstatus": 1,
        }
    )


def test_con_el_stock_no_confiable_el_consejo_dice_lo_que_esta_asumiendo(
    erp, reparto_semanal, monkeypatch
):
    """MUTACIÓN: `inventario.confiable(code, deposito)` ->
    `inventario.confiable(code, deposito, ignorar_postura=True)`.

    Mata sólo este test. `STOCK_CONFIABLE=false` es la postura de lanzamiento, y
    el doble de `tests/conftest.py` honra `ignorar_postura`, así que con la
    mutación el consejo sale SIN supuesto: le dice al dueño «te quedás sin leche»
    con la misma cara que si alguien hubiera contado esta mañana. La postura no
    apaga el consejo —no avisarle nunca sería peor— pero obliga a decir en voz
    alta qué se está dando por cierto.
    """
    inventario_confiable(monkeypatch, maestra=False)
    poner_stock(erp, "LECHE-1L", hay=3.0, minimo=5.0)
    poner_venta(erp, "LECHE-1L", qty=30.0, stock_qty=30.0)

    salida = consejos.quiebres(HOY)
    assert [c.sobre for c in salida] == ["LECHE-1L"]
    assert salida[0].supuesto == "el inventario está marcado como no confiable"
    assert salida[0].supuesto in salida[0].cuerpo
    assert salida[0].datos["ya_bajo_minimo"] is True


def test_el_horizonte_es_el_proximo_reparto_y_no_el_de_hoy(erp, reparto_semanal, monkeypatch):
    """MUTACIÓN: en `_proximo_reparto`, `desde=1` -> `desde=0`.

    Mata sólo este test. HOY es día de reparto, así que con `desde=0` el
    horizonte es de un día y no de siete: quedan 20 unidades, se venden 2,5 por
    día y el mínimo es 5 — a un día llega con 17,5 y no pasa nada; a siete días
    llega con 2,5 y el camión de la semana que viene sale sin producto. El
    reparto de hoy ya salió: el stock que importa es el que tiene que llegar al
    siguiente.
    """
    inventario_confiable(monkeypatch, maestra=True)
    poner_stock(erp, "YOGUR-1K", hay=20.0, minimo=5.0)
    poner_venta(erp, "YOGUR-1K", qty=75.0, stock_qty=75.0)  # 75/30 = 2,5 por día

    salida = consejos.quiebres(HOY)
    assert [c.sobre for c in salida] == ["YOGUR-1K"]
    assert salida[0].datos["dias_hasta_reparto"] == 7
    assert salida[0].datos["proximo_reparto"] == (HOY + timedelta(days=7)).isoformat()
    assert salida[0].datos["proyectado"] == pytest.approx(2.5)


def test_sin_dias_de_reparto_configurados_no_se_proyecta_nada(erp, monkeypatch):
    """MUTACIÓN: en `quiebres`, tratar `proximo is None` como `dias_hasta = 1`.

    Mata sólo este test. «Cruza el mínimo antes del próximo reparto» no significa
    nada si nadie configuró cuándo sale el camión, y un horizonte inventado de un
    día es un número que el dueño no puso.
    """
    inventario_confiable(monkeypatch, maestra=True)
    monkeypatch.setattr(excepciones, "dias_reparto", list)
    poner_stock(erp, "LECHE-1L", hay=1.0, minimo=5.0)
    poner_venta(erp, "LECHE-1L", qty=30.0, stock_qty=30.0)
    assert consejos.quiebres(HOY) == []


def test_la_demanda_se_mide_en_unidad_de_stock(erp, reparto_semanal, monkeypatch):
    """MUTACIÓN: en `_demanda_diaria`, `stock_qty` -> `qty` **en los dos lados** —
    el `fields` de la consulta y la suma que lo lee.

    Mata sólo este test. Tiene que ser en los dos a la vez porque el doble de
    este archivo proyecta a `fields` como hace Frappe: cambiar uno solo deja el
    campo ausente y la demanda en cero para TODOS los productos, que mata tres
    tests y no mide nada. La mutación que mide es la coherente: la que un autor
    escribiría creyendo que la unidad de venta sirve.

    El `Bin` y el mínimo de reposición están en unidad de
    STOCK; la venta fue de 3 cajones, que son 36 litros. Restarle «3» a un stock
    en litros da una proyección con la forma correcta y el número equivocado —
    la misma trampa de `uom` que `_costo_de` cuida del otro lado del módulo.
    Los números están elegidos para que la conclusión no dependa del horizonte:
    con `stock_qty` hay consejo a uno y a siete días, con `qty` no lo hay en
    ninguno de los dos.
    """
    inventario_confiable(monkeypatch, maestra=True)
    poner_stock(erp, "MANTECA-200", hay=6.0, minimo=5.0)
    poner_venta(erp, "MANTECA-200", qty=3.0, stock_qty=36.0)  # 36/30 = 1,2 por día

    salida = consejos.quiebres(HOY)
    assert [c.sobre for c in salida] == ["MANTECA-200"]
    assert salida[0].datos["demanda_diaria"] == pytest.approx(1.2)


# ------------------------------------------- el techo de la página de ventas
#
# EL PRIMER TECHO de `_demanda_diaria`, que no tenía ningún test. Los dos de
# más abajo miran el segundo (los renglones de cada lote) y los dos de
# `dormidos` miran el suyo; éste se pedía con la fila de más, se logueaba y se
# recortaba, y nada lo medía. Van dos porque son dos consumidores de la misma
# condición: el log y el recorte se mutan por separado.
#
# La dirección del error es la peligrosa, igual que con la venta futura pero al
# revés: una página recortada BAJA la demanda diaria, SUBE lo proyectado y el
# quiebre no se avisa. El silencio se ve igual que «no hay nada que decir».


def _tres_ventas_del_mismo_producto(erp, code: str) -> None:
    """Tres ventas de 30 unidades cada una, de lo más nuevo a lo más viejo.

        SO-V-<code>-1   HOY-1    30
        SO-V-<code>-3   HOY-3    30
        SO-V-<code>-5   HOY-5    30

    Con las tres, 90/30 = 3 por día. Con las dos más nuevas —que es lo que deja
    el recorte con el techo en 2—, 60/30 = 2. Los dos números salen de dos
    corridas sobre ESTA misma historia, no de la constante.
    """
    for atras in (1, 3, 5):
        poner_venta(erp, code, qty=30.0, stock_qty=30.0, atras=atras)


def test_una_pagina_de_ventas_llena_se_recorta_al_techo_y_no_al_techo_mas_uno(
    erp, reparto_semanal, monkeypatch
):
    """MUTACIÓN: en `_demanda_diaria`, borrar el `[:MAX_PEDIDOS_HISTORIA]`.

    Cae ésta y sólo ésta. El `limit` es techo+1 —hay que pedir una de más para
    saber que faltan—, así que sin el recorte la demanda se calcula con una
    venta que la función decidió no tener. Acá eso la sube de 2 a 3 por día.

    Y la mutación de al lado (`limit=MAX_PEDIDOS_HISTORIA + 1` -> `limit=
    MAX_PEDIDOS_HISTORIA`) NO mata a ésta, por lo mismo que en el techo de
    renglones: con el techo justo se suman `techo` ventas y con techo+1 también,
    porque el recorte saca la fila sonda. Esa mutación la mata el test del log.
    """
    inventario_confiable(monkeypatch, maestra=True)
    poner_stock(erp, "MANTECA-200", hay=6.0, minimo=5.0)
    _tres_ventas_del_mismo_producto(erp, "MANTECA-200")

    # Techo 3: las tres entran. 90/30 = 3 por día.
    monkeypatch.setattr(consejos, "MAX_PEDIDOS_HISTORIA", 3)
    entera = consejos.quiebres(HOY)
    assert [c.sobre for c in entera] == ["MANTECA-200"]
    assert entera[0].datos["demanda_diaria"] == pytest.approx(3.0)

    # Techo 2: se piden 3, vuelven 3, se recorta a las DOS MÁS NUEVAS —la
    # consulta pide `transaction_date desc`— y la demanda sale de 60/30 = 2.
    monkeypatch.setattr(consejos, "MAX_PEDIDOS_HISTORIA", 2)
    recortada = consejos.quiebres(HOY)
    assert [c.sobre for c in recortada] == ["MANTECA-200"]
    assert recortada[0].datos["demanda_diaria"] == pytest.approx(2.0)


def test_una_pagina_de_ventas_recortada_se_dice_en_el_log(
    erp, reparto_semanal, monkeypatch, capsys
):
    """MUTACIÓN: borrar el `print` de adentro de ese `if`.

    Cae éste y sólo éste. La proyección sale igual, con menos ventas de las que
    hubo, y sin el log no queda de dónde enterarse de que salió recortada. La
    corrida con el techo alto está para que el assert no se pueda cumplir por
    la vía de no imprimir nunca.
    """
    inventario_confiable(monkeypatch, maestra=True)
    poner_stock(erp, "MANTECA-200", hay=6.0, minimo=5.0)
    _tres_ventas_del_mismo_producto(erp, "MANTECA-200")

    monkeypatch.setattr(consejos, "MAX_PEDIDOS_HISTORIA", 2)
    consejos.quiebres(HOY)
    assert "más de 2 ventas en la ventana de demanda" in capsys.readouterr().out

    monkeypatch.setattr(consejos, "MAX_PEDIDOS_HISTORIA", 3)
    consejos.quiebres(HOY)
    assert "ventana de demanda" not in capsys.readouterr().out


# ------------------------------------------- el techo de renglones por lote
#
# `_demanda_diaria` tiene DOS techos —la página de pedidos y los renglones de
# cada lote— y el segundo se pedía justo, sin la fila de más que permite
# enterarse. Van dos tests porque son dos consumidores de la misma condición:
# el log y el recorte se mutan por separado.


def _dos_renglones_del_mismo_pedido(erp, code: str, *, stock_qty: float) -> None:
    """UN pedido con DOS renglones del mismo producto, que es legal en ERPNext.

    Con `MAX_RENGLONES_POR_PEDIDO` en 1 el techo del lote es 1 y estos dos no
    entran; en 2 entran los dos. La demanda sale de la mitad o del total, y ésa
    es la diferencia que el recorte produce.
    """
    nombre = f"SO-DOBLE-{code}"
    erp.tablas["Sales Order"].append(pedido(nombre, dia=HOY - timedelta(days=5)))
    for _ in range(2):
        erp.tablas["Sales Order Item"].append(
            {
                "parent": nombre,
                "item_code": code,
                "warehouse": DEPOSITO,
                "qty": stock_qty,
                "stock_qty": stock_qty,
                "docstatus": 1,
            }
        )


def test_un_lote_que_llena_el_techo_de_renglones_se_recorta(erp, reparto_semanal, monkeypatch):
    """MUTACIÓN: en `_demanda_diaria`, borrar `filas = filas[:tope]`.

    Cae éste y sólo éste — MEDIDO, después de escribirlo mal: el docstring
    decía `limit=tope + 1` -> `limit=tope`, y esa mutación deja este test EN
    VERDE. Con el techo justo la demanda sale de `tope` renglones y con
    `tope + 1` sale de `tope` también, porque el recorte saca la fila de más:
    los dos números que este test compara no se mueven. Lo que sí mata es
    borrar el recorte, que deja entrar la fila sonda y sube la demanda.

    La lección, que es la de CLAUDE.md: la mutación hay que CORRERLA. Escrita
    de memoria, ésta nombraba la línea de al lado — y la línea de al lado tiene
    su propio test (`..._se_dice_en_el_log`), que es el que se caía.

    Las dos mitades son dos corridas con techos distintos sobre la MISMA venta,
    no un número leído de la constante.
    """
    inventario_confiable(monkeypatch, maestra=True)
    poner_stock(erp, "MANTECA-200", hay=6.0, minimo=5.0)
    _dos_renglones_del_mismo_pedido(erp, "MANTECA-200", stock_qty=18.0)

    # Techo 2: los dos renglones entran. 36/30 = 1,2 por día.
    monkeypatch.setattr(consejos, "MAX_RENGLONES_POR_PEDIDO", 2)
    entera = consejos.quiebres(HOY)
    assert [c.sobre for c in entera] == ["MANTECA-200"]
    assert entera[0].datos["demanda_diaria"] == pytest.approx(1.2)

    # Techo 1: se pide uno de más para darse cuenta, y se calcula con uno solo.
    monkeypatch.setattr(consejos, "MAX_RENGLONES_POR_PEDIDO", 1)
    recortada = consejos.quiebres(HOY)
    assert [c.sobre for c in recortada] == ["MANTECA-200"]
    assert recortada[0].datos["demanda_diaria"] == pytest.approx(0.6)


def test_un_lote_que_llena_el_techo_de_renglones_se_dice_en_el_log(
    erp, reparto_semanal, monkeypatch, capsys
):
    """MUTACIÓN: borrar el `print` de adentro de ese `if`.

    Cae éste y sólo éste. La proyección sigue saliendo —con menos renglones de
    los que hubo— y sin el log no queda rastro de que salió recortada. La
    corrida con el techo alto está para que el assert no se pueda cumplir por
    la vía de no imprimir nunca.
    """
    inventario_confiable(monkeypatch, maestra=True)
    poner_stock(erp, "MANTECA-200", hay=6.0, minimo=5.0)
    _dos_renglones_del_mismo_pedido(erp, "MANTECA-200", stock_qty=18.0)

    monkeypatch.setattr(consejos, "MAX_RENGLONES_POR_PEDIDO", 1)
    consejos.quiebres(HOY)
    assert "llenó el techo" in capsys.readouterr().out

    monkeypatch.setattr(consejos, "MAX_RENGLONES_POR_PEDIDO", 2)
    consejos.quiebres(HOY)
    assert "llenó el techo" not in capsys.readouterr().out


def test_una_venta_del_futuro_no_infla_la_demanda_diaria(erp, reparto_semanal, monkeypatch):
    """MUTACIÓN: sacar `["transaction_date", "<=", dia.isoformat()]` de
    `_demanda_diaria`. Cae ésta y sólo ésta.

    `perdidas` y `dormidos` descartan la fila futura también del lado de
    Python; `_demanda_diaria` NO mira la fecha —sólo junta los nombres de los
    pedidos y suma sus renglones—, así que acá el filtro de la consulta es el
    único guardia que hay. Y la dirección del error es la peligrosa: una venta
    que todavía no pasó SUBE la demanda diaria, BAJA lo proyectado y hace
    avisar un quiebre que las ventas reales no sostienen. El dueño sale a
    comprar stock por un pedido que puede no existir.

    Dos corridas sobre la misma historia: con la venta futura adentro de la
    tabla y sin ella. La demanda tiene que ser la MISMA.
    """
    inventario_confiable(monkeypatch, maestra=True)
    poner_stock(erp, "MANTECA-200", hay=6.0, minimo=5.0)
    poner_venta(erp, "MANTECA-200", qty=3.0, stock_qty=36.0)  # 36/30 = 1,2 por día

    solo_reales = consejos.quiebres(HOY)
    assert [c.sobre for c in solo_reales] == ["MANTECA-200"]
    assert solo_reales[0].datos["demanda_diaria"] == pytest.approx(1.2)

    # Y ahora una venta ENORME con fecha de la semana que viene. No cuenta.
    poner_venta(erp, "MANTECA-200", qty=100.0, stock_qty=1200.0, atras=-7)

    con_futura = consejos.quiebres(HOY)
    assert [c.sobre for c in con_futura] == ["MANTECA-200"]
    assert con_futura[0].datos["demanda_diaria"] == pytest.approx(1.2)


def test_un_producto_que_no_se_vende_no_es_una_urgencia(erp, reparto_semanal, monkeypatch):
    """MUTACIÓN: borrar el `if por_dia <= 0: continue` de `quiebres`.

    Mata sólo este test. Un producto bajo el mínimo que nadie pidió en un mes es
    una decisión de compras para el próximo pedido al proveedor, no un mensaje
    que se lleva uno de los tres cupos del día.
    """
    inventario_confiable(monkeypatch, maestra=True)
    poner_stock(erp, "DULCE-500", hay=1.0, minimo=5.0)
    assert consejos.quiebres(HOY) == []


# ===========================================================================
# 8. Un consejo es un consejo: lo estructural
# ===========================================================================

_FUENTE = Path(__file__).resolve().parents[1] / "app" / "consejos.py"

# La lista blanca: SÓLO lecturas, y por módulo. Agregar un nombre acá es una
# decisión que se ve en el diff; escribir `erpnext.submit_doc` en el módulo no
# lo sería si este test no existiera.
PERMITIDO = {
    "erpnext": {
        "policy_get_list",
        "policy_run_report",
        "default_company",
        "default_warehouse",
        "ERPNextError",
    },
    "inventario": {"confiable"},
    "locks": {"conexion"},
    "reloj": {"ahora_con_respaldo"},
    "notificar": {"telefono_dueno"},
    "pendientes": {"en_silencio"},
    "excepciones": {"dias_reparto", "_proxima_fecha"},
}

# Módulos que este archivo no puede importar ni por casualidad: los que emiten,
# cancelan, confirman, cambian un límite o le hablan a alguien.
PROHIBIDOS = {
    "acciones",
    "agenda",
    "aprobacion",
    "avisos",
    "confirmacion",
    "decisiones",
    "entrega",
    "graph",
    "limites",
    "main",
    "policy",
    "solicitudes",
    "whatsapp",
}


def _arbol() -> ast.Module:
    return ast.parse(_FUENTE.read_text(encoding="utf-8"))


def test_un_consejo_no_puede_escribir_nada():
    """MUTACIÓN: agregar `erpnext.submit_doc("Sales Order", "SO-1")` en cualquier
    función de `app/consejos.py`.

    Mata sólo este test. «Un consejo nunca confirma nada» no puede quedar como
    una promesa en un docstring: esto lo lee del AST. Cada `modulo.nombre` que el
    archivo menciona tiene que estar en `PERMITIDO`, que son todas lecturas —
    así que aconsejar y decidir no pueden mezclarse por descuido.

    Y lo mismo por el lado de la lectura: TODO va por `policy_get_*`. Un hilo de
    fondo no tiene scope y cae en la clave del agente de CLIENTE, que ve lo que
    ese usuario ve; con ella, «este cliente dejó de comprar» se calcularía sobre
    una fracción de los pedidos.
    """
    usados: dict[str, set[str]] = {}
    for nodo in ast.walk(_arbol()):
        if isinstance(nodo, ast.Attribute) and isinstance(nodo.value, ast.Name):
            usados.setdefault(nodo.value.id, set()).add(nodo.attr)
    vigilados = {mod: nombres for mod, nombres in usados.items() if mod in PERMITIDO}
    assert set(vigilados) == set(PERMITIDO), "algún módulo de la lista blanca dejó de usarse"
    for modulo, nombres in vigilados.items():
        de_mas = nombres - PERMITIDO[modulo]
        assert not de_mas, f"app/consejos.py usa {modulo}.{sorted(de_mas)}, que no es una lectura"


def test_un_consejo_no_puede_ni_importar_lo_que_decide():
    """MUTACIÓN: agregar `from app import decisiones` en `app/consejos.py`.

    Mata sólo este test. Es el cinturón además de los tirantes del test de
    arriba: un import con alias (`from app import decisiones as d`) esquivaría la
    lista blanca de atributos, porque `d.confirmar` no se parece a nada vigilado.
    """
    importados: set[str] = set()
    for nodo in ast.walk(_arbol()):
        if isinstance(nodo, ast.ImportFrom):
            if nodo.module and nodo.module.startswith("app."):
                importados.add(nodo.module.split(".", 1)[1].split(".")[0])
            if nodo.module == "app":
                importados.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.Import):
            for alias in nodo.names:
                if alias.name.startswith("app."):
                    importados.add(alias.name.split(".", 1)[1].split(".")[0])
    assert importados & PROHIBIDOS == set()


# ------------------------------------------- el idioma de lo que lee el dueño

def test_un_consejo_sale_en_el_idioma_del_dueno(erp, con_lista_de_costo, monkeypatch):
    """Los cuatro detectores escribían su prosa a mano, en castellano.

    `main.py` se los pasa a `notificar.avisar_dueno`, que los manda tal cual: un
    dueño con IDIOMA_GERENCIA=en recibía el consejo entero en castellano aunque
    todo lo demás del agente ya le contestara en inglés. El módulo lo sabía —los
    `TODO(idioma)` nombraban estas claves— y quedó pendiente porque se escribió
    mientras otra rama editaba el catálogo.

    Se corre la MISMA detección dos veces y se exige que difieran, y que ninguno
    sea la clave cruda: `idioma.t` devuelve la clave cuando no existe, así que
    «está en inglés» no distingue una traducción de una clave sin cargar.

    MUTACIÓN: volver el `titulo`/`cuerpo` de `perdidas` a su literal castellano.
    Cae ésta y sólo ésta.
    """
    erp.tablas["Sales Order"] = [pedido("SO-1")]
    erp.tablas["Sales Order Item"] = [renglon("SO-1", "LECHE-1L", qty=10, rate=100.0)]
    erp.tablas["Item Price"] = [precio()]

    monkeypatch.setenv("IDIOMA_GERENCIA", "es")
    en_es = consejos.perdidas(HOY)
    monkeypatch.setenv("IDIOMA_GERENCIA", "en")
    en_en = consejos.perdidas(HOY)

    assert len(en_es) == len(en_en) == 1
    uno_es, uno_en = en_es[0], en_en[0]

    assert uno_es.titulo != uno_en.titulo, "el título sale igual en los dos idiomas"
    assert uno_es.cuerpo != uno_en.cuerpo, "el cuerpo sale igual en los dos idiomas"
    assert "consejo." not in uno_en.titulo + uno_en.cuerpo, "salió la clave cruda"
    # Los dos lados escritos acá, no leídos del catálogo.
    assert "por debajo del costo" in uno_es.titulo
    assert "below cost" in uno_en.titulo
    # Y el DATO no se traduce: el número de pedido vale igual en los dos.
    assert "SO-1" in uno_es.cuerpo and "SO-1" in uno_en.cuerpo
    # La clave durable tampoco cambia con el idioma: si cambiara, el mismo
    # consejo se mandaría dos veces al cambiar de idioma.
    assert uno_es.clave == uno_en.clave


def test_el_renglon_del_detalle_tampoco_queda_en_castellano(
    erp, con_lista_de_costo, monkeypatch
):
    """EL NIVEL DE ADENTRO, que la prueba de arriba no ve.

    El cuerpo ya salía traducido, pero `{detalle}` —las viñetas, una por
    renglón— se armaba a mano con «a» y «y cuesta» y se interpolaba adentro.
    Un dueño con IDIOMA_GERENCIA=en recibía un párrafo en inglés con los seis
    renglones en castellano: media frase en cada idioma, que es el mismo
    defecto que `gestion.py` tenía con `exc`, un nivel más adentro.

    La de arriba compara `cuerpo != cuerpo` y por eso NO lo agarra: la cáscara
    ya difería. Ésta mira las palabras del renglón.

    MUTACIÓN: volver `detalle` al f-string castellano. Cae ésta y sólo ésta.
    """
    erp.tablas["Sales Order"] = [pedido("SO-1")]
    erp.tablas["Sales Order Item"] = [renglon("SO-1", "LECHE-1L", qty=10, rate=100.0)]
    erp.tablas["Item Price"] = [precio()]

    monkeypatch.setenv("IDIOMA_GERENCIA", "es")
    en_es = consejos.perdidas(HOY)[0].cuerpo
    monkeypatch.setenv("IDIOMA_GERENCIA", "en")
    en_en = consejos.perdidas(HOY)[0].cuerpo

    # Las palabras DEL RENGLÓN, escritas acá y no leídas del catálogo.
    assert "y cuesta" in en_es
    assert "and costs" in en_en
    # Y nada del castellano adentro del inglés. Es lo único que la mutación
    # cambia: el resto del cuerpo ya salía traducido.
    assert "y cuesta" not in en_en
    assert "consejo." not in en_en, "salió la clave cruda"
    # El nombre del producto y la unidad son datos: iguales en los dos.
    assert "LECHE-1L" in en_es and "LECHE-1L" in en_en
    assert "Litro" in en_es and "Litro" in en_en
