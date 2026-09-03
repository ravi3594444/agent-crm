"""The ERPNext list double has to answer the query it was ASKED.

Every durable read in this system is a Frappe list query with a `limit` and an
`order_by`, and the production code depends on both: "the newest event on this
order" is `creation desc` with a limit of one page, and "rebuild the index" is a
bounded page of history. A double that returned every matching row in insertion
order made those two calls indistinguishable, so a reconstruction that only ever
saw the OLDEST page could not be written down as a failing test — the bug was
invisible in the suite and visible only in production.

So the double is tested like production code: filter, then ORDER, then CUT.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fakes import listar

FILAS = [
    {"content": "[solicitud] a", "reference_doctype": "Sales Order", "reference_name": "SO-1", "creation": "2026-09-03 10:00:01"},
    {"content": "[solicitud] b", "reference_doctype": "Sales Order", "reference_name": "SO-2", "creation": "2026-09-03 10:00:02"},
    {"content": "[limite] c", "reference_doctype": "Company", "reference_name": "ACME", "creation": "2026-09-03 10:00:03"},
    {"content": "[solicitud] d", "reference_doctype": "Sales Order", "reference_name": "SO-1", "creation": "2026-09-03 10:00:04"},
]


def _contenidos(filas: list[dict]) -> list[str]:
    return [f["content"] for f in filas]


@pytest.mark.parametrize(
    "filtro, esperado",
    [
        ([["reference_name", "=", "SO-1"]], ["[solicitud] a", "[solicitud] d"]),
        ([["reference_name", "in", ["SO-1", "SO-2"]]], ["[solicitud] a", "[solicitud] b", "[solicitud] d"]),
        ([["reference_name", "not in", ["SO-1"]]], ["[solicitud] b", "[limite] c"]),
        ([["reference_doctype", "!=", "Sales Order"]], ["[limite] c"]),
        ([["content", "like", "%[limite]%"]], ["[limite] c"]),
        (
            [["reference_doctype", "=", "Sales Order"], ["content", "like", "%[solicitud]%"]],
            ["[solicitud] a", "[solicitud] b", "[solicitud] d"],
        ),
    ],
)
def test_the_filters_the_application_actually_sends_are_honoured(filtro, esperado) -> None:
    assert _contenidos(listar(FILAS, filtro, limit=100)) == esperado


def test_order_by_creation_asc_is_oldest_first_and_desc_is_newest_first() -> None:
    assert _contenidos(listar(FILAS, limit=100, order_by="creation asc")) == [
        "[solicitud] a", "[solicitud] b", "[limite] c", "[solicitud] d"
    ]
    assert _contenidos(listar(FILAS, limit=100, order_by="creation desc")) == [
        "[solicitud] d", "[limite] c", "[solicitud] b", "[solicitud] a"
    ]


def test_rows_sharing_a_stamp_still_put_the_last_written_first_under_desc() -> None:
    """Real events can land in the same second. "Newest first" must not degrade
    into "insertion order", or a desc query silently returns the oldest row."""
    empatadas = [
        {"content": "primera", "creation": "2026-09-03 10:00:00"},
        {"content": "segunda", "creation": "2026-09-03 10:00:00"},
        {"content": "tercera", "creation": "2026-09-03 10:00:00"},
    ]
    assert _contenidos(listar(empatadas, limit=100, order_by="creation desc")) == [
        "tercera", "segunda", "primera"
    ]
    assert _contenidos(listar(empatadas, limit=100, order_by="creation asc")) == [
        "primera", "segunda", "tercera"
    ]


def test_limit_truncates_and_says_so_by_returning_exactly_that_many() -> None:
    assert len(listar(FILAS, limit=2)) == 2
    assert len(listar(FILAS, limit=99)) == len(FILAS)


def test_the_cut_happens_AFTER_the_ordering_which_is_the_whole_point() -> None:
    """Cutting before sorting would hand back the oldest rows for a `desc`
    query — exactly the answer that hid the reconstruction bug."""
    assert _contenidos(listar(FILAS, limit=1, order_by="creation desc")) == ["[solicitud] d"]
    assert _contenidos(listar(FILAS, limit=1, order_by="creation asc")) == ["[solicitud] a"]


def test_the_cut_happens_after_the_FILTER_too() -> None:
    """A limit of 2 over SO-1 means two SO-1 rows, not two rows of history that
    happen to contain one SO-1."""
    filas = listar(FILAS, [["reference_name", "=", "SO-1"]], limit=2, order_by="creation desc")
    assert _contenidos(filas) == ["[solicitud] d", "[solicitud] a"]


def test_an_order_by_the_double_cannot_honour_is_an_error_not_a_silent_pass() -> None:
    """Silently ignoring an unknown ordering is how a fake starts lying again."""
    with pytest.raises(AssertionError):
        listar(FILAS, limit=10, order_by="creation asc, name desc")


def test_the_rows_handed_back_are_copies() -> None:
    """ERPNext returns JSON, not references. A test that mutates a result must
    not rewrite the stored history."""
    (fila,) = listar(FILAS, [["reference_name", "=", "SO-2"]], limit=1)
    fila["content"] = "manoseado"
    assert FILAS[1]["content"] == "[solicitud] b"


# ---------------------------------------------------------------------------
# The suite has to BE the suite.
# ---------------------------------------------------------------------------


def test_no_test_in_this_suite_is_shadowed_by_another() -> None:
    """Two functions with one name in a module: the second wins, the first is
    dead, and nothing says so.

    It happened. `test_an_unreadable_order_is_never_read_as_a_live_draft` was
    defined twice in test_solicitudes.py, so one of the invariants about closing
    a draft nobody had proven released never ran — and removing the duplicate is
    what turned up a real surviving defect. ruff cannot catch it: tests/* ignores
    F811 on purpose, because a fixture used as a test argument reads as a
    redefinition.

    So the suite checks itself. This is about silent LOSS of coverage, which is
    the same failure as a test that is never executed at all.
    """
    import ast

    tests = Path(__file__).resolve().parent
    duplicados: list[str] = []
    for archivo in sorted(tests.glob("*.py")):
        arbol = ast.parse(archivo.read_text(), filename=str(archivo))
        vistos: set[str] = set()
        for nodo in arbol.body:
            if not isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if nodo.name in vistos:
                duplicados.append(f"{archivo.name}:{nodo.lineno} {nodo.name}")
            vistos.add(nodo.name)

    assert duplicados == [], "definiciones que tapan a otra: " + ", ".join(duplicados)
