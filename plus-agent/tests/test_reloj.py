"""El único reloj del negocio, y las cuatro políticas de error que unifica.

POR QUÉ ESTE ARCHIVO EXISTE
`app/reloj.py` consolidó 13 copias en 11 archivos. Las copias NO estaban de
acuerdo sobre qué pasa con una zona inválida —cuatro comportamientos
distintos— y **ningún test afirmaba ninguno de los cuatro**: se midió cambiando
el concepto en una copia, y rompía UN archivo de test, el de esa copia. La
divergencia era invisible porque ningún test miraba dos copias a la vez.

Así que lo que se prueba acá no es «el reloj anda». Es la parte que nadie
probaba: **qué política de error tiene cada forma, y que las dos son
distintas a propósito**.

Y una cosa que antes no se podía escribir: un test que le pida al reloj una
zona DISTINTA de la que supone el código. Los seis archivos que definían su
propio `AHORA`/`ZONA` compartían la suposición del código, así que no podían
estar en desacuerdo con ella — que es por lo que el bug de zona de #16 pasó
inadvertido a todos los tests que tocaban justo ese campo.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import time_machine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import reloj

# Un instante en que la fecha del negocio y la del servidor NO coinciden, que es
# el único momento en que la diferencia se puede ver: 02:30 UTC del 30 son las
# 23:30 del 29 en Buenos Aires.
CRUCE = datetime(2026, 8, 30, 2, 30, tzinfo=UTC)


# ------------------------------------------------------- las dos políticas


def test_lo_que_decide_levanta_con_una_zona_invalida(monkeypatch) -> None:
    """`zona()` no adivina. `policy._hoy_del_negocio` la usa para fechar un
    pedido, y una fecha adivinada auto-confirma contra el día equivocado."""
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Marte/Olympus_Mons")

    with pytest.raises(reloj.ZonaInvalida, match="BUSINESS_TIMEZONE"):
        reloj.zona()
    with pytest.raises(reloj.ZonaInvalida):
        reloj.ahora()
    with pytest.raises(reloj.ZonaInvalida):
        reloj.hoy()


def test_lo_que_informa_usa_el_default_y_lo_dice(monkeypatch, capsys) -> None:
    """`zona_con_respaldo` NUNCA levanta: el barrido que se cae por una zona mal
    escrita deja de recordarle al cliente para siempre.

    Pero lo dice en el log, con el nombre de quien se conformó. `digest._zona`
    era el mismo respaldo EN SILENCIO, así que una zona mal escrita movía la
    hora del resumen y no quedaba constancia en ninguna parte.
    """
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Marte/Olympus_Mons")

    assert reloj.zona_con_respaldo("prueba").key == reloj.ZONA_DEFAULT
    assert reloj.ahora_con_respaldo("prueba").tzinfo.key == reloj.ZONA_DEFAULT

    salida = capsys.readouterr().out
    assert "[prueba]" in salida
    assert "Marte/Olympus_Mons" in salida
    assert reloj.ZONA_DEFAULT in salida


def test_la_zona_invalida_se_sigue_atrapando_como_RuntimeError(monkeypatch) -> None:
    """`conversacion.business_today` levantaba `RuntimeError` pelado, así que
    quien lo atrapaba así tiene que seguir atrapándolo. Es la razón por la que
    `ZonaInvalida` hereda de `RuntimeError` y no de `ValueError`."""
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Marte/Olympus_Mons")

    with pytest.raises(RuntimeError):
        reloj.zona()


def test_una_zona_vacia_no_es_un_error_sino_el_default(monkeypatch) -> None:
    """`BUSINESS_TIMEZONE=` en un .env es «no la configuré», no «configurala
    mal»: eso es lo que hay en un checkout limpio."""
    monkeypatch.setenv("BUSINESS_TIMEZONE", "   ")

    assert reloj.zona().key == reloj.ZONA_DEFAULT


# ------------------------------------------- el día del negocio, no del server


@time_machine.travel(CRUCE, tick=False)
def test_el_dia_es_el_del_negocio_y_no_el_del_servidor(monkeypatch) -> None:
    """UN instante, TRES zonas, tres fechas. Es la forma de afirmar la regla sin
    fijar el nombre del default: un reloj que leyera la hora del servidor daría
    la misma respuesta las tres veces.

    Es también el test que los seis archivos con su propio `AHORA` no podían
    escribir, porque cada uno tenía la zona del código escrita a mano.
    """
    monkeypatch.setenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires")
    assert reloj.hoy().isoformat() == "2026-08-29"  # 23:30 del 29

    monkeypatch.setenv("BUSINESS_TIMEZONE", "UTC")
    assert reloj.hoy().isoformat() == "2026-08-30"  # 02:30 del 30

    monkeypatch.setenv("BUSINESS_TIMEZONE", "Asia/Tokyo")
    assert reloj.hoy().isoformat() == "2026-08-30"  # 11:30 del 30


# ------------------------------------------------ el sello sin zona de ERPNext


def test_un_sello_sin_zona_se_lee_en_la_zona_del_negocio(monkeypatch) -> None:
    """El caso que #16 encontró y que ninguna de las tres copias probaba.

    `creation`, `posting_date` y `posting_time` vienen SIN zona, en la hora del
    sistema de ERPNext, y acá se interpretan en la del negocio. Las dos zonas
    se configuran por separado; `readiness.chequear_zona_erpnext` las compara.
    """
    monkeypatch.setenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires")
    momento = reloj.de_erpnext("2026-09-08 14:00:00")

    assert momento is not None
    assert momento.tzinfo is not None
    assert momento.isoformat() == "2026-09-08T14:00:00-03:00"


def test_el_mismo_sello_en_otra_zona_es_otro_instante(monkeypatch) -> None:
    """Tres horas de diferencia sobre el MISMO string, que es exactamente el
    error que #16 medía: la edad sale corrida por el offset entero."""
    monkeypatch.setenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires")
    en_negocio = reloj.de_erpnext("2026-09-08 14:00:00")

    monkeypatch.setenv("BUSINESS_TIMEZONE", "UTC")
    en_utc = reloj.de_erpnext("2026-09-08 14:00:00")

    assert (en_negocio - en_utc).total_seconds() / 3600.0 == 3.0


def test_un_sello_que_ya_trae_zona_no_se_re_interpreta(monkeypatch) -> None:
    """Un ERPNext configurado para devolver offsets no se pisa: `replace` sobre
    un momento que ya tiene zona lo movería tres horas por nada."""
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Asia/Tokyo")

    momento = reloj.de_erpnext("2026-09-08T14:00:00+05:30")

    assert momento.isoformat() == "2026-09-08T14:00:00+05:30"


@pytest.mark.parametrize("sello", ["", None, "   ", "ayer", "2026-13-45", 0, {}])
def test_un_sello_ilegible_es_None_y_nunca_ahora(sello) -> None:
    """None NO es 0 y no es «ahora».

    Los tres llamadores ya dependían de esto: un `creation` ilegible tiene que
    dejar el documento en paz, no tratarlo como recién creado (que lo haría
    joven para siempre) ni como vencido hace un mes (que lo cerraría).
    """
    assert reloj.de_erpnext(sello) is None


def test_el_sello_acepta_la_zona_ya_resuelta(monkeypatch) -> None:
    """`pendientes.edad_horas` corre por fila en el barrido y le pasa la zona
    que ya tiene, para no releer el entorno en cada una. El resultado tiene que
    ser el mismo que resolviéndola adentro."""
    monkeypatch.setenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires")

    adentro = reloj.de_erpnext("2026-09-08 14:00:00")
    afuera = reloj.de_erpnext(
        "2026-09-08 14:00:00", en=ZoneInfo("America/Argentina/Buenos_Aires")
    )

    assert adentro == afuera


# --------------------------------------------------------- el default, una vez


def test_el_default_vive_en_un_solo_lugar() -> None:
    """Era un literal en 11 archivos de `app/` y 16 de `tests/`: configurar un
    cliente que no está en Buenos Aires eran 27 sitios.

    Este test no es decorativo — es el que se rompe si alguien vuelve a
    escribir la zona a mano en un módulo. Se mide sobre `app/`, no sobre
    `tests/`, porque un test que nombra su zona a propósito es correcto (ver
    `test_el_dia_es_el_del_negocio_y_no_el_del_servidor`).
    """
    raiz = Path(__file__).resolve().parents[1] / "app"
    culpables = sorted(
        str(py.relative_to(raiz))
        for py in raiz.rglob("*.py")
        if py.name != "reloj.py" and reloj.ZONA_DEFAULT in py.read_text()
    )

    # `briefing.py` sólo la nombra en su docstring, contando a qué hora corre su
    # cron; no resuelve ninguna zona con ella.
    assert culpables == ["briefing.py"], f"la zona volvió a escribirse en {culpables}"
