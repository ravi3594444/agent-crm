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

from app import aprobacion, whatsapp  # noqa: E402
from app.formato import cantidad, pesos, whatsapp_texto  # noqa: E402


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
