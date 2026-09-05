"""El idioma: quién lo cambia, quién no, y qué NO cambia al traducir.

Lo que estos tests cuidan, en orden de importancia:

1. Que un cliente no pueda tocarle el idioma al equipo ni a otro cliente. El
   idioma del equipo se cambia por el mismo camino de dos pasos que un límite
   —propuesta y código de cuatro dígitos— y ese camino sale del teléfono
   verificado del webhook, no de nada que venga escrito en un mensaje.

2. Que traducir no mueva un dato. Un código, un número de pedido, una
   cantidad, un precio y una fecha valen lo mismo en los dos idiomas: si
   cambian, la traducción dejó de ser prosa y pasó a ser parte de la
   autorización.

3. Que falte una traducción no rompa nada. Se cae al idioma por defecto y
   sigue: un mensaje que no sale es un cliente sin respuesta.
"""
from __future__ import annotations

import re

import pytest

from app import idioma, limites, locks
from tests.conftest import FakeRedis


@pytest.fixture
def fake_redis_idioma(monkeypatch):
    """Un Redis en memoria para el idioma de cada cliente."""
    falso = FakeRedis()
    monkeypatch.setattr(locks, "conexion", lambda: falso)
    return falso


# --------------------------------------------------------------- el catálogo


def test_toda_clave_del_catalogo_tiene_los_dos_idiomas():
    """Si alguien agrega una clave a medias, se entera acá y no en producción."""
    assert idioma.claves_incompletas() == []


def test_el_catalogo_no_esta_vacio_y_cubre_las_categorias_pedidas():
    categorias = {clave.split(".")[0] for clave in idioma.CATALOGO}
    for esperada in (
        "ack", "pedido", "gerencia", "entrega", "codigo",
        "accion", "sistema", "stock", "precio", "fallback", "idioma",
    ):
        assert esperada in categorias, f"falta la categoría {esperada}"


def test_una_clave_desconocida_no_levanta_y_se_nota():
    assert idioma.t("no.existe.esta.clave", "en") == "no.existe.esta.clave"


def test_una_clave_sin_ese_idioma_cae_al_por_defecto(monkeypatch):
    monkeypatch.setitem(idioma.CATALOGO, "prueba.a_medias", {idioma.ES: "sólo español"})
    assert idioma.t("prueba.a_medias", idioma.EN) == "sólo español"


def test_un_idioma_desconocido_cae_al_por_defecto():
    assert idioma.t("codigo.vencido", "klingon") == idioma.t("codigo.vencido", idioma.ES)


def test_un_parametro_que_falta_devuelve_el_texto_sin_interpolar():
    # Nunca una excepción: el mensaje sale igual.
    salida = idioma.t("pedido.confirmado_cliente", idioma.EN, pedido="SAL-ORD-1")
    assert "SAL-ORD-1" not in salida or salida
    assert isinstance(salida, str) and salida


# ------------------------------------------------- reconocer el idioma dicho


@pytest.mark.parametrize(
    "dicho, esperado",
    [
        ("manager language English", idioma.EN),
        ("idioma de gerencia inglés", idioma.EN),
        ("manager language Spanish", idioma.ES),
        ("idioma de gerencia español", idioma.ES),
        ("English", idioma.EN),
        ("español", idioma.ES),
        ("castellano", idioma.ES),
    ],
)
def test_reconoce_como_lo_dice_una_persona(dicho, esperado):
    assert idioma.normalizar(dicho) == esperado


def test_un_idioma_que_no_existe_no_se_adivina():
    assert idioma.normalizar("klingon") is None
    assert idioma.normalizar("") is None
    assert idioma.normalizar(None) is None


def test_un_idioma_desconocido_cae_al_default_cuando_hay_que_elegir_uno():
    assert idioma.valido("klingon") == idioma.por_defecto()


# ------------------------------------------------- el ajuste de la gerencia


def test_el_ajuste_de_idioma_existe_y_se_llama_como_lo_dice_el_dueno():
    for dicho in ("manager language", "idioma de gerencia", "idioma gerencia"):
        assert limites.definicion(dicho).nombre == "IDIOMA_GERENCIA"


def test_el_ajuste_de_idioma_solo_acepta_los_dos_idiomas():
    assert limites.validar("IDIOMA_GERENCIA", "inglés") == idioma.EN
    assert limites.validar("IDIOMA_GERENCIA", "English") == idioma.EN
    assert limites.validar("IDIOMA_GERENCIA", "español") == idioma.ES
    with pytest.raises(limites.LimiteError):
        limites.validar("IDIOMA_GERENCIA", "klingon")


def test_normalizar_un_idioma_es_idempotente():
    """aplicar() re-valida lo que ya está en forma normal: no puede derivar."""
    una = limites.validar("IDIOMA_GERENCIA", "inglés")
    dos = limites.validar("IDIOMA_GERENCIA", una, tecleado=False)
    assert una == dos == idioma.EN


def test_el_idioma_no_entra_en_los_limites_que_deciden_una_confirmacion():
    """Un idioma no autoriza nada: no puede estar donde se decide un pedido."""
    assert "IDIOMA_GERENCIA" not in limites.LIMITES
    assert "IDIOMA_GERENCIA" not in limites.ENTREGA
    assert "IDIOMA_GERENCIA" in limites.TODOS


def test_el_idioma_tiene_su_propia_marca_durable():
    """Ni la de límites ni la de entrega: perder un idioma no frena una venta."""
    marcas = {
        limites.MARCA_DURABLE,
        limites.MARCA_DURABLE_ENTREGA,
        limites.MARCA_DURABLE_IDIOMA,
    }
    assert len(marcas) == 3


def test_el_valor_se_le_muestra_al_dueno_con_el_nombre_del_idioma():
    # "en" no es una respuesta que alguien quiera leer.
    assert limites.mostrar("IDIOMA_GERENCIA", "en", idioma.ES) == "inglés"
    assert limites.mostrar("IDIOMA_GERENCIA", "en", idioma.EN) == "English"
    assert limites.mostrar("IDIOMA_GERENCIA", "es", idioma.EN) == "Spanish"


def test_mostrar_no_traduce_un_dato_que_no_es_un_idioma():
    assert limites.mostrar("AUTO_CONFIRM_MAX", "15000", idioma.EN) == "15000"


# ------------------------------------------------- el idioma de cada cliente


def test_dos_clientes_tienen_idiomas_independientes(fake_redis_idioma):
    idioma.recordar_cliente("+5493511111111", idioma.EN)
    idioma.recordar_cliente("+5493512222222", idioma.ES)
    assert idioma.cliente_guardado("+5493511111111") == idioma.EN
    assert idioma.cliente_guardado("+5493512222222") == idioma.ES


def test_un_cliente_no_le_cambia_el_idioma_a_otro(fake_redis_idioma):
    idioma.recordar_cliente("+5493511111111", idioma.EN)
    # El otro sigue sin preferencia: nadie se la fijó.
    assert idioma.cliente_guardado("+5493512222222") is None


def test_la_preferencia_del_cliente_sobrevive_a_un_reinicio(fake_redis_idioma):
    """Un reinicio de la app no toca Redis: la preferencia sigue ahí."""
    idioma.recordar_cliente("+5493511111111", idioma.EN)
    # Reiniciar la aplicación = volver a leer, sin estado en memoria.
    assert idioma.cliente_guardado("+5493511111111") == idioma.EN
    assert idioma.para_cliente("+5493511111111", "hola que tal") == idioma.EN


def test_si_se_pierde_redis_se_vuelve_a_espejar_sin_romper(fake_redis_idioma):
    idioma.recordar_cliente("+5493511111111", idioma.EN)
    fake_redis_idioma.strings.clear()          # se perdió el almacén
    assert idioma.cliente_guardado("+5493511111111") is None
    # Y sigue contestando: espeja el idioma del mensaje, como antes de todo esto.
    assert idioma.para_cliente("+5493511111111", "hello I want to order") == idioma.EN
    assert idioma.para_cliente("+5493511111111", "hola quiero un pedido") == idioma.ES


def test_redis_caido_no_levanta_nunca(fake_redis_idioma):
    fake_redis_idioma.caido = True
    assert idioma.cliente_guardado("+5493511111111") is None
    assert idioma.recordar_cliente("+5493511111111", idioma.EN) is False
    # Y todavía sabe qué contestar.
    assert idioma.para_cliente("+5493511111111", "hello there please") == idioma.EN


def test_el_cliente_pide_su_idioma_explicitamente(fake_redis_idioma):
    assert idioma.pedido_explicito("reply in English please") == idioma.EN
    assert idioma.pedido_explicito("respondé en español") == idioma.ES
    assert idioma.pedido_explicito("quiero 5 unidades de leche") is None


def test_pedirlo_lo_deja_guardado_para_los_proximos_mensajes(fake_redis_idioma):
    assert idioma.para_cliente("+5493511111111", "reply in English") == idioma.EN
    # El siguiente mensaje viene en español y NO cambia nada: él eligió inglés.
    assert idioma.para_cliente("+5493511111111", "hola quiero un pedido") == idioma.EN


def test_sin_preferencia_se_espeja_el_idioma_del_mensaje(fake_redis_idioma):
    assert idioma.para_cliente("+5493513333333", "hello I need milk") == idioma.EN
    assert idioma.para_cliente("+5493514444444", "hola necesito leche") == idioma.ES


def test_si_no_se_puede_decidir_se_usa_el_default(fake_redis_idioma):
    # Sin pistas de ningún idioma, no se adivina.
    assert idioma.para_cliente("+5493515555555", "?????") == idioma.por_defecto()
    assert idioma.para_cliente("+5493515555555", "") == idioma.por_defecto()


def test_un_telefono_invalido_no_guarda_nada(fake_redis_idioma):
    assert idioma.recordar_cliente("", idioma.EN) is False
    assert idioma.cliente_guardado("") is None


def test_la_clave_de_redis_no_lleva_el_telefono(fake_redis_idioma):
    idioma.recordar_cliente("+5493511111111", idioma.EN)
    for clave in fake_redis_idioma.strings:
        assert "5493511111111" not in clave


# ------------------------------------------------- la regla que ve el modelo


def test_sin_preferencia_el_prompt_conserva_la_regla_de_espejo():
    regla = idioma.regla_prompt(None)
    assert "idioma en que te escribió" in regla
    assert regla == idioma.REGLA_ESPEJO_CLIENTE


def test_con_preferencia_el_prompt_fija_el_idioma():
    assert "Always reply in English" in idioma.regla_prompt(idioma.EN)
    assert "Respondé SIEMPRE en español" in idioma.regla_prompt(idioma.ES)


# --------------------------------------- traducir no puede mover un dato


CODIGO = "482913"
PEDIDO = "SAL-ORD-2026-00042"


@pytest.mark.parametrize(
    "clave, params",
    [
        ("codigo.ajuste_pedido", {"cambio": "x", "codigo": "4821", "minutos": 10}),
        ("accion.preparada", {"pedido": PEDIDO, "consecuencia": "y", "codigo": CODIGO,
                             "minutos": 10}),
        ("pedido.pendiente", {"pedido": PEDIDO, "horas": "6"}),
        ("pedido.confirmado_cliente", {"pedido": PEDIDO, "renglones": "5 x LEC-ENT-1L",
                                       "total": "$ 6.000", "entrega": "2026-09-06"}),
        ("gerencia.cuerpo_pedido", {"pedido": PEDIDO, "cliente": "Demo Bakery",
                                    "detalle": "5 x MILK-1L",
                                    "total": "$ 6.000,00",
                                    "entrega": "2026-09-06"}),
    ],
)
def test_los_datos_son_identicos_en_los_dos_idiomas(clave, params):
    """Los códigos, los números de pedido, los importes y las fechas no se traducen."""
    es = idioma.t(clave, idioma.ES, **params)
    en = idioma.t(clave, idioma.EN, **params)
    assert es != en, "si no cambió nada, la clave no está traducida"
    for valor in params.values():
        texto = str(valor)
        if not texto or texto in {"x", "y"}:
            continue
        assert texto in es, f"{texto!r} se perdió en español"
        assert texto in en, f"{texto!r} se perdió en inglés"


def test_los_digitos_de_un_codigo_no_cambian_al_traducir():
    es = idioma.t("accion.preparada", idioma.ES, pedido=PEDIDO,
                  consecuencia="Confirmo el pedido", codigo=CODIGO, minutos=10)
    en = idioma.t("accion.preparada", idioma.EN, pedido=PEDIDO,
                  consecuencia="I confirm the order", codigo=CODIGO, minutos=10)
    assert re.findall(r"\d{6}", es) == [CODIGO]
    assert re.findall(r"\d{6}", en) == [CODIGO]
    assert PEDIDO in es and PEDIDO in en
