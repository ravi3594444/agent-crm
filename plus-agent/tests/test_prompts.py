"""Lo que el prompt tiene que decir, escrito como test para que no se pierda.

Cada regla acá salió de un problema visto en vivo:
- el agente contestaba en español a quien escribía en inglés (regla "Nunca uses inglés");
- pedía permiso para cargar un pedido que ya tenía completo (un mensaje de más, siempre);
- adivinaba el año porque no sabía qué día era.
"""
from __future__ import annotations

from app import idioma
from app.conversacion import prompt_clientes, prompt_gerencia
from app.prompts import SYSTEM_ES_AR
from app.prompts_gerencia import SYSTEM_GERENCIA


def _texto_cliente(config=None):
    return prompt_clientes({"messages": []}, config or {"configurable": {}})[0].content


def _texto_gerencia():
    return prompt_gerencia({"messages": []}, {})[0].content


# La regla de idioma dejó de estar escrita a mano en la plantilla y ahora la
# pone app/idioma.py al armar el prompt, así que estos tests miran el prompt
# RENDERIZADO: es lo que el modelo lee de verdad, y no la plantilla.
def test_cliente_responde_en_el_idioma_del_cliente():
    texto = _texto_cliente()
    assert "Nunca uses inglés" not in texto
    assert "idioma en que te escribió" in texto
    assert "inglés" in texto and "español rioplatense" in texto


def test_gerencia_responde_en_el_idioma_configurado():
    # Sin nada fijado rige el idioma por defecto, que es español.
    assert "español rioplatense" in _texto_gerencia()


def test_la_plantilla_delega_la_regla_de_idioma_en_el_catalogo():
    # Nadie debe volver a escribir la regla a mano en la plantilla: si vuelve,
    # hay dos fuentes de verdad y una se queda vieja.
    assert "{IDIOMA_REGLA}" in SYSTEM_ES_AR
    assert "{IDIOMA_REGLA}" in SYSTEM_GERENCIA
    assert "idioma en que te escribió" in idioma.REGLA_ESPEJO_CLIENTE


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
