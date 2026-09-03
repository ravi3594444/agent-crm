"""Stage 2e: exactly-once confirmed-order notice, order-id commands, dispatch in
two human steps, the 18:00 digest, and notifications that never vanish."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import aprobacion, decisiones, digest, erpnext, inventario, notificar, outbound_status

STAFF = "5493511111111"
SO = {
    "name": "SAL-ORD-2026-00009",
    "docstatus": 1,
    "status": "To Deliver and Bill",
    "customer": "CUST-001",
    "customer_name": "Kiosco La Esquina",
    "company": "Lacteos Test SA",
    "currency": "INR",
    "grand_total": 4800.0,
    "delivery_date": "2026-09-03",
    "shipping_address_name": "Kiosco La Esquina-Shipping",
    "items": [
        {"name": "row1", "item_code": "MAN-200", "item_name": "Manteca 200 g", "qty": 2, "uom": "Unidad", "warehouse": "Principal - LT"},
        {"name": "row2", "item_code": "QUE-CRE", "item_name": "Queso cremoso", "qty": 1.5, "uom": "Kg"},
    ],
}
ADDRESS = {"address_line1": "Av. Colón 1234", "city": "Córdoba", "pincode": "5000"}


class _RedisMarcas:
    """set NX / get / delete / rpush / llen / scan_iter — enough for the claims."""

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

    def scan_iter(self, match="*", count=100):
        prefix = match.rstrip("*")
        return iter([k for k in self.values if k.startswith(prefix)])


@pytest.fixture
def canal(monkeypatch: pytest.MonkeyPatch):
    """Staff configured, window open, no templates, every send recorded."""
    marcas = _RedisMarcas()
    monkeypatch.setattr(outbound_status, "_client", marcas)
    monkeypatch.setattr(notificar, "STAFF", [STAFF])
    for var in ("WHATSAPP_STAFF_CONFIRMED_TEMPLATE", "WHATSAPP_STAFF_PENDING_TEMPLATE", "WHATSAPP_STAFF_ALERT_TEMPLATE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NOTIFICAR_SOLO_PRIMERO", "true")
    monkeypatch.setattr(notificar, "window_open", lambda phone: phone == STAFF)
    monkeypatch.setattr(erpnext, "add_comment", Mock())
    monkeypatch.setattr(erpnext, "policy_get_doc", lambda dt, name: dict(ADDRESS) if dt == "Address" else dict(SO))
    todos: list[dict] = []
    monkeypatch.setattr(erpnext, "create_doc", lambda dt, payload: todos.append(payload) or {"name": f"TD-{len(todos)}"})
    enviados: list[tuple[str, str]] = []

    def send(phone, text):
        enviados.append((phone, text))
        return {"messages": [{"id": f"wamid.{len(enviados)}"}]}

    monkeypatch.setattr(notificar, "enviar_mensaje", send)
    monkeypatch.setattr(notificar, "enviar_plantilla", Mock(side_effect=AssertionError("sin plantilla")))
    return {"enviados": enviados, "todos": todos, "marcas": marcas}


# ------------------------------------------------------------ A. exactly once


def test_confirmation_notice_has_every_field_the_manager_needs(canal, monkeypatch) -> None:
    monkeypatch.setattr(notificar, "_momento_negocio", lambda: "2026-09-02 19:49")
    assert notificar.notificar_confirmacion(SO, "automática (política)") is True
    (phone, texto), = canal["enviados"]
    assert phone == STAFF
    assert texto.splitlines()[0] == "✅ Pedido SAL-ORD-2026-00009 confirmado"
    assert "Cliente: Kiosco La Esquina" in texto
    assert "2 Unidad × Manteca 200 g; 1,5 Kg × Queso cremoso" in texto
    assert "Total: $4.800,00 INR" in texto
    assert "Entrega: Av. Colón 1234, Córdoba (CP 5000) — 2026-09-03" in texto
    assert "Origen: automática (política)" in texto
    assert "Confirmado: 2026-09-02 19:49" in texto
    assert STAFF not in texto


def test_the_manager_hears_about_a_confirmed_order_exactly_once_across_both_paths(canal, monkeypatch) -> None:
    """Policy confirms and notifies; a later human tap on the same order must
    not send a second notice, and neither must a second tap."""
    assert notificar.notificar_confirmacion(SO, "automática (política)") is True

    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: phone == STAFF)
    monkeypatch.setattr(aprobacion, "_leer_doc", lambda dt, name: dict(SO))
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", Mock(side_effect=AssertionError("ya confirmado")))
    monkeypatch.setattr(aprobacion.avisos, "confirmacion_cliente", lambda so: True)
    monkeypatch.setattr(aprobacion.confirmacion, "registrar", lambda *a, **k: True)
    aprobacion.manejar_boton(f"ok:{SO['name']}", STAFF)
    aprobacion.manejar_boton(f"ok:{SO['name']}", STAFF)

    assert len(canal["enviados"]) == 1


def test_a_human_confirmation_notifies_once_and_a_later_automatic_path_is_silent(canal, monkeypatch) -> None:
    borrador = {**SO, "docstatus": 0, "status": "Draft"}
    lecturas = iter([borrador, dict(SO), dict(SO), dict(SO)])
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    monkeypatch.setattr(aprobacion, "_leer_doc", lambda dt, name: next(lecturas))
    submit = Mock(return_value=dict(SO))
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", submit)
    monkeypatch.setattr(aprobacion.avisos, "confirmacion_cliente", lambda so: True)
    monkeypatch.setattr(aprobacion.confirmacion, "registrar", lambda *a, **k: True)

    respuesta = aprobacion.manejar_boton(f"ok:{SO['name']}", STAFF)
    assert "confirmado" in respuesta
    submit.assert_called_once_with("Sales Order", SO["name"])
    assert len(canal["enviados"]) == 1
    assert "Origen: manual (confirmación humana)" in canal["enviados"][0][1]

    # The automatic path arriving afterwards (a retried post-create) is silent.
    assert notificar.notificar_confirmacion(SO, "automática (política)") is True
    assert len(canal["enviados"]) == 1


def test_a_notice_nobody_can_receive_is_parked_with_one_todo_and_can_be_retried(canal, monkeypatch) -> None:
    monkeypatch.setattr(notificar, "window_open", lambda phone: False)  # no template, no window
    assert notificar.notificar_confirmacion(SO, "automática (política)") is False
    assert canal["enviados"] == []
    assert canal["marcas"].llen(outbound_status.DEAD_NOTIFY_KEY) == 1
    assert len(canal["todos"]) == 1
    assert SO["name"] in canal["todos"][0]["description"]
    assert canal["todos"][0]["reference_name"] == SO["name"]

    # Same failure again: parked again, but no second ToDo for the same order.
    assert notificar.notificar_confirmacion(SO, "manual (confirmación humana)") is False
    assert canal["marcas"].llen(outbound_status.DEAD_NOTIFY_KEY) == 2
    assert len(canal["todos"]) == 1

    # The window opens (he wrote to the bot): the claim was released, so it sends.
    monkeypatch.setattr(notificar, "window_open", lambda phone: True)
    assert notificar.notificar_confirmacion(SO, "manual (confirmación humana)") is True
    assert len(canal["enviados"]) == 1


def test_a_pending_alert_that_reaches_nobody_opens_one_deduplicated_todo(canal, monkeypatch) -> None:
    monkeypatch.setattr(notificar, "window_open", lambda phone: False)
    monkeypatch.setattr(notificar, "has_accepted", lambda *a: False)
    monkeypatch.setattr(notificar, "record_outbound", Mock())
    assert notificar.notificar_equipo(SO["name"], SO, auto=False, motivos="auto-confirmación desactivada") is False
    assert notificar.notificar_equipo(SO["name"], SO, auto=False, motivos="auto-confirmación desactivada") is False
    assert canal["marcas"].llen(outbound_status.DEAD_NOTIFY_KEY) == 2
    assert len(canal["todos"]) == 1


def test_the_parked_entry_never_carries_a_phone_number(canal, monkeypatch) -> None:
    monkeypatch.setattr(notificar, "window_open", lambda phone: False)
    notificar.notificar_confirmacion(SO, "automática (política)")
    raw = canal["marcas"].lists[outbound_status.DEAD_NOTIFY_KEY][0]
    assert STAFF not in raw


# --------------------------------------------------------- B/C. commands, dispatch


@pytest.mark.parametrize("accion", ["preparar", "despachar", "no", "ok", "ver"])
def test_unauthorized_phones_get_nothing_from_any_order_command(monkeypatch, accion) -> None:
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: False)
    preparar = Mock()
    despachar = Mock()
    monkeypatch.setattr(decisiones, "preparar", preparar)
    monkeypatch.setattr(decisiones, "despachar", despachar)
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", Mock(side_effect=AssertionError("nunca")))
    assert "permiso" in aprobacion.manejar_boton(f"{accion}:{SO['name']}", "5490000000000")
    preparar.assert_not_called()
    despachar.assert_not_called()


@pytest.fixture
def remitos(monkeypatch: pytest.MonkeyPatch):
    """ERPNext for dispatch: the order, the draft Delivery Notes, the submits."""
    estado = {"so": dict(SO), "borradores": []}
    creados: list[dict] = []
    submits: list[tuple[str, str]] = []
    monkeypatch.setattr(decisiones, "_leer_doc", lambda dt, name: dict(estado["so"]) if dt == "Sales Order" else {"name": name, "docstatus": 1})

    def policy_get_list(doctype, filters=None, fields=None, limit=20, parent=None, order_by=None, start=0):
        assert doctype == "Delivery Note Item" and parent == "Delivery Note"
        return [{"parent": n, "against_sales_order": SO["name"], "docstatus": 0} for n in estado["borradores"]]

    monkeypatch.setattr(erpnext, "policy_get_list", policy_get_list)

    def policy_create_doc(doctype, payload):
        assert doctype == "Delivery Note"
        creados.append(payload)
        nombre = f"MAT-DN-{len(creados)}"
        estado["borradores"].append(nombre)
        return {"name": nombre, "docstatus": 0}

    monkeypatch.setattr(erpnext, "policy_create_doc", policy_create_doc)
    monkeypatch.setattr(erpnext, "submit_doc", lambda dt, name: submits.append((dt, name)) or {"name": name, "docstatus": 1})
    monkeypatch.setattr(erpnext, "create_doc", Mock(side_effect=AssertionError("el path manual no usa la identidad del agente")))
    monkeypatch.setattr(erpnext, "default_context", lambda: ("Lacteos Test SA", "Principal - LT"))
    monkeypatch.setattr(erpnext, "add_comment", Mock())
    monkeypatch.setattr(decisiones, "_hoy", lambda: "2026-09-02")
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: phone == STAFF)
    return {"estado": estado, "creados": creados, "submits": submits}


def test_preparar_creates_one_draft_delivery_note_with_the_policy_identity(remitos) -> None:
    respuesta = aprobacion.manejar_boton(f"preparar:{SO['name']}", STAFF)
    assert "MAT-DN-1" in respuesta and "borrador" in respuesta and "despachar" in respuesta
    (payload,) = remitos["creados"]
    assert payload["company"] == "Lacteos Test SA"
    assert payload["customer"] == "CUST-001"
    assert payload["set_posting_time"] == 1 and payload["posting_date"] == "2026-09-02"
    assert [i["warehouse"] for i in payload["items"]] == ["Principal - LT", "Principal - LT"]
    assert all(i["against_sales_order"] == SO["name"] for i in payload["items"])
    assert [i["so_detail"] for i in payload["items"]] == ["row1", "row2"]
    assert "docstatus" not in payload or payload["docstatus"] == 0
    assert remitos["submits"] == []  # preparing never dispatches

    # Idempotent: preparing again reuses the draft.
    otra = aprobacion.manejar_boton(f"preparar:{SO['name']}", STAFF)
    assert "ya tiene el remito MAT-DN-1" in otra
    assert len(remitos["creados"]) == 1


def test_despachar_is_a_separate_step_that_needs_the_prepared_draft(remitos) -> None:
    sin_remito = aprobacion.manejar_boton(f"despachar:{SO['name']}", STAFF)
    assert "preparar" in sin_remito
    assert remitos["submits"] == []

    aprobacion.manejar_boton(f"preparar:{SO['name']}", STAFF)
    despacho = aprobacion.manejar_boton(f"despachar:{SO['name']}", STAFF)
    assert "despachado" in despacho
    assert remitos["submits"] == [("Delivery Note", "MAT-DN-1")]


def test_despachar_refuses_when_several_drafts_exist(remitos) -> None:
    remitos["estado"]["borradores"].extend(["MAT-DN-7", "MAT-DN-8"])
    respuesta = aprobacion.manejar_boton(f"despachar:{SO['name']}", STAFF)
    assert "2 remitos" in respuesta
    assert remitos["submits"] == []


@pytest.mark.parametrize(
    ("docstatus", "status"), [(0, "Draft"), (0, "Closed"), (1, "Closed"), (2, "Cancelled")]
)
def test_only_a_live_confirmed_order_can_be_prepared(remitos, docstatus, status) -> None:
    remitos["estado"]["so"].update({"docstatus": docstatus, "status": status})
    respuesta = aprobacion.manejar_boton(f"preparar:{SO['name']}", STAFF)
    assert remitos["creados"] == []
    assert "confirmar" in respuesta or status in respuesta


def test_no_llm_tool_can_prepare_or_dispatch_and_none_can_submit(monkeypatch) -> None:
    import inspect

    from app.graph import TOOLS_CLIENTES, TOOLS_GERENCIA

    prohibidas = {decisiones.preparar, decisiones.despachar}
    for lista in (TOOLS_CLIENTES, TOOLS_GERENCIA):
        for herramienta in lista:
            fn = getattr(herramienta, "func", None) or getattr(herramienta, "coroutine", None)
            assert fn not in prohibidas, herramienta.name
            assert herramienta.name not in {"preparar", "despachar"}
            fuente = inspect.getsource(fn)
            for verbo in ("submit_doc", "policy_create_doc", "policy_update_status", "docstatus\": 1"):
                assert verbo not in fuente, f"{herramienta.name} puede {verbo}"


# ------------------------------------------------------------------- D. digest


@pytest.fixture
def erp_digest(monkeypatch: pytest.MonkeyPatch):
    def policy_get_list(doctype, filters=None, fields=None, limit=20, parent=None, order_by=None, start=0):
        if doctype == "Sales Order":
            docstatus = next(f[2] for f in filters if f[0] == "docstatus")
            if docstatus == 1:
                return [{**SO, "status": "To Deliver and Bill"}]
            return [
                {"name": "SAL-ORD-2026-00010", "customer": "CUST-002", "customer_name": "Almacén Don José", "grand_total": 12000, "delivery_date": "2026-09-04", "status": "Draft"},
            ]
        if doctype == "Bin":
            return [{"item_code": "MAN-200"}, {"item_code": "QUE-CRE"}, {"item_code": "LEC-ENT-1L"}]
        raise AssertionError(doctype)

    monkeypatch.setattr(erpnext, "policy_get_list", policy_get_list)
    monkeypatch.setattr(erpnext, "default_warehouse", lambda: "Principal - LT")
    monkeypatch.setenv("STOCK_CONFIABLE", "true")
    monkeypatch.setenv("STOCK_CONFIABLE_HORAS", "24")
    ahora = datetime(2026, 9, 2, 18, 5, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))
    monkeypatch.setattr(digest, "_ahora", lambda: ahora)
    conteos = {
        "MAN-200": ahora.replace(hour=7),          # fresh: 11 h old
        "QUE-CRE": ahora - __import__("datetime").timedelta(hours=22),  # 2 h left
        "LEC-ENT-1L": None,                         # never counted
    }
    monkeypatch.setattr(inventario, "ultimo_conteo", lambda code, wh: conteos[code])
    monkeypatch.setattr(
        outbound_status,
        "contar_pendientes",
        lambda: {"respuestas_en_dead_letter": 2, "avisos_en_dead_letter": 1, "entregas_fallidas": 0},
    )
    alertas: list[tuple[str, str]] = []
    monkeypatch.setattr(
        notificar, "alertar_excepcion", lambda asunto, cuerpo, **kw: alertas.append((asunto, cuerpo)) or True
    )
    return alertas


def test_digest_reports_the_four_sections(erp_digest) -> None:
    texto = digest.resumen()
    assert "📋 Resumen del 2026-09-02" in texto
    assert "🚚 Confirmados para preparar/despachar (1)" in texto and "SAL-ORD-2026-00009" in texto
    assert "🟡 Esperan tu decisión" in texto and "SAL-ORD-2026-00010" in texto and "$12.000" in texto
    assert "'confirmar <pedido>'" in texto
    assert "LEC-ENT-1L: nunca se confirmó un conteo" in texto
    assert "QUE-CRE: vence en 2 h" in texto
    assert "MAN-200" not in texto.split("📦")[1]
    assert "1 avisos sin entregar, 2 respuestas a clientes en dead-letter, 0 mensajes" in texto


def test_digest_goes_out_once_a_day_from_the_configured_hour(erp_digest, monkeypatch) -> None:
    monkeypatch.setenv("DIGEST_HORA", "18:00")
    assert digest.tick() is True
    assert digest.tick() is False  # same day, already sent
    assert len(erp_digest) == 1
    assert erp_digest[0][0] == "📋 Resumen del día"

    monkeypatch.setenv("DIGEST_HORA", "18:30")  # before the hour: nothing
    monkeypatch.setattr(digest, "enviado_hoy", lambda dia=None: False)
    assert digest.tick() is False
    assert len(erp_digest) == 1


def test_digest_respects_the_off_switch_and_the_manual_run(erp_digest, monkeypatch) -> None:
    monkeypatch.setenv("DIGEST_ACTIVO", "false")
    assert digest.tick() is False
    assert digest.enviar(forzar=True) is True  # python -m app.digest still works
    assert len(erp_digest) == 1


def test_digest_sections_degrade_instead_of_failing(erp_digest, monkeypatch) -> None:
    monkeypatch.setattr(erpnext, "policy_get_list", Mock(side_effect=erpnext.ERPNextError("caído")))
    texto = digest.resumen()
    assert "Confirmados para preparar/despachar: no pude leer ERPNext" in texto
    assert "Esperan tu decisión: no pude leer ERPNext" in texto
    assert "Conteos: no pude leer ERPNext" in texto
    assert "⚠️ Comunicación" in texto


def test_digest_hour_is_validated() -> None:
    import os

    os.environ["DIGEST_HORA"] = "25:99"
    try:
        assert digest.hora_objetivo() == (18, 0)
    finally:
        del os.environ["DIGEST_HORA"]
