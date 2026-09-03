"""cancelar <pedido> <motivo> and despreparar <pedido>: one test per rule.

The twelve cancellation rules are numbered in the comments below. The tests
after them cover the two repairs this command needed before release: a
cancellation window that survives a Redis flush or a restart (the deadline is
an ERPNext record, not a Redis string), and a prepared order that can be
cancelled without anything being deleted behind the manager's back.
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import aprobacion, confirmacion, decisiones, erpnext, outbound_status
from tests.fakes import FakeMarcas

STAFF = "5493511111111"
SO = "SAL-ORD-2026-00009"
CUSTOMER_PHONE = "5493512222222"
REMITO = "MAT-DN-1"


def _remito_del_agente(nombre: str = REMITO) -> dict:
    """A draft Delivery Note exactly as decisiones.preparar leaves it."""
    return {
        "name": nombre,
        "docstatus": 0,
        "customer": "CUST-001",
        "company": "Lacteos Test SA",
        "remarks": (
            f"{decisiones.MARCA_REMITO_AGENTE} Preparado por WhatsApp por un "
            f"integrante autorizado ({STAFF})."
        ),
        "items": [
            {
                "item_code": "LECHE-1L",
                "qty": 5,
                "against_sales_order": SO,
                "so_detail": "row-1",
            }
        ],
    }


@pytest.fixture
def mundo(monkeypatch: pytest.MonkeyPatch):
    """A confirmed order this system confirmed a minute ago, no linked docs."""
    marcas = FakeMarcas()
    monkeypatch.setattr(outbound_status, "_client", marcas)
    estado = {
        "so": {
            "name": SO,
            "docstatus": 1,
            "status": "To Deliver and Bill",
            "customer": "CUST-001",
            "company": "Lacteos Test SA",
            "items": [{"item_code": "LECHE-1L", "qty": 5, "name": "row-1"}],
        },
        "vinculados": [],
        "remitos": {},
        "locks": [],
        "durables": [],
        "borrados": [],
    }

    # The durable confirmation record: written here, read back by
    # app/confirmacion.py exactly as it would be from ERPNext.
    def registrar_comentario(doctype, name, text):
        estado["durables"].append({"content": text, "creation": text[:19]})

    monkeypatch.setattr(erpnext, "registrar_comentario", registrar_comentario)

    @contextmanager
    def lock(nombre, **kwargs):
        estado["locks"].append(nombre)
        yield

    monkeypatch.setattr(decisiones, "distributed_lock", lock)
    monkeypatch.setattr(decisiones, "es_equipo", lambda phone: phone == STAFF)
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: phone == STAFF)

    def leer(doctype, name):
        if doctype == "Sales Order":
            return dict(estado["so"])
        if doctype == "Delivery Note":
            return dict(estado["remitos"][name])
        return {"name": name, "mobile_no": CUSTOMER_PHONE}

    monkeypatch.setattr(decisiones, "_leer_doc", leer)

    def policy_get_list(doctype, filters=None, fields=None, limit=20, parent=None, order_by=None):
        if doctype == "Comment":
            return list(estado["durables"])
        campo = "against_sales_order" if doctype == "Delivery Note Item" else "sales_order"
        return [
            {"parent": n, campo: SO, "docstatus": d}
            for (tipo, n, d) in estado["vinculados"]
            if tipo == doctype
        ]

    monkeypatch.setattr(erpnext, "policy_get_list", policy_get_list)
    cancelados: list[str] = []

    def policy_cancel_doc(doctype, name):
        cancelados.append(name)
        estado["so"]["docstatus"] = 2
        return {"name": name, "docstatus": 2}

    monkeypatch.setattr(erpnext, "policy_cancel_doc", policy_cancel_doc)

    def policy_delete_doc(doctype, name):
        assert doctype == "Delivery Note", f"no se borra un {doctype}"
        assert int(estado["remitos"][name].get("docstatus") or 0) == 0
        estado["borrados"].append(name)
        estado["remitos"].pop(name)
        estado["vinculados"] = [v for v in estado["vinculados"] if v[1] != name]

    monkeypatch.setattr(erpnext, "policy_delete_doc", policy_delete_doc)
    for prohibido in ("get_doc", "submit_doc", "policy_update_status"):
        monkeypatch.setattr(
            erpnext, prohibido, Mock(side_effect=AssertionError(f"{prohibido} no debe usarse"))
        )
    comentarios: list[str] = []
    monkeypatch.setattr(erpnext, "add_comment", lambda dt, name, text: comentarios.append(text))
    todos: list[dict] = []

    def create_doc(doctype, payload):
        # The only agent-identity write the cancellation path may cause is the
        # follow-up ToDo opened by outbound_status.registrar_aviso_fallido.
        assert doctype == "ToDo", f"create_doc({doctype}) no debe usarse al cancelar"
        todos.append(payload)
        return {"name": f"TD-{len(todos)}"}

    monkeypatch.setattr(erpnext, "create_doc", create_doc)
    enviados: list[tuple[str, str]] = []
    import app.whatsapp as whatsapp

    monkeypatch.setattr(
        whatsapp,
        "enviar_mensaje",
        lambda tel, texto: enviados.append((tel, texto))
        or {"messages": [{"id": f"wamid.{len(enviados)}"}]},
    )
    monkeypatch.setattr(whatsapp, "enviar_plantilla", Mock(side_effect=AssertionError("sin plantilla")))
    monkeypatch.setattr(decisiones, "window_open", lambda tel: tel == CUSTOMER_PHONE)
    monkeypatch.delenv("WHATSAPP_CUSTOMER_CANCELLED_TEMPLATE", raising=False)
    monkeypatch.delenv("CANCELACION_HORAS", raising=False)

    mundo = {
        "estado": estado,
        "marcas": marcas,
        "cancelados": cancelados,
        "comentarios": comentarios,
        "enviados": enviados,
        "todos": todos,
    }

    def confirmado_hace(horas: float) -> None:
        """Rewrite the durable record, and drop the cache the way a flush does."""
        estado["durables"].clear()
        marcas.values.pop(confirmacion._clave_cache(SO), None)
        momento = datetime.now(UTC) - timedelta(hours=horas)
        estado["durables"].append(
            {
                "content": f"{confirmacion.MARCA} {confirmacion.sello(momento)} fuente=prueba",
                "creation": "2026-09-03 10:00:00",
            }
        )

    def preparar_remito(doc: dict | None = None) -> str:
        doc = doc or _remito_del_agente()
        estado["remitos"][doc["name"]] = doc
        estado["vinculados"].append(("Delivery Note Item", doc["name"], 0))
        return doc["name"]

    mundo["confirmado_hace"] = confirmado_hace
    mundo["preparar_remito"] = preparar_remito

    confirmacion.registrar(SO, "automática (política)")
    return mundo


def _cancelar(motivo="el cliente se arrepintió", por=STAFF):
    return aprobacion.manejar_boton(f"cancelar:{SO}:{motivo}", por)


def _despreparar(por=STAFF):
    return aprobacion.manejar_boton(f"despreparar:{SO}", por)


# 1. only staff
def test_only_staff_phones_can_cancel(mundo) -> None:
    assert "permiso" in _cancelar(por="5490000000000")
    assert mundo["cancelados"] == []


def test_the_decision_rechecks_the_phone_itself(mundo) -> None:
    """Even if a caller skipped the router, the function refuses."""
    resultado = decisiones.cancelar(SO, "5490000000000", "motivo válido")
    assert resultado["ok"] is False and "permiso" in resultado["detalle"]
    assert mundo["cancelados"] == []


# 7. reason required
@pytest.mark.parametrize("motivo", ["", "  ", "ok"])
def test_a_reason_is_required(mundo, motivo) -> None:
    assert "Falta el motivo" in _cancelar(motivo)
    assert mundo["cancelados"] == []


# 2. submitted, within the window
def test_a_confirmed_order_inside_the_window_is_cancelled_and_audited(mundo) -> None:
    respuesta = _cancelar()
    assert "cancelado" in respuesta and "avisé al cliente" in respuesta
    assert mundo["cancelados"] == [SO]
    assert any(
        "Cancelado por un integrante autorizado" in c and "el cliente se arrepintió" in c
        for c in mundo["comentarios"]
    )


def test_a_draft_order_cannot_be_cancelled_here(mundo) -> None:
    mundo["estado"]["so"].update({"docstatus": 0, "status": "Draft"})
    respuesta = _cancelar()
    assert "no está confirmado" in respuesta and "rechazar" in respuesta
    assert mundo["cancelados"] == []


def test_outside_the_24_hour_window_it_refuses(mundo) -> None:
    mundo["confirmado_hace"](25)
    respuesta = _cancelar()
    assert "hace 25 h" in respuesta and "24 h" in respuesta and "ERPNext" in respuesta
    assert mundo["cancelados"] == []


def test_the_window_is_configurable(mundo, monkeypatch) -> None:
    monkeypatch.setenv("CANCELACION_HORAS", "48")
    mundo["confirmado_hace"](25)
    assert "cancelado" in _cancelar()


def test_an_order_this_system_did_not_confirm_cannot_be_proven_inside_the_window(mundo) -> None:
    mundo["estado"]["durables"].clear()
    mundo["marcas"].values.pop(confirmacion._clave_cache(SO), None)
    respuesta = _cancelar()
    assert "No puedo establecer cuándo se confirmó" in respuesta
    assert mundo["cancelados"] == []


# 3./4. linked documents: refuse, never cascade
@pytest.mark.parametrize(
    ("doctype", "nombre", "etiqueta"),
    [
        ("Delivery Note Item", REMITO, f"remito {REMITO} (confirmado)"),
        ("Sales Invoice Item", "ACC-SINV-1", "factura ACC-SINV-1 (confirmado)"),
    ],
)
def test_a_submitted_delivery_note_or_invoice_blocks_the_cancellation(
    mundo, doctype, nombre, etiqueta
) -> None:
    mundo["estado"]["vinculados"].append((doctype, nombre, 1))
    respuesta = _cancelar()
    assert etiqueta in respuesta and "cascada" in respuesta
    assert mundo["cancelados"] == []
    assert mundo["estado"]["borrados"] == []


def test_a_draft_delivery_note_sends_the_manager_to_despreparar_first(mundo) -> None:
    """Rule 3 without the trap: the order is not cancellable yet, and the draft
    is NOT deleted from inside cancelar."""
    mundo["preparar_remito"]()
    respuesta = _cancelar()
    assert f"remito {REMITO}" in respuesta and "no lo borro solo" in respuesta
    assert f"despreparar {SO}" in respuesta
    assert mundo["cancelados"] == []
    assert mundo["estado"]["borrados"] == []


def test_a_draft_invoice_is_left_to_erpnext(mundo) -> None:
    mundo["estado"]["vinculados"].append(("Sales Invoice Item", "ACC-SINV-9", 0))
    respuesta = _cancelar()
    assert "factura ACC-SINV-9" in respuesta and "ERPNext" in respuesta
    assert mundo["cancelados"] == []


def test_an_unreadable_link_check_refuses_instead_of_assuming_none(mundo, monkeypatch) -> None:
    monkeypatch.setattr(
        erpnext,
        "policy_get_list",
        lambda doctype, **kwargs: (
            list(mundo["estado"]["durables"])
            if doctype == "Comment"
            else (_ for _ in ()).throw(erpnext.ERPNextError("caído"))
        ),
    )
    assert "No pude cancelar" in _cancelar()
    assert mundo["cancelados"] == []


# 5. under the lock
def test_everything_runs_under_the_distributed_lock(mundo) -> None:
    _cancelar()
    assert mundo["estado"]["locks"] == [f"cancelar:{SO}"]


def test_a_lock_that_cannot_be_taken_cancels_nothing(mundo, monkeypatch) -> None:
    @contextmanager
    def sin_lock(nombre, **kwargs):
        raise decisiones.CoordinationError("ocupado")
        yield

    monkeypatch.setattr(decisiones, "distributed_lock", sin_lock)
    assert "No pude coordinar" in _cancelar()
    assert mundo["cancelados"] == []


# 6. policy identity only — the fixture makes the agent-side functions raise
def test_only_policy_identity_functions_are_used(mundo) -> None:
    _cancelar()
    assert mundo["cancelados"] == [SO]


# 8. idempotent
def test_a_repeated_cancellation_changes_nothing_and_notifies_nobody_twice(mundo) -> None:
    _cancelar()
    segunda = _cancelar("otro motivo")
    assert "ya estaba cancelado" in segunda
    assert mundo["cancelados"] == [SO]
    assert len(mundo["enviados"]) == 1


# 9./10. customer told once, free text inside the window, template outside
def test_the_customer_is_told_once_in_free_text_inside_their_window(mundo) -> None:
    _cancelar()
    ((tel, texto),) = mundo["enviados"]
    assert tel == CUSTOMER_PHONE
    assert SO in texto and "el cliente se arrepintió" in texto and "cancelled" in texto


def test_outside_the_customer_window_a_configured_template_is_used(mundo, monkeypatch) -> None:
    import app.whatsapp as whatsapp

    monkeypatch.setattr(decisiones, "window_open", lambda tel: False)
    monkeypatch.setenv("WHATSAPP_CUSTOMER_CANCELLED_TEMPLATE", "pedido_cancelado_cliente")
    plantilla = Mock(return_value={"messages": [{"id": "wamid.tpl"}]})
    monkeypatch.setattr(whatsapp, "enviar_plantilla", plantilla)
    respuesta = _cancelar()
    assert "avisé al cliente" in respuesta
    assert plantilla.call_args.args[0] == CUSTOMER_PHONE
    assert plantilla.call_args.args[1] == "pedido_cancelado_cliente"
    assert plantilla.call_args.args[3] == [SO, "el cliente se arrepintió"]
    assert mundo["enviados"] == []


# 11. failure -> dead-letter + one ToDo
def test_a_failed_customer_notice_is_parked_with_one_todo(mundo, monkeypatch) -> None:
    monkeypatch.setattr(decisiones, "window_open", lambda tel: False)  # closed, no template
    respuesta = _cancelar()
    assert "cancelado" in respuesta and "NO pude avisarle" in respuesta and "tarea" in respuesta
    assert mundo["marcas"].llen(outbound_status.DEAD_NOTIFY_KEY) == 1
    assert len(mundo["todos"]) == 1 and SO in mundo["todos"][0]["description"]
    assert any("NO enviado" in c for c in mundo["comentarios"])


# 12. no LLM tool — cancelling and unpreparing are both human-only
def test_no_llm_tool_can_cancel_or_unprepare() -> None:
    import inspect

    from app.graph import TOOLS_CLIENTES, TOOLS_GERENCIA

    for lista in (TOOLS_CLIENTES, TOOLS_GERENCIA):
        for herramienta in lista:
            fn = getattr(herramienta, "func", None) or getattr(herramienta, "coroutine", None)
            assert fn is not decisiones.cancelar
            assert fn is not decisiones.despreparar
            assert herramienta.name not in ("cancelar", "despreparar")
            fuente = inspect.getsource(fn)
            assert "policy_cancel_doc" not in fuente
            assert "policy_delete_doc" not in fuente
            assert 'docstatus": 2' not in fuente


# ---------------------------------------------------------------------------
# Repair 2 — the deadline is durable: a restart or a Redis flush cannot move it.
# ---------------------------------------------------------------------------


def test_the_window_survives_a_redis_flush(mundo) -> None:
    """Everything Redis knew is gone; ERPNext still holds the confirmation."""
    mundo["marcas"].values.clear()
    mundo["marcas"].lists.clear()
    mundo["marcas"].zsets.clear()

    assert "cancelado" in _cancelar()
    assert mundo["cancelados"] == [SO]


def test_the_window_survives_an_application_restart(mundo) -> None:
    """A fresh process has no cache at all and must re-read the durable record."""
    mundo["marcas"].values.pop(confirmacion._clave_cache(SO), None)

    momento = confirmacion.momento(SO)

    assert momento is not None
    assert time.time() - momento < 60
    # ...and the re-read is cached, so the next attempt costs no ERPNext call.
    assert mundo["marcas"].values.get(confirmacion._clave_cache(SO)) is not None


def test_a_flushed_redis_does_not_reopen_a_window_that_had_closed(mundo) -> None:
    mundo["confirmado_hace"](30)
    mundo["marcas"].values.clear()

    respuesta = _cancelar()

    assert "hace 30 h" in respuesta
    assert mundo["cancelados"] == []


def test_an_order_confirmed_only_inside_erpnext_fails_closed(mundo) -> None:
    """No durable record from this system: it cannot prove the window at all."""
    mundo["estado"]["durables"].clear()
    mundo["marcas"].values.clear()

    assert "No puedo establecer cuándo se confirmó" in _cancelar()
    assert mundo["cancelados"] == []


def test_a_durable_record_that_cannot_be_written_closes_the_window(mundo, monkeypatch) -> None:
    """If ERPNext refused the record, no Redis-only deadline takes its place."""
    mundo["estado"]["durables"].clear()
    mundo["marcas"].values.clear()
    monkeypatch.setattr(
        erpnext, "registrar_comentario", Mock(side_effect=erpnext.ERPNextError("sin permiso"))
    )

    assert confirmacion.registrar(SO, "automática (política)") is False
    assert confirmacion.momento(SO) is None
    assert "No puedo establecer cuándo se confirmó" in _cancelar()


def test_the_earliest_durable_record_is_the_one_that_counts(mundo) -> None:
    """Two records must not extend the deadline; the first confirmation wins."""
    mundo["confirmado_hace"](30)
    mundo["estado"]["durables"].append(
        {
            "content": f"{confirmacion.MARCA} {confirmacion.sello()} fuente=segunda",
            "creation": "2026-09-03 12:00:00",
        }
    )
    mundo["marcas"].values.pop(confirmacion._clave_cache(SO), None)

    assert "hace 30 h" in _cancelar()


def test_an_unparseable_durable_record_is_not_trusted(mundo) -> None:
    mundo["estado"]["durables"].clear()
    mundo["marcas"].values.clear()
    mundo["estado"]["durables"].append(
        {"content": f"{confirmacion.MARCA} ayer a la tarde", "creation": "2026-09-03 12:00:00"}
    )

    assert confirmacion.momento(SO) is None


# ---------------------------------------------------------------------------
# Repair 3 — despreparar: the one command allowed to remove a linked draft.
# ---------------------------------------------------------------------------


def test_only_staff_phones_can_unprepare(mundo) -> None:
    mundo["preparar_remito"]()
    assert "permiso" in _despreparar(por="5490000000000")
    assert mundo["estado"]["borrados"] == []


def test_the_unprepare_decision_rechecks_the_phone_itself(mundo) -> None:
    mundo["preparar_remito"]()
    resultado = decisiones.despreparar(SO, "5490000000000")
    assert resultado["ok"] is False and "permiso" in resultado["detalle"]
    assert mundo["estado"]["borrados"] == []


def test_unpreparing_deletes_the_agents_own_draft_and_audits_it(mundo) -> None:
    mundo["preparar_remito"]()

    respuesta = _despreparar()

    assert REMITO in respuesta and f"cancelar {SO}" in respuesta
    assert mundo["estado"]["borrados"] == [REMITO]
    assert any(
        "Despreparado por un integrante autorizado" in c and REMITO in c
        for c in mundo["comentarios"]
    )
    assert any("5 x LECHE-1L" in c for c in mundo["comentarios"])


def test_the_record_is_written_before_the_draft_is_deleted(mundo, monkeypatch) -> None:
    """A deletion with no trace is not auditable, so the order is annotated
    first and a failing delete leaves the record explaining the attempt."""
    mundo["preparar_remito"]()
    orden: list[str] = []
    monkeypatch.setattr(
        erpnext, "add_comment", lambda dt, name, text: orden.append(f"comentario:{text[:12]}")
    )
    monkeypatch.setattr(
        erpnext,
        "policy_delete_doc",
        lambda dt, name: orden.append(f"borrado:{name}"),
    )

    _despreparar()

    assert orden == ["comentario:Despreparado", f"borrado:{REMITO}"]


def test_after_unpreparing_the_order_can_be_cancelled(mundo) -> None:
    mundo["preparar_remito"]()
    assert "borrado" in _despreparar()

    respuesta = _cancelar()

    assert "cancelado" in respuesta
    assert mundo["cancelados"] == [SO]


def test_a_draft_the_agent_did_not_create_is_never_deleted(mundo) -> None:
    hecho_a_mano = _remito_del_agente()
    hecho_a_mano["remarks"] = "Remito cargado a mano por administración."
    mundo["preparar_remito"](hecho_a_mano)

    respuesta = _despreparar()

    assert "no lo preparó el agente" in respuesta and "ERPNext" in respuesta
    assert mundo["estado"]["borrados"] == []


@pytest.mark.parametrize(
    ("cambio", "esperado"),
    [
        ({"items": [{"item_code": "LECHE-1L", "qty": 3, "against_sales_order": SO, "so_detail": "row-1"}]}, "cantidades"),
        ({"items": [{"item_code": "QUESO-1K", "qty": 5, "against_sales_order": SO, "so_detail": "row-9"}]}, "ya no son los del pedido"),
        ({"customer": "CUST-999"}, "cliente"),
        ({"company": "Otra SA"}, "compañía"),
    ],
)
def test_an_edited_draft_is_never_deleted(mundo, cambio, esperado) -> None:
    editado = _remito_del_agente()
    editado.update(cambio)
    mundo["preparar_remito"](editado)

    respuesta = _despreparar()

    assert esperado in respuesta
    assert mundo["estado"]["borrados"] == []


def test_a_draft_carrying_another_orders_lines_is_never_deleted(mundo) -> None:
    intruso = _remito_del_agente()
    intruso["items"] = [
        {"item_code": "LECHE-1L", "qty": 5, "against_sales_order": "SAL-ORD-OTRO", "so_detail": "row-1"}
    ]
    mundo["preparar_remito"](intruso)

    assert "otro pedido" in _despreparar()
    assert mundo["estado"]["borrados"] == []


def test_unpreparing_never_touches_a_submitted_document(mundo) -> None:
    mundo["preparar_remito"]()
    mundo["estado"]["vinculados"].append(("Sales Invoice Item", "ACC-SINV-1", 1))

    respuesta = _despreparar()

    assert "factura ACC-SINV-1 (confirmado)" in respuesta and "cascada" in respuesta
    assert mundo["estado"]["borrados"] == []


def test_unpreparing_leaves_a_draft_invoice_to_erpnext(mundo) -> None:
    mundo["preparar_remito"]()
    mundo["estado"]["vinculados"].append(("Sales Invoice Item", "ACC-SINV-2", 0))

    respuesta = _despreparar()

    assert "ACC-SINV-2" in respuesta and "ERPNext" in respuesta
    assert mundo["estado"]["borrados"] == []


def test_several_drafts_are_left_for_a_person(mundo) -> None:
    mundo["preparar_remito"]()
    mundo["preparar_remito"](_remito_del_agente("MAT-DN-2"))

    respuesta = _despreparar()

    assert "2 remitos" in respuesta and "ERPNext" in respuesta
    assert mundo["estado"]["borrados"] == []


def test_unpreparing_twice_changes_nothing_the_second_time(mundo) -> None:
    mundo["preparar_remito"]()
    assert "borrado" in _despreparar()

    segunda = _despreparar()

    assert "no tiene remito preparado" in segunda
    assert mundo["estado"]["borrados"] == [REMITO]


def test_unpreparing_runs_under_the_distributed_lock(mundo) -> None:
    mundo["preparar_remito"]()
    _despreparar()
    assert mundo["estado"]["locks"] == [f"despreparar:{SO}"]


def test_a_lock_that_cannot_be_taken_deletes_nothing(mundo, monkeypatch) -> None:
    mundo["preparar_remito"]()

    @contextmanager
    def sin_lock(nombre, **kwargs):
        raise decisiones.CoordinationError("ocupado")
        yield

    monkeypatch.setattr(decisiones, "distributed_lock", sin_lock)

    assert "No pude coordinar" in _despreparar()
    assert mundo["estado"]["borrados"] == []


def test_a_prepared_delivery_note_carries_the_agent_marker(mundo, monkeypatch) -> None:
    """preparar must stamp the marker, or despreparar could never undo it."""
    creado: dict = {}

    def policy_create_doc(doctype, payload):
        creado.update(payload)
        return {"name": REMITO}

    monkeypatch.setattr(erpnext, "policy_create_doc", policy_create_doc)
    monkeypatch.setattr(erpnext, "default_context", lambda: ("Lacteos Test SA", "Principal - LT"))

    assert decisiones.preparar(SO, STAFF)["ok"] is True
    assert decisiones.MARCA_REMITO_AGENTE in creado["remarks"]


def test_policy_delete_doc_refuses_anything_that_is_not_a_draft(monkeypatch) -> None:
    """The credential itself will not destroy a submitted or cancelled document."""
    monkeypatch.setattr(
        erpnext, "policy_get_doc", lambda dt, name: {"name": name, "docstatus": 1}
    )
    peticion = Mock(side_effect=AssertionError("no debe llamar a ERPNext"))
    monkeypatch.setattr(erpnext, "_request", peticion)

    with pytest.raises(erpnext.ERPNextError, match="no es un borrador"):
        erpnext.policy_delete_doc("Delivery Note", REMITO)
    peticion.assert_not_called()
