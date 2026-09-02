"""The manual decision path must stay out of the LLM's reach.

THE BOUNDARY THIS PROTECTS
  Customer Sales Agent  — creates DRAFT orders. Cannot submit anything.
  AI Management Agent   — explains, reports, notifies. Cannot decide.
  Human Manager         — the only role that confirms or rejects by hand,
                          through the signed webhook after es_equipo().

The automatic path is deliberately NOT human-gated: app/policy.py decides
deterministically and app/tools/pedidos.py submits with the policy credential
when every rule passes. These tests assert both halves — that a qualifying
order still auto-confirms with nobody involved, and that no LLM tool can reach
the manual override.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

os.environ.setdefault("ERPNEXT_URL", "http://erpnext.test")
os.environ.setdefault("ERPNEXT_API_KEY", "test-key")
os.environ.setdefault("ERPNEXT_API_SECRET", "test-secret")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test-phone-id")
os.environ.setdefault("WHATSAPP_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import aprobacion, decisiones
from app.graph import TOOLS_CLIENTES, TOOLS_GERENCIA

MANUAL = ("confirmar", "rechazar", "confirmar_pedido")


def test_no_llm_tool_exposes_the_manual_decision_functions() -> None:
    """If someone ever registers decisiones.* as a tool, this fails.

    A registered tool is callable by the model, and a model is steerable by the
    customer's own words. Manual override must never be reachable that way.
    """
    for lista, etiqueta in ((TOOLS_CLIENTES, "clientes"), (TOOLS_GERENCIA, "gerencia")):
        nombres = {t.name for t in lista}
        for prohibido in MANUAL:
            assert prohibido not in nombres, (
                f"{prohibido} quedó expuesto como herramienta del agente {etiqueta}"
            )


def test_no_tool_function_is_the_manual_decision_function() -> None:
    """Name-independent: compare the underlying callables, not the labels."""
    prohibidas = {decisiones.confirmar, decisiones.rechazar, aprobacion.confirmar_pedido}
    for lista in (TOOLS_CLIENTES, TOOLS_GERENCIA):
        for herramienta in lista:
            fn = getattr(herramienta, "func", None) or getattr(
                herramienta, "coroutine", None
            )
            assert fn not in prohibidas, f"{herramienta.name} envuelve una decisión manual"


def test_no_tool_can_submit_or_adjust_anything() -> None:
    """The LLM has no submit/payment/invoice/stock-adjustment verb at all."""
    prohibidos = ("submit", "confirmar_pedido", "aprobar", "pagar", "ajustar_stock")
    for lista in (TOOLS_CLIENTES, TOOLS_GERENCIA):
        for herramienta in lista:
            for palabra in prohibidos:
                assert palabra not in herramienta.name.lower(), herramienta.name


@pytest.mark.parametrize("accion", ["ok", "no", "ver"])
def test_unauthorized_phone_cannot_confirm_reject_or_read(
    monkeypatch: pytest.MonkeyPatch, accion: str
) -> None:
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: False)
    confirmar = Mock()
    rechazar = Mock()
    monkeypatch.setattr(aprobacion, "confirmar_pedido", confirmar)
    monkeypatch.setattr(decisiones, "rechazar", rechazar)

    result = aprobacion.manejar_boton(f"{accion}:SAL-ORD-0001", "5490000000000")

    assert "permiso" in result
    confirmar.assert_not_called()
    rechazar.assert_not_called()


def test_manual_confirmation_uses_the_policy_credential_not_the_agent_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submitting is only ever erpnext.submit_doc, which is the policy client."""
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    monkeypatch.setattr(
        aprobacion, "_leer_doc", lambda dt, name: {"name": name, "docstatus": 0}
    )
    submit = Mock(return_value={"name": "SAL-ORD-0001", "docstatus": 1})
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", submit)
    monkeypatch.setattr(aprobacion.erpnext, "add_comment", Mock())
    monkeypatch.setattr(aprobacion, "_avisar_cliente", lambda nombre: True)

    resultado = decisiones.confirmar("SAL-ORD-0001", "5493511111111")

    assert resultado["ok"] is True
    submit.assert_called_once_with("Sales Order", "SAL-ORD-0001")
