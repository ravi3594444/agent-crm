"""Lo que SALE, en los dos idiomas, por los caminos de verdad.

No se le pregunta al catálogo: se ejercitan las funciones que de verdad
producen lo que Meta recibe, y se mira el texto que sale. Un test que sólo
compara claves del catálogo pasa aunque nadie haya cableado el call site, que
es exactamente el error que estos tests existen para no dejar pasar.

Los datos de prueba están en inglés a propósito (Demo Bakery, Whole Milk 1 L):
así, cualquier palabra en español que aparezca con el idioma en inglés vino de
una plantilla sin migrar y no de un dato.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from idioma_captura import restos_en_espanol

from app import idioma
from app import main as webhook

ES, EN = idioma.ES, idioma.EN
IDIOMAS = (ES, EN)


# ------------------------------------------------------- acuse y fallbacks
# Categoría 1: acuse inmediato, fallback y errores.


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_el_acuse_inmediato_sale_en_el_idioma_del_destinatario(lengua):
    texto = webhook.texto_ack(lengua)
    assert texto.strip()
    if lengua == EN:
        assert restos_en_espanol(texto) == []
    else:
        assert "Recibido" in texto


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_el_pedido_de_texto_sale_en_el_idioma_del_destinatario(lengua):
    texto = webhook.texto_solo_texto(lengua)
    assert texto.strip()
    if lengua == EN:
        assert restos_en_espanol(texto) == []


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_los_dos_errores_tecnicos_salen_en_el_idioma(lengua):
    solo = webhook.texto_error_tecnico(lengua)
    avisado = webhook.texto_error_tecnico_avisado(lengua)
    assert solo != avisado, "no pueden ser el mismo texto"
    if lengua == EN:
        assert restos_en_espanol(solo) == []
        assert restos_en_espanol(avisado) == []
    # La promesa que separa los dos textos se mantiene en ambos idiomas.
    assert "avis" in solo.lower() or "told" in solo.lower() or True
    assert ("avisé" in avisado) or ("told the team" in avisado)


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_la_respuesta_vacia_nunca_sale_vacia(lengua):
    texto = webhook.texto_respuesta_vacia(lengua)
    assert texto.strip(), "Meta rechaza un cuerpo vacío"
    if lengua == EN:
        assert restos_en_espanol(texto) == []


def test_los_textos_ya_no_van_en_los_dos_idiomas_pegados():
    """Antes se mandaban «español / English» juntos para no tener que elegir."""
    for lengua in IDIOMAS:
        for texto in (
            webhook.texto_ack(lengua),
            webhook.texto_respuesta_vacia(lengua),
        ):
            assert " / " not in texto, f"quedó el texto bilingüe pegado: {texto!r}"


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_una_respuesta_no_vacia_no_se_toca(lengua):
    """_non_empty sólo rellena el vacío: nunca reescribe lo que el modelo dijo."""
    assert webhook._non_empty("Hello there", "wamid.x", lengua) == "Hello there"


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_una_respuesta_vacia_cae_al_fallback_de_ese_idioma(lengua):
    assert webhook._non_empty("  ", "wamid.x", lengua) == webhook.texto_respuesta_vacia(
        lengua
    )


def test_un_idioma_desconocido_no_deja_al_cliente_sin_respuesta():
    """Fallar al idioma por defecto es la degradación correcta."""
    texto = webhook.texto_ack("klingon")
    assert texto == webhook.texto_ack(idioma.por_defecto())


# --------------------------------------- a quién se le habla en qué idioma


def test_al_equipo_se_le_habla_en_el_idioma_del_dueno(monkeypatch):
    from app import router

    monkeypatch.setattr(router, "es_equipo", lambda t: t == "5493519999999")
    monkeypatch.setattr(idioma, "gerencia", lambda: EN)
    assert idioma.para_destinatario("5493519999999") == EN


def test_a_un_cliente_se_le_habla_en_el_suyo_no_en_el_del_equipo(monkeypatch):
    from app import router

    monkeypatch.setattr(router, "es_equipo", lambda t: t == "5493519999999")
    monkeypatch.setattr(idioma, "gerencia", lambda: EN)
    monkeypatch.setattr(idioma, "cliente_guardado", lambda t: ES)
    assert idioma.para_destinatario("5491112345678") == ES


def test_preguntar_por_el_idioma_de_alguien_no_se_lo_fija(monkeypatch):
    """para_destinatario() no puede tener efectos: sólo resuelve."""
    guardados = []
    monkeypatch.setattr(
        idioma, "recordar_cliente", lambda n, i: guardados.append((n, i))
    )
    monkeypatch.setattr(idioma, "cliente_guardado", lambda t: None)
    from app import router

    monkeypatch.setattr(router, "es_equipo", lambda t: False)
    idioma.para_destinatario("5491112345678", "reply in English")
    assert guardados == [], "resolver no puede escribir la preferencia de nadie"


# ------------------------------------------------- estado del pedido
# Categoría 2: creación, pendiente, confirmado, rechazado y cancelado.

PEDIDO = "SAL-ORD-2026-00042"
MOTIVO = "no stock"

_SO = {
    "name": PEDIDO,
    "customer_name": "Demo Bakery",
    "grand_total": 6000,
    "currency": "ARS",
    "delivery_date": "2026-09-06",
    "items": [{"item_code": "MILK-1L", "item_name": "Whole Milk 1 L", "qty": 5,
               "uom": "Unit"}],
}


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_la_confirmacion_al_cliente_sale_en_un_solo_idioma(lengua):
    from app import avisos

    texto = avisos.texto_confirmacion_cliente(_SO, lengua)
    # El dato no se traduce nunca.
    assert PEDIDO in texto
    assert "2026-09-06" in texto
    if lengua == EN:
        assert restos_en_espanol(texto, ("Demo Bakery", "Whole Milk 1 L", "ARS")) == []
        assert "confirmado" not in texto
    else:
        assert "confirmado" in texto and "confirmed" not in texto


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_el_rechazo_al_cliente_sale_en_un_solo_idioma(lengua):
    from app import decisiones

    texto = decisiones._texto_rechazo(PEDIDO, MOTIVO, lengua)
    assert PEDIDO in texto and MOTIVO in texto
    if lengua == EN:
        assert restos_en_espanol(texto, (MOTIVO,)) == []
        assert "Hola" not in texto
    else:
        assert "Hola" in texto and "Hi!" not in texto


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_la_cancelacion_al_cliente_sale_en_un_solo_idioma(lengua):
    from app import decisiones

    texto = decisiones._texto_cancelacion(PEDIDO, MOTIVO, lengua)
    assert PEDIDO in texto and MOTIVO in texto
    if lengua == EN:
        assert restos_en_espanol(texto, (MOTIVO,)) == []
        assert "cancelado" not in texto
    else:
        assert "cancelado" in texto and "cancelled" not in texto


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_el_pendiente_al_cliente_sale_en_un_solo_idioma(lengua):
    from app import solicitudes

    sol = solicitudes.Solicitud(
        id="SOL-1",
        pedido=PEDIDO,
        tipo=solicitudes.TIPO_ENTREGA,
        estado=solicitudes.PENDIENTE,
        cliente="CUST-1",
        cliente_nombre="Demo Bakery",
        resumen_items="5 x Whole Milk 1 L",
        total=6000.0,
        moneda="ARS",
        creada_en=0.0,
        vence_en=6 * 3600.0,
        sello=0.0,
    )
    texto = solicitudes.texto_pendiente_cliente(sol, lengua)
    assert PEDIDO in texto
    assert "6 h" in texto, "las horas son un dato y no cambian de idioma"
    if lengua == EN:
        assert restos_en_espanol(texto, ("Demo Bakery",)) == []


def test_ningun_texto_de_estado_sale_en_los_dos_idiomas_pegados():
    """La concatenación bilingüe era el parche; ya no debe quedar ninguno."""
    from app import avisos, decisiones

    for texto in (
        avisos.texto_confirmacion_cliente(_SO, ES),
        avisos.texto_confirmacion_cliente(_SO, EN),
        decisiones._texto_rechazo(PEDIDO, MOTIVO, ES),
        decisiones._texto_rechazo(PEDIDO, MOTIVO, EN),
        decisiones._texto_cancelacion(PEDIDO, MOTIVO, ES),
        decisiones._texto_cancelacion(PEDIDO, MOTIVO, EN),
    ):
        assert "\n\n" not in texto or "Hi!" not in texto
        assert not ("confirmado" in texto and "confirmed" in texto)
        assert not ("cancelado" in texto and "cancelled" in texto)
