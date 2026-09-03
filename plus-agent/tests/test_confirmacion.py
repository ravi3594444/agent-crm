"""The durable confirmation record: ERPNext is the fact, Redis is the cache."""
from __future__ import annotations

import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import confirmacion, erpnext, outbound_status
from tests.fakes import FakeMarcas, listar

SO = "SAL-ORD-2026-00012"


def _fila(contenido: str, creation: str = "2026-09-03 10:00:00") -> dict:
    """One Comment row as ERPNext returns it — including the fields the query
    filters and orders on, so the double answers the real query."""
    return {
        "content": contenido,
        "reference_doctype": "Sales Order",
        "reference_name": SO,
        "creation": creation,
    }


@pytest.fixture
def erp(monkeypatch: pytest.MonkeyPatch):
    marcas = FakeMarcas()
    monkeypatch.setattr(outbound_status, "_client", marcas)
    filas: list[dict] = []

    def registrar_comentario(dt, name, text):
        # A distinct, increasing `creation` per row: ERPNext orders and CUTS by
        # it, and rows that all share one stamp make the cut invisible.
        filas.append(
            {
                "content": text,
                "reference_doctype": dt,
                "reference_name": name,
                "creation": f"2026-09-03 10:00:{len(filas):02d}.000000",
            }
        )

    monkeypatch.setattr(erpnext, "registrar_comentario", registrar_comentario)
    lecturas: list[dict] = []

    def policy_get_list(doctype, filters=None, fields=None, limit=20, parent=None, order_by=None):
        lecturas.append(
            {"doctype": doctype, "filters": filters, "limit": limit, "order_by": order_by}
        )
        return listar(filas, filters, limit=limit, order_by=order_by)

    monkeypatch.setattr(erpnext, "policy_get_list", policy_get_list)
    monkeypatch.delenv("CANCELACION_HORAS", raising=False)
    return {"marcas": marcas, "filas": filas, "lecturas": lecturas}


def test_the_record_names_the_marker_the_time_and_the_source(erp) -> None:
    assert confirmacion.registrar(SO, "automática (política)") is True

    (fila,) = erp["filas"]
    assert fila["content"].startswith(confirmacion.MARCA)
    assert "fuente=automática (política)" in fila["content"]
    # An explicit offset, so the deadline cannot be read in a local timezone.
    assert "+00:00" in fila["content"]


def test_the_timestamp_round_trips_through_erpnext(erp) -> None:
    confirmacion.registrar(SO, "prueba")
    erp["marcas"].values.clear()

    momento = confirmacion.momento(SO)

    assert momento is not None and abs(time.time() - momento) < 60


def test_the_read_is_cached_so_a_second_attempt_costs_no_erpnext_call(erp) -> None:
    confirmacion.registrar(SO, "prueba")
    erp["marcas"].values.clear()
    confirmacion.momento(SO)
    lecturas = len(erp["lecturas"])

    confirmacion.momento(SO)

    assert len(erp["lecturas"]) == lecturas


def test_the_cache_never_invents_a_deadline_of_its_own(erp) -> None:
    """A failed durable write leaves nothing cached: the window stays closed."""
    erp["filas"].clear()

    assert confirmacion.momento(SO) is None
    assert erp["marcas"].values == {}


def test_a_write_erpnext_refuses_is_reported_and_not_cached(erp, monkeypatch) -> None:
    monkeypatch.setattr(
        erpnext, "registrar_comentario", Mock(side_effect=erpnext.ERPNextError("sin permiso"))
    )

    assert confirmacion.registrar(SO, "prueba") is False
    assert confirmacion.momento(SO) is None


def test_the_read_asks_erpnext_for_this_order_only(erp) -> None:
    confirmacion.registrar(SO, "prueba")
    erp["marcas"].values.clear()
    confirmacion.momento(SO)

    (lectura,) = erp["lecturas"]
    assert lectura["doctype"] == "Comment"
    assert ["reference_doctype", "=", "Sales Order"] in lectura["filters"]
    assert ["reference_name", "=", SO] in lectura["filters"]
    assert lectura["order_by"] == "creation asc"


def test_the_earliest_record_wins(erp) -> None:
    viejo = datetime.now(UTC) - timedelta(hours=40)
    erp["filas"].append(
        _fila(f"{confirmacion.MARCA} {confirmacion.sello(viejo)} fuente=primera",
              "2026-09-01 10:00:00")
    )
    erp["filas"].append(
        _fila(f"{confirmacion.MARCA} {confirmacion.sello()} fuente=segunda")
    )

    momento = confirmacion.momento(SO)

    assert momento is not None and (time.time() - momento) / 3600 > 39


def test_a_record_with_no_offset_is_read_as_utc(erp) -> None:
    """The direction that expires the window sooner, never later."""
    erp["filas"].append(
        _fila(f"{confirmacion.MARCA} 2026-09-03 10:00:00 fuente=otro sistema")
    )

    momento = confirmacion.momento(SO)

    assert momento == datetime(2026, 9, 3, 10, 0, 0, tzinfo=UTC).timestamp()


@pytest.mark.parametrize("contenido", ["", "sin marca alguna", "[confirmado-por-agente] ayer"])
def test_anything_unparseable_cannot_prove_a_window(erp, contenido) -> None:
    erp["filas"].append(_fila(contenido))

    assert confirmacion.momento(SO) is None


def test_erpnext_being_unreadable_fails_closed(erp, monkeypatch) -> None:
    monkeypatch.setattr(
        erpnext, "policy_get_list", Mock(side_effect=erpnext.ERPNextError("caído"))
    )

    assert confirmacion.momento(SO) is None


def test_an_empty_order_name_is_never_looked_up(erp) -> None:
    assert confirmacion.momento("") is None
    assert confirmacion.registrar("", "prueba") is False
    assert erp["lecturas"] == []


@pytest.mark.parametrize(
    ("valor", "esperado"), [("48", 48.0), ("0", 24.0), ("-3", 24.0), ("no", 24.0)]
)
def test_the_window_length_is_the_owners_and_never_zero(erp, monkeypatch, valor, esperado) -> None:
    monkeypatch.setenv("CANCELACION_HORAS", valor)
    assert confirmacion.horas_ventana() == esperado
