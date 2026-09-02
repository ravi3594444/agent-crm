"""Payload shapes ERPNext v15/v16 actually accepts, verified against a live site."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import erpnext, policy, whatsapp  # noqa: E402
from app.tools import captura, pedidos  # noqa: E402


@pytest.fixture(autouse=True)
def _context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERPNEXT_COMPANY", "Lacteos Test SA")
    monkeypatch.setenv("ERPNEXT_WAREHOUSE", "Principal - LT")
    monkeypatch.setenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires")
    monkeypatch.setattr(erpnext, "add_comment", Mock())


def _unregistered_config() -> dict:
    return {
        "configurable": {
            "thread_id": "cli:t",
            "actor_scope": "customer",
            "customer_code": "",
            "actor_phone": "5491100000009",
            "inbound_message_id": "wamid.lead-001",
        }
    }


def test_crear_lead_sends_notes_as_child_table_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(erpnext, "get_list", Mock(return_value=[]))
    create = Mock(return_value={"name": "CRM-LEAD-2026-00001"})
    monkeypatch.setattr(erpnext, "create_doc", create)
    result = pedidos.crear_lead.invoke(
        {"nombre": "Almacén Nuevo", "nota": "quiere leche"},
        config=_unregistered_config(),
    )

    assert "CRM-LEAD-2026-00001" in result
    doctype, payload = create.call_args.args
    assert doctype == "Lead"
    assert payload["lead_name"] == "Almacén Nuevo"
    assert payload["mobile_no"] == "5491100000009"
    assert "source" not in payload  # field does not exist on v16 Lead
    assert isinstance(payload["notes"], list) and len(payload["notes"]) == 1
    note = payload["notes"][0]["note"]
    assert "WhatsApp" in note and "quiere leche" in note
    assert "Referencia segura: WA-" in note
    assert "wamid.lead-001" not in note  # only the hashed reference


def test_offline_sale_invoice_has_company_and_warehouses(monkeypatch: pytest.MonkeyPatch) -> None:
    create = Mock(return_value={"name": "ACC-SINV-2026-00001"})
    monkeypatch.setattr(erpnext, "create_doc", create)

    result = captura.registrar_venta_offline.invoke(
        {
            "cliente": "Almacen Don Jose",
            "lineas": [{"item_code": "LEC-ENT-1L", "cantidad": 20}, {"item_code": "QUE-CRE", "cantidad": 2.5, "precio_unitario": 9000}],
            "cobrado": False,
            "nota": "reparto",
        }
    )

    assert "ACC-SINV-2026-00001" in result
    doctype, payload = create.call_args.args
    assert doctype == "Sales Invoice"
    assert payload["company"] == "Lacteos Test SA"
    assert payload["set_warehouse"] == "Principal - LT"
    assert payload["update_stock"] == 1
    assert payload["is_pos"] == 0
    assert payload["set_posting_time"] == 1
    assert payload["posting_date"] == policy._hoy_del_negocio().isoformat()
    assert all(item["warehouse"] == "Principal - LT" for item in payload["items"])
    assert payload["items"][1]["rate"] == 9000
    assert "Pendiente de cobro" in payload["remarks"]


def test_stock_count_without_change_does_not_create_a_document(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(erpnext, "get_list", Mock(return_value=[{"actual_qty": 12}]))
    create = Mock(side_effect=AssertionError("ERPNext rechaza una reconciliación sin cambios"))
    monkeypatch.setattr(erpnext, "create_doc", create)

    result = captura.contar_stock.invoke({"item_code": "QUE-CRE", "cantidad_real": 12})

    assert "no hace falta" in result
    create.assert_not_called()


def test_stock_count_payload_has_company_and_business_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(erpnext, "get_list", Mock(return_value=[{"actual_qty": 15}]))
    create = Mock(return_value={"name": "MAT-RECO-2026-00001"})
    monkeypatch.setattr(erpnext, "create_doc", create)

    result = captura.contar_stock.invoke({"item_code": "QUE-CRE", "cantidad_real": 12})

    assert "faltan 3" in result
    doctype, payload = create.call_args.args
    assert doctype == "Stock Reconciliation"
    assert payload["company"] == "Lacteos Test SA"
    assert payload["set_posting_time"] == 1
    assert payload["posting_date"] == policy._hoy_del_negocio().isoformat()
    assert payload["items"] == [{"item_code": "QUE-CRE", "warehouse": "Principal - LT", "qty": 12}]


def test_delivery_note_carries_company_and_item_warehouses(monkeypatch: pytest.MonkeyPatch) -> None:
    so = {
        "name": "SAL-ORD-2026-00006",
        "docstatus": 1,
        "company": "Lacteos Test SA",
        "customer": "Almacen Don Jose",
        "items": [
            {"name": "row1", "item_code": "QUE-CRE", "qty": 5, "warehouse": "Principal - LT"},
            {"name": "row2", "item_code": "MAN-200", "qty": 2},
        ],
    }
    monkeypatch.setattr(erpnext, "get_doc", Mock(return_value=so))
    create = Mock(return_value={"name": "MAT-DN-2026-00001"})
    monkeypatch.setattr(erpnext, "create_doc", create)

    result = captura.confirmar_entrega.invoke({"numero_pedido": "SAL-ORD-2026-00006"})

    assert "MAT-DN-2026-00001" in result
    doctype, payload = create.call_args.args
    assert doctype == "Delivery Note"
    assert payload["company"] == "Lacteos Test SA"
    assert payload["set_posting_time"] == 1
    assert [i["warehouse"] for i in payload["items"]] == ["Principal - LT", "Principal - LT"]
    assert [i["so_detail"] for i in payload["items"]] == ["row1", "row2"]


def test_delivery_note_refuses_a_draft_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(erpnext, "get_doc", Mock(return_value={"docstatus": 0, "customer": "X", "items": []}))
    create = Mock()
    monkeypatch.setattr(erpnext, "create_doc", create)
    assert "borrador" in captura.confirmar_entrega.invoke({"numero_pedido": "SAL-ORD-1"})
    create.assert_not_called()


def _response(status: int, body) -> Mock:
    response = Mock(status_code=status)
    response.json.return_value = body
    return response


def test_credential_check_reports_meta_rejection_without_leaking_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Mock()
    client.get.return_value = _response(401, {"error": {"code": 190, "message": "Error validating access token"}})
    monkeypatch.setattr(whatsapp, "_client", client)

    ok, detail = whatsapp.verificar_credenciales()

    assert ok is False
    assert "190" in detail and "System User" in detail
    assert whatsapp.PHONE_ID not in detail


def test_credential_check_succeeds_and_survives_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Mock()
    client.get.return_value = _response(200, {"id": "1", "quality_rating": "GREEN", "display_phone_number": "+54 9 11"})
    monkeypatch.setattr(whatsapp, "_client", client)
    ok, detail = whatsapp.verificar_credenciales()
    assert ok is True and "GREEN" in detail and "+54" not in detail

    client.get.side_effect = httpx.ConnectError("boom")
    ok, detail = whatsapp.verificar_credenciales()
    assert ok is False and "ConnectError" in detail


def test_graph_api_version_is_configurable() -> None:
    assert whatsapp._client.base_url.host == "graph.facebook.com"
    assert str(whatsapp._client.base_url).rstrip("/").endswith(whatsapp.GRAPH_VERSION)


def _meta_response(status: int, body=None, headers=None) -> Mock:
    response = Mock(status_code=status, headers=headers or {})
    response.json.return_value = body if body is not None else {}
    return response


@pytest.mark.parametrize(
    ("status", "code", "permanent"),
    [
        (401, 190, True),        # expired/invalid access token
        (400, 131030, True),     # recipient not in allowed list
        (400, 131047, True),     # 24-hour window closed
        (400, 132001, True),     # template does not exist
        (400, 130429, False),    # rate limit hit
        (400, 131048, False),    # spam rate limit
        (429, 4, False),         # API too many calls
        (500, None, False),
        (503, 131016, False),    # service unavailable
    ],
)
def test_send_errors_are_classified_and_never_carry_the_recipient(
    monkeypatch: pytest.MonkeyPatch, status: int, code, permanent: bool
) -> None:
    client = Mock()
    client.post.return_value = _meta_response(
        status,
        {"error": {"code": code, "message": "recipient +5491100000001 rejected"}},
        {"x-fb-request-id": "req-1", "retry-after": "30"},
    )
    monkeypatch.setattr(whatsapp, "_client", client)

    with pytest.raises(whatsapp.WhatsAppSendError) as caught:
        whatsapp.enviar_mensaje("5491100000001", "hola")

    error = caught.value
    assert error.permanent is permanent
    assert error.status_code == status
    assert error.error_code == code
    assert error.request_id == "req-1"
    assert error.retry_after == 30.0
    assert "5491100000001" not in str(error)
    assert "rejected" not in str(error)


def test_transport_errors_become_transient_send_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Mock()
    client.post.side_effect = httpx.ConnectTimeout("timed out")
    monkeypatch.setattr(whatsapp, "_client", client)

    with pytest.raises(whatsapp.WhatsAppSendError) as caught:
        whatsapp.enviar_mensaje("5491100000001", "hola")

    assert caught.value.permanent is False
    assert caught.value.status_code is None
    assert "ConnectTimeout" in str(caught.value)


def test_es_permanente_matrix() -> None:
    assert whatsapp.es_permanente(401, 190) is True
    assert whatsapp.es_permanente(400, None) is True
    assert whatsapp.es_permanente(400, 130429) is False
    assert whatsapp.es_permanente(429, None) is False
    assert whatsapp.es_permanente(502, None) is False
