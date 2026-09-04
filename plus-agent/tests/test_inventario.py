"""La confianza en el inventario se GANA y se VENCE.

`STOCK_CONFIABLE=true` era una promesa escrita una vez en el .env, y el sistema
la cumplía para siempre: prometía stock aunque nadie hubiera contado nada en
tres semanas. En una lechería el número del sistema se despega de la realidad
en horas. Lo que se prueba acá:

  * un producto es confiable sólo si alguien lo CONTÓ y CONFIRMÓ el ajuste
    hace menos de STOCK_CONFIABLE_HORAS;
  * un borrador de conteo no alcanza — es un mensaje de WhatsApp con un número;
  * la confianza es por par (item_code, warehouse): contar la leche no hace
    confiable al queso, ni la leche del depósito de al lado;
  * cualquier duda es "no confiable": nunca se estima una fecha ni una hora.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import erpnext, inventario

ZONA = ZoneInfo("America/Argentina/Buenos_Aires")
AHORA = datetime(2026, 9, 2, 8, 0, tzinfo=ZONA)
DEPOSITO = "Depósito A - LP"


@pytest.fixture(autouse=True)
def reloj_y_maestra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inventario, "_ahora", lambda: AHORA)
    monkeypatch.setenv("STOCK_CONFIABLE", "true")
    monkeypatch.setenv("STOCK_CONFIABLE_HORAS", "24")


def _conteos(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hace_horas: float | None = 1.0,
    item: str = "LECHE-1L",
    deposito: str = DEPOSITO,
    docstatus_renglon: int = 1,
    docstatus_doc: int = 1,
    posting: tuple[str, str] | None = None,
) -> Mock:
    """One confirmed count of `item`, `hace_horas` ago. None = no counts."""
    renglones: list[dict] = []
    documentos: list[dict] = []
    if hace_horas is not None or posting is not None:
        if posting is None:
            momento = AHORA - timedelta(hours=hace_horas or 0)
            posting = (momento.date().isoformat(), momento.strftime("%H:%M:%S"))
        renglones = [
            {
                "parent": "SR-0001",
                "item_code": item,
                "warehouse": deposito,
                "docstatus": docstatus_renglon,
            }
        ]
        documentos = [
            {
                "name": "SR-0001",
                "docstatus": docstatus_doc,
                "posting_date": posting[0],
                "posting_time": posting[1],
            }
        ]

    def policy_get_list(
        doctype, filters=None, fields=None, limit=20, parent=None, order_by=None, start=0
    ):
        if doctype == "Stock Reconciliation Item":
            return [dict(r) for r in renglones]
        if doctype == "Stock Reconciliation":
            return [dict(d) for d in documentos]
        return []

    lector = Mock(side_effect=policy_get_list)
    monkeypatch.setattr(erpnext, "policy_get_list", lector)
    return lector


def test_a_product_counted_and_confirmed_this_morning_is_trustworthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _conteos(monkeypatch, hace_horas=1)

    assert inventario.confiable("LECHE-1L", DEPOSITO) == (True, "")


def test_a_product_nobody_ever_counted_is_not_trustworthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _conteos(monkeypatch, hace_horas=None)

    confiable, motivo = inventario.confiable("LECHE-1L", DEPOSITO)

    assert confiable is False
    assert "nadie confirmó un conteo" in motivo


def test_trust_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """This is the whole point: yesterday's count does not vouch for today's
    fridge, and the reason says how old it is."""
    _conteos(monkeypatch, hace_horas=40)

    confiable, motivo = inventario.confiable("LECHE-1L", DEPOSITO)

    assert confiable is False
    assert "hace 40 h" in motivo
    assert "vale 24 h" in motivo


def test_the_window_holds_at_its_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    _conteos(monkeypatch, hace_horas=24)
    assert inventario.confiable("LECHE-1L", DEPOSITO)[0] is True

    _conteos(monkeypatch, hace_horas=24.02)  # ~1 minute past
    assert inventario.confiable("LECHE-1L", DEPOSITO)[0] is False


def test_the_owner_can_shorten_or_lengthen_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _conteos(monkeypatch, hace_horas=10)

    monkeypatch.setenv("STOCK_CONFIABLE_HORAS", "8")
    assert inventario.confiable("LECHE-1L", DEPOSITO)[0] is False

    monkeypatch.setenv("STOCK_CONFIABLE_HORAS", "12")
    assert inventario.confiable("LECHE-1L", DEPOSITO)[0] is True


def test_a_draft_count_is_not_a_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A draft Stock Reconciliation is somebody's WhatsApp message with a
    number in it. Until a person confirms it, nothing is trustworthy."""
    _conteos(monkeypatch, hace_horas=1, docstatus_renglon=0, docstatus_doc=0)

    confiable, motivo = inventario.confiable("LECHE-1L", DEPOSITO)

    assert confiable is False
    assert "nadie confirmó un conteo" in motivo


def test_counting_the_milk_says_nothing_about_the_cheese(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trust is per (item_code, warehouse) pair on purpose: one global flag
    would let a count of milk vouch for every product in every warehouse."""
    _conteos(monkeypatch, hace_horas=1, item="LECHE-1L")

    assert inventario.confiable("LECHE-1L", DEPOSITO)[0] is True
    assert inventario.confiable("QUE-CRE", DEPOSITO)[0] is False


def test_a_count_in_another_warehouse_does_not_travel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _conteos(monkeypatch, hace_horas=1, deposito="Depósito B - LP")

    assert inventario.confiable("LECHE-1L", DEPOSITO)[0] is False


def test_the_master_switch_still_turns_everything_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """However fresh the count, STOCK_CONFIABLE=false means promise nothing.
    It is the way to shut the whole thing down in one move."""
    _conteos(monkeypatch, hace_horas=0.1)
    monkeypatch.setenv("STOCK_CONFIABLE", "false")

    confiable, motivo = inventario.confiable("LECHE-1L", DEPOSITO)

    assert confiable is False
    assert "no confiable" in motivo


@pytest.mark.parametrize("horas", ["0", "-5", "muchas", ""])
def test_a_nonsense_window_authorises_nothing(
    monkeypatch: pytest.MonkeyPatch, horas: str
) -> None:
    _conteos(monkeypatch, hace_horas=0.1)
    monkeypatch.setenv("STOCK_CONFIABLE_HORAS", horas)

    assert inventario.confiable("LECHE-1L", DEPOSITO)[0] is False


def test_a_count_dated_in_the_future_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clock that is wrong is not evidence of anything."""
    _conteos(monkeypatch, hace_horas=-3)

    confiable, motivo = inventario.confiable("LECHE-1L", DEPOSITO)

    assert confiable is False
    assert "futuro" in motivo


@pytest.mark.parametrize(
    "posting",
    [("", "07:15:00"), ("no es fecha", "07:15:00"), ("2026-09-02", "no es hora")],
)
def test_a_date_that_cannot_be_read_is_not_a_recent_count(
    monkeypatch: pytest.MonkeyPatch, posting: tuple[str, str]
) -> None:
    _conteos(monkeypatch, posting=posting)

    assert inventario.confiable("LECHE-1L", DEPOSITO)[0] is False


def test_an_erpnext_that_cannot_be_read_fails_closed_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The customer agent calls this on the way to answering a customer: it
    must degrade into "I am not promising", never into an exception."""
    monkeypatch.setattr(
        erpnext,
        "policy_get_list",
        Mock(side_effect=erpnext.ERPNextError("ERPNext no disponible")),
    )

    confiable, motivo = inventario.confiable("LECHE-1L", DEPOSITO)

    assert confiable is False
    assert "no pude verificar el último conteo" in motivo


def test_the_newest_confirmed_count_is_the_one_that_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Several counts of the same product: the most recent decides, not the
    first row ERPNext happens to return."""
    viejo = AHORA - timedelta(hours=40)
    nuevo = AHORA - timedelta(hours=2)

    def policy_get_list(
        doctype, filters=None, fields=None, limit=20, parent=None, order_by=None, start=0
    ):
        if doctype == "Stock Reconciliation Item":
            return [
                {"parent": "SR-VIEJO", "item_code": "LECHE-1L", "warehouse": DEPOSITO, "docstatus": 1},
                {"parent": "SR-NUEVO", "item_code": "LECHE-1L", "warehouse": DEPOSITO, "docstatus": 1},
            ]
        return [
            {
                "name": "SR-VIEJO",
                "docstatus": 1,
                "posting_date": viejo.date().isoformat(),
                "posting_time": viejo.strftime("%H:%M:%S"),
            },
            {
                "name": "SR-NUEVO",
                "docstatus": 1,
                "posting_date": nuevo.date().isoformat(),
                "posting_time": nuevo.strftime("%H:%M:%S"),
            },
        ]

    monkeypatch.setattr(erpnext, "policy_get_list", Mock(side_effect=policy_get_list))

    assert inventario.confiable("LECHE-1L", DEPOSITO)[0] is True


def test_the_counts_are_read_with_the_policy_identity_and_name_their_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frappe refuses to list a child doctype without its parent, and reading
    every count in the company is a policy read, not a customer one."""
    lector = _conteos(monkeypatch, hace_horas=1)

    inventario.confiable("LECHE-1L", DEPOSITO)

    renglones = lector.call_args_list[0]
    assert renglones.args[0] == "Stock Reconciliation Item"
    assert renglones.kwargs["parent"] == "Stock Reconciliation"
    assert renglones.kwargs["order_by"] == "modified desc"
    assert ["docstatus", "=", 1] in renglones.kwargs["filters"]
    assert ["item_code", "=", "LECHE-1L"] in renglones.kwargs["filters"]
    assert ["warehouse", "=", DEPOSITO] in renglones.kwargs["filters"]
    assert lector.call_args_list[1].args[0] == "Stock Reconciliation"


def test_no_item_or_no_warehouse_is_never_trustworthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _conteos(monkeypatch, hace_horas=1)

    assert inventario.confiable("", DEPOSITO)[0] is False
    assert inventario.confiable("LECHE-1L", "")[0] is False


def test_the_window_is_read_on_every_call_not_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner changes it and it applies to the next order, like every other
    limit. Nothing is frozen at import time."""
    os.environ["STOCK_CONFIABLE_HORAS"] = "48"
    try:
        assert inventario.horas_de_validez() == 48.0
        os.environ["STOCK_CONFIABLE_HORAS"] = "6"
        assert inventario.horas_de_validez() == 6.0
    finally:
        os.environ.pop("STOCK_CONFIABLE_HORAS", None)
