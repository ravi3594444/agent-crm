"""Normalización de teléfonos argentinos.

ESTE ES EL TEST MÁS IMPORTANTE DEL REPO y el que documenta el bug que hacía
que el sistema no funcionara para NINGÚN cliente: Meta manda `5493511234567`
y ERPNext tenía guardado `+5493511234567`, así que el filtro `=` no matcheaba
nunca y todo cliente registrado entraba como desconocido.
"""

from __future__ import annotations

import pytest

from app import telefono

CANONICO = "5493511234567"

# Todas estas formas son el MISMO número, escrito como lo escribe cada uno.
FORMAS_EQUIVALENTES = [
    "5493511234567",  # lo que manda Meta
    "+5493511234567",  # E.164 con +
    "+54 9 351 123-4567",  # como lo carga una persona
    "0351 15 123-4567",  # formato local con 0 y 15
    "03511512345 67",  # con espacios raros
    "(0351) 15-123-4567",  # con paréntesis
    "351 123 4567",  # sin 0, sin 15, sin país
    "3511234567",  # solo área + abonado
    "005493511234567",  # con prefijo de salida internacional
]


@pytest.mark.parametrize("crudo", FORMAS_EQUIVALENTES)
def test_todas_las_formas_dan_el_mismo_numero(crudo):
    assert telefono.normalizar(crudo) == CANONICO


@pytest.mark.parametrize("crudo", FORMAS_EQUIVALENTES)
def test_lo_que_manda_meta_matchea_lo_que_hay_en_erpnext(crudo):
    """El bug original, como test: el número de Meta tiene que matchear
    cualquier formato guardado a mano."""
    assert telefono.son_el_mismo("5493511234567", crudo)


def test_buenos_aires_area_de_dos_digitos():
    assert telefono.normalizar("+54 9 11 4567-8901") == "5491145678901"
    assert telefono.normalizar("011 15 4567-8901") == "5491145678901"
    assert telefono.son_el_mismo("5491145678901", "011 15 4567 8901")


def test_area_de_cuatro_digitos():
    # 03544 (Carlos Paz) + 15 + 6 dígitos
    assert telefono.normalizar("03544 15 12-3456") == "5493544123456"
    assert telefono.son_el_mismo("5493544123456", "03544 15 123456")


@pytest.mark.parametrize("crudo", FORMAS_EQUIVALENTES)
def test_normalizar_es_idempotente(crudo):
    """Normalizar dos veces tiene que dar lo mismo que una.

    Importa porque el número pasa por normalizar() en varios lugares
    (router, whatsapp, clientes): si no fuera idempotente, el segundo paso
    agregaría otro 9 y dejaría de matchear.
    """
    una = telefono.normalizar(crudo)
    assert telefono.normalizar(una) == una


def test_numeros_distintos_no_matchean():
    assert not telefono.son_el_mismo("5493511234567", "5493511234568")
    assert not telefono.son_el_mismo("5493511234567", "5491145678901")


def test_basura_no_explota():
    for entrada in (None, "", "   ", "hola", "+++", "no-es-un-numero"):
        assert telefono.normalizar(entrada) == ""
        assert not telefono.son_el_mismo(entrada, CANONICO)


def test_numero_extranjero_se_deja_como_esta():
    """Un número de Uruguay o Brasil no se le aplican las reglas argentinas."""
    assert telefono.normalizar("+598 99 123 456") == "59899123456"
    assert telefono.normalizar("5511987654321") == "5511987654321"


def test_clave_de_busqueda_es_estable_entre_formatos():
    """La clave con la que buscamos en ERPNext tiene que ser la misma para
    todos los formatos, o el `like` no encuentra nada."""
    claves = {telefono.clave_busqueda(f) for f in FORMAS_EQUIVALENTES}
    assert len(claves) == 1
    assert len(claves.pop()) == telefono.LARGO_BUSQUEDA


def test_clave_de_busqueda_vacia_para_basura():
    assert telefono.clave_busqueda("") == ""
    assert telefono.clave_busqueda("abc") == ""
