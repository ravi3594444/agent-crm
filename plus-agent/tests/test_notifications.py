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

from app import aprobacion, notificar, whatsapp


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
    monkeypatch.setattr(aprobacion, "has_accepted", lambda *args: False)
    monkeypatch.setattr(aprobacion, "record_outbound", Mock())


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


def test_reject_tells_the_customer_and_leaves_the_draft_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The customer was told the order was received and would be confirmed.

    Rejecting used to tell only the manager 'no cambié su estado', so the
    customer waited indefinitely. Now the customer is notified and the
    rejection is audited. The draft is never deleted and never cancelled —
    that trail is of something the customer was already told about — but it IS
    marked Closed, so it stops holding stock the next customer could have.
    """
    from app import decisiones

    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    monkeypatch.setattr(
        decisiones, "telefono_del_cliente", lambda nombre: "5493511234567"
    )
    monkeypatch.setattr(decisiones, "_avisar_cliente_rechazo", lambda *a: True)
    comment = Mock()
    monkeypatch.setattr(decisiones.erpnext, "add_comment", comment)
    monkeypatch.setattr(
        decisiones, "_leer_doc", Mock(return_value={"docstatus": 0, "status": "Draft"})
    )
    estado = Mock(return_value={"name": "SAL-ORD-0001", "status": "Closed"})
    monkeypatch.setattr(decisiones.erpnext, "policy_update_status", estado)
    submit = Mock()
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", submit)

    result = aprobacion.manejar_boton("no:SAL-ORD-0001", "5491100000000")

    assert "rechazado" in result.lower()
    assert "Ya le avisé al cliente" in result
    submit.assert_not_called()
    audit = " ".join(str(c) for c in comment.call_args_list)
    assert "Rechazado manualmente" in audit
    # policy._borradores_que_reservan reads this status: without it the
    # rejected draft keeps the product away from live orders for ever.
    estado.assert_called_once_with("Sales Order", "SAL-ORD-0001", "Closed")
    assert "ya no compromete stock" in audit


def test_a_rejection_that_cannot_be_closed_says_so_instead_of_pretending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the draft is best effort: an older ERPNext may refuse to close a
    document that was never submitted, and a Frappe PUT can even answer 200
    having quietly recomputed the field back. The rejection still stands, and
    the audit trail says the stock is still committed so somebody can act."""
    from app import decisiones

    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    monkeypatch.setattr(
        decisiones, "telefono_del_cliente", lambda nombre: "5493511234567"
    )
    monkeypatch.setattr(decisiones, "_avisar_cliente_rechazo", lambda *a: True)
    comment = Mock()
    monkeypatch.setattr(decisiones.erpnext, "add_comment", comment)
    monkeypatch.setattr(
        decisiones, "_leer_doc", Mock(return_value={"docstatus": 0, "status": "Draft"})
    )
    monkeypatch.setattr(
        decisiones.erpnext,
        "policy_update_status",
        Mock(side_effect=decisiones.erpnext.ERPNextError("ERPNext rechazó")),
    )
    submit = Mock()
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", submit)

    result = aprobacion.manejar_boton("no:SAL-ORD-0003", "5491100000000")

    assert "rechazado" in result.lower()
    submit.assert_not_called()
    audit = " ".join(str(c) for c in comment.call_args_list)
    assert "sigue comprometiendo stock" in audit


def test_rejecting_never_touches_an_order_somebody_already_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both buttons live in the same WhatsApp message, so [Rechazar] can be
    tapped after [Confirmar]. Stamping Closed on a submitted order would
    release the stock ERPNext reserved for it and drop it out of the delivery
    queue — ERPNext does not count a Closed order in reserved_qty."""
    from app import decisiones

    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    monkeypatch.setattr(decisiones, "telefono_del_cliente", lambda nombre: "")
    monkeypatch.setattr(decisiones.erpnext, "add_comment", Mock())
    monkeypatch.setattr(
        decisiones,
        "_leer_doc",
        Mock(return_value={"docstatus": 1, "status": "To Deliver and Bill"}),
    )
    estado = Mock()
    monkeypatch.setattr(decisiones.erpnext, "policy_update_status", estado)

    aprobacion.manejar_boton("no:SAL-ORD-0004", "5491100000000")

    estado.assert_not_called()


def test_a_status_that_did_not_stick_is_reported_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Frappe PUT is a save, not a field write: the doctype's validate() can
    recompute status and still answer 200 with the old value. Trusting the 200
    would tell the manager the stock was released when it was not."""
    from app import erpnext as cliente

    monkeypatch.setattr(
        cliente, "_request", Mock(return_value={"data": {"status": "Draft"}})
    )
    with pytest.raises(cliente.ERPNextError):
        cliente.policy_update_status("Sales Order", "SAL-ORD-0005", "Closed")

    monkeypatch.setattr(
        cliente, "_request", Mock(return_value={"data": {"status": "Closed"}})
    )
    assert cliente.policy_update_status("Sales Order", "SAL-ORD-0005", "Closed") == {
        "status": "Closed"
    }


def test_a_rejected_order_is_not_submittable_by_a_later_tap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submitting a Closed order would promise units no reservation system can
    see: ERPNext leaves status Closed across a submit and its get_reserved_qty
    skips exactly that state. Reopening it has to be a deliberate act."""
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    monkeypatch.setattr(
        aprobacion,
        "_leer_doc",
        Mock(return_value={"docstatus": 0, "status": "Closed"}),
    )
    submit = Mock()
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", submit)

    result = aprobacion.manejar_boton("ok:SAL-ORD-0006", "5491100000000")

    submit.assert_not_called()
    assert "reabrilo en ERPNext" in result


def test_reject_warns_the_manager_when_the_customer_could_not_be_told(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import decisiones

    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    monkeypatch.setattr(decisiones, "telefono_del_cliente", lambda nombre: "")
    monkeypatch.setattr(decisiones.erpnext, "add_comment", Mock())
    monkeypatch.setattr(
        decisiones, "_leer_doc", Mock(return_value={"docstatus": 0, "status": "Draft"})
    )
    monkeypatch.setattr(
        decisiones.erpnext,
        "policy_update_status",
        Mock(return_value={"status": "Closed"}),
    )

    result = aprobacion.manejar_boton("no:SAL-ORD-0002", "5491100000000")

    assert "NO pude avisarle al cliente" in result


# --------------------------------------------------------------------------
# El conteo físico: un número por WhatsApp no es un inventario hasta que una
# PERSONA lo confirma. Recién entonces el bot puede hablar de stock.
# --------------------------------------------------------------------------
GERENTE = "5493511111111"


def _config_gerencia(telefono: str = GERENTE) -> dict:
    return {
        "configurable": {
            "thread_id": "ger:thread",
            "actor_scope": "management",
            "actor_phone": telefono,
            "inbound_message_id": "wamid.staff-conteo",
        }
    }


def test_a_count_stays_a_draft_until_a_person_taps_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """contar_stock turns "quedan 12 kilos" into a DRAFT Stock Reconciliation
    and asks for one tap. Nothing about the stock is trusted until that tap:
    the AI has no Submit permission and must not have one."""
    from app import router
    from app.tools import captura

    monkeypatch.setattr(router, "STAFF", [GERENTE])
    monkeypatch.setattr(captura.erpnext, "default_warehouse", lambda: "Depósito A - LP")
    monkeypatch.setattr(
        captura.erpnext, "get_list", Mock(return_value=[{"actual_qty": 20}])
    )
    crear = Mock(return_value={"name": "SR-0001"})
    monkeypatch.setattr(captura.erpnext, "create_doc", crear)
    monkeypatch.setattr(captura.erpnext, "add_comment", Mock())
    submit = Mock()
    monkeypatch.setattr(captura.erpnext, "submit_doc", submit)
    botones = Mock(return_value=True)
    monkeypatch.setattr(captura.notificar, "pedir_confirmacion_conteo", botones)

    reply = captura.contar_stock.invoke(
        {"item_code": "QUE-CRE", "cantidad_real": 12}, config=_config_gerencia()
    )

    assert crear.call_args.args[0] == "Stock Reconciliation"
    submit.assert_not_called()
    botones.assert_called_once()
    assert botones.call_args.args[0] == GERENTE
    assert botones.call_args.args[1] == "SR-0001"
    assert "Confirmar conteo" in reply
    assert "no promete stock" in reply


def test_when_the_button_cannot_be_sent_he_is_told_to_use_erpnext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import router
    from app.tools import captura

    monkeypatch.setattr(router, "STAFF", [GERENTE])
    monkeypatch.setattr(captura.erpnext, "default_warehouse", lambda: "Depósito A - LP")
    monkeypatch.setattr(captura.erpnext, "get_list", Mock(return_value=[]))
    monkeypatch.setattr(
        captura.erpnext, "create_doc", Mock(return_value={"name": "SR-0002"})
    )
    monkeypatch.setattr(captura.erpnext, "add_comment", Mock())
    monkeypatch.setattr(
        captura.notificar, "pedir_confirmacion_conteo", Mock(return_value=False)
    )

    reply = captura.contar_stock.invoke(
        {"item_code": "QUE-CRE", "cantidad_real": 12}, config=_config_gerencia()
    )

    assert "en ERPNext" in reply
    assert "no promete stock" in reply


def test_a_stranger_cannot_load_a_count(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import router
    from app.tools import captura

    monkeypatch.setattr(router, "STAFF", [GERENTE])
    crear = Mock()
    monkeypatch.setattr(captura.erpnext, "create_doc", crear)

    reply = captura.contar_stock.invoke(
        {"item_code": "QUE-CRE", "cantidad_real": 12},
        config=_config_gerencia("5490000000000"),
    )

    assert "No pude autenticar" in reply
    crear.assert_not_called()


def test_tapping_confirm_submits_the_count_with_the_policy_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import decisiones

    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    monkeypatch.setattr(
        decisiones, "_leer_doc", Mock(return_value={"docstatus": 0})
    )
    submit = Mock(return_value={"name": "SR-0001", "docstatus": 1})
    monkeypatch.setattr(decisiones.erpnext, "submit_doc", submit)
    comentario = Mock()
    monkeypatch.setattr(decisiones.erpnext, "add_comment", comentario)

    reply = aprobacion.manejar_boton("conteo:SR-0001", GERENTE)

    submit.assert_called_once_with("Stock Reconciliation", "SR-0001")
    assert "confirmado" in reply
    audit = " ".join(str(c) for c in comentario.call_args_list)
    assert "Conteo confirmado por un integrante autorizado" in audit


def test_an_unauthorized_phone_cannot_confirm_a_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import decisiones

    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: False)
    confirmar = Mock()
    monkeypatch.setattr(decisiones, "confirmar_conteo", confirmar)

    reply = aprobacion.manejar_boton("conteo:SR-0001", "5490000000000")

    assert "permiso" in reply
    confirmar.assert_not_called()


def test_a_count_already_confirmed_is_not_submitted_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two taps on the same button must not post the adjustment twice."""
    from app import decisiones

    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    monkeypatch.setattr(
        decisiones, "_leer_doc", Mock(return_value={"docstatus": 1})
    )
    submit = Mock()
    monkeypatch.setattr(decisiones.erpnext, "submit_doc", submit)

    reply = aprobacion.manejar_boton("conteo:SR-0001", GERENTE)

    submit.assert_not_called()
    assert "ya estaba confirmado" in reply


def test_a_delivery_review_draft_is_confirmed_by_the_manager_not_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The system said "review the delivery". A person looks at the address
    and taps Confirmar: the existing human-only path submits it with the policy
    credential. Nothing new was added for this, on purpose — the override is
    the same button as every other exception."""
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    monkeypatch.setattr(
        aprobacion,
        "_leer_doc",
        Mock(return_value={"docstatus": 0, "status": "Draft", "customer": "CUST-001"}),
    )
    submit = Mock(return_value={"name": "SAL-ORD-0100", "docstatus": 1})
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", submit)
    monkeypatch.setattr(aprobacion.erpnext, "add_comment", Mock())
    monkeypatch.setattr(aprobacion, "_avisar_cliente", lambda nombre: True)

    reply = aprobacion.manejar_boton("ok:SAL-ORD-0100", GERENTE)

    submit.assert_called_once_with("Sales Order", "SAL-ORD-0100")
    assert "confirmado" in reply


def test_a_delivery_review_draft_can_be_rejected_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import decisiones

    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    monkeypatch.setattr(decisiones, "telefono_del_cliente", lambda nombre: "5493511234567")
    aviso = Mock(return_value=True)
    monkeypatch.setattr(decisiones, "_avisar_cliente_rechazo", aviso)
    monkeypatch.setattr(decisiones.erpnext, "add_comment", Mock())
    monkeypatch.setattr(decisiones, "_leer_doc", Mock(return_value={"docstatus": 0}))
    monkeypatch.setattr(decisiones.erpnext, "policy_update_status", Mock(return_value={"status": "Closed"}))
    submit = Mock()
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", submit)

    reply = aprobacion.manejar_boton("no:SAL-ORD-0100", GERENTE)

    submit.assert_not_called()
    aviso.assert_called_once()  # the customer is told, honestly
    assert "rechazado" in reply.lower()


def test_unauthorized_phone_cannot_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import decisiones

    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: False)
    rechazar = Mock()
    monkeypatch.setattr(decisiones, "rechazar", rechazar)

    result = aprobacion.manejar_boton("no:SAL-ORD-0001", "5490000000000")

    assert "permiso" in result
    rechazar.assert_not_called()


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
