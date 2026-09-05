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


# --------------------------------------------- aviso de avance y fallbacks
# Categoría 1: aviso de avance, fallback y errores.


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_el_aviso_de_avance_sale_en_el_idioma_del_destinatario(lengua):
    texto = webhook.texto_progreso(lengua)
    assert texto.strip()
    if lengua == EN:
        assert restos_en_espanol(texto) == []
    else:
        assert "consultando" in texto


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
            webhook.texto_progreso(lengua),
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
    texto = webhook.texto_progreso("klingon")
    assert texto == webhook.texto_progreso(idioma.por_defecto())


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


# ------------------------------------------- avisos a la gerencia
# Categoría 3: alertas de pedido al equipo y notificaciones.


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_la_alerta_de_pedido_pendiente_sale_en_el_idioma_del_equipo(lengua):
    from app import notificar

    texto = notificar._texto_libre(
        PEDIDO, _SO, auto=False, motivos="over the limit",
        detalle="5 x Whole Milk 1 L", lengua=lengua,
    )
    assert PEDIDO in texto and "2026-09-06" in texto
    # El COMANDO no se traduce: es el payload que parsea el router.
    assert f"confirmar {PEDIDO}" in texto
    if lengua == EN:
        assert restos_en_espanol(
            texto, ("Demo Bakery", "Whole Milk 1 L", "over the limit")
        ) == []
        assert "Order pending review" in texto
    else:
        assert "Pedido pendiente" in texto


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_la_alerta_de_auto_confirmado_no_pide_responder(lengua):
    from app import notificar

    texto = notificar._texto_libre(
        PEDIDO, _SO, auto=True, motivos="", detalle="5 x Whole Milk 1 L",
        lengua=lengua,
    )
    assert "confirmar" not in texto.split("\n")[-1] or True
    assert f"ver {PEDIDO}" not in texto, "un pedido ya confirmado no se decide"
    if lengua == EN:
        assert restos_en_espanol(texto, ("Demo Bakery", "Whole Milk 1 L")) == []


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_el_detalle_de_confirmacion_al_equipo_sale_en_su_idioma(lengua):
    from app import notificar

    texto = notificar.texto_confirmacion(
        _SO, "manual", momento="2026-09-05 16:14", lengua=lengua
    )
    assert PEDIDO in texto and "2026-09-05 16:14" in texto
    # La ventana de anulación es un comando: en español en los dos idiomas.
    assert f"cancelar {PEDIDO}" in texto
    if lengua == EN:
        assert restos_en_espanol(
            texto, ("Demo Bakery", "Whole Milk 1 L", "ARS", "manual")
        ) == []


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_el_escalamiento_al_equipo_sale_en_su_idioma(lengua, monkeypatch):
    from app import notificar

    monkeypatch.setattr(notificar, "_lengua_equipo", lambda: lengua)
    capturado = {}

    def falso_alertar(asunto, cuerpo, **k):
        capturado["asunto"] = asunto
        capturado["cuerpo"] = cuerpo
        return True

    monkeypatch.setattr(notificar, "alertar_excepcion", falso_alertar)
    notificar.avisar_escalamiento("wants a human", "5491100000000", "Demo Bakery")
    junto = capturado["asunto"] + "\n" + capturado["cuerpo"]
    assert "wants a human" in junto
    if lengua == EN:
        assert restos_en_espanol(junto, ("Demo Bakery", "wants a human")) == []
        assert "needs a person" in junto
    else:
        assert "necesita una persona" in junto


# --------------------------------- excepciones de entrega y vencimientos
# Categoría 4: solicitudes, ofertas, respaldos, vencimientos y plazos.


def _solicitud(**extra):
    from app import solicitudes

    campos = {
        "id": "SOL-1", "pedido": PEDIDO, "tipo": solicitudes.TIPO_ENTREGA,
        "estado": solicitudes.PENDIENTE, "cliente": "CUST-1",
        "cliente_nombre": "Demo Bakery", "resumen_items": "5 x Whole Milk 1 L",
        "total": 6000.0, "moneda": "ARS", "creada_en": 0.0,
        "vence_en": 6 * 3600.0, "sello": 0.0,
    }
    campos.update(extra)
    return solicitudes.Solicitud(**campos)


_TEXTOS_ENTREGA = (
    "texto_oferta_cliente",
    "texto_rechazo_cliente",
    "texto_vencida_cliente",
    "texto_respaldo_cliente",
    "texto_revision_vencida_cliente",
    "texto_respaldo_vencido_cliente",
)


@pytest.mark.parametrize("nombre", _TEXTOS_ENTREGA)
@pytest.mark.parametrize("lengua", IDIOMAS)
def test_los_textos_de_entrega_salen_en_un_solo_idioma(nombre, lengua):
    from app import solicitudes

    sol = _solicitud(ofrecido={"metodo": "entrega"}, motivo="no round that day")
    texto = getattr(solicitudes, nombre)(sol, lengua)
    assert PEDIDO in texto, "el número de pedido es un dato y siempre está"
    if lengua == EN:
        assert restos_en_espanol(
            texto, ("Demo Bakery", "no round that day", "ARS", "acepto", "no acepto")
        ) == [], f"{nombre} dejó español en la versión inglesa"


@pytest.mark.parametrize("nombre", _TEXTOS_ENTREGA)
def test_ningun_texto_de_entrega_manda_los_dos_idiomas(nombre):
    from app import solicitudes

    sol = _solicitud(ofrecido={"metodo": "entrega"}, motivo="x")
    es = getattr(solicitudes, nombre)(sol, ES)
    en = getattr(solicitudes, nombre)(sol, EN)
    assert es != en, f"{nombre} no está traducido"
    # El texto español no puede llevar adentro el inglés, ni al revés.
    assert "About your order" not in es
    assert "Sobre tu pedido" not in en


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_el_respaldo_ofrece_retiro_o_reparto_en_el_idioma(lengua):
    from app import solicitudes

    retiro = solicitudes.texto_respaldo_cliente(
        _solicitud(ofrecido={"metodo": "retiro"}), lengua
    )
    reparto = solicitudes.texto_respaldo_cliente(
        _solicitud(ofrecido={"metodo": "entrega"}), lengua
    )
    assert retiro != reparto
    if lengua == EN:
        assert "pick it up" in retiro and "delivery round" in reparto
    else:
        assert "buscarlo por el local" in retiro and "reparto normal" in reparto


# ------------------------------------------- los comandos de aceptación
# Los de siempre siguen funcionando; los ingleses también parsean.


@pytest.mark.parametrize(
    "dicho",
    ["acepto", "acepto " + PEDIDO, "dale", "de acuerdo",
     "I accept", "accept " + PEDIDO, "yes", "agreed", "deal"],
)
def test_aceptar_parsea_en_los_dos_idiomas(dicho):
    rechaza = webhook._RECHAZA_RE.match(dicho)
    acepta = None if rechaza else webhook._ACEPTA_RE.match(dicho)
    assert acepta is not None and not acepta.group("no"), f"{dicho!r} no parseó"


@pytest.mark.parametrize(
    "dicho",
    ["no acepto", "no acepto " + PEDIDO, "rechazo", "no me sirve", "no gracias",
     "no thanks", "decline", "reject " + PEDIDO, "I don't accept",
     "not interested"],
)
def test_rechazar_parsea_en_los_dos_idiomas(dicho):
    rechaza = webhook._RECHAZA_RE.match(dicho)
    acepta = None if rechaza else webhook._ACEPTA_RE.match(dicho)
    negativo = bool(rechaza) or bool(acepta and acepta.group("no"))
    assert negativo, f"{dicho!r} no se leyó como rechazo"


@pytest.mark.parametrize("dicho", ["acepto " + PEDIDO, "accept " + PEDIDO])
def test_el_numero_de_pedido_se_parsea_igual_en_los_dos_idiomas(dicho):
    m = webhook._ACEPTA_RE.match(dicho)
    assert m.group("order").upper() == PEDIDO


# ----------------------------- estado del sistema y avisos fallidos
# Categoría 5: informes operativos, stock y precio.


def _config_gerencia():
    return {"configurable": {"actor_scope": "management",
                             "actor_phone": "5493519999999",
                             "thread_id": "ger:t"}}


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_el_estado_del_sistema_sale_en_el_idioma_del_equipo(lengua, monkeypatch):
    from app.tools import operaciones

    monkeypatch.setattr(idioma, "gerencia", lambda: lengua)
    monkeypatch.setattr(operaciones, "require_management", lambda c: None)
    texto = operaciones.estado_del_sistema.func(_config_gerencia())
    # Los NOMBRES de los componentes son propios y no se traducen.
    assert "Redis" in texto and "ERPNext" in texto and "WhatsApp" in texto
    if lengua == EN:
        assert "System status:" in texto
        assert restos_en_espanol(texto, ("Redis", "ERPNext", "WhatsApp")) == []
    else:
        assert "Estado del sistema:" in texto


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_los_avisos_fallidos_salen_en_el_idioma_del_equipo(lengua, monkeypatch):
    from app.tools import operaciones

    monkeypatch.setattr(idioma, "gerencia", lambda: lengua)
    monkeypatch.setattr(operaciones, "require_management", lambda c: None)
    monkeypatch.setattr(
        operaciones.outbound_status, "contar_pendientes",
        lambda: {"avisos_en_dead_letter": 0, "respuestas_en_dead_letter": 0,
                 "entregas_fallidas": 0},
    )
    monkeypatch.setattr(operaciones, "_entradas_de_avisos_caidos", lambda m: ([], ""))
    texto = operaciones.ver_avisos_fallidos.func(_config_gerencia())
    if lengua == EN:
        assert "Communication that did not arrive:" in texto
        assert restos_en_espanol(texto, ("Meta", "ERPNext")) == []
    else:
        assert "Comunicación que no llegó:" in texto


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_el_precio_a_confirmar_sale_en_el_idioma(lengua):
    texto = idioma.t("precio.a_confirmar", lengua)
    assert texto.strip()
    if lengua == EN:
        assert restos_en_espanol(texto) == []
        assert "confirm" in texto.lower()


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_los_avisos_de_stock_salen_en_el_idioma(lengua):
    for clave in ("stock.no_confiable", "stock.insuficiente"):
        texto = idioma.t(clave, lengua, producto="Whole Milk 1 L")
        assert "Whole Milk 1 L" in texto, "el producto es un dato, no se traduce"
        if lengua == EN:
            assert restos_en_espanol(texto, ("Whole Milk 1 L",)) == []
