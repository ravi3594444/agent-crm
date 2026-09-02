"""The authorization boundary: a customer only sees and touches their own data.

The hole this guards against: tools that receive the customer identifier as a
PARAMETER FROM THE MODEL. Then the only thing stopping "what does Almacén Don
José usually order?" is a line in the prompt, and a prompt is not an access
control. In BASE the identity travels in ``RunnableConfig["configurable"]``
(``app/runtime_context.py``), which the webhook fills from the phone number
and the model never sees.

These tests fail if anyone puts the customer back into a tool signature, or
if a tool ever trusts an identity the server did not authenticate.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from app import erpnext
from app.runtime_context import (
    RuntimeContextError,
    actor_context,
    require_customer,
)
from app.tools import catalogo, pedidos

OTHER_CUSTOMER_ORDER = {
    "name": "SO-0042",
    "customer": "CUST-OTRO",
    "customer_name": "Almacén Don José",
    "docstatus": 1,
    "grand_total": 53_210.55,
    "currency": "ARS",
    "delivery_date": "2026-09-02",
    "transaction_date": "2026-08-30",
    "items": [{"item_code": "QUESO-SECRETO", "qty": 40, "uom": "kg"}],
}

IDENTITY_KEYS = {
    "cliente", "customer", "customer_code", "cliente_code",
    "telefono", "actor_phone", "phone", "config", "actor_scope",
    "inbound_message_id", "thread_id",
}


def _customer_config(customer: str = "CUST-001", message_id: str = "wamid.test") -> dict:
    return {
        "configurable": {
            "thread_id": "cli:thread",
            "actor_scope": "customer",
            "customer_code": customer,
            "actor_phone": "5493510000000",
            "inbound_message_id": message_id,
        }
    }


def _management_config() -> dict:
    return {
        "configurable": {
            "thread_id": "ger:thread",
            "actor_scope": "management",
            "actor_phone": "5493519999999",
            "inbound_message_id": "wamid.staff",
        }
    }


def _no_scope_config() -> dict:
    return {"configurable": {"thread_id": "cli:thread", "customer_code": "CUST-001"}}


@pytest.fixture
def erp_denies_everything(monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    """Any ERPNext call from an unauthorized path is a test failure."""
    mocks = {}
    for name in ("get_list", "get_doc", "create_doc", "submit_doc", "add_comment"):
        mock = Mock(side_effect=AssertionError(f"erpnext.{name} no debe llamarse"))
        monkeypatch.setattr(erpnext, name, mock)
        mocks[name] = mock
    return mocks


# --------------------------------------------------------------------------
# 1. The model cannot even NAME a customer: the parameter does not exist.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "expected_args"),
    [
        (pedidos.crear_pedido, {"lineas", "fecha_entrega"}),
        (catalogo.estado_pedido, {"numero_pedido"}),
        (catalogo.pedido_habitual, set()),
        (pedidos.crear_lead, {"nombre", "nota"}),
        (pedidos.escalar_a_humano, {"motivo"}),
    ],
    ids=lambda value: getattr(value, "name", None) or "args",
)
def test_model_visible_schema_has_no_identity_parameter(tool, expected_args) -> None:
    visible = set(tool.args)

    assert visible == expected_args
    assert not (visible & IDENTITY_KEYS)
    # The JSON schema handed to the LLM must agree with .args.
    assert set(tool.args_schema.model_json_schema()["properties"]) == expected_args


# --------------------------------------------------------------------------
# 2. runtime_context fails closed.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [None, {}, {"configurable": {}}, _no_scope_config(),
     {"configurable": {"actor_scope": "admin", "customer_code": "CUST-001"}},
     {"configurable": {"actor_scope": " ", "customer_code": "CUST-001"}}],
    ids=["none", "empty", "empty-configurable", "no-scope", "unknown-scope", "blank-scope"],
)
def test_actor_context_rejects_missing_or_unknown_scope(config) -> None:
    with pytest.raises(RuntimeContextError):
        actor_context(config)


def test_require_customer_rejects_unregistered_phone_and_management_scope() -> None:
    with pytest.raises(RuntimeContextError):
        require_customer(_customer_config(customer=""))
    with pytest.raises(RuntimeContextError):
        require_customer(_customer_config(customer="   "))
    with pytest.raises(RuntimeContextError):
        require_customer(_management_config())

    actor = require_customer(_customer_config("CUST-001"))
    assert actor.customer_code == "CUST-001"
    assert actor.is_management is False
    assert actor_context(_management_config()).is_management is True


# --------------------------------------------------------------------------
# 3. Cross-customer read: blocked and indistinguishable from "not found".
# --------------------------------------------------------------------------


def test_customer_cannot_read_another_customers_order_and_nothing_leaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(erpnext, "get_doc", Mock(return_value=dict(OTHER_CUSTOMER_ORDER)))

    reply = catalogo.estado_pedido.invoke(
        {"numero_pedido": "SO-0042"}, config=_customer_config("CUST-001")
    )

    assert reply == "No encontré el pedido SO-0042."
    for secret in ("53", "210", "Don José", "CUST-OTRO", "2026-09-02", "QUESO", "confirmado"):
        assert secret not in reply, secret


def test_denied_reply_is_byte_identical_to_a_truly_missing_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the wording differed, a customer could enumerate order numbers and
    learn which ones exist."""
    monkeypatch.setattr(erpnext, "get_doc", Mock(return_value=dict(OTHER_CUSTOMER_ORDER)))
    denied = catalogo.estado_pedido.invoke(
        {"numero_pedido": "SO-0042"}, config=_customer_config("CUST-001")
    )

    monkeypatch.setattr(erpnext, "get_doc", Mock(side_effect=erpnext.ERPNextError("404")))
    missing = catalogo.estado_pedido.invoke(
        {"numero_pedido": "SO-0042"}, config=_customer_config("CUST-001")
    )

    assert denied == missing


def test_customer_can_read_their_own_order(monkeypatch: pytest.MonkeyPatch) -> None:
    own = {**OTHER_CUSTOMER_ORDER, "customer": "CUST-001", "docstatus": 0}
    monkeypatch.setattr(erpnext, "get_doc", Mock(return_value=own))

    reply = catalogo.estado_pedido.invoke(
        {"numero_pedido": "SO-0042"}, config=_customer_config("CUST-001")
    )

    assert "Pedido SO-0042" in reply
    assert "borrador" in reply


def test_unregistered_phone_cannot_read_any_order_even_with_the_right_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(erpnext, "get_doc", Mock(return_value=dict(OTHER_CUSTOMER_ORDER)))

    reply = catalogo.estado_pedido.invoke(
        {"numero_pedido": "SO-0042"}, config=_customer_config(customer="")
    )

    assert reply == "No encontré el pedido SO-0042."
    assert "Don José" not in reply


def test_order_owner_check_is_exact_not_prefix_or_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(erpnext, "get_doc", Mock(return_value=dict(OTHER_CUSTOMER_ORDER)))

    for lookalike in ("CUST-OTR", "CUST-OTRO-2", "cust-otro", "XCUST-OTRO"):
        reply = catalogo.estado_pedido.invoke(
            {"numero_pedido": "SO-0042"}, config=_customer_config(lookalike)
        )
        assert reply == "No encontré el pedido SO-0042.", lookalike


def test_management_scope_can_read_any_customers_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(erpnext, "get_doc", Mock(return_value=dict(OTHER_CUSTOMER_ORDER)))

    reply = catalogo.estado_pedido.invoke(
        {"numero_pedido": "SO-0042"}, config=_management_config()
    )

    assert "Pedido SO-0042" in reply
    assert "confirmado" in reply
    assert "53" in reply


def test_status_lookup_without_authorization_context_fails_closed_before_erp(
    erp_denies_everything,
) -> None:
    reply = catalogo.estado_pedido.invoke(
        {"numero_pedido": "SO-0042"}, config=_no_scope_config()
    )

    assert reply == "No pude autorizar la consulta del pedido."
    erp_denies_everything["get_doc"].assert_not_called()


# --------------------------------------------------------------------------
# 4. Order history: bound to the authenticated account only.
# --------------------------------------------------------------------------


def test_unregistered_phone_cannot_read_history(erp_denies_everything) -> None:
    reply = catalogo.pedido_habitual.invoke({}, config=_customer_config(customer=""))

    assert reply == "No pude identificar una cuenta de cliente registrada."
    erp_denies_everything["get_list"].assert_not_called()


def test_management_scope_has_no_usual_order_because_it_is_not_a_customer(
    erp_denies_everything,
) -> None:
    reply = catalogo.pedido_habitual.invoke({}, config=_management_config())

    assert reply == "No pude identificar una cuenta de cliente registrada."
    erp_denies_everything["get_list"].assert_not_called()


def test_history_without_authorization_context_fails_closed(erp_denies_everything) -> None:
    reply = catalogo.pedido_habitual.invoke({}, config=_no_scope_config())

    assert "No pude identificar" in reply
    erp_denies_everything["get_list"].assert_not_called()


def test_history_query_filters_by_the_config_customer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_list = Mock(return_value=[])
    monkeypatch.setattr(erpnext, "get_list", get_list)

    reply = catalogo.pedido_habitual.invoke({}, config=_customer_config("CUST-001"))

    assert reply == "Esta cuenta no tiene pedidos anteriores confirmados."
    filters = get_list.call_args.kwargs["filters"]
    assert ["customer", "=", "CUST-001"] in filters
    assert ["docstatus", "=", 1] in filters


# --------------------------------------------------------------------------
# 5. Order creation: only an authenticated customer, only as themselves.
# --------------------------------------------------------------------------

_LINES = {
    "lineas": [{"item_code": "LECHE-1L", "cantidad": 5, "unidad": "Unidad"}],
    "fecha_entrega": "mañana",
}


def test_unregistered_phone_cannot_create_an_order(erp_denies_everything) -> None:
    reply = pedidos.crear_pedido.invoke(_LINES, config=_customer_config(customer=""))

    assert reply.startswith("PEDIDO_NO_CREADO")
    assert "No hay una cuenta de cliente autenticada" in reply
    erp_denies_everything["create_doc"].assert_not_called()
    erp_denies_everything["get_list"].assert_not_called()


def test_management_scope_cannot_create_an_order_on_behalf_of_nobody(
    erp_denies_everything,
) -> None:
    reply = pedidos.crear_pedido.invoke(_LINES, config=_management_config())

    assert reply.startswith("PEDIDO_NO_CREADO")
    erp_denies_everything["create_doc"].assert_not_called()


def test_order_without_authorization_context_fails_closed(erp_denies_everything) -> None:
    reply = pedidos.crear_pedido.invoke(_LINES, config=_no_scope_config())

    assert reply.startswith("PEDIDO_NO_CREADO")
    erp_denies_everything["create_doc"].assert_not_called()


def test_idempotency_lookup_is_scoped_to_the_authenticated_customer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The message-key lookup must include the customer filter: otherwise one
    customer's retry could surface another customer's order."""
    get_list = Mock(return_value=[{"name": "SO-0001", "customer": "CUST-001", "docstatus": 1}])
    monkeypatch.setattr(erpnext, "get_list", get_list)
    monkeypatch.setattr(
        erpnext,
        "get_doc",
        Mock(return_value={"name": "SO-0001", "customer": "CUST-001", "docstatus": 1,
                           "items": [], "delivery_date": "2026-08-30"}),
    )
    monkeypatch.setattr(erpnext, "create_doc", Mock(side_effect=AssertionError("no")))

    reply = pedidos.crear_pedido.invoke(_LINES, config=_customer_config("CUST-001"))

    assert reply.startswith("PEDIDO_CONFIRMADO")
    filters = get_list.call_args.kwargs["filters"]
    assert ["customer", "=", "CUST-001"] in filters
    assert any(f[0] == "po_no" for f in filters)


# --------------------------------------------------------------------------
# 6. Lead and escalation: also refuse to act on an unauthenticated context.
# --------------------------------------------------------------------------


def test_lead_creation_refuses_without_context_and_for_registered_or_staff(
    erp_denies_everything,
) -> None:
    assert "No pude autenticar" in pedidos.crear_lead.invoke(
        {"nombre": "X"}, config=_no_scope_config()
    )
    assert "No pude autenticar" in pedidos.crear_lead.invoke(
        {"nombre": "X"}, config=_management_config()
    )
    assert "ya está registrada" in pedidos.crear_lead.invoke(
        {"nombre": "X"}, config=_customer_config("CUST-001")
    )
    erp_denies_everything["create_doc"].assert_not_called()


def test_escalation_refuses_without_context(erp_denies_everything) -> None:
    reply = pedidos.escalar_a_humano.invoke({"motivo": "x"}, config=_no_scope_config())

    assert "No pude autenticar" in reply
    erp_denies_everything["create_doc"].assert_not_called()
