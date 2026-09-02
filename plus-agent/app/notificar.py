"""Notify staff about an order using approved WhatsApp templates.

The customer's inbound message does not open a service window for the
owner's different phone number.  These alerts therefore must never fall back
to a free-form or free-form interactive message.
"""
import hashlib
import os

from app import erpnext
from app.outbound_status import has_accepted, record_outbound
from app.router import STAFF
from app.whatsapp import enviar_plantilla


def notificar_equipo(
    nombre: str, so: dict, auto: bool, motivos: str = ""
) -> bool:
    """Return True only when Meta accepts at least one staff notification."""
    detalle = "; ".join(
        f"{i['qty']:g} x {i.get('item_name') or i['item_code']}"
        for i in so.get("items", [])
    )[:1000] or "Sin líneas"
    plantilla = os.getenv(
        "WHATSAPP_STAFF_CONFIRMED_TEMPLATE"
        if auto
        else "WHATSAPP_STAFF_PENDING_TEMPLATE",
        "",
    ).strip()
    idioma = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "es_AR").strip() or "es_AR"

    if not STAFF:
        print(f"[staff-notify] {nombre}: TELEFONOS_EQUIPO vacío")
        erpnext.add_comment(
            "Sales Order",
            nombre,
            "Alerta al equipo no enviada: TELEFONOS_EQUIPO no está configurado.",
        )
        return False
    if not plantilla:
        variable = (
            "WHATSAPP_STAFF_CONFIRMED_TEMPLATE"
            if auto
            else "WHATSAPP_STAFF_PENDING_TEMPLATE"
        )
        print(f"[staff-notify] {nombre}: falta {variable}")
        erpnext.add_comment(
            "Sales Order",
            nombre,
            f"Alerta al equipo no enviada: falta configurar {variable}.",
        )
        return False

    parametros = [
        nombre,
        "Confirmado" if auto else "Pendiente de revisión",
        str(so.get("customer_name") or so.get("customer") or "Cliente"),
        detalle,
        f"{float(so.get('grand_total') or 0):.2f}",
        str(so.get("delivery_date") or "Sin fecha"),
        (motivos or "Sin observaciones")[:1000],
    ]
    # A generic ERPNext Sales Order has no durable "rejected draft" state.
    # Offer only actions whose state transition we can enforce truthfully.
    acciones = None if auto else [f"ok:{nombre}", f"ver:{nombre}"]

    enviados = 0
    telefonos = sorted(STAFF)
    if os.getenv("NOTIFICAR_SOLO_PRIMERO", "true").lower() == "true":
        telefonos = telefonos[:1]
    for telefono in telefonos:
        recipient_tag = hashlib.sha256(telefono.encode()).hexdigest()[:12]
        purpose = (
            "staff_order_confirmed" if auto else "staff_order_pending"
        ) + f":{recipient_tag}"
        try:
            if has_accepted(nombre, purpose):
                enviados += 1
                continue
            result = enviar_plantilla(
                telefono,
                plantilla,
                idioma,
                parametros,
                acciones,
            )
            enviados += 1
            wamid = result["messages"][0]["id"]
            try:
                record_outbound(wamid, purpose, order_name=nombre)
            except Exception as tracking_error:
                # The API acceptance is still real. Record the observability
                # gap without causing a duplicate template send.
                print(
                    f"[staff-notify] {nombre}: tracking falló "
                    f"({type(tracking_error).__name__})"
                )
                erpnext.add_comment(
                    "Sales Order",
                    nombre,
                    "Meta aceptó la alerta al equipo, pero no se pudo guardar "
                    "su seguimiento de entrega.",
                )
        except Exception as e:
            print(
                f"[staff-notify] {nombre}: envío falló "
                f"({type(e).__name__})"
            )

    if enviados:
        erpnext.add_comment(
            "Sales Order",
            nombre,
            f"Alerta de WhatsApp aceptada por Meta para {enviados} integrante(s).",
        )
        return True

    erpnext.add_comment(
        "Sales Order",
        nombre,
        "Alerta al equipo no enviada; requiere seguimiento manual.",
    )
    return False


# ---------------------------------------------------------------------------
# Exception alerts to the HUMAN MANAGER.
#
# Everything that needs a human's attention outside the normal order flow goes
# through alertar_excepcion(). It is the single funnel on purpose: the client
# wants the AI Management Agent to be able to PHONE the manager for urgent
# situations later, and that channel plugs in here — one place to add, with
# every caller already routed through it. No call code is written yet.
#
# These alerts are informational. They never decide anything: confirming or
# rejecting an order stays in app/decisiones.py, reachable only from the signed
# webhook after router.es_equipo authenticates the manager.
# ---------------------------------------------------------------------------

# Urgency is carried explicitly so a future voice channel can pick a threshold
# without every caller having to be revisited.
URGENCIA_NORMAL = "normal"
URGENCIA_ALTA = "alta"


def alertar_excepcion(
    asunto: str,
    cuerpo: str,
    *,
    urgencia: str = URGENCIA_NORMAL,
    plantilla_env: str = "",
    parametros: list[str] | None = None,
) -> bool:
    """Tell the human manager something needs them. Never raises.

    Free text first, because staff usually have an open window; an approved
    template only if Meta refuses and one is configured. Returns True when Meta
    acknowledged at least one message, so callers can be honest about whether
    the manager was really reached.
    """
    from app import router, whatsapp

    destinatarios = _destinatarios(router)
    if not destinatarios:
        print(f"[alerta] {asunto}: TELEFONOS_EQUIPO vacío, nadie fue avisado")
        return False

    texto = f"{asunto}\n{cuerpo}".strip()[:3500]
    idioma = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "es_AR").strip() or "es_AR"
    plantilla = os.getenv(plantilla_env, "").strip() if plantilla_env else ""

    enviados = 0
    for numero in destinatarios:
        try:
            whatsapp.enviar_mensaje(numero, texto)
            enviados += 1
            continue
        except Exception as exc:
            print(f"[alerta] {asunto}: texto falló ({type(exc).__name__})")
        if not plantilla:
            continue
        try:
            whatsapp.enviar_plantilla(
                numero, plantilla, idioma, parametros or [asunto, cuerpo[:512]]
            )
            enviados += 1
        except Exception as exc:
            print(f"[alerta] {asunto}: plantilla falló ({type(exc).__name__})")

    if not enviados:
        print(f"[alerta] {asunto}: urgencia={urgencia} no llegó a nadie")
    return bool(enviados)


def _destinatarios(router_module) -> list[str]:
    """Staff phones, read at call time so a reload of TELEFONOS_EQUIPO applies."""
    staff = getattr(router_module, "STAFF", None) or []
    numeros = sorted(staff)
    if os.getenv("NOTIFICAR_SOLO_PRIMERO", "true").lower() == "true":
        return numeros[:1]
    return numeros


def avisar_falla_tecnica(telefono: str, texto: str, error: str) -> bool:
    """A customer got the technical-problem apology. That text says the team was
    told, so this makes it true."""
    return alertar_excepcion(
        "⚠️ Falló un mensaje de WhatsApp",
        (
            f"Cliente: {telefono}\n"
            f"Mensaje: {str(texto)[:300]}\n"
            f"Error: {str(error)[:200]}\n"
            "El cliente recibió una disculpa; nadie le respondió todavía."
        ),
        urgencia=URGENCIA_ALTA,
        plantilla_env="WHATSAPP_STAFF_ALERT_TEMPLATE",
    )


def avisar_escalamiento(
    motivo: str, telefono: str, cliente: str, tarea: str = ""
) -> bool:
    """An ERPNext ToDo is invisible until someone opens the system; a complaint
    would wait until morning. This makes the phone ring instead."""
    cuerpo = (
        f"Cliente: {cliente or 'no registrado'}\n"
        f"Tel: {telefono or 'n/d'}\n"
        f"Motivo: {str(motivo)[:300]}"
    )
    if tarea:
        cuerpo += f"\nTarea: {tarea}"
    return alertar_excepcion(
        "🙋 Un cliente necesita una persona",
        cuerpo,
        urgencia=URGENCIA_ALTA,
        plantilla_env="WHATSAPP_STAFF_ALERT_TEMPLATE",
    )
