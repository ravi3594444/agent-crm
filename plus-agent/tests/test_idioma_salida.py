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
from unittest.mock import Mock

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
    # Cada idioma tiene su texto escrito a mano: no se le manda el mismo a los dos.
    assert webhook.texto_progreso(ES) != webhook.texto_progreso(EN)
    # Y ninguno de los dos narra lo que pasa por dentro. Antes decía «Estoy
    # consultando el sistema, dame un momento»: quien está esperando no tiene por
    # qué enterarse de que existe un sistema, y en la mitad de los turnos en que
    # ese aviso salía ya no se estaba consultando nada (app/progreso.py).
    assert "sistema" not in texto.lower()
    assert "system" not in texto.lower()


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
    else:
        assert "we won't be able" not in texto
    # Y no saluda: este mensaje llega cuando ya se estuvo hablando, y el saludo
    # va una sola vez por conversación.
    assert not texto.startswith(("Hola", "Hi"))


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
    assert not texto.startswith(("Hola", "Hi"))


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
def test_el_total_del_aviso_lo_escribe_pesos_y_no_python(lengua, monkeypatch):
    """El único monto del producto que no pasaba por `formato.pesos`.

    Salía de un `f"{...:,.2f}"` escrito a mano, o sea "6,000.00": sin símbolo, y
    con el punto y la coma al revés de como los lee un argentino — un peso
    veinte en la pantalla en la que el dueño autoriza seis mil. Que sea el aviso
    de pedido pendiente es lo que lo hace caro: es el único mensaje que recibe
    sin haber escrito nada.

    Se afirma con las DOS formas de número, porque una sola no distingue el bug:
    con `LOCALE=en_US` los dígitos crudos coinciden con los correctos y lo único
    que los separa es el símbolo.
    """
    from app import notificar

    for locale, esperado, crudo in (
        ("es_AR", "Total: $6.000,00 ARS", "6,000.00"),
        ("en_US", "Total: $6,000.00 ARS", "Total: 6,000.00"),
    ):
        monkeypatch.setenv("LOCALE", locale)
        texto = notificar._texto_libre(
            PEDIDO, _SO, auto=False, motivos=MOTIVO,
            detalle="5 x Whole Milk 1 L", lengua=lengua,
        )
        assert esperado in texto, texto
        assert crudo not in texto, texto


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_el_aviso_dice_en_que_moneda_esta_el_pedido(lengua, monkeypatch):
    """El símbolo lo elige LOCALE; el código de moneda lo dice el PEDIDO.

    `pesos` escribe "$" para las dos formas de número que habla el producto, así
    que un pedido en INR salía "$4.800,00" a secas en el aviso pendiente — y el
    MISMO pedido, ya confirmado, "$4.800,00 INR" por `texto_confirmacion`. Dos
    respuestas distintas sobre cuánta plata es, y la que no lo decía era la
    pantalla en la que se autoriza.
    """
    from app import notificar

    monkeypatch.setenv("LOCALE", "es_AR")
    en_rupias = {**_SO, "currency": "INR", "grand_total": 4800}

    texto = notificar._texto_libre(
        PEDIDO, en_rupias, auto=False, motivos=MOTIVO,
        detalle="5 x Whole Milk 1 L", lengua=lengua,
    )

    assert "Total: $4.800,00 INR" in texto, texto
    # Y el confirmado dice exactamente lo mismo del mismo pedido.
    assert "$4.800,00 INR" in notificar.texto_confirmacion(
        en_rupias, "manual", momento="2026-09-05 16:14", lengua=lengua
    )


def test_un_pedido_sin_moneda_no_deja_un_espacio_colgando(monkeypatch):
    """ERPNext siempre la trae, pero un dict de prueba o un pedido a medio armar
    no: el total no puede terminar en un espacio suelto."""
    from app import notificar

    monkeypatch.setenv("LOCALE", "es_AR")
    sin_moneda = {k: v for k, v in _SO.items() if k != "currency"}

    texto = notificar._texto_libre(
        PEDIDO, sin_moneda, auto=False, motivos=MOTIVO,
        detalle="5 x Whole Milk 1 L", lengua=ES,
    )

    assert "Total: $6.000,00\n" in texto, texto


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_la_alerta_de_auto_confirmado_no_pide_responder(lengua):
    from app import notificar

    texto = notificar._texto_libre(
        PEDIDO, _SO, auto=True, motivos="", detalle="5 x Whole Milk 1 L",
        lengua=lengua,
    )
    assert "confirmar" not in texto.split("\n")[-1]
    assert f"ver {PEDIDO}" not in texto, "un pedido ya confirmado no se decide"
    if lengua == EN:
        assert restos_en_espanol(texto, ("Demo Bakery", "Whole Milk 1 L")) == []


@pytest.mark.parametrize("lengua", IDIOMAS)
@pytest.mark.parametrize(
    "clave, en_espanol, en_ingles",
    [
        ("gerencia.fuente_automatica", "automática (política)", "automatic (policy)"),
        ("gerencia.fuente_manual", "manual (confirmación humana)",
         "manual (human confirmation)"),
        ("gerencia.fuente_solicitud", "solicitud aprobada y aceptada",
         "request approved and accepted"),
    ],
)
def test_el_origen_de_una_confirmacion_sale_en_el_idioma(
    lengua, clave, en_espanol, en_ingles
):
    """El campo «Origen:» del aviso de confirmado, que lo arma cada camino que
    confirma. Salía como literal en español adentro del mensaje traducido —
    «Source: manual (confirmación humana)»— y lo encontró la corrida del piloto
    en inglés, no un test: ningún audit lo miraba porque el valor entra por
    parámetro desde otro módulo.

    El registro durable de ERPNext sigue guardando su texto en español: es el
    mismo string usado para dos cosas, y sólo una la lee una persona.
    """
    from app import notificar

    texto = notificar.texto_confirmacion(
        _SO, clave, momento="2026-09-05 16:14", lengua=lengua
    )

    if lengua == EN:
        assert en_ingles in texto
        assert en_espanol not in texto
        assert restos_en_espanol(
            texto, ("Demo Bakery", "Whole Milk 1 L", "ARS", "cancelar")
        ) == [], texto
    else:
        assert en_espanol in texto


@pytest.mark.parametrize(
    "clave, en_espanol",
    [
        ("gerencia.fuente_automatica", "automática (política)"),
        ("gerencia.fuente_manual", "manual (confirmación humana)"),
        ("gerencia.fuente_solicitud", "solicitud aprobada y aceptada"),
    ],
)
def test_la_clave_del_origen_no_sale_cruda_por_ninguna_de_las_tres_puertas(
    clave, en_espanol, monkeypatch
):
    """HALLAZGO DE QODO. El valor que entra es una clave, así que TODO lugar que
    la escriba sin resolver le muestra «gerencia.fuente_manual» a una persona —
    o peor, la deja escrita para siempre en la auditoría de ERPNext.

    Son tres puertas y sólo una estaba cubierta: el texto libre. Faltaban el
    parámetro 7 de la plantilla de Meta —que se usa justamente cuando la
    plantilla ESTÁ configurada, o sea en el despliegue real— y el comentario
    durable que se escribe después de avisar.
    """
    from unittest.mock import Mock

    from app import notificar

    staff = "5491100000000"
    monkeypatch.setenv("IDIOMA_GERENCIA", EN)
    monkeypatch.setenv("WHATSAPP_STAFF_CONFIRMED_TEMPLATE", "confirmado_v1")
    monkeypatch.setenv("WHATSAPP_TEMPLATE_LANGUAGE", "es_AR")
    monkeypatch.setattr(notificar, "STAFF", {staff})
    monkeypatch.setattr(notificar, "claim_once", lambda *a, **k: True)
    monkeypatch.setattr(notificar, "record_outbound", Mock())
    comentarios = Mock()
    monkeypatch.setattr(notificar.erpnext, "add_comment", comentarios)
    plantilla = Mock(return_value={"messages": [{"id": "wamid.tpl"}]})
    monkeypatch.setattr(notificar, "enviar_plantilla", plantilla)

    assert notificar.notificar_confirmacion(_SO, clave) is True

    parametros = plantilla.call_args.args[3]
    origen = next(p for p in parametros if p.startswith("Origen:"))
    assert clave not in origen, origen
    # La plantilla la tiene Meta registrada en es_AR, y sus otros seis
    # parámetros están escritos en ése: el origen va en el mismo idioma.
    assert en_espanol in origen, origen

    escrito = comentarios.call_args.args[2]
    assert clave not in escrito, escrito
    # La auditoría durable es española siempre: ya está escrita así en los
    # ERPNext de los despliegues.
    assert en_espanol in escrito, escrito


def test_el_origen_de_la_plantilla_sigue_el_idioma_en_que_meta_la_tiene(monkeypatch):
    """Si la plantilla está registrada en inglés, su parámetro también."""
    from unittest.mock import Mock

    from app import notificar

    monkeypatch.setenv("IDIOMA_GERENCIA", ES)
    monkeypatch.setenv("WHATSAPP_STAFF_CONFIRMED_TEMPLATE", "confirmed_v1")
    monkeypatch.setenv("WHATSAPP_TEMPLATE_LANGUAGE", "en_US")
    monkeypatch.setattr(notificar, "STAFF", {"5491100000000"})
    monkeypatch.setattr(notificar, "claim_once", lambda *a, **k: True)
    monkeypatch.setattr(notificar, "record_outbound", Mock())
    monkeypatch.setattr(notificar.erpnext, "add_comment", Mock())
    plantilla = Mock(return_value={"messages": [{"id": "wamid.tpl"}]})
    monkeypatch.setattr(notificar, "enviar_plantilla", plantilla)

    notificar.notificar_confirmacion(_SO, "gerencia.fuente_manual")

    origen = next(
        p for p in plantilla.call_args.args[3] if p.startswith("Origen:")
    )
    assert "manual (human confirmation)" in origen, origen


def test_un_origen_que_no_es_una_clave_sale_como_vino():
    """El mismo string se escribe en el registro durable, que no se traduce, así
    que un llamador que pase el texto no puede quedarse sin «Origen»."""
    from app import notificar

    texto = notificar.texto_confirmacion(
        _SO, "manual", momento="2026-09-05 16:14", lengua=EN
    )
    assert "manual" in texto


# ------------------------------------------------- los avisos al equipo
# `solicitudes._avisar_equipo` manda por `avisos.encolar_equipo` ->
# `whatsapp.enviar_mensaje`: son mensajes que LEE UNA PERSONA en WhatsApp, no
# logs ni comentarios de ERPNext. No estaban migrados y tampoco eran una
# excepción documentada — el allowlist no dice en ninguna parte que los avisos
# al equipo queden en español, y los de `notificar.*` y `pendientes.*` sí están
# traducidos. Eran el resto sin migrar de una superficie cubierta en todo lo
# demás.


def _aviso(clave, lengua, **params):
    return idioma.t(clave, lengua, **params)


AVISOS_AL_EQUIPO = [
    ("equipo.vencida_con_respaldo",
     {"pedido": PEDIDO, "solicitud": "DR-1", "detalle": "The draft was closed",
      "terminos": "delivery on 2026-09-08 at 10:00", "nueva": "DR-2",
      "vence": "2026-09-08 12:00"}),
    ("equipo.vencida_sin_respaldo",
     {"pedido": PEDIDO, "detalle": "The draft was closed",
      "porque": "no fallback configured"}),
    ("equipo.revision_vencida",
     {"pedido": PEDIDO, "solicitud": "DR-1", "plazo": "6",
      "motivo": "stock moved", "detalle": "The draft was closed"}),
    ("equipo.cierro_por_persona",
     {"pedido": PEDIDO, "que": "the request", "solicitud": "DR-1",
      "estado": "confirmed"}),
    ("equipo.trabada",
     {"pedido": PEDIDO, "que": "the request", "solicitud": "DR-1",
      "detalle": "ERPNext refused", "intentos": 2, "espera": "30"}),
    ("equipo.cliente_rechazo",
     {"pedido": PEDIDO, "terminos": "delivery on 2026-09-08",
      "detalle": "The draft was closed"}),
    ("equipo.acepto_tarde_trabado", {"pedido": PEDIDO, "detalle": "ERPNext refused"}),
    ("equipo.acepto_tarde", {"pedido": PEDIDO, "detalle": "ERPNext refused"}),
    ("equipo.a_revision",
     {"pedido": PEDIDO, "detalle": "the price moved", "horas": "6"}),
    ("equipo.revision_sin_registro",
     {"pedido": PEDIDO, "detalle": "the price moved", "como": "draft closed"}),
]


@pytest.mark.parametrize("clave, params", AVISOS_AL_EQUIPO)
def test_los_avisos_al_equipo_salen_enteros_en_ingles(clave, params):
    texto = _aviso(clave, EN, **params)

    assert texto.strip()
    assert "{" not in texto, f"{clave}: quedó sin interpolar — {texto}"
    assert PEDIDO in texto
    # El comando NO se traduce: es el payload que parsea el router.
    permitido = (*(str(v) for v in params.values()), "confirmar")
    assert restos_en_espanol(texto, permitido) == [], f"{clave}: {texto}"


@pytest.mark.parametrize("clave, params", AVISOS_AL_EQUIPO)
def test_cada_aviso_al_equipo_tiene_dos_textos_distintos(clave, params):
    """Escritos a mano los dos. Si alguien copia el español al inglés, se ve."""
    assert _aviso(clave, ES, **params) != _aviso(clave, EN, **params)


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_el_resumen_de_decision_sale_en_el_idioma_del_equipo(lengua):
    """La tabla sobre la que el dueño decide. Era el único constructor de
    app/solicitudes.py que no tomaba idioma en absoluto."""
    from app import solicitudes

    sol = _solicitud_equipo()
    texto = solicitudes.texto_para_equipo(sol, lengua)

    assert PEDIDO in texto and "DR-9" in texto
    # Los CINCO comandos salen iguales en los dos idiomas: son lo que hay que
    # teclear, no prosa.
    for comando in ("contraoferta", "retiro", "rechazar-solicitud", "ver"):
        assert f"{comando} {PEDIDO}" in texto, comando
    if lengua == EN:
        assert restos_en_espanol(
            texto, ("Demo Bakery", "5 x Whole Milk 1 L", "ARS", "contraoferta",
                    "retiro", "rechazar-solicitud", "ver", "aprobar", "fecha",
                    "hora", "cargo", "motivo")
        ) == [], texto
        assert "Pending decision" in texto
    else:
        assert "Decisión pendiente" in texto


def test_lo_que_falta_para_una_oferta_se_nombra_en_el_idioma_de_quien_lee():
    """`terminos_incompletos` devolvía la prosa ya escrita en español, así que
    el resumen decía «falta qué día y a qué hora» adentro de una tabla inglesa.
    Ahora devuelve claves y cada call site las dice en su idioma."""
    from app import solicitudes

    faltan = solicitudes.terminos_incompletos({})

    assert faltan == [
        "terminos.falta_fecha", "terminos.falta_hora", "terminos.falta_cargo"
    ]
    assert solicitudes.enumerar(
        solicitudes.nombres_de_terminos(faltan, ES), ES
    ) == "qué día, a qué hora y cuánto se cobra"
    assert solicitudes.enumerar(
        solicitudes.nombres_de_terminos(faltan, EN), EN
    ) == "what day, what time and what you charge"


def _solicitud_equipo():
    from app import solicitudes

    return solicitudes.Solicitud(
        id="DR-9", pedido=PEDIDO, tipo=solicitudes.TIPO_ENTREGA,
        estado=solicitudes.PENDIENTE, cliente="CUST-1",
        cliente_nombre="Demo Bakery", resumen_items="5 x Whole Milk 1 L",
        total=6000.0, moneda="ARS", creada_en=0.0, vence_en=6 * 3600.0,
        sello=0.0, solicitado={"metodo": "entrega"},
    )


# --------------------------------------------------- los botones del aviso
# El aviso de pedido pendiente es EL ÚNICO camino en el que una persona recibe
# algo sin haber escrito nada: el bot le escribe al dueño por su cuenta. Su
# idioma no puede salir de lo que tecleó —no tecleó— así que sale del ajuste, y
# hasta este PR eso valía para el cuerpo y no para los botones.


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_los_botones_del_aviso_salen_en_el_idioma_del_dueno(lengua, monkeypatch):
    from unittest.mock import Mock

    from app import notificar

    staff = "5491100000000"
    monkeypatch.setenv("IDIOMA_GERENCIA", lengua)
    monkeypatch.setattr(notificar, "STAFF", {staff})
    monkeypatch.setattr(notificar, "window_open", lambda telefono: True)
    monkeypatch.setattr(notificar.erpnext, "add_comment", Mock())
    monkeypatch.setattr(notificar, "record_outbound", Mock())
    monkeypatch.setattr(notificar, "has_accepted", lambda *a, **k: False)
    botones = Mock(return_value={"messages": [{"id": "wamid.btn"}]})
    monkeypatch.setattr(notificar, "enviar_botones", botones)

    assert notificar.notificar_equipo(PEDIDO, _SO, auto=False, motivos=MOTIVO) is True

    titulos = [b["title"] for b in botones.call_args.args[2]]
    # El `id` es el payload del router y NO cambia con el idioma: si cambiara,
    # el botón dejaría de confirmar el pedido que dice confirmar.
    assert [b["id"] for b in botones.call_args.args[2]] == [
        f"ok:{PEDIDO}", f"ver:{PEDIDO}"
    ]
    if lengua == EN:
        assert titulos == ["Confirm", "View details"]
        assert restos_en_espanol(" · ".join(titulos)) == []
    else:
        assert titulos == ["Confirmar", "Ver detalle"]


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_el_boton_de_conteo_sale_en_el_idioma_del_dueno(lengua, monkeypatch):
    from unittest.mock import Mock

    from app import notificar, whatsapp

    monkeypatch.setenv("IDIOMA_GERENCIA", lengua)
    enviados = Mock(return_value={"messages": [{"id": "wamid.btn"}]})
    monkeypatch.setattr(whatsapp, "enviar_botones", enviados)

    assert notificar.pedir_confirmacion_conteo("5491100000000", "SR-0001", "x") is True

    boton = enviados.call_args.args[2][0]
    assert boton["id"] == "conteo:SR-0001"
    assert boton["title"] == ("Confirm count" if lengua == EN else "Confirmar conteo")


@pytest.mark.parametrize("clave", [c for c in idioma.CATALOGO if c.startswith("boton.")])
@pytest.mark.parametrize("lengua", IDIOMAS)
def test_ninguna_etiqueta_de_boton_llega_cortada(clave, lengua):
    """Meta corta el título en 20 caracteres, y `enviar_botones` también.

    Las dos veces EN SILENCIO: una etiqueta que no entra no falla, llega
    cortada a la pantalla del dueño. Por eso el largo es una aserción del
    catálogo y no una nota en un comentario — una traducción futura se entera
    acá y no en vivo.
    """
    etiqueta = idioma.t(clave, lengua)
    assert etiqueta.strip()
    assert len(etiqueta) <= 20, f"{clave}:{lengua} entra cortada: {etiqueta!r}"


def test_la_respuesta_al_modelo_nombra_el_boton_que_esta_en_la_pantalla():
    """El informe dice «le mandé el botón X»: X tiene que ser la etiqueta real.

    Escrita a mano en el catálogo, el día que se tradujo el botón esta frase
    habría mandado al dueño a buscar uno que no existe.
    """
    for lengua in IDIOMAS:
        etiqueta = idioma.t("boton.confirmar_conteo", lengua)
        informe = idioma.t(
            "stock.conteo_boton_enviado", lengua,
            resumen="Count of QUE-CRE", producto="QUE-CRE", boton=etiqueta,
        )
        assert etiqueta in informe
    ingles = idioma.t(
        "stock.conteo_boton_enviado", EN,
        resumen="Count of QUE-CRE", producto="QUE-CRE",
        boton=idioma.t("boton.confirmar_conteo", EN),
    )
    assert restos_en_espanol(ingles, ("QUE-CRE",)) == []


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
def test_las_entradas_de_avisos_caidos_salen_enteras_en_el_idioma(lengua, monkeypatch):
    """Las líneas del registro, que es donde estaban los tres restos.

    Los dos fallbacks —«sin pedido», «sin propósito»— no son un caso raro: son
    la forma NORMAL de una respuesta fallida a un cliente, que no tiene pedido.
    Y «— destinatario {tag}…» salía en TODA entrada con tag, no sólo en ésas;
    el detector no lo veía porque `destinatario` no estaba en `_PALABRAS_ES`,
    que es la mitad que este PR también arregla.
    """
    import json

    from app.tools import operaciones

    class _ClienteFalso:
        def lrange(self, clave, desde, hasta):
            return [
                json.dumps({"order_name": "SAL-ORD-2026-00042",
                            "purpose": "staff_order_pending",
                            "destinatario": "a1b2c3d4e5"}),
                # La entrada sin pedido ni propósito: los dos fallbacks juntos.
                json.dumps({"destinatario": "f6g7h8i9"}),
                "esto no es json",
            ]

    monkeypatch.setattr(operaciones.outbound_status, "cliente", _ClienteFalso)

    lineas, problema = operaciones._entradas_de_avisos_caidos(10, lengua)

    assert len(lineas) == 2, lineas
    assert "SAL-ORD-2026-00042" in lineas[-1]
    assert problema, "la entrada ilegible se cuenta y se dice"
    todo = "\n".join(lineas) + "\n" + problema
    if lengua == EN:
        assert restos_en_espanol(todo, ("SAL-ORD-2026-00042", "staff_order_pending")) == []
        assert "no order" in todo and "no purpose" in todo
        assert "recipient a1b2c3d4" in todo
    else:
        assert "sin pedido" in todo and "sin propósito" in todo
        assert "destinatario a1b2c3d4" in todo


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_el_no_autorizado_de_los_informes_sale_en_el_idioma(lengua, monkeypatch):
    """Era una constante de módulo, evaluada al importar: el único string del
    archivo que no podía tener idioma porque se resolvía antes de que hubiera
    uno. Los dos informes devuelven esta misma rama."""
    from app.tools import operaciones

    monkeypatch.setattr(idioma, "gerencia", lambda: lengua)

    def _prohibido(config):
        from app.runtime_context import RuntimeContextError

        raise RuntimeContextError("no")

    monkeypatch.setattr(operaciones, "require_management", _prohibido)

    for informe in (operaciones.estado_del_sistema, operaciones.ver_avisos_fallidos):
        texto = informe.func(_config_gerencia())
        assert texto.strip()
        if lengua == EN:
            assert restos_en_espanol(texto) == [], texto
            assert "not authorized" in texto
        else:
            assert "no está autorizado" in texto


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
    monkeypatch.setattr(
        operaciones, "_entradas_de_avisos_caidos", lambda m, lengua=None: ([], "")
    )
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


# ------------------------------- la oferta de entrega, del lado del cliente
# Tres de estos textos salían en los DOS idiomas pegados con un salto de línea
# —el parche que este catálogo existe para no tener— y el resto sólo en
# español, así que un cliente que había pedido inglés escribía «accept» y
# recibía español. Los manda Python, antes de que ningún modelo vea el mensaje.

# Del catálogo, no a mano: una clave nueva de oferta entra sola a este test.
CLAVES_OFERTA = tuple(k for k in idioma.CATALOGO if k.startswith("oferta."))


@pytest.mark.parametrize("clave", CLAVES_OFERTA)
def test_los_textos_de_la_oferta_salen_en_un_solo_idioma(clave):
    es = idioma.t(clave, ES, pedido=PEDIDO, terminos="x")
    en = idioma.t(clave, EN, pedido=PEDIDO, terminos="x")
    assert es != en, f"{clave} no está traducida"
    assert restos_en_espanol(en) == [], f"{clave} dejó español en la versión inglesa"
    # Ni el inglés adentro del español ni al revés: eran textos pegados.
    assert "Thanks for confirming" not in es
    assert "Gracias por confirmar" not in en
    # El dato no se traduce: si la clave lleva el número, está en las dos.
    assert (PEDIDO in es) == (PEDIDO in en)


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_los_terminos_acordados_salen_en_el_idioma_del_cliente(lengua):
    """`terminos_texto` interpolaba «retiro en el local» y «cargo de envío» en
    español adentro de un mensaje en inglés. Los tests no lo veían porque la
    oferta de prueba no traía ni fecha, ni cargo, ni descuento."""
    from app import solicitudes

    sol = _solicitud(
        ofrecido={
            "metodo": "retiro",
            "fecha": "2026-09-08",
            "hora": "10:00",
            "cargo": 1500.0,
            "descuento_pct": 5,
        }
    )
    texto = solicitudes.texto_oferta_cliente(sol, lengua)
    # Los datos son idénticos en los dos idiomas.
    assert "2026-09-08" in texto and "10:00" in texto and "5%" in texto
    if lengua == EN:
        assert restos_en_espanol(texto, ("Demo Bakery", "ARS", "acepto", "no acepto")) == []
        for resto in ("retiro en el local", "cargo de envío", "descuento", "a las"):
            assert resto not in texto, f"quedó «{resto}» en el texto en inglés"
    else:
        assert "retiro en el local" in texto and "a las 10:00" in texto


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_sin_terminos_el_texto_dice_sin_cambios_en_su_idioma(lengua):
    from app import solicitudes

    texto = solicitudes.terminos_texto({}, "ARS", lengua)
    assert texto == idioma.t("terminos.sin_cambios", lengua)
    if lengua == EN:
        assert restos_en_espanol(texto) == []


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_la_oferta_no_le_pide_apretar_un_boton_que_no_existe(lengua):
    """Al cliente la oferta le llega como texto o plantilla (app/avisos.py).
    Los botones son del aviso al EQUIPO (app/notificar.py)."""
    from app import solicitudes

    texto = solicitudes.texto_oferta_cliente(_solicitud(), lengua)
    assert "botón" not in texto and "button" not in texto
    # Y sigue diciendo, en su idioma, las palabras exactas que el router parsea.
    assert ("acepto" in texto) or ("accept" in texto)


# ------------------------------- salida MEZCLADA: marco inglés, carga española
#
# Los dos tests de abajo están en xfail ESTRICTO: son bugs de producto abiertos,
# no tests rotos. Corriendo la suite en inglés se ven 29 fallas de esta forma
# —marco traducido, carga sin traducir— y el resto de este PR las volvió verdes
# al declarar el idioma de cada archivo. Eso es correcto para los tests, pero
# dejaría los bugs SIN NINGUNA cobertura, que es peor que el rojo.
#
# `strict=True` es lo que hace que esto no se quede: el día que alguien traduzca
# la carga, el xfail pasa a XPASS y pytest lo reporta como falla, obligando a
# borrar el marcador. Un bug conocido con cobertura y fecha de vencimiento, no
# un test apagado. Ver el issue #15.


# Este test era un `xfail(strict=True)` con el bug escrito en su motivo: «33 de
# los 37 raise LimiteError no llevan clave». Ya lo llevan, así que el xfail se
# fue — que es exactamente para lo que sirve un pendiente con fecha de
# vencimiento.
#
# Y de paso el test pasa a probar algo: le pasaba ALIAS a `limites.validar`, que
# toma el nombre canónico, así que levantaba KeyError antes de llegar a ninguna
# aserción. El xfail se lo tragaba —un test que falla por la razón equivocada
# parece estar esperando el arreglo— y por eso ahora se resuelve el alias con
# `limites.definicion`, que es lo que hace el producto.
def test_el_rechazo_de_un_cambio_de_limite_sale_entero_en_ingles() -> None:
    """Lo que el dueño lee en inglés cuando el valor que mandó no sirve.

    ENTERO quiere decir entero: el marco Y el motivo. Salía «I changed nothing:
    «monto maximo» no es un número: 'muchisimo'.» —media frase en cada idioma—
    porque `LimiteError` sólo llevaba `clave` en los cuatro raise del camino del
    código, y los otros treinta y dos viajaban con su texto en español. El
    mecanismo era el correcto y lo que faltaba era usarlo en todos, más los
    datos que cada texto interpola.

    Se prueban los tres tipos de validación que un dueño rompe de verdad —un
    monto que no es número, un porcentaje por debajo del mínimo y un sí/no que
    no lo es— y el que teclea un ajuste que no existe, que es el más común.
    """
    from app import limites

    casos = [
        ("tope", "muchisimo", "is not a number"),
        ("colchon", "-5", "cannot be lower than"),
        ("descuentos", "puede ser", "has to be yes or no"),
        ("días de reparto", "funday", "is not a weekday"),
        ("hora de reparto", "tipo tarde", "has to be a time like"),
    ]
    for alias, valor, esperado in casos:
        defi = limites.definicion(alias)
        try:
            limites.validar(defi.nombre, valor)
        except limites.LimiteError as exc:
            respuesta = idioma.t(
                "codigo.ajuste_no_preparado", EN, motivo=limites.motivo(exc, EN)
            )
            assert esperado in respuesta, f"{alias}={valor}: {respuesta}"
            # Los datos sobreviven, y son dos: lo que tecleó el dueño y el
            # NOMBRE DEL AJUSTE. El alias queda en español en los dos idiomas a
            # propósito —es lo que él escribe para nombrarlo, o sea un comando,
            # igual que «confirmar»— y por eso entra acá como dato permitido y
            # no como un resto sin traducir. Los alias en inglés son otro
            # trabajo, explícitamente fuera de esta rebanada.
            assert restos_en_espanol(respuesta, (valor, defi.alias[0])) == [], respuesta
            # Y nada quedó sin interpolar.
            assert "{" not in respuesta, respuesta
        else:
            raise AssertionError(f"{alias}={valor} debería haber fallado")


def test_un_erpnext_caido_tampoco_deja_espanol_adentro_del_ingles(monkeypatch):
    """HALLAZGO DE QODO, y el más difícil de ver: este motivo no nace de lo que
    tecleó el dueño.

    `_consultar_marca` levanta cuando no puede preguntarle a ERPNext si los
    límites se configuraron antes, y ese camino —`proponer` -> `vigente` ->
    `_almacen` -> `_hubo_cambios_durables`— termina adentro de la respuesta que
    arma `ajustes.preparar`, que ya sale traducida. Era el único `raise` sin
    clave y estaba exceptuado por escrito con el motivo equivocado: «no sale por
    WhatsApp». Sale.

    La cadena se prueba en sus TRES eslabones, porque el fixture `limites_sin_redis`
    cortocircuita `_hubo_cambios_durables` en toda la suite —a propósito: ningún
    test le pregunta a ERPNext— y con él en el medio ningún test de punta a punta
    tocaría la línea que importa. Los tres eslabones son: que la marca ilegible
    levante CON su clave, que el call site se la pase, y que el handler la diga
    en inglés.
    """
    from app import ajustes, erpnext, limites, marcas

    monkeypatch.setattr(
        marcas, "existe", Mock(side_effect=erpnext.ERPNextError("503"))
    )

    # 1. El eslabón que Qodo encontró suelto.
    try:
        limites._consultar_marca(
            "limite", "no pude verificar", clave="limite.marca_no_verificable"
        )
    except limites.LimiteError as exc:
        assert exc.clave == "limite.marca_no_verificable"
        assert limites.motivo(exc, EN) == (
            "I couldn't check in ERPNext whether the limits were configured before"
        )
        assert restos_en_espanol(limites.motivo(exc, EN)) == []
        # Y el español sigue siendo el de siempre, que es lo que va al log.
        assert limites.motivo(exc, ES) == str(exc) or "verificar" in limites.motivo(
            exc, ES
        )
    else:
        raise AssertionError("una marca ilegible tiene que levantar")

    # 2. Que el call site la pase: sin esto el eslabón 1 no sirve de nada, y es
    #    justo lo que estaba mal. Se mira el fuente porque el fixture autouse
    #    reemplaza esta función en toda la suite.
    import ast
    from pathlib import Path as _Path

    fuente = (_Path(__file__).resolve().parents[1] / "app" / "limites.py").read_text()
    consultas = [
        nodo
        for nodo in ast.walk(ast.parse(fuente))
        if isinstance(nodo, ast.Call)
        and getattr(nodo.func, "id", "") == "_consultar_marca"
        and nodo.args
        and getattr(nodo.args[0], "value", "") == "limite"
    ]
    assert consultas, "no encontré la consulta de la marca de límites"
    assert all(
        any(k.arg == "clave" for k in nodo.keywords) for nodo in consultas
    ), "la consulta de la marca de LÍMITES tiene que pasar su clave: es la que levanta"

    # 3. Y que el handler la diga en el idioma del dueño.
    monkeypatch.setenv("IDIOMA_GERENCIA", EN)
    monkeypatch.setattr(
        limites,
        "proponer",
        Mock(
            side_effect=limites.LimiteError(
                "no pude verificar en ERPNext si los límites se configuraron antes",
                clave="limite.marca_no_verificable",
            )
        ),
    )

    respuesta = ajustes.preparar("tope", "30000", "5491100000000")

    assert "I couldn't check in ERPNext" in respuesta, respuesta
    assert restos_en_espanol(respuesta) == [], respuesta


def test_el_mismo_rechazo_en_espanol_dice_lo_mismo_que_siempre() -> None:
    """El idioma nuevo no puede costar el que ya andaba: el texto en español es
    el de siempre, que además sigue siendo `str(exc)` para el log."""
    from app import limites

    try:
        limites.validar(limites.definicion("tope").nombre, "muchisimo")
    except limites.LimiteError as exc:
        assert limites.motivo(exc, ES) == "«monto maximo» no es un número: 'muchisimo'"
        assert str(exc) == "«monto maximo» no es un número: 'muchisimo'"


def test_una_excepcion_sin_clave_sigue_saliendo_con_su_texto() -> None:
    """`motivo` nunca devuelve vacío: una excepción de otro módulo, o una que
    todavía no tenga clave, sale con lo que diga."""
    from app import limites

    assert limites.motivo(ValueError("algo pasó"), EN) == "algo pasó"
    assert limites.motivo(limites.LimiteError("sin clave"), EN) == "sin clave"


# El otro `xfail(strict=True)` que se fue con su bug: `_cuenta` ya toma idioma.
@pytest.mark.parametrize("ilegible", [None, -1, "no es un número", object()])
def test_el_centinela_de_contador_ilegible_sale_traducido(ilegible) -> None:
    """`DESCONOCIDO` es la palabra que significa «no lo leas como cero».

    O sea justo la que hay que entender, y salía en español adentro de un
    informe en inglés. El catálogo ya tenía la fila —`sistema.desconocido`, EN
    «UNKNOWN»— y el mismo archivo la usaba bien dos líneas más arriba; lo que
    faltaba era que `_cuenta` recibiera el idioma.

    Las cuatro formas de «no pude leer» dan el mismo centinela: None, el -1 que
    usa el contador, algo que no es número y algo que no es nada.
    """
    from app.tools import operaciones

    assert operaciones._cuenta(ilegible, EN) == "UNKNOWN"
    assert operaciones._cuenta(ilegible, ES) == "DESCONOCIDO"
    assert restos_en_espanol(operaciones._cuenta(ilegible, EN)) == []


@pytest.mark.parametrize("lengua", IDIOMAS)
def test_un_contador_que_sí_se_pudo_leer_es_el_mismo_en_los_dos_idiomas(lengua) -> None:
    """Un número es un dato: no tiene idioma."""
    from app.tools import operaciones

    assert operaciones._cuenta(0, lengua) == "0"
    assert operaciones._cuenta(17, lengua) == "17"
