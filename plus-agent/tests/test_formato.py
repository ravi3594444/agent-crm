"""Cómo se ven la plata y el texto en la pantalla del dueño y del cliente.

Un argentino lee `$98,000` como noventa y ocho pesos: acá los miles van con
punto y los decimales con coma. Y WhatsApp no entiende Markdown: `**x**` se
muestra con los asteriscos a la vista. Estos tests fijan ambas cosas.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import aprobacion, whatsapp
from app.formato import cantidad, locale_configurado, pesos, whatsapp_texto

# Este archivo afirma montos con forma argentina, así que la declara en vez de
# heredarla del entorno. Los tests de `en_US` fijan el suyo en la llamada. Ver
# `_locale_declarado` en tests/conftest.py.
pytestmark = pytest.mark.locale("es_AR")

# --- pesos ------------------------------------------------------------------


def test_pesos_usa_punto_para_miles() -> None:
    assert pesos(12000) == "$12.000"
    assert pesos(98000) == "$98.000"
    assert pesos(1234567) == "$1.234.567"


def test_pesos_con_decimales_usa_coma() -> None:
    assert pesos(1500.5, 2) == "$1.500,50"
    assert pesos(0.5, 2) == "$0,50"


def test_pesos_montos_chicos_sin_separador() -> None:
    assert pesos(0) == "$0"
    assert pesos(999) == "$999"
    assert pesos(1000) == "$1.000"


def test_pesos_negativos() -> None:
    assert pesos(-12000) == "-$12.000"
    assert pesos(-1500.5, 2) == "-$1.500,50"


def test_pesos_none_y_basura_no_explotan() -> None:
    assert pesos(None) == "$0"
    assert pesos("") == "$0"
    assert pesos("abc") == "$0"
    assert pesos("12000") == "$12.000"
    assert pesos(object()) == "$0"


def test_pesos_redondea_a_entero_por_defecto() -> None:
    assert pesos(12000.4) == "$12.000"
    assert pesos(12000.6) == "$12.001"


# LA MESA DE FIJACIÓN. Cada fila es UNA salida que hoy ve una persona en su
# pantalla, escrita como literal y no como cuenta: es lo que hace que un cambio
# de implementación sea un cambio de implementación y no un cambio de producto.
#
# Existe porque `pesos` está por dejar de dar vuelta los separadores a mano y
# pasar a `babel.numbers.format_currency`. Un test que dice «los miles van con
# punto» no alcanza para eso: la diferencia entre las dos implementaciones no es
# el separador, es el redondeo del medio, el cero negativo y el `$` pegado o
# separado del número. Por eso las filas se escribieron ANTES de tocar la
# implementación, corriendo la vieja: si alguna se mueve al cambiar el motor,
# se movió algo que el dueño lee.
#
# Los montos no son inventados: son los que ya afirman los otros tests y los
# que arma el seed (tests/test_autonomia.py, tests/test_etapa_2e.py,
# tests/test_demo.py), más los bordes que ninguno tenía escritos — el medio
# exacto, el negativo que redondea a cero y la basura.
FIJADOS_ES_AR = [
    # (monto, decimales, lo que se ve)
    (12000, 0, "$12.000"),
    (98000, 0, "$98.000"),
    (1234567, 0, "$1.234.567"),
    (1000, 0, "$1.000"),
    (999, 0, "$999"),
    (200, 0, "$200"),
    (50, 0, "$50"),
    (19, 0, "$19"),
    (4, 0, "$4"),
    (1, 0, "$1"),
    (0, 0, "$0"),
    (9000, 0, "$9.000"),
    (85000, 0, "$85.000"),
    (8450, 0, "$8.450"),
    (5000, 0, "$5.000"),
    (30000, 0, "$30.000"),
    (13000, 0, "$13.000"),
    # Con decimales: el separador decimal es la coma.
    (1500.5, 2, "$1.500,50"),
    (1200.5, 2, "$1.200,50"),
    (4800, 2, "$4.800,00"),
    (1234567.89, 2, "$1.234.567,89"),
    (0.5, 2, "$0,50"),
    (0, 2, "$0,00"),
    # Negativos: el signo va ANTES del símbolo.
    (-12000, 0, "-$12.000"),
    (-1500.5, 2, "-$1.500,50"),
    (-0.5, 2, "-$0,50"),
    # Redondeo. `12000,5` a cero decimales da $12.000 y no $12.001: es el
    # redondeo al par de Python, y queda fijado acá porque un motor nuevo bien
    # puede redondear el medio para arriba.
    (12000.4, 0, "$12.000"),
    (12000.5, 0, "$12.000"),
    (12000.6, 0, "$12.001"),
    (12001.5, 0, "$12.002"),
    (1200.5, 0, "$1.200"),
    # Un negativo que redondea a cero sigue mostrando el signo. Es feo y es lo
    # que hay hoy: si un día se decide que no, se decide acá y a propósito.
    (0.4, 0, "$0"),
    (-0.4, 0, "-$0"),
    (-0.6, 0, "-$1"),
    # Lo que no es un número no rompe un mensaje: vale cero.
    (None, 0, "$0"),
    ("", 0, "$0"),
    ("abc", 0, "$0"),
    ("12000", 0, "$12.000"),
]


@pytest.mark.parametrize("monto, decimales, esperado", FIJADOS_ES_AR)
def test_pesos_fijado_byte_a_byte(monto, decimales, esperado) -> None:
    """La salida de HOY, byte a byte, para cada monto que alguien ya lee."""
    assert pesos(monto, decimales) == esperado


def test_pesos_objeto_cualquiera_vale_cero() -> None:
    """Fuera de la mesa porque `object()` no se puede escribir como literal."""
    assert pesos(object()) == "$0"


# --- pesos en_US ------------------------------------------------------------

# El mismo monto, para la otra persona. `$1.200,50` le dice a un estadounidense
# "un dólar veinte": no es una preferencia de estilo, es otra cifra.
FIJADOS_EN_US = [
    (1200.5, 2, "$1,200.50"),
    (12000, 0, "$12,000"),
    (1234567, 0, "$1,234,567"),
    (1500.5, 2, "$1,500.50"),
    (0.5, 2, "$0.50"),
    (999, 0, "$999"),
    (0, 0, "$0"),
    (-12000, 0, "-$12,000"),
    (-1500.5, 2, "-$1,500.50"),
    (None, 0, "$0"),
    ("abc", 0, "$0"),
]


@pytest.mark.parametrize("monto, decimales, esperado", FIJADOS_EN_US)
def test_pesos_en_us(monkeypatch: pytest.MonkeyPatch, monto, decimales, esperado) -> None:
    monkeypatch.setenv("LOCALE", "en_US")
    assert pesos(monto, decimales) == esperado


def test_el_locale_lo_decide_el_despliegue_y_se_lee_en_cada_llamada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No hay estado de proceso: `babel` toma el locale por llamada.

    Es la diferencia con el módulo `locale` de la stdlib, y es la razón por la
    que esto se puede usar en un servidor con hilos de barrido. Si algún día
    alguien lo cambia por `locale.setlocale`, este test se cae.
    """
    monkeypatch.setenv("LOCALE", "en_US")
    assert pesos(1200.5, 2) == "$1,200.50"
    monkeypatch.setenv("LOCALE", "es_AR")
    assert pesos(1200.5, 2) == "$1.200,50"


@pytest.mark.parametrize(
    "crudo, esperado",
    [
        ("en_US", "en_US"),
        ("en-US", "en_US"),
        ("EN_us", "en_US"),
        ("es_AR", "es_AR"),
        ("", "es_AR"),
        ("   ", "es_AR"),
        # Un locale que existe pero que este producto no habla, y uno que no
        # existe: los dos caen al de por defecto en vez de romper un mensaje.
        ("pt_BR", "es_AR"),
        ("no-es-un-locale", "es_AR"),
    ],
)
def test_locale_configurado_nunca_queda_vacio_ni_levanta(
    monkeypatch: pytest.MonkeyPatch, crudo: str, esperado: str
) -> None:
    monkeypatch.setenv("LOCALE", crudo)
    assert locale_configurado() == esperado


def test_sin_LOCALE_en_el_entorno_es_el_de_por_defecto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un despliegue que ya existe no migra: sin la variable, todo sigue igual."""
    monkeypatch.delenv("LOCALE", raising=False)
    assert locale_configurado() == "es_AR"
    assert pesos(12000) == "$12.000"


def test_un_monto_que_no_es_un_numero_finito_no_llega_a_la_pantalla() -> None:
    """`$inf` en la pantalla en la que alguien aprueba un pedido es peor que $0."""
    assert pesos(float("inf")) == "$0"
    assert pesos(float("nan"), 2) == "$0,00"


def test_cantidad_sin_ceros_y_con_coma_decimal() -> None:
    assert cantidad(10) == "10"
    assert cantidad(10.0) == "10"
    assert cantidad(2.5) == "2,5"
    assert cantidad(None) == "0"
    assert cantidad("x") == "0"


# --- whatsapp_texto ---------------------------------------------------------


def test_whatsapp_texto_respuesta_real_del_modelo() -> None:
    entrada = (
        "- **Número de pedido:** SAL-ORD-2026-00008\n"
        "- **Resumen:** 10 Kg de Queso cremoso"
    )
    salida = whatsapp_texto(entrada)
    assert salida == (
        "• *Número de pedido:* SAL-ORD-2026-00008\n"
        "• *Resumen:* 10 Kg de Queso cremoso"
    )
    assert "**" not in salida
    assert "SAL-ORD-2026-00008" in salida


def test_whatsapp_texto_deja_igual_lo_que_ya_esta_en_formato_whatsapp() -> None:
    ya_bien = (
        "*Pedido SAL-ORD-2026-00008*\n"
        "• 10 Kg de Queso cremoso\n"
        "• 2 Kg de Manteca\n"
        "_Entrega:_ 2026-09-05\n\n"
        "Total *$98.000*. ¿Confirmás?"
    )
    assert whatsapp_texto(ya_bien) == ya_bien


def test_whatsapp_texto_no_toca_texto_plano() -> None:
    plano = "Hola Ravi, tu pedido SAL-ORD-2026-00008 queda para el viernes."
    assert whatsapp_texto(plano) == plano


def test_whatsapp_texto_negrita_y_subrayado_markdown() -> None:
    assert whatsapp_texto("Total: **$98.000**") == "Total: *$98.000*"
    assert whatsapp_texto("__importante__") == "_importante_"
    # Negrita de WhatsApp adentro de una frase con `**` de Markdown al lado.
    assert whatsapp_texto("*ya* y **ahora**") == "*ya* y *ahora*"


def test_whatsapp_texto_asterisco_suelto_no_se_toca() -> None:
    assert whatsapp_texto("2 * 3 = 6") == "2 * 3 = 6"
    assert whatsapp_texto("*solo negrita*") == "*solo negrita*"
    # `**` sin cierre queda tal cual: mejor no adivinar.
    assert whatsapp_texto("** abierto") == "** abierto"


def test_whatsapp_texto_vinetas() -> None:
    assert whatsapp_texto("- uno\n- dos") == "• uno\n• dos"
    assert whatsapp_texto("* uno\n* dos") == "• uno\n• dos"
    assert whatsapp_texto("  - anidado") == "  • anidado"
    # Una línea que arranca con negrita de WhatsApp NO es una viñeta.
    assert whatsapp_texto("*Total:* $12.000") == "*Total:* $12.000"
    # Un número negativo o un guion pegado no es una viñeta.
    assert whatsapp_texto("-5 grados") == "-5 grados"
    # Listas numeradas quedan como están.
    assert whatsapp_texto("1. uno\n2. dos") == "1. uno\n2. dos"


def test_whatsapp_texto_titulos() -> None:
    assert whatsapp_texto("# Resumen\ntexto") == "*Resumen*\ntexto"
    assert whatsapp_texto("### Detalle ###") == "*Detalle*"
    # Título que ya venía en negrita Markdown: no duplicar asteriscos.
    assert whatsapp_texto("## **Resumen**") == "*Resumen*"
    # Un `#` en medio de una frase no es título.
    assert whatsapp_texto("pedido #8 listo") == "pedido #8 listo"
    assert whatsapp_texto("#hashtag") == "#hashtag"


def test_whatsapp_texto_colapsa_lineas_en_blanco() -> None:
    assert whatsapp_texto("a\n\n\n\nb") == "a\n\nb"
    assert whatsapp_texto("a\n\nb") == "a\n\nb"
    assert whatsapp_texto("a\r\nb") == "a\nb"


def test_whatsapp_texto_no_altera_urls() -> None:
    url = "https://erp.example.com/app/sales-order/SAL-ORD-2026-00008?x=__a__&y=**b**"
    assert whatsapp_texto(f"Mirá **acá**: {url}") == f"Mirá *acá*: {url}"
    url2 = "www.example.com/a__b__c"
    assert whatsapp_texto(url2) == url2


def test_whatsapp_texto_no_altera_codigos_con_guiones_bajos() -> None:
    assert whatsapp_texto("ITEM__A__B") == "ITEM__A__B"


def test_whatsapp_texto_none_y_vacio() -> None:
    assert whatsapp_texto(None) == ""
    assert whatsapp_texto("") == ""


# --- integración: la traducción corre en cada salida de texto libre ---------


def test_enviar_mensaje_traduce_markdown_antes_de_armar_el_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = Mock(return_value={"messages": [{"id": "wamid.out"}]})
    monkeypatch.setattr(whatsapp, "_post", post)

    whatsapp.enviar_mensaje("5491100000000", "- **Resumen:** 10 Kg")

    payload = post.call_args.args[0]
    assert payload["type"] == "text"
    assert payload["text"]["body"] == "• *Resumen:* 10 Kg"


def test_enviar_botones_traduce_markdown_en_el_cuerpo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = Mock(return_value={"messages": [{"id": "wamid.out"}]})
    monkeypatch.setattr(whatsapp, "_post", post)

    whatsapp.enviar_botones(
        "5491100000000",
        "**Pedido nuevo**\n- Total: $12.000",
        [{"id": "ok:SAL-ORD-0001", "title": "Confirmar"}],
    )

    payload = post.call_args.args[0]
    assert payload["interactive"]["body"]["text"] == "*Pedido nuevo*\n• Total: $12.000"
    # Los botones no se tocan.
    assert payload["interactive"]["action"]["buttons"][0]["reply"]["id"] == "ok:SAL-ORD-0001"


def test_ver_pedido_muestra_montos_con_miles_argentinos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    monkeypatch.setattr(
        aprobacion,
        "_leer_doc",
        lambda doctype, name: {
            "name": name,
            "docstatus": 0,
            "customer": "CUST-0001",
            "customer_name": "Almacén Don Pepe",
            "delivery_date": "2026-09-05",
            "grand_total": 98000,
            "items": [
                {"qty": 10, "item_name": "Queso cremoso", "amount": 85000},
                {"qty": 2.5, "item_code": "MANTECA", "amount": 13000},
            ],
        },
    )

    result = aprobacion.manejar_boton("ver:SAL-ORD-2026-00008", "5491100000000")

    assert "Total $98.000" in result
    assert "$85.000" in result
    assert "$13.000" in result
    # Miles con punto, nunca con coma.
    assert re.search(r"\$\d{1,3}(\.\d{3})+", result)
    assert not re.search(r"\$\d{1,3}(,\d{3})+", result)
    # Las cantidades siguen siendo cantidades, no plata.
    assert "10 x Queso cremoso" in result
    assert "2.5 x MANTECA" in result
