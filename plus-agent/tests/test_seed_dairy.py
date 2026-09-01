from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import Mock, call

import pytest


os.environ.setdefault("ERPNEXT_URL", "http://erpnext.test")
os.environ.setdefault("ERPNEXT_API_KEY", "test-key")
os.environ.setdefault("ERPNEXT_API_SECRET", "test-secret")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import seed_dairy as seed  # noqa: E402


@pytest.fixture(autouse=True)
def blocked_erpnext_network(monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    """Fail any ERPNext boundary that a test did not explicitly replace."""
    boundaries = {}
    for name in (
        "get_list",
        "get_doc",
        "create_doc",
        "default_context",
        "submit_doc",
    ):
        boundary = Mock(side_effect=AssertionError(f"unexpected ERPNext call: {name}"))
        monkeypatch.setattr(seed.erpnext, name, boundary)
        boundaries[name] = boundary
    return boundaries


def _item(
    code: str = "LEC-ENT-1L",
    warehouse: str = "Productos Terminados - LP",
    qty: object = 5,
    valuation_rate: object = 720,
) -> dict:
    return {
        "item_code": code,
        "warehouse": warehouse,
        "qty": qty,
        "valuation_rate": valuation_rate,
    }


def test_opening_account_is_selected_by_structural_type(
    blocked_erpnext_network: dict[str, Mock],
) -> None:
    get_list = blocked_erpnext_network["get_list"]
    get_list.side_effect = None
    get_list.return_value = [{"name": "Apertura Transitoria - LP"}]
    items = [_item()]

    payload = seed._stock_reconciliation_payload("Lácteos Plus SA", items)

    assert payload == {
        "company": "Lácteos Plus SA",
        "purpose": "Opening Stock",
        "expense_account": "Apertura Transitoria - LP",
        "items": items,
    }
    get_list.assert_called_once_with(
        "Account",
        filters=[
            ["company", "=", "Lácteos Plus SA"],
            ["is_group", "=", 0],
            ["account_type", "=", "Temporary"],
        ],
        fields=["name"],
        limit=1,
    )
    assert "account_name" not in repr(get_list.call_args)


def test_fallback_stock_adjustment_is_also_selected_by_type(
    blocked_erpnext_network: dict[str, Mock],
) -> None:
    get_list = blocked_erpnext_network["get_list"]
    get_list.side_effect = [[], [{"name": "Ajustes de Inventario - LP"}]]
    items = [_item()]

    payload = seed._stock_reconciliation_payload("Lácteos Plus SA", items)

    assert payload == {
        "company": "Lácteos Plus SA",
        "purpose": "Stock Reconciliation",
        "expense_account": "Ajustes de Inventario - LP",
        "items": items,
    }
    assert get_list.call_args_list == [
        call(
            "Account",
            filters=[
                ["company", "=", "Lácteos Plus SA"],
                ["is_group", "=", 0],
                ["account_type", "=", "Temporary"],
            ],
            fields=["name"],
            limit=1,
        ),
        call(
            "Account",
            filters=[
                ["company", "=", "Lácteos Plus SA"],
                ["is_group", "=", 0],
                ["account_type", "=", "Stock Adjustment"],
            ],
            fields=["name"],
            limit=1,
        ),
    ]
    assert "account_name" not in repr(get_list.call_args_list)


def test_existing_reconciliation_matches_numbers_and_item_order(
    blocked_erpnext_network: dict[str, Mock],
) -> None:
    expected = [
        _item("LEC-ENT-1L", qty=5, valuation_rate=720),
        _item("QUE-CRE", qty=2, valuation_rate=5880),
    ]
    get_list = blocked_erpnext_network["get_list"]
    get_list.side_effect = None
    get_list.return_value = [{"name": "MAT-RECO-2026-00001", "docstatus": 1}]
    get_doc = blocked_erpnext_network["get_doc"]
    get_doc.side_effect = None
    get_doc.return_value = {
        "name": "MAT-RECO-2026-00001",
        "docstatus": 1,
        "items": [
            _item("QUE-CRE", qty="2.000", valuation_rate="5880.00"),
            _item("LEC-ENT-1L", qty="5.0", valuation_rate="720.000"),
        ],
    }

    assert seed._existing_stock_reconciliation(
        "Lácteos Plus SA", expected
    ) == ("MAT-RECO-2026-00001", 1)
    get_list.assert_called_once_with(
        "Stock Reconciliation",
        filters=[
            ["company", "=", "Lácteos Plus SA"],
            ["docstatus", "!=", 2],
        ],
        fields=["name", "docstatus"],
        limit=500,
    )


def test_main_reuses_matching_reconciliation_and_never_submits(
    monkeypatch: pytest.MonkeyPatch,
    blocked_erpnext_network: dict[str, Mock],
) -> None:
    monkeypatch.setattr(seed, "_ensure", Mock())
    monkeypatch.setattr(seed, "PRODUCTOS", [])
    monkeypatch.setattr(seed, "CLIENTES", [])
    monkeypatch.setattr(seed, "STOCK_INICIAL", {"LEC-ENT-1L": 5})
    monkeypatch.setattr(
        seed,
        "_existing_stock_reconciliation",
        Mock(return_value=("MAT-RECO-2026-00001", 0)),
    )
    context = blocked_erpnext_network["default_context"]
    context.side_effect = None
    context.return_value = ("Lácteos Plus SA", "Productos Terminados - LP")

    seed.main()

    blocked_erpnext_network["create_doc"].assert_not_called()
    blocked_erpnext_network["submit_doc"].assert_not_called()


def test_main_creates_only_a_draft_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    blocked_erpnext_network: dict[str, Mock],
) -> None:
    monkeypatch.setattr(seed, "_ensure", Mock())
    monkeypatch.setattr(seed, "PRODUCTOS", [])
    monkeypatch.setattr(seed, "CLIENTES", [])
    monkeypatch.setattr(seed, "STOCK_INICIAL", {"LEC-ENT-1L": 5})
    monkeypatch.setattr(seed, "_existing_stock_reconciliation", Mock(return_value=None))
    payload = {
        "company": "Lácteos Plus SA",
        "purpose": "Opening Stock",
        "expense_account": "Apertura Transitoria - LP",
        "items": [_item(qty=5, valuation_rate=1)],
    }
    monkeypatch.setattr(
        seed, "_stock_reconciliation_payload", Mock(return_value=payload)
    )
    context = blocked_erpnext_network["default_context"]
    context.side_effect = None
    context.return_value = ("Lácteos Plus SA", "Productos Terminados - LP")
    create_doc = blocked_erpnext_network["create_doc"]
    create_doc.side_effect = None
    create_doc.return_value = {"name": "MAT-RECO-2026-00002"}

    seed.main()

    create_doc.assert_called_once_with("Stock Reconciliation", payload)
    assert payload.get("docstatus", 0) == 0
    blocked_erpnext_network["submit_doc"].assert_not_called()
