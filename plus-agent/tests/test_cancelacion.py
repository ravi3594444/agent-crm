"""cancelar <pedido> <motivo>: twelve rules, one test each (or more)."""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import aprobacion, decisiones, erpnext, outbound_status

STAFF = "5493511111111"
SO = "SAL-ORD-2026-00009"
CUSTOMER_PHONE = "5493512222222"


class _RedisMarcas:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        self.values.pop(key, None)

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def llen(self, key):
        return len(self.lists.get(key, []))

    def eval(self, *args):
        return "accepted_by_meta"


@pytest.fixture
def mundo(monkeypatch: pytest.MonkeyPatch):
    """A confirmed order this system confirmed a minute ago, no linked docs."""
    marcas = _RedisMarcas()
    monkeypatch.setattr(outbound_status, "_client", marcas)
    outbound_status.marcar_confirmacion(SO)
    estado = {
        "so": {"name": SO, "docstatus": 1, "status": "To Deliver and Bill", "customer": "CUST-001"},
        "vinculados": [],
        "locks": [],
    }

    @contextmanager
    def lock(nombre, **kwargs):
        estado["locks"].append(nombre)
        yield

    monkeypatch.setattr(decisiones, "distributed_lock", lock)
    monkeypatch.setattr(decisiones, "es_equipo", lambda phone: phone == STAFF)
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: phone == STAFF)
    monkeypatch.setattr(
        decisiones, "_leer_doc",
        lambda dt, name: dict(estado["so"]) if dt == "Sales Order" else {"name": name, "mobile_no": CUSTOMER_PHONE},
    )

    def policy_get_list(doctype, filters=None, fields=None, limit=20, parent=None, order_by=None):
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
        monkeypatch.setattr(erpnext, prohibido, Mock(side_effect=AssertionError(f"{prohibido} no debe usarse")))
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

    monkeypatch.setattr(whatsapp, "enviar_mensaje", lambda tel, texto: enviados.append((tel, texto)) or {"messages": [{"id": f"wamid.{len(enviados)}"}]})
    monkeypatch.setattr(whatsapp, "enviar_plantilla", Mock(side_effect=AssertionError("sin plantilla")))
    monkeypatch.setattr(decisiones, "window_open", lambda tel: tel == CUSTOMER_PHONE)
    monkeypatch.delenv("WHATSAPP_CUSTOMER_CANCELLED_TEMPLATE", raising=False)
    monkeypatch.delenv("CANCELACION_HORAS", raising=False)
    return {"estado": estado, "marcas": marcas, "cancelados": cancelados, "comentarios": comentarios, "enviados": enviados, "todos": todos}


def _cancelar(motivo="el cliente se arrepintió", por=STAFF):
    return aprobacion.manejar_boton(f"cancelar:{SO}:{motivo}", por)


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
    mundo["marcas"].values[outbound_status._clave_confirmacion(SO)] = f"{time.time() - 25 * 3600:.3f}"
    respuesta = _cancelar()
    assert "hace 25 h" in respuesta and "24 h" in respuesta and "ERPNext" in respuesta
    assert mundo["cancelados"] == []


def test_the_window_is_configurable(mundo, monkeypatch) -> None:
    monkeypatch.setenv("CANCELACION_HORAS", "48")
    mundo["marcas"].values[outbound_status._clave_confirmacion(SO)] = f"{time.time() - 25 * 3600:.3f}"
    assert "cancelado" in _cancelar()


def test_an_order_this_system_did_not_confirm_cannot_be_proven_inside_the_window(mundo) -> None:
    mundo["marcas"].values.pop(outbound_status._clave_confirmacion(SO))
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
    monkeypatch.setattr(erpnext, "policy_get_list", Mock(side_effect=erpnext.ERPNextError("caído")))
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


# the clarified workflow around confirmations
def test_an_auto_confirmed_order_never_gets_a_second_customer_confirmation(monkeypatch) -> None:
    marcas = _RedisMarcas()
    monkeypatch.setattr(outbound_status, "_client", marcas)
    outbound_status.marcar_confirmacion(SO, informado_en_chat=True)  # what pedidos._after_create does
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    monkeypatch.setattr(aprobacion, "_leer_doc", lambda dt, name: {"name": SO, "docstatus": 1, "customer": "CUST-001"})
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", Mock(side_effect=AssertionError("ya confirmado")))
    monkeypatch.setattr(aprobacion, "_notificar_confirmada", lambda *a, **k: None)
    aviso = Mock(side_effect=AssertionError("segunda confirmación al cliente"))
    monkeypatch.setattr(aprobacion, "_avisar_cliente", aviso)

    respuesta = aprobacion.manejar_boton(f"ok:{SO}", STAFF)

    assert "ya recibió la confirmación en la conversación" in respuesta
    aviso.assert_not_called()


def test_the_manager_alert_is_informational_and_says_so(monkeypatch) -> None:
    from app import notificar

    monkeypatch.setattr(notificar, "_direccion_de_entrega", lambda so: "Av. Colón 1234")
    texto = notificar.texto_confirmacion({"name": SO, "customer": "C", "items": [], "grand_total": 1, "currency": "ARS", "delivery_date": "2026-09-04"}, "automática (política)", "2026-09-03 10:00")
    assert "Informativo: no hace falta responder" in texto
    assert f"cancelar {SO} <motivo>" in texto
