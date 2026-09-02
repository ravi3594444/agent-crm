"""Manual decisions taken by the HUMAN MANAGER on an exception order.

WHERE THIS SITS AMONG THE THREE ROLES
  Customer Sales Agent  — talks to customers, creates DRAFT orders, never submits.
  AI Management Agent   — explains, reports and notifies. It may NOT decide.
  Human Manager         — the only role that can confirm or reject by hand.

This module is the manual path ONLY. It is deliberately NOT registered in any
tool list (see app/graph.py): an LLM has no route to call it, so no prompt can
talk the system into confirming or rejecting an order.

The AUTOMATIC path is somewhere else and stays there: app/policy.py evaluates
the deterministic rules and app/tools/pedidos.py::_after_create re-validates
under a distributed lock and submits with the policy credential. A routine
order that passes every rule is confirmed with no human involved. Nothing in
this file participates in that.

CREDENTIAL BOUNDARIES ARE UNCHANGED. Submitting still happens only through
erpnext.submit_doc (policy identity); reads after a privileged transition use
erpnext.policy_get_doc. The customer-facing agent identity cannot submit.

HONESTY RULES
  - Never invent an order number, a stock figure or a confirmation.
  - Tell the customer only what ERPNext actually says.
  - When a customer notice cannot be delivered, say so to the manager instead
    of pretending it was sent.
"""
from __future__ import annotations

import os

from app import erpnext, telefono

_PURPOSE_RECHAZO = "customer_order_rejected"


def _resultado(ok: bool, aviso_cliente: bool, detalle: str) -> dict:
    return {"ok": ok, "aviso_cliente": aviso_cliente, "detalle": detalle}


def _leer_doc(doctype: str, name: str) -> dict:
    """Manual decisions run outside the LLM and may use the policy identity."""
    getter = getattr(erpnext, "policy_get_doc", erpnext.get_doc)
    return getter(doctype, name)


def telefono_del_cliente(nombre_so: str) -> str:
    """Normalized mobile number of the order's customer, or '' if unknown.

    Returns '' rather than raising: a missing phone must degrade into "tell the
    manager to call them", never into a crash on the decision path.
    """
    try:
        so = _leer_doc("Sales Order", nombre_so)
        cliente = _leer_doc("Customer", so["customer"])
    except Exception as exc:
        print(f"[decisiones] {nombre_so}: no pude leer el cliente ({type(exc).__name__})")
        return ""
    return telefono.normalizar(cliente.get("mobile_no")) or ""


def confirmar(nombre: str, por: str) -> dict:
    """Confirm an exception order by hand. HUMAN MANAGER ONLY.

    The implementation lives in app/aprobacion.py, where it is already proven
    against duplicate taps and submit timeouts that commit after the client
    gives up. This is the stable entry point for the manual path; the import is
    late so the two modules do not depend on each other at import time.

    The caller is responsible for having authenticated the manager
    (aprobacion.manejar_boton checks router.es_equipo on the signed webhook).
    """
    from app.aprobacion import confirmar_pedido

    return confirmar_pedido(nombre, por)


def rechazar(nombre: str, por: str, motivo: str = "") -> dict:
    """Reject an exception order by hand. HUMAN MANAGER ONLY.

    THE SILENCE THIS FIXES
    Until now tapping [Rechazar] only told the MANAGER that nothing had
    changed. The customer — who had been told the order was received and would
    be confirmed shortly — was never told anything and waited indefinitely.
    That is the worst outcome this system can produce, so the customer notice
    comes first and its success is reported back to the manager.

    The draft is NOT deleted or cancelled. A generic ERPNext Sales Order has no
    durable "rejected" state, and inventing one by deleting the document would
    destroy the audit trail of something the customer was already told about.
    The rejection is recorded as a Comment and the manager is told the draft is
    still there to amend or remove in ERPNext.
    """
    razon = (motivo or "").strip()
    tel = telefono_del_cliente(nombre)

    avisado = False
    if tel:
        avisado = _avisar_cliente_rechazo(nombre, tel, razon)
    else:
        _comentar(
            nombre,
            "Rechazo no avisado al cliente: no tiene teléfono cargado. "
            "Requiere contacto manual.",
        )

    _comentar(
        nombre,
        f"Rechazado manualmente por un integrante autorizado ({por}). "
        f"Motivo: {razon or 'sin detalle'}. "
        f"Aviso al cliente: {'enviado' if avisado else 'NO enviado'}. "
        "El borrador queda sin confirmar para revisión en ERPNext.",
    )

    if avisado:
        detalle = "Rechazo registrado y cliente avisado."
    elif tel:
        detalle = "Rechazo registrado, pero NO pude avisarle al cliente."
    else:
        detalle = "Rechazo registrado; el cliente no tiene teléfono cargado."
    return _resultado(True, avisado, detalle)


def _comentar(nombre: str, texto: str) -> None:
    try:
        erpnext.add_comment("Sales Order", nombre, texto)
    except Exception as exc:
        print(f"[decisiones] {nombre}: comentario falló ({type(exc).__name__})")


def _texto_rechazo(nombre: str, razon: str) -> str:
    """Bilingual: outside a model turn the customer's language is unknown."""
    detalle_es = f" ({razon})" if razon else ""
    detalle_en = f" ({razon})" if razon else ""
    return (
        f"Hola! Sobre tu pedido {nombre}: no vamos a poder cumplirlo"
        f"{detalle_es}. En breve te escribe alguien del equipo. Perdón por la molestia."
        f"\n\nHi! About your order {nombre}: we won't be able to fulfil it"
        f"{detalle_en}. Someone from our team will message you shortly. Sorry about that."
    )


def _avisar_cliente_rechazo(nombre: str, tel: str, razon: str) -> bool:
    """Free text first; an approved template only if Meta closed the window.

    Returns True only when Meta acknowledged a concrete message id.
    """
    from app import whatsapp
    from app.outbound_status import has_accepted, record_outbound

    try:
        if has_accepted(nombre, _PURPOSE_RECHAZO):
            return True
    except Exception as exc:
        print(f"[decisiones] {nombre}: has_accepted falló ({type(exc).__name__})")

    wamid = ""
    try:
        result = whatsapp.enviar_mensaje(tel, _texto_rechazo(nombre, razon))
        wamid = _wamid(result)
    except Exception as exc:
        print(f"[decisiones] {nombre}: texto de rechazo falló ({type(exc).__name__})")
        wamid = _plantilla_rechazo(nombre, tel, razon)

    if not wamid:
        return False
    try:
        record_outbound(wamid, _PURPOSE_RECHAZO, order_name=nombre)
    except Exception as exc:
        print(f"[decisiones] {nombre}: tracking del rechazo falló ({type(exc).__name__})")
    return True


def _plantilla_rechazo(nombre: str, tel: str, razon: str) -> str:
    """Fallback for a closed 24-hour window. No template configured -> no send."""
    from app import whatsapp

    plantilla = os.getenv("WHATSAPP_CUSTOMER_REJECTED_TEMPLATE", "").strip()
    if not plantilla:
        print(f"[decisiones] {nombre}: falta WHATSAPP_CUSTOMER_REJECTED_TEMPLATE")
        return ""
    try:
        result = whatsapp.enviar_plantilla(
            tel,
            plantilla,
            os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "es_AR").strip() or "es_AR",
            [nombre, razon or "sin detalle"],
        )
        return _wamid(result)
    except Exception as exc:
        print(f"[decisiones] {nombre}: plantilla de rechazo falló ({type(exc).__name__})")
        return ""


def _wamid(result: object) -> str:
    if not isinstance(result, dict):
        return ""
    messages = result.get("messages")
    if not isinstance(messages, list) or not messages:
        return ""
    first = messages[0]
    wamid = first.get("id") if isinstance(first, dict) else None
    return wamid.strip() if isinstance(wamid, str) else ""
