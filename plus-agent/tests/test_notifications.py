from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest


os.environ.setdefault("ERPNEXT_URL", "http://erpnext.test")
os.environ.setdefault("ERPNEXT_API_KEY", "test-key")
os.environ.setdefault("ERPNEXT_API_SECRET", "test-secret")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test-phone-id")
os.environ.setdefault("WHATSAPP_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import aprobacion, notificar, whatsapp  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "WHATSAPP_STAFF_PENDING_TEMPLATE",
        "WHATSAPP_STAFF_CONFIRMED_TEMPLATE",
        "WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WHATSAPP_TEMPLATE_LANGUAGE", "es_AR")
    monkeypatch.setattr(notificar, "has_accepted", lambda *args: False)
    monkeypatch.setattr(notificar, "record_outbound", Mock())
    monkeypatch.setattr(notificar, "window_open", lambda phone: False)
    monkeypatch.setattr(aprobacion, "has_accepted", lambda *args: False)
    monkeypatch.setattr(aprobacion, "record_outbound", Mock())
    monkeypatch.setattr(aprobacion, "window_open", lambda phone: False)


def test_template_quick_replies_use_approved_template_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = Mock(return_value={"messages": [{"id": "wamid.out"}]})
    monkeypatch.setattr(whatsapp, "_post", post)

    result = whatsapp.enviar_plantilla(
        "5491100000000",
        "pedido_pendiente_equipo",
        "es_AR",
        ["SAL-ORD-0001", "Pendiente"],
        ["ok:SAL-ORD-0001", "ver:SAL-ORD-0001"],
    )

    assert result == {"messages": [{"id": "wamid.out"}]}
    payload = post.call_args.args[0]
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "pedido_pendiente_equipo"
    assert payload["template"]["components"][1:] == [
        {
            "type": "button",
            "sub_type": "quick_reply",
            "index": str(index),
            "parameters": [{"type": "payload", "payload": action}],
        }
        for index, action in enumerate(
            ["ok:SAL-ORD-0001", "ver:SAL-ORD-0001"]
        )
    ]


def test_meta_2xx_without_message_id_is_not_treated_as_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = Mock(status_code=200, headers={})
    response.json.return_value = {}
    client = Mock()
    client.post.return_value = response
    monkeypatch.setattr(whatsapp, "_client", client)

    with pytest.raises(whatsapp.WhatsAppResponseError, match="identificador"):
        whatsapp.enviar_mensaje("5491100000000", "prueba")


def test_pending_staff_alert_fails_closed_without_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notificar, "STAFF", {"5491100000000"})
    send = Mock(side_effect=AssertionError("no debe enviar mensaje libre"))
    comment = Mock()
    monkeypatch.setattr(notificar, "enviar_plantilla", send)
    monkeypatch.setattr(notificar.erpnext, "add_comment", comment)

    sent = notificar.notificar_equipo(
        "SAL-ORD-0001",
        {
            "customer": "CUST-001",
            "items": [{"item_code": "LECHE-1L", "qty": 5}],
            "grand_total": 5000,
            "delivery_date": "2026-08-31",
        },
        auto=False,
        motivos="Revisión manual",
    )

    assert sent is False
    send.assert_not_called()
    assert "falta configurar" in comment.call_args.args[2]


def test_pending_staff_alert_uses_template_and_reports_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHATSAPP_STAFF_PENDING_TEMPLATE", "pedido_pendiente_equipo")
    monkeypatch.setattr(notificar, "STAFF", {"5491100000000"})
    monkeypatch.setattr(notificar.erpnext, "add_comment", Mock())
    send = Mock(return_value={"messages": [{"id": "wamid.out"}]})
    monkeypatch.setattr(notificar, "enviar_plantilla", send)

    sent = notificar.notificar_equipo(
        "SAL-ORD-0001",
        {
            "customer": "CUST-001",
            "items": [{"item_code": "LECHE-1L", "qty": 5}],
            "grand_total": 5000,
            "delivery_date": "2026-08-31",
        },
        auto=False,
    )

    assert sent is True
    assert send.call_args.args[1:3] == ("pedido_pendiente_equipo", "es_AR")
    assert send.call_args.args[4] == [
        "ok:SAL-ORD-0001",
        "ver:SAL-ORD-0001",
    ]
    record_args = notificar.record_outbound.call_args
    assert record_args.args[0] == "wamid.out"
    assert record_args.args[1].startswith("staff_order_pending:")
    assert record_args.kwargs == {"order_name": "SAL-ORD-0001"}


def test_confirmed_staff_alert_has_seven_parameters_and_no_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "WHATSAPP_STAFF_CONFIRMED_TEMPLATE", "pedido_confirmado_equipo"
    )
    monkeypatch.setattr(notificar, "STAFF", {"5491100000000"})
    monkeypatch.setattr(notificar.erpnext, "add_comment", Mock())
    send = Mock(return_value={"messages": [{"id": "wamid.out"}]})
    monkeypatch.setattr(notificar, "enviar_plantilla", send)

    assert notificar.notificar_equipo(
        "SAL-ORD-0001",
        {
            "customer": "CUST-001",
            "items": [{"item_code": "LECHE-1L", "qty": 5}],
            "grand_total": 5000,
            "delivery_date": "2026-08-31",
        },
        auto=True,
    ) is True
    assert len(send.call_args.args[3]) == 7
    assert send.call_args.args[4] is None


def test_approval_does_not_claim_customer_was_notified_on_send_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    monkeypatch.setattr(
        aprobacion,
        "_leer_doc",
        lambda doctype, name: {"name": name, "docstatus": 0},
    )
    submit = Mock(return_value={"name": "SAL-ORD-0001", "docstatus": 1})
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", submit)
    monkeypatch.setattr(aprobacion.erpnext, "add_comment", Mock())
    monkeypatch.setattr(aprobacion, "_avisar_cliente", lambda name: False)

    result = aprobacion.manejar_boton("ok:SAL-ORD-0001", "5491100000000")

    assert "confirmado" in result
    assert "No pude enviar" in result
    assert "Ya le avisé" not in result
    submit.assert_called_once_with("Sales Order", "SAL-ORD-0001")


def test_unauthorized_phone_cannot_trigger_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: False)
    submit = Mock()
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", submit)

    result = aprobacion.manejar_boton("ok:SAL-ORD-0001", "5491100000000")

    assert "No tenés permiso" in result
    submit.assert_not_called()


def test_legacy_reject_button_never_claims_or_changes_order_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    comment = Mock()
    monkeypatch.setattr(aprobacion.erpnext, "add_comment", comment)

    result = aprobacion.manejar_boton("no:SAL-ORD-0001", "5491100000000")

    assert "no cambié su estado" in result
    comment.assert_not_called()


def test_duplicate_approval_skips_submit_and_recovers_customer_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    monkeypatch.setattr(
        aprobacion,
        "_leer_doc",
        lambda doctype, name: {"name": name, "docstatus": 1},
    )
    submit = Mock()
    notify = Mock(return_value=True)
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", submit)
    monkeypatch.setattr(aprobacion, "_avisar_cliente", notify)

    result = aprobacion.manejar_boton("ok:SAL-ORD-0001", "5491100000000")

    assert "Ya estaba confirmado" in result
    submit.assert_not_called()
    notify.assert_called_once_with("SAL-ORD-0001")


def test_submit_timeout_reconciles_committed_erp_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    read = Mock(
        side_effect=[
            {"name": "SAL-ORD-0001", "docstatus": 0},
            {"name": "SAL-ORD-0001", "docstatus": 1},
        ]
    )
    monkeypatch.setattr(aprobacion, "_leer_doc", read)
    monkeypatch.setattr(
        aprobacion.erpnext,
        "submit_doc",
        Mock(side_effect=aprobacion.erpnext.ERPNextError("timeout")),
    )
    monkeypatch.setattr(aprobacion.erpnext, "add_comment", Mock())
    monkeypatch.setattr(aprobacion, "_avisar_cliente", Mock(return_value=True))

    result = aprobacion.manejar_boton("ok:SAL-ORD-0001", "5491100000000")

    assert "confirmado" in result
    assert "No pude comprobar" not in result
    assert read.call_count == 2


def test_delayed_customer_confirmation_uses_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE", "pedido_confirmado_cliente"
    )
    docs = {
        ("Sales Order", "SAL-ORD-0001"): {
            "name": "SAL-ORD-0001",
            "customer": "CUST-001",
            "delivery_date": "2026-08-31",
        },
        ("Customer", "CUST-001"): {
            "name": "CUST-001",
            "mobile_no": "5491100000001",
        },
    }
    monkeypatch.setattr(aprobacion, "_leer_doc", lambda doctype, name: docs[(doctype, name)])
    monkeypatch.setattr(aprobacion.erpnext, "add_comment", Mock())
    send = Mock(return_value={"messages": [{"id": "wamid.out"}]})
    monkeypatch.setattr(aprobacion, "enviar_plantilla", send)

    assert aprobacion._avisar_cliente("SAL-ORD-0001") is True
    send.assert_called_once_with(
        "5491100000001",
        "pedido_confirmado_cliente",
        "es_AR",
        ["SAL-ORD-0001", "2026-08-31"],
    )
    aprobacion.record_outbound.assert_called_once_with(
        "wamid.out",
        "customer_order_confirmation",
        order_name="SAL-ORD-0001",
    )


def test_customer_confirmation_replay_uses_recorded_acceptance_without_resend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aprobacion, "has_accepted", lambda *args: True)
    send = Mock(side_effect=AssertionError("no debe reenviar"))
    monkeypatch.setattr(aprobacion, "enviar_plantilla", send)

    assert aprobacion._avisar_cliente("SAL-ORD-0001") is True
    send.assert_not_called()


_SO = {
    "customer": "CUST-001",
    "customer_name": "Kiosco La Esquina",
    "items": [{"item_code": "MAN-200", "item_name": "Manteca 200 g", "qty": 2}],
    "grand_total": 4800,
    "delivery_date": "2026-09-03",
}


def test_pending_alert_without_template_uses_buttons_inside_staff_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staff = "5491100000000"
    monkeypatch.setattr(notificar, "STAFF", {staff})
    monkeypatch.setattr(notificar, "window_open", lambda phone: phone == staff)
    monkeypatch.setattr(notificar.erpnext, "add_comment", Mock())
    template = Mock(side_effect=AssertionError("sin plantilla no se usa enviar_plantilla"))
    buttons = Mock(return_value={"messages": [{"id": "wamid.btn"}]})
    monkeypatch.setattr(notificar, "enviar_plantilla", template)
    monkeypatch.setattr(notificar, "enviar_botones", buttons)

    assert notificar.notificar_equipo(
        "SAL-ORD-2026-00008", _SO, auto=False, motivos="auto-confirmación desactivada"
    ) is True

    phone, body, botones = buttons.call_args.args
    assert phone == staff
    assert "SAL-ORD-2026-00008" in body
    assert "Kiosco La Esquina" in body
    assert "2 x Manteca 200 g" in body
    assert "auto-confirmación desactivada" in body
    assert "confirmar SAL-ORD-2026-00008" in body
    assert len(body) <= 1024
    assert [b["id"] for b in botones] == ["ok:SAL-ORD-2026-00008", "ver:SAL-ORD-2026-00008"]
    assert notificar.record_outbound.call_args.args[0] == "wamid.btn"


def test_confirmed_alert_without_template_uses_plain_text_inside_staff_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staff = "5491100000000"
    monkeypatch.setattr(notificar, "STAFF", {staff})
    monkeypatch.setattr(notificar, "window_open", lambda phone: True)
    monkeypatch.setattr(notificar.erpnext, "add_comment", Mock())
    text = Mock(return_value={"messages": [{"id": "wamid.txt"}]})
    buttons = Mock(side_effect=AssertionError("un pedido confirmado no lleva botones"))
    monkeypatch.setattr(notificar, "enviar_mensaje", text)
    monkeypatch.setattr(notificar, "enviar_botones", buttons)

    assert notificar.notificar_equipo("SAL-ORD-2026-00008", _SO, auto=True) is True
    assert text.call_args.args[0] == staff
    assert "confirmado" in text.call_args.args[1]


def test_pending_alert_without_template_and_closed_window_fails_closed_with_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notificar, "STAFF", {"5491100000000"})
    comment = Mock()
    monkeypatch.setattr(notificar.erpnext, "add_comment", comment)
    monkeypatch.setattr(notificar, "enviar_botones", Mock(side_effect=AssertionError("ventana cerrada")))
    monkeypatch.setattr(notificar, "enviar_mensaje", Mock(side_effect=AssertionError("ventana cerrada")))

    assert notificar.notificar_equipo("SAL-ORD-2026-00008", _SO, auto=False) is False
    texto = comment.call_args.args[2]
    assert "falta configurar WHATSAPP_STAFF_PENDING_TEMPLATE" in texto
    assert "24 h" in texto


def test_customer_confirmation_without_template_uses_text_inside_customer_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docs = {
        ("Sales Order", "SAL-ORD-0001"): {
            "name": "SAL-ORD-0001",
            "customer": "CUST-001",
            "delivery_date": "2026-08-31",
        },
        ("Customer", "CUST-001"): {"name": "CUST-001", "mobile_no": "+5491100000001"},
    }
    monkeypatch.setattr(aprobacion, "_leer_doc", lambda doctype, name: docs[(doctype, name)])
    monkeypatch.setattr(aprobacion.erpnext, "add_comment", Mock())
    monkeypatch.setattr(aprobacion, "window_open", lambda phone: phone == "+5491100000001")
    text = Mock(return_value={"messages": [{"id": "wamid.out"}]})
    template = Mock(side_effect=AssertionError("sin plantilla no se usa enviar_plantilla"))
    monkeypatch.setattr(aprobacion, "enviar_mensaje", text)
    monkeypatch.setattr(aprobacion, "enviar_plantilla", template)

    assert aprobacion._avisar_cliente("SAL-ORD-0001") is True
    phone, body = text.call_args.args
    assert phone == "+5491100000001"
    assert "SAL-ORD-0001" in body and "2026-08-31" in body
    aprobacion.record_outbound.assert_called_once_with(
        "wamid.out", "customer_order_confirmation", order_name="SAL-ORD-0001"
    )


def test_customer_confirmation_without_template_and_closed_window_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docs = {
        ("Sales Order", "SAL-ORD-0001"): {"name": "SAL-ORD-0001", "customer": "CUST-001"},
        ("Customer", "CUST-001"): {"name": "CUST-001", "mobile_no": "+5491100000001"},
    }
    monkeypatch.setattr(aprobacion, "_leer_doc", lambda doctype, name: docs[(doctype, name)])
    comment = Mock()
    monkeypatch.setattr(aprobacion.erpnext, "add_comment", comment)
    monkeypatch.setattr(aprobacion, "enviar_mensaje", Mock(side_effect=AssertionError("cerrada")))

    assert aprobacion._avisar_cliente("SAL-ORD-0001") is False
    assert "ventana de 24 h" in comment.call_args.args[2]
