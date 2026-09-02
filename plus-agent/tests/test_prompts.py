"""Lo que el prompt tiene que decir, escrito como test para que no se pierda.

Cada regla acá salió de un problema visto en vivo:
- el agente contestaba en español a quien escribía en inglés (regla "Nunca uses inglés");
- pedía permiso para cargar un pedido que ya tenía completo (un mensaje de más, siempre);
- adivinaba el año porque no sabía qué día era.
"""
from __future__ import annotations

from app.prompts import SYSTEM_ES_AR
from app.prompts_gerencia import SYSTEM_GERENCIA


def test_cliente_responde_en_el_idioma_del_cliente():
    assert "Nunca uses inglés" not in SYSTEM_ES_AR
    assert "idioma en que te escribió" in SYSTEM_ES_AR
    assert "inglés" in SYSTEM_ES_AR and "español rioplatense" in SYSTEM_ES_AR


def test_gerencia_tambien_responde_en_el_idioma_del_que_escribe():
    assert "idioma en que te escribieron" in SYSTEM_GERENCIA


def test_los_nombres_de_producto_no_se_traducen():
    assert "no los traduzcas" in SYSTEM_ES_AR


def test_con_los_cuatro_datos_crea_el_pedido_sin_pedir_permiso():
    assert "DIRECTAMENTE" in SYSTEM_ES_AR
    assert "No pidas permiso" in SYSTEM_ES_AR
    assert "Antes de crear_pedido confirmá" not in SYSTEM_ES_AR


def test_pregunta_solo_si_falta_algo_y_una_sola_vez():
    assert "UNA sola pregunta corta" in SYSTEM_ES_AR


def test_sabe_que_dia_es():
    assert "{HOY}" in SYSTEM_ES_AR and "{HOY}" in SYSTEM_GERENCIA
