"""La auditoría final: con el idioma en inglés, ¿queda algo en español?

Este archivo es el guard. Los otros tests prueban una categoría cada uno; éste
recorre TODOS los constructores de mensajes migrados, los ejecuta en inglés, y
exige que lo que sale no tenga una palabra en español que no esté justificada
en tests/idioma_allowlist.py.

Dos auditorías, y hacen falta las dos:

  * la de EJECUCIÓN corre los constructores de verdad y mira el texto. Es la
    que encuentra el call site que nadie cableó — un test de catálogo pasa
    igual aunque el call site siga escribiendo el literal a mano.

  * la ESTÁTICA mira los puntos de salida (enviar_mensaje / enviar_botones /
    enviar_plantilla) y exige que ninguno reciba un literal en español escrito
    ahí mismo. Cubre los caminos que la de ejecución no alcanza.
"""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from idioma_allowlist import PERMITIDO_EN_SALIDA_INGLESA
from idioma_captura import restos_en_espanol

from app import idioma

EN = idioma.EN
ES = idioma.ES

PEDIDO = "SAL-ORD-2026-00042"
MOTIVO = "no stock"
DATOS = ("Demo Bakery", "Whole Milk 1 L", "ARS", "manual", MOTIVO, "over the limit")

_SO = {
    "name": PEDIDO,
    "customer_name": "Demo Bakery",
    "grand_total": 6000,
    "currency": "ARS",
    "delivery_date": "2026-09-06",
    "items": [{"item_code": "MILK-1L", "item_name": "Whole Milk 1 L", "qty": 5,
               "uom": "Unit"}],
}


def _solicitud(**extra):
    from app import solicitudes

    campos = {
        "id": "SOL-1", "pedido": PEDIDO, "tipo": solicitudes.TIPO_ENTREGA,
        "estado": solicitudes.PENDIENTE, "cliente": "CUST-1",
        "cliente_nombre": "Demo Bakery", "resumen_items": "5 x Whole Milk 1 L",
        "total": 6000.0, "moneda": "ARS", "creada_en": 0.0,
        "vence_en": 6 * 3600.0, "sello": 0.0, "ofrecido": {"metodo": "entrega"},
    }
    campos.update(extra)
    return solicitudes.Solicitud(**campos)


def _todos_los_constructores(lengua):
    """(nombre, texto) de CADA mensaje determinista migrado, en `lengua`."""
    from app import avisos, decisiones, main, notificar, solicitudes

    sol = _solicitud(motivo=MOTIVO)
    salida = [
        # 1. acuse, fallbacks y errores
        ("main.texto_ack", main.texto_ack(lengua)),
        ("main.texto_solo_texto", main.texto_solo_texto(lengua)),
        ("main.texto_error_tecnico", main.texto_error_tecnico(lengua)),
        ("main.texto_error_tecnico_avisado", main.texto_error_tecnico_avisado(lengua)),
        ("main.texto_respuesta_vacia", main.texto_respuesta_vacia(lengua)),
        # 2. estado del pedido
        ("avisos.texto_confirmacion_cliente",
         avisos.texto_confirmacion_cliente(_SO, lengua)),
        ("decisiones._texto_rechazo", decisiones._texto_rechazo(PEDIDO, MOTIVO, lengua)),
        ("decisiones._texto_cancelacion",
         decisiones._texto_cancelacion(PEDIDO, MOTIVO, lengua)),
        ("solicitudes.texto_pendiente_cliente",
         solicitudes.texto_pendiente_cliente(sol, lengua)),
        # 3. avisos a la gerencia
        ("notificar._texto_libre(pendiente)",
         notificar._texto_libre(PEDIDO, _SO, False, "over the limit",
                                "5 x Whole Milk 1 L", lengua)),
        ("notificar._texto_libre(auto)",
         notificar._texto_libre(PEDIDO, _SO, True, "", "5 x Whole Milk 1 L", lengua)),
        ("notificar.texto_confirmacion",
         notificar.texto_confirmacion(_SO, "manual", "2026-09-05 16:14", lengua)),
        # 4. excepciones de entrega y vencimientos
        ("solicitudes.texto_oferta_cliente",
         solicitudes.texto_oferta_cliente(sol, lengua)),
        ("solicitudes.texto_rechazo_cliente",
         solicitudes.texto_rechazo_cliente(sol, lengua)),
        ("solicitudes.texto_vencida_cliente",
         solicitudes.texto_vencida_cliente(sol, lengua)),
        ("solicitudes.texto_respaldo_cliente",
         solicitudes.texto_respaldo_cliente(sol, lengua)),
        ("solicitudes.texto_revision_vencida_cliente",
         solicitudes.texto_revision_vencida_cliente(sol, lengua)),
        ("solicitudes.texto_respaldo_vencido_cliente",
         solicitudes.texto_respaldo_vencido_cliente(sol, lengua)),
    ]
    return salida


# ---------------------------------------------------- auditoría de ejecución


def test_la_auditoria_final_no_encuentra_espanol_con_el_idioma_en_ingles():
    """LA prueba. Si esto falla, quedó un mensaje sin migrar."""
    sucios = {}
    for nombre, texto in _todos_los_constructores(EN):
        restos = restos_en_espanol(texto, DATOS + PERMITIDO_EN_SALIDA_INGLESA)
        if restos:
            sucios[nombre] = restos
    assert sucios == {}, f"mensajes con español sin justificar: {sucios}"


def test_cada_constructor_dice_algo_distinto_en_cada_idioma():
    """Un texto idéntico en los dos idiomas es un texto sin traducir."""
    iguales = [
        nombre
        for (nombre, es), (_, en) in zip(
            _todos_los_constructores(ES), _todos_los_constructores(EN), strict=True
        )
        if es == en
    ]
    assert iguales == [], f"sin traducir: {iguales}"


def test_ningun_constructor_manda_los_dos_idiomas_a_la_vez():
    """La concatenación bilingüe era el parche viejo; no puede quedar ninguna."""
    for nombre, texto in _todos_los_constructores(EN):
        assert "Sobre tu pedido" not in texto, nombre
        assert "Hola!" not in texto, nombre
    for nombre, texto in _todos_los_constructores(ES):
        assert "About your order" not in texto, nombre
        assert "Hi!" not in texto, nombre


@pytest.mark.parametrize("dato", [PEDIDO, "2026-09-06", "Demo Bakery"])
def test_los_datos_sobreviven_identicos_a_los_dos_idiomas(dato):
    """Un número de pedido, una fecha y un nombre valen lo mismo en los dos."""
    for (nombre, es), (_, en) in zip(
        _todos_los_constructores(ES), _todos_los_constructores(EN), strict=True
    ):
        if dato in es:
            assert dato in en, f"{nombre} perdió {dato!r} al traducir"


# ------------------------------------------------------- auditoría estática

_SINKS = {"enviar_mensaje", "enviar_botones", "enviar_plantilla"}
_APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def _literales_en_sinks() -> list[tuple[str, int, str]]:
    """Literales en español pasados DIRECTO a un punto de salida."""
    hallados = []
    for archivo in sorted(_APP.rglob("*.py")):
        if "__pycache__" in str(archivo):
            continue
        arbol = ast.parse(archivo.read_text())
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            nombre = getattr(nodo.func, "attr", None) or getattr(nodo.func, "id", None)
            if nombre not in _SINKS:
                continue
            for arg in nodo.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    restos = restos_en_espanol(
                        arg.value, PERMITIDO_EN_SALIDA_INGLESA
                    )
                    if restos:
                        hallados.append(
                            (str(archivo.relative_to(_APP.parent)), nodo.lineno,
                             arg.value[:70])
                        )
    return hallados


def test_ningun_punto_de_salida_recibe_un_literal_en_espanol():
    """Nadie le pasa prosa escrita a mano a enviar_mensaje y compañía."""
    assert _literales_en_sinks() == []


def test_el_catalogo_esta_completo_en_los_dos_idiomas():
    assert idioma.claves_incompletas() == []


def test_el_catalogo_cubre_las_categorias_de_la_migracion():
    categorias = {clave.split(".")[0] for clave in idioma.CATALOGO}
    for esperada in ("ack", "fallback", "pedido", "gerencia", "entrega",
                     "codigo", "accion", "sistema", "stock", "precio", "idioma"):
        assert esperada in categorias, f"falta {esperada}"


def test_la_lista_de_intencionalmente_sin_traducir_esta_documentada():
    """El allowlist no puede ser una lista muda: cada grupo se explica."""
    import idioma_allowlist as permitido

    assert permitido.__doc__ and "no tiene idioma" in permitido.__doc__
    assert permitido.MARCAS_DURABLES
    assert permitido.ERPNEXT_CANONICO
    assert permitido.COMANDOS_ES
    # Los comandos en inglés se AGREGARON; los de siempre siguen.
    assert "acepto" in permitido.COMANDOS_ES
    assert "accept" in permitido.COMANDOS_EN_QUE_TAMBIEN_PARSEAN
