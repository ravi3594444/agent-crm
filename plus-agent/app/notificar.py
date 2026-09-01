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
            except Exception as tracking_error:  # noqa: BLE001
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
        except Exception as e:  # noqa: BLE001
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
