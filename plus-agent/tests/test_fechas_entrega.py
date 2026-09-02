"""Fechas de entrega como las escribe la gente por WhatsApp.

EL BUG QUE ESTO CONGELA (visto en vivo el 2026-09-01)
El cliente escribió "para mañana, 2 de septiembre" y el bot respondió
"esa fecha ya pasó. El pedido NO se creó". Dos causas:
  1. El prompt del agente de CLIENTES no incluía la fecha de hoy (el de
     gerencia sí), así que el modelo adivinó el año: 2025-09-02.
  2. El parser no entendía nombres de mes en español: solo ISO y DD/MM.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.tools.pedidos import FechaEntregaInvalida, _parse_fecha

HOY = date(2026, 9, 1)


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("2 de septiembre", "2026-09-02"),
        ("el 2 de septiembre", "2026-09-02"),
        ("2 sep", "2026-09-02"),
        ("2 sept", "2026-09-02"),
        ("2 setiembre", "2026-09-02"),
        ("2 de setiembre", "2026-09-02"),
        ("15 de octubre", "2026-10-15"),
        ("15 oct", "2026-10-15"),
        ("2 de septiembre 2026", "2026-09-02"),
        ("2 de septiembre de 2026", "2026-09-02"),
        ("1 de enero", "2027-01-01"),          # ya pasó este año -> el que viene
        ("30 de agosto", "2027-08-30"),        # idem
        ("1 de Septiembre", "2026-09-01"),     # hoy cuenta como válido
        ("Mañana 2 de Septiembre", "2026-09-02"),
    ],
)
def test_nombres_de_mes_en_espanol(texto, esperado):
    assert _parse_fecha(texto, hoy=HOY) == esperado


def test_lo_que_ya_funcionaba_sigue_funcionando():
    assert _parse_fecha("2026-09-02", hoy=HOY) == "2026-09-02"
    assert _parse_fecha("mañana", hoy=HOY) == "2026-09-02"
    assert _parse_fecha("para mañana", hoy=HOY) == "2026-09-02"
    assert _parse_fecha("pasado mañana", hoy=HOY) == "2026-09-03"
    assert _parse_fecha("02/09", hoy=HOY) == "2026-09-02"
    assert _parse_fecha("2/9", hoy=HOY) == "2026-09-02"


def test_fecha_pasada_sigue_rechazada():
    """El año adivinado por el modelo (2025) tiene que seguir rechazándose:
    la corrección es darle la fecha de hoy, no aceptar fechas viejas."""
    with pytest.raises(FechaEntregaInvalida, match="ya pasó"):
        _parse_fecha("2025-09-02", hoy=HOY)
    with pytest.raises(FechaEntregaInvalida, match="ya pasó"):
        _parse_fecha("2 de septiembre 2025", hoy=HOY)


def test_fecha_inexistente_se_rechaza():
    with pytest.raises(FechaEntregaInvalida, match="no existe"):
        _parse_fecha("31 de febrero", hoy=HOY)
    with pytest.raises(FechaEntregaInvalida, match="no existe"):
        _parse_fecha("31 de febrero 2026", hoy=HOY)


def test_el_prompt_de_clientes_lleva_la_fecha_de_hoy():
    """Sin esto el modelo adivina el año. Verificado en vivo: adivinó 2025."""
    from app.prompts import SYSTEM_ES_AR

    assert "{HOY}" in SYSTEM_ES_AR
    assert "Nunca adivines el año" in SYSTEM_ES_AR
