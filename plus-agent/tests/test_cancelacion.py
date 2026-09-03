"""cancelar <pedido> <motivo>: twelve rules, one test each (or more).

The tests at the end cover the repair this command needed before release: the
cancellation window is an ERPNext record, so it survives a Redis flush and a
restart instead of quietly disappearing with the cache.
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

from app import aprobacion, avisos, confirmacion, decisiones, erpnext, outbound_status
from tests.fakes import FakeMarcas

STAFF = "5493511111111"
SO = "SAL-ORD-2026-00009"
CUSTOMER_PHONE = "5493512222222"
REMITO = "MAT-DN-1"


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

    mundo["confirmado_hace"] = confirmado_hace

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


def test_the_decision_rechecks_the_phone_itself(mundo, monkeypatch) -> None:
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
    assert any("Cancelado por un integrante autorizado" in c and "el cliente se arrepintió" in c for c in mundo["comentarios"])


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
    ("doctype", "estado_doc", "etiqueta"),
    [("Delivery Note Item", 1, "remito MAT-DN-1 (confirmado)"), ("Sales Invoice Item", 1, "factura ACC-SINV-1 (confirmado)"),
     ("Delivery Note Item", 0, "remito MAT-DN-1 (borrador)")],
)
def test_linked_delivery_notes_or_invoices_block_the_cancellation(mundo, doctype, estado_doc, etiqueta) -> None:
    nombre = "MAT-DN-1" if "Delivery" in doctype else "ACC-SINV-1"
    mundo["estado"]["vinculados"].append((doctype, nombre, estado_doc))
    respuesta = _cancelar()
    assert etiqueta in respuesta and "cascada" in respuesta
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
    assert mundo["cancelados"] == [SO]  # policy_cancel_doc; create_doc/get_doc/submit_doc would have raised


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
    (tel, texto), = mundo["enviados"]
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


# 12. no LLM tool
def test_cancelar_is_not_reachable_from_any_llm_tool() -> None:
    import inspect

    from app.graph import TOOLS_CLIENTES, TOOLS_GERENCIA

    for lista in (TOOLS_CLIENTES, TOOLS_GERENCIA):
        for herramienta in lista:
            fn = getattr(herramienta, "func", None) or getattr(herramienta, "coroutine", None)
            assert fn is not decisiones.cancelar
            assert herramienta.name != "cancelar"
            assert "policy_cancel_doc" not in inspect.getsource(fn)
            assert "docstatus\": 2" not in inspect.getsource(fn)


# The customer's confirmation is queued once per order by whichever path
# confirms it, so it never depends on the model repeating a token and a second
# path never sends a second message.
def test_an_auto_confirmed_order_never_gets_a_second_customer_confirmation(monkeypatch) -> None:
    marcas = FakeMarcas()
    monkeypatch.setattr(outbound_status, "_client", marcas)
    pedido = {"name": SO, "docstatus": 1, "customer": "CUST-001"}
    monkeypatch.setattr(
        erpnext, "policy_get_doc", lambda dt, name: {"name": name, "mobile_no": CUSTOMER_PHONE}
    )
    monkeypatch.setattr(erpnext, "add_comment", Mock())
    # What the automatic path does in app/tools/pedidos.py::_notificar_confirmada.
    assert avisos.confirmacion_cliente(pedido) is True

    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    monkeypatch.setattr(aprobacion, "_leer_doc", lambda dt, name: dict(pedido))
    monkeypatch.setattr(
        aprobacion.erpnext, "submit_doc", Mock(side_effect=AssertionError("ya confirmado"))
    )
    monkeypatch.setattr(aprobacion, "_notificar_confirmada", lambda *a, **k: None)

    respuesta = aprobacion.manejar_boton(f"ok:{SO}", STAFF)

    assert "ya tenía su confirmación" in respuesta
    assert marcas.zcard(avisos.COLA) == 1


def test_the_manager_alert_is_informational_and_says_so(monkeypatch) -> None:
    from app import notificar

    monkeypatch.setattr(notificar, "_direccion_de_entrega", lambda so: "Av. Colón 1234")
    texto = notificar.texto_confirmacion({"name": SO, "customer": "C", "items": [], "grand_total": 1, "currency": "ARS", "delivery_date": "2026-09-04"}, "automática (política)", "2026-09-03 10:00")
    assert "Informativo: no hace falta responder" in texto
    assert f"cancelar {SO} <motivo>" in texto


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
