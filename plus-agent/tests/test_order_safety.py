from __future__ import annotations

import os
import sys
from contextlib import contextmanager, nullcontext
from datetime import date
from datetime import datetime as RealDateTime
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

os.environ.setdefault("ERPNEXT_URL", "http://erpnext.test")
os.environ.setdefault("ERPNEXT_API_KEY", "test-key")
os.environ.setdefault("ERPNEXT_API_SECRET", "test-secret")
os.environ.setdefault("ERPNEXT_POLICY_API_KEY", "policy-key")
os.environ.setdefault("ERPNEXT_POLICY_API_SECRET", "policy-secret")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test-phone-id")
os.environ.setdefault("WHATSAPP_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import erpnext, policy
from app.tools import catalogo, pedidos


def _limites_del_dueno(monkeypatch: pytest.MonkeyPatch, **cambios: object) -> None:
    """The owner's six limits, set the way the bootstrap environment sets them.

    The store is empty in every test (conftest), so these resolve exactly as on
    a fresh install. `cambios` overrides one of them for a single test.
    """
    valores: dict[str, object] = {
        "AUTO_CONFIRM_MAX": 1_000,
        "AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO": 100,
        "AUTO_CONFIRM_MAX_DEBT": 0,
        "AUTO_CONFIRM_MAX_CLIENTE_NUEVO": 0,
        "AUTO_CONFIRM_DESCUENTOS_APRUEBAN": "true",
        "STOCK_BUFFER_PCT": 0,
    }
    valores.update(cambios)
    for nombre, valor in valores.items():
        monkeypatch.setenv(nombre, str(valor))


def _customer_config(
    customer: str = "CUST-001", message_id: str = "wamid.test-001"
) -> dict:
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
            "inbound_message_id": "wamid.staff-001",
        }
    }


def _order(**overrides) -> dict:
    order = {
        "name": "SO-0001",
        "customer": "CUST-001",
        "customer_name": "Cliente Uno",
        "docstatus": 0,
        "grand_total": 100.0,
        "selling_price_list": "Standard Selling",
        "currency": "ARS",
        "transaction_date": "2026-08-29",
        "delivery_date": "2026-08-30",
        "discount_amount": 0,
        "base_discount_amount": 0,
        "additional_discount_percentage": 0,
        "items": [
            {
                "item_code": "LECHE-1L",
                "qty": 5,
                "uom": "Unidad",
                "stock_uom": "Unidad",
                "conversion_factor": 1,
                "rate": 20,
                "price_list_rate": 20,
                "discount_percentage": 0,
                "discount_amount": 0,
                "distributed_discount_amount": 0,
                "warehouse": "Depósito A - LP",
            }
        ],
    }
    order.update(overrides)
    return order


@pytest.fixture(autouse=True)
def _safe_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERPNEXT_COMPANY", "Lácteos Plus SA")
    monkeypatch.setenv("ERPNEXT_WAREHOUSE", "Depósito A - LP")
    monkeypatch.setenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires")


def test_context_requires_both_explicit_values_without_warehouse_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read = Mock(side_effect=AssertionError("no debe elegir un Warehouse arbitrario"))
    monkeypatch.setattr(erpnext, "get_list", read)

    assert erpnext.default_context() == (
        "Lácteos Plus SA",
        "Depósito A - LP",
    )
    read.assert_not_called()

    monkeypatch.delenv("ERPNEXT_COMPANY")
    monkeypatch.delenv("ERPNEXT_WAREHOUSE")
    with pytest.raises(erpnext.ERPNextError, match="configurarse explícitamente"):
        erpnext.default_context()
    read.assert_not_called()


@pytest.mark.parametrize(
    ("company", "warehouse"),
    [("Lácteos Plus SA", ""), ("", "Depósito A - LP")],
)
def test_partial_context_fails_closed(
    monkeypatch: pytest.MonkeyPatch, company: str, warehouse: str
) -> None:
    monkeypatch.setenv("ERPNEXT_COMPANY", company)
    monkeypatch.setenv("ERPNEXT_WAREHOUSE", warehouse)
    with pytest.raises(erpnext.ERPNextError):
        erpnext.default_context()


def test_erp_error_never_contains_raw_response_body() -> None:
    response = httpx.Response(
        403,
        text='{"exception":"secret customer data and stack trace"}',
        request=httpx.Request("GET", "http://erpnext.test/api/resource/Customer"),
    )
    client = Mock()
    client.request.return_value = response

    with pytest.raises(erpnext.ERPNextError) as caught:
        erpnext._request(client, "GET", "/x", operation="prueba")

    assert "secret customer data" not in str(caught.value)
    assert "stack trace" not in str(caught.value)
    assert "estado 403" in str(caught.value)


def test_manager_scope_uses_distinct_credentials_and_fails_closed_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(erpnext, "_manager_client", None)
    monkeypatch.delenv("ERPNEXT_MANAGER_API_KEY", raising=False)
    monkeypatch.delenv("ERPNEXT_MANAGER_API_SECRET", raising=False)
    with pytest.raises(erpnext.ERPNextError, match="gerencia"), erpnext.manager_scope():
        pass
    assert erpnext._active_client() is erpnext._client

    monkeypatch.setenv("ERPNEXT_MANAGER_API_KEY", "manager-key")
    monkeypatch.setenv("ERPNEXT_MANAGER_API_SECRET", "manager-secret")
    manager = Mock(spec=httpx.Client)
    constructor = Mock(return_value=manager)
    monkeypatch.setattr(erpnext.httpx, "Client", constructor)
    with erpnext.manager_scope():
        assert erpnext._active_client() is manager
    assert erpnext._active_client() is erpnext._client
    headers = constructor.call_args.kwargs["headers"]
    assert headers["Authorization"] == "token manager-key:manager-secret"


def test_policy_get_doc_uses_policy_client(monkeypatch: pytest.MonkeyPatch) -> None:
    privileged = Mock(spec=httpx.Client)
    monkeypatch.setattr(erpnext, "_policy", Mock(return_value=privileged))
    request = Mock(return_value={"data": {"name": "SO-0001", "docstatus": 1}})
    monkeypatch.setattr(erpnext, "_request", request)

    assert erpnext.policy_get_doc("Sales Order", "SO-0001")["docstatus"] == 1
    assert request.call_args.args[0] is privileged


def test_tool_schemas_hide_authenticated_identity_and_require_unit_and_date() -> None:
    order_schema = pedidos.crear_pedido.args_schema.model_json_schema()
    properties = order_schema["properties"]
    assert set(properties) == {"lineas", "fecha_entrega"}
    assert set(properties["lineas"]["items"]["$ref"].split("/"))
    assert set(order_schema["$defs"]["LineaPedido"]["required"]) == {
        "item_code",
        "cantidad",
        "unidad",
    }
    assert set(order_schema["required"]) == {"lineas", "fecha_entrega"}
    assert "config" not in catalogo.estado_pedido.args_schema.model_json_schema()[
        "properties"
    ]
    assert catalogo.pedido_habitual.args_schema.model_json_schema()["properties"] == {}


def test_status_lookup_enforces_customer_ownership_but_management_can_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(erpnext, "get_doc", Mock(return_value=_order(customer="CUST-002")))

    denied = catalogo.estado_pedido.invoke(
        {"numero_pedido": "SO-0001"}, config=_customer_config("CUST-001")
    )
    allowed = catalogo.estado_pedido.invoke(
        {"numero_pedido": "SO-0001"}, config=_management_config()
    )

    assert denied == "No encontré el pedido SO-0001."
    assert "Pedido SO-0001" in allowed


def test_usual_order_uses_bound_customer_not_model_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_list = Mock(return_value=[{"name": "SO-0001"}])
    monkeypatch.setattr(erpnext, "get_list", get_list)
    monkeypatch.setattr(erpnext, "get_doc", Mock(return_value=_order()))

    result = catalogo.pedido_habitual.invoke({}, config=_customer_config("CUST-001"))

    assert "Último pedido" in result
    get_list.assert_called_once_with(
        "Sales Order",
        filters=[["customer", "=", "CUST-001"], ["docstatus", "=", 1]],
        fields=["name"],
        limit=1,
    )


def test_date_parser_uses_business_timezone_and_never_defaults_missing_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    class Clock:
        @classmethod
        def now(cls, tz):
            seen.append(tz.key)
            return RealDateTime(2026, 8, 29, 23, 30, tzinfo=tz)

    monkeypatch.setattr(pedidos, "datetime", Clock)
    assert pedidos._parse_fecha("mañana") == "2026-08-30"
    assert seen == ["America/Argentina/Buenos_Aires"]
    with pytest.raises(pedidos.FechaEntregaInvalida, match="falta"):
        pedidos._parse_fecha(None)  # type: ignore[arg-type]


def test_yearless_date_resolves_to_next_occurrence() -> None:
    today = date(2026, 8, 29)
    assert pedidos._parse_fecha("27/08", hoy=today) == "2027-08-27"
    assert pedidos._parse_fecha("29/02", hoy=today) == "2028-02-29"


@pytest.mark.parametrize(
    "raw",
    ["2026-02-30", "2026-08-28", "28/08/2026", "31/02", "la semana que viene", ""],
)
def test_invalid_past_or_unrecognized_date_is_rejected(raw: str) -> None:
    with pytest.raises(pedidos.FechaEntregaInvalida):
        pedidos._parse_fecha(raw, hoy=date(2026, 8, 29))


def test_order_rejects_unit_mismatch_before_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pedidos, "_hoy_del_negocio", lambda: date(2026, 8, 29))
    monkeypatch.setattr(pedidos, "distributed_lock", lambda *a, **k: nullcontext())
    monkeypatch.setattr(erpnext, "get_list", Mock(return_value=[]))
    monkeypatch.setattr(
        erpnext,
        "get_doc",
        Mock(return_value={"name": "LECHE-1L", "stock_uom": "Unidad", "disabled": 0}),
    )
    create = Mock(side_effect=AssertionError("no debe escribir"))
    monkeypatch.setattr(erpnext, "create_doc", create)

    result = pedidos.crear_pedido.invoke(
        {
            "lineas": [
                {"item_code": "LECHE-1L", "cantidad": 5, "unidad": "kg"}
            ],
            "fecha_entrega": "mañana",
        },
        config=_customer_config(),
    )

    assert "PEDIDO_NO_CREADO" in result
    assert "se vende por Unidad, no por kg" in result
    create.assert_not_called()


def test_order_requires_authenticated_message_id_before_any_erp_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read = Mock(side_effect=AssertionError("no debe consultar ERPNext"))
    monkeypatch.setattr(erpnext, "get_list", read)
    config = _customer_config(message_id="")

    result = pedidos.crear_pedido.invoke(
        {
            "lineas": [
                {"item_code": "LECHE-1L", "cantidad": 5, "unidad": "Unidad"}
            ],
            "fecha_entrega": "mañana",
        },
        config=config,
    )

    assert "Falta la referencia segura" in result
    read.assert_not_called()


def test_order_write_uses_bound_customer_uom_and_hashed_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pedidos, "_hoy_del_negocio", lambda: date(2026, 8, 29))
    monkeypatch.setattr(pedidos, "distributed_lock", lambda *a, **k: nullcontext())
    monkeypatch.setattr(erpnext, "get_list", Mock(return_value=[]))
    complete = _order()

    def get_doc(doctype: str, name: str) -> dict:
        if doctype == "Item":
            return {"name": name, "stock_uom": "Unidad", "disabled": 0}
        return complete

    monkeypatch.setattr(erpnext, "get_doc", Mock(side_effect=get_doc))
    create = Mock(return_value={"name": "SO-0001", "docstatus": 0})
    monkeypatch.setattr(erpnext, "create_doc", create)
    monkeypatch.setattr(erpnext, "add_comment", Mock())
    monkeypatch.setattr(policy, "evaluar", Mock(return_value=policy.Decision(False, ["review"])))
    notify = Mock(return_value=True)
    monkeypatch.setattr(pedidos, "notificar_equipo", notify)

    result = pedidos.crear_pedido.invoke(
        {
            "lineas": [
                {"item_code": "LECHE-1L", "cantidad": 5, "unidad": "unidades"}
            ],
            "fecha_entrega": "mañana",
        },
        config=_customer_config("CUST-001", "wamid.private-meta-id"),
    )

    assert result.startswith("PEDIDO_PENDIENTE. Número real: SO-0001")
    payload = create.call_args.args[1]
    assert payload["customer"] == "CUST-001"
    assert payload["po_no"] == pedidos._message_key("wamid.private-meta-id")
    assert "private-meta-id" not in payload["po_no"]
    assert payload["items"] == [
        {
            "item_code": "LECHE-1L",
            "qty": 5.0,
            "uom": "Unidad",
            "delivery_date": "2026-08-30",
            "warehouse": "Depósito A - LP",
            "conversion_factor": 1,
        }
    ]
    notify.assert_called_once()


def test_retry_returns_existing_order_without_second_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _order(docstatus=1)
    monkeypatch.setattr(
        erpnext,
        "get_list",
        Mock(return_value=[{"name": "SO-0001", "customer": "CUST-001", "docstatus": 1}]),
    )
    monkeypatch.setattr(erpnext, "get_doc", Mock(return_value=existing))
    create = Mock(side_effect=AssertionError("no debe duplicar"))
    monkeypatch.setattr(erpnext, "create_doc", create)

    result = pedidos.crear_pedido.invoke(
        {
            "lineas": [
                {"item_code": "LECHE-1L", "cantidad": 5, "unidad": "Unidad"}
            ],
            "fecha_entrega": "mañana",
        },
        config=_customer_config(),
    )

    assert result.startswith("PEDIDO_CONFIRMADO. Número real: SO-0001")
    create.assert_not_called()


def test_post_create_policy_failure_keeps_real_order_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        erpnext, "get_doc", Mock(side_effect=erpnext.ERPNextError("safe"))
    )
    monkeypatch.setattr(erpnext, "add_comment", Mock())
    monkeypatch.setattr(policy, "evaluar", Mock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(pedidos, "notificar_equipo", Mock(return_value=False))

    result = pedidos._after_create(
        {"name": "SO-0001", "docstatus": 0},
        [{"item_code": "LECHE-1L", "qty": 5, "uom": "Unidad"}],
        "2026-08-30",
    )

    assert result.startswith("PEDIDO_PENDIENTE. Número real: SO-0001")
    assert "boom" not in result


def test_auto_confirm_revalidates_under_lock_before_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = _order()
    confirmed = _order(docstatus=1)
    monkeypatch.setattr(erpnext, "get_doc", Mock(return_value=draft))
    monkeypatch.setattr(erpnext, "add_comment", Mock())
    evaluate = Mock(return_value=policy.Decision(True))
    monkeypatch.setattr(policy, "evaluar", evaluate)
    lock = Mock(return_value=nullcontext())
    monkeypatch.setattr(policy, "auto_submit_lock", lock)
    submit = Mock(return_value=confirmed)
    monkeypatch.setattr(erpnext, "submit_doc", submit)
    monkeypatch.setattr(pedidos, "notificar_equipo", Mock(return_value=True))

    result = pedidos._after_create(draft, draft["items"], draft["delivery_date"])

    assert result.startswith("PEDIDO_CONFIRMADO. Número real: SO-0001")
    assert evaluate.call_count == 2
    lock.assert_called_once_with()
    submit.assert_called_once_with("Sales Order", "SO-0001")


def test_policy_never_auto_confirms_when_stock_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _limites_del_dueno(monkeypatch)
    monkeypatch.setattr(policy, "STOCK_CONFIABLE", False)
    monkeypatch.setattr(policy, "PRICE_LIST", "Standard Selling")
    monkeypatch.setattr(policy, "CURRENCY", "ARS")
    monkeypatch.setattr(policy, "MIN_PEDIDOS", 1)
    monkeypatch.setattr(erpnext, "get_list", Mock(return_value=[{"grand_total": 100}]))
    monkeypatch.setattr(policy, "_saldo_vencido", Mock(return_value=0))
    monkeypatch.setattr(policy, "_precio_estandar", Mock(return_value=True))
    monkeypatch.setattr(policy, "_hoy_del_negocio", lambda: date(2026, 8, 29))

    decision = policy.evaluar(_order())

    assert decision.auto is False
    assert "inventario no marcado como confiable" in decision.motivos


def test_policy_aggregates_duplicate_lines_per_item_and_warehouse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _limites_del_dueno(monkeypatch)
    monkeypatch.setattr(policy, "STOCK_CONFIABLE", True)
    monkeypatch.setattr(policy, "PRICE_LIST", "Standard Selling")
    monkeypatch.setattr(policy, "CURRENCY", "ARS")
    monkeypatch.setattr(policy, "MIN_PEDIDOS", 1)
    monkeypatch.setattr(erpnext, "get_list", Mock(return_value=[{"grand_total": 100}]))
    monkeypatch.setattr(policy, "_saldo_vencido", Mock(return_value=0))
    stock = Mock(return_value=True)
    monkeypatch.setattr(policy, "_hay_stock", stock)
    monkeypatch.setattr(policy, "_precio_estandar", Mock(return_value=True))
    monkeypatch.setattr(policy, "_hoy_del_negocio", lambda: date(2026, 8, 29))
    first = _order()["items"][0]
    second = {**first, "qty": 4}

    decision = policy.evaluar(_order(items=[first, second]))

    assert decision.auto is True
    stock.assert_called_once_with(
        "LECHE-1L", 9.0, "Depósito A - LP", excluir="SO-0001", company="", desde=""
    )


def test_policy_stock_query_is_scoped_to_assigned_warehouse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STOCK_BUFFER_PCT", "0")
    get_list = Mock(return_value=[{"actual_qty": 8, "reserved_qty": 2}])
    monkeypatch.setattr(erpnext, "get_list", get_list)
    monkeypatch.setattr(erpnext, "policy_get_list", Mock(return_value=[]))

    assert policy._hay_stock("LECHE-1L", 5, "Depósito A - LP") is True
    # Still exactly one read on the restricted identity, and still that one.
    get_list.assert_called_once_with(
        "Bin",
        filters=[
            ["item_code", "=", "LECHE-1L"],
            ["warehouse", "=", "Depósito A - LP"],
        ],
        fields=["actual_qty", "reserved_qty"],
        limit=10,
    )


def _catalogo_erp(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fisico: float,
    prometido: float = 0.0,
    borradores_fallan: bool = False,
) -> None:
    """Wire consultar_stock's two reads: Bin, and what other drafts hold."""
    monkeypatch.setattr(catalogo, "STOCK_CONFIABLE", True)
    monkeypatch.setenv("STOCK_BUFFER_PCT", "0")
    monkeypatch.setenv("STOCK_POCO", "20")
    monkeypatch.setattr(erpnext, "default_warehouse", lambda: "Depósito A - LP")
    monkeypatch.setattr(erpnext, "default_company", lambda: "Lácteos Plus SA")
    monkeypatch.setattr(
        erpnext,
        "get_list",
        Mock(return_value=[{"warehouse": "Depósito A - LP", "actual_qty": fisico, "reserved_qty": 0}]),
    )

    def policy_get_list(doctype, filters=None, fields=None, limit=20, parent=None):
        if borradores_fallan:
            raise erpnext.ERPNextError("ERPNext no disponible")
        if doctype == "Sales Order":
            return [
                {
                    "name": "SO-OTHER",
                    "docstatus": 0,
                    "status": "Draft",
                    "company": "Lácteos Plus SA",
                    "creation": "2026-08-28 09:00:00",
                }
            ]
        return [
            {
                "parent": "SO-OTHER",
                "item_code": "LECHE-1L",
                "warehouse": "Depósito A - LP",
                "docstatus": 0,
                "qty": prometido,
                "stock_qty": prometido,
                "uom": "Unidad",
                "stock_uom": "Unidad",
                "conversion_factor": 1,
            }
        ]

    monkeypatch.setattr(erpnext, "policy_get_list", Mock(side_effect=policy_get_list))


def test_the_customer_answer_deducts_what_other_drafts_already_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Bin alone this said "stock registrado" for units another customer is
    already waiting for, and the customer heard a promise. 30 in the fridge
    with 25 promised is not 30."""
    _catalogo_erp(monkeypatch, fisico=30, prometido=25)

    assert "POCO STOCK" in catalogo.consultar_stock.invoke({"item_code": "LECHE-1L"})


def test_the_customer_answer_says_sin_stock_when_drafts_hold_it_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _catalogo_erp(monkeypatch, fisico=10, prometido=10)

    assert "SIN STOCK" in catalogo.consultar_stock.invoke({"item_code": "LECHE-1L"})


def test_the_customer_answer_promises_nothing_when_the_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uncertainty must reach the customer as "I am not confirming", never as
    availability."""
    _catalogo_erp(monkeypatch, fisico=1_000, borradores_fallan=True)

    answer = catalogo.consultar_stock.invoke({"item_code": "LECHE-1L"})

    assert "No confirmes disponibilidad" in answer
    assert "stock registrado" not in answer


def _politica_verde(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fisico: float = 10.0,
    renglones: tuple[dict, ...] = (),
    padres: tuple[dict, ...] = (),
    borradores_fallan: bool = False,
    rastro: list[str] | None = None,
) -> dict:
    """Every rule green EXCEPT that the real _hay_stock runs against ERPNext.

    Nothing here mocks _hay_stock: the point of these tests is the whole
    confirmation path — policy, the global lock, the re-read and the submit —
    with the competing-draft lookup answering for real.
    """
    _limites_del_dueno(monkeypatch)
    monkeypatch.setattr(policy, "MAX_MULT", 2.0)
    monkeypatch.setattr(policy, "MIN_PEDIDOS", 1)
    monkeypatch.setattr(policy, "STOCK_CONFIABLE", True)
    monkeypatch.setattr(policy, "PRICE_LIST", "Standard Selling")
    monkeypatch.setattr(policy, "CURRENCY", "ARS")
    monkeypatch.setattr(policy, "_hoy_del_negocio", lambda: date(2026, 8, 29))
    monkeypatch.setattr(policy, "_saldo_vencido", Mock(return_value=0.0))
    monkeypatch.setattr(policy, "_precio_estandar", Mock(return_value=True))
    monkeypatch.setenv("STOCK_BUFFER_PCT", "0")

    def get_list(doctype, filters=None, fields=None, limit=20, parent=None):
        if doctype == "Bin":
            return [{"actual_qty": fisico, "reserved_qty": 0}]
        if doctype == "Sales Order":  # the customer's order history
            return [{"grand_total": 100}]
        return []

    def policy_get_list(doctype, filters=None, fields=None, limit=20, parent=None):
        if rastro is not None:
            rastro.append(f"lee:{doctype}")
        if borradores_fallan:
            raise erpnext.ERPNextError("ERPNext no disponible")
        if doctype == "Sales Order":
            return [dict(row) for row in padres]
        if doctype == "Sales Order Item":
            return [dict(row) for row in renglones]
        return []

    monkeypatch.setattr(erpnext, "get_list", Mock(side_effect=get_list))
    monkeypatch.setattr(erpnext, "policy_get_list", Mock(side_effect=policy_get_list))
    monkeypatch.setattr(erpnext, "get_doc", Mock(return_value=_order()))
    comment = Mock()
    monkeypatch.setattr(erpnext, "add_comment", comment)
    submit = Mock(return_value=_order(docstatus=1))
    monkeypatch.setattr(erpnext, "submit_doc", submit)
    notify = Mock(return_value=True)
    monkeypatch.setattr(pedidos, "notificar_equipo", notify)
    return {"submit": submit, "comment": comment, "notify": notify}


def test_an_order_with_no_competing_draft_still_auto_confirms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reservation rule must not quietly switch auto-confirmation off."""
    mocks = _politica_verde(monkeypatch, fisico=10, renglones=())
    draft = _order()

    result = pedidos._after_create(draft, draft["items"], draft["delivery_date"])

    assert result.startswith("PEDIDO_CONFIRMADO. Número real: SO-0001")
    mocks["submit"].assert_called_once_with("Sales Order", "SO-0001")


def test_a_competing_draft_sends_the_second_order_to_a_human(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both customers asked for 5 of the last 8. The first one keeps them; the
    second waits for a person instead of being promised what is gone."""
    mocks = _politica_verde(
        monkeypatch,
        fisico=8,
        renglones=(
            {
                "parent": "SO-OTHER",
                "item_code": "LECHE-1L",
                "warehouse": "Depósito A - LP",
                "docstatus": 0,
                "qty": 5,
                "stock_qty": 5,
                "uom": "Unidad",
                "stock_uom": "Unidad",
                "conversion_factor": 1,
            },
        ),
        padres=(
            {
                "name": "SO-OTHER",
                "docstatus": 0,
                "status": "Draft",
                "creation": "2026-08-28 09:00:00",
            },
        ),
    )
    draft = _order()

    result = pedidos._after_create(draft, draft["items"], draft["delivery_date"])

    assert result.startswith("PEDIDO_PENDIENTE. Número real: SO-0001")
    mocks["submit"].assert_not_called()
    audit = " ".join(str(call) for call in mocks["comment"].call_args_list)
    assert "stock insuficiente de LECHE-1L" in audit
    assert mocks["notify"].call_args.kwargs["auto"] is False


def test_a_failed_draft_lookup_leaves_the_order_pending_and_unsubmitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uncertain stock is not a green light. The order stays a draft and the
    reason travels the existing exception route to the team."""
    mocks = _politica_verde(monkeypatch, fisico=1_000, borradores_fallan=True)
    draft = _order()

    result = pedidos._after_create(draft, draft["items"], draft["delivery_date"])

    assert result.startswith("PEDIDO_PENDIENTE. Número real: SO-0001")
    mocks["submit"].assert_not_called()
    audit = " ".join(str(call) for call in mocks["comment"].call_args_list)
    assert "no se pudo verificar stock de LECHE-1L" in audit
    assert mocks["notify"].call_args.kwargs["auto"] is False
    assert "no se pudo verificar stock" in mocks["notify"].call_args.kwargs["motivos"]


def test_competing_drafts_are_re_read_inside_the_submit_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two workers can hold two drafts for the same last units at the same
    time. Only a lookup INSIDE the global lock, after the re-read, can see the
    other one; a lookup before the lock is a race, not a check."""
    rastro: list[str] = []
    mocks = _politica_verde(monkeypatch, fisico=10, rastro=rastro)

    @contextmanager
    def lock():
        rastro.append("lock-in")
        try:
            yield
        finally:
            rastro.append("lock-out")

    monkeypatch.setattr(policy, "auto_submit_lock", lock)
    draft = _order()

    result = pedidos._after_create(draft, draft["items"], draft["delivery_date"])

    assert result.startswith("PEDIDO_CONFIRMADO")
    mocks["submit"].assert_called_once_with("Sales Order", "SO-0001")
    dentro = rastro[rastro.index("lock-in") : rastro.index("lock-out")]
    assert "lee:Sales Order" in dentro


def test_the_locked_revalidation_uses_the_limits_in_force_at_that_moment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner can lower a limit while an order is in flight.

    Policy is evaluated twice: once on arrival, once inside the submit lock
    just before confirming. The second read is the one that decides, so a
    ceiling lowered in between applies to this order and not to the next one —
    without a restart, and without the first evaluation's answer being cached.
    """
    from conftest import FakeRedis

    from app import locks

    mocks = _politica_verde(monkeypatch, fisico=1_000)
    dentro_del_lock: list[bool] = []

    class Cambiante(FakeRedis):
        def hgetall(self, key):
            # The owner drops the ceiling to $50 exactly while the lock is held.
            return {"AUTO_CONFIRM_MAX": "50"} if dentro_del_lock else {}

    monkeypatch.setattr(locks, "conexion", lambda: Cambiante())

    @contextmanager
    def lock():
        dentro_del_lock.append(True)
        try:
            yield
        finally:
            pass

    monkeypatch.setattr(policy, "auto_submit_lock", lock)
    draft = _order()  # $100, which the ceiling of $1000 would have allowed

    result = pedidos._after_create(draft, draft["items"], draft["delivery_date"])

    # The lock was entered at all, so the first evaluation did say "confirm".
    assert dentro_del_lock == [True]
    assert result.startswith("PEDIDO_PENDIENTE. Número real: SO-0001")
    mocks["submit"].assert_not_called()
    audit = " ".join(str(call) for call in mocks["comment"].call_args_list)
    assert "supera el tope" in audit


def test_standard_price_requires_exact_unscoped_valid_currency_uom_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policy, "PRICE_LIST", "Standard Selling")
    monkeypatch.setattr(policy, "CURRENCY", "ARS")
    valid = {
        "price_list_rate": 20,
        "price_list": "Standard Selling",
        "currency": "ARS",
        "uom": "Unidad",
        "valid_from": "2026-01-01",
        "valid_upto": "2026-12-31",
        "customer": None,
        "batch_no": None,
    }
    get_list = Mock(return_value=[valid])
    monkeypatch.setattr(erpnext, "get_list", get_list)
    item = _order()["items"][0]

    assert policy._precio_estandar(item, date(2026, 8, 29)) is True
    assert policy._precio_estandar({**item, "discount_percentage": 5}, date(2026, 8, 29)) is False
    get_list.return_value = [{**valid, "customer": "CUST-001"}]
    assert policy._precio_estandar(item, date(2026, 8, 29)) is False
    get_list.return_value = [{**valid, "valid_upto": "2026-08-28"}]
    assert policy._precio_estandar(item, date(2026, 8, 29)) is False


def test_debt_check_uses_policy_identity_and_due_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policy, "_hoy_del_negocio", lambda: date(2026, 8, 29))
    report = Mock(
        return_value=[
            {"outstanding_amount": 50, "due_date": "2026-08-28"},
            {"outstanding_amount": 75, "due_date": "2026-08-30"},
        ]
    )
    monkeypatch.setattr(erpnext, "policy_run_report", report)

    assert policy._saldo_vencido("CUST-001") == 50
    report.assert_called_once_with(
        "Accounts Receivable",
        {
            "company": "Lácteos Plus SA",
            "customer": ["CUST-001"],
            "based_on": "Due Date",
            "report_date": "2026-08-29",
        },
    )


def test_policy_delivery_limit_uses_business_date_not_host_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _limites_del_dueno(monkeypatch)
    monkeypatch.setattr(policy, "STOCK_CONFIABLE", True)
    monkeypatch.setattr(policy, "PRICE_LIST", "Standard Selling")
    monkeypatch.setattr(policy, "CURRENCY", "ARS")
    monkeypatch.setattr(policy, "MIN_PEDIDOS", 1)
    monkeypatch.setattr(erpnext, "get_list", Mock(return_value=[{"grand_total": 100}]))
    monkeypatch.setattr(policy, "_saldo_vencido", Mock(return_value=0))
    monkeypatch.setattr(policy, "_hay_stock", Mock(return_value=True))
    monkeypatch.setattr(policy, "_precio_estandar", Mock(return_value=True))
    monkeypatch.setattr(policy, "_hoy_del_negocio", lambda: date(2026, 8, 29))

    decision = policy.evaluar(_order(delivery_date="2026-09-29"))

    assert decision.auto is False
    assert "fecha de entrega muy lejana" in decision.motivos
