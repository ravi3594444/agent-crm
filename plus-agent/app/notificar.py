"""Notify staff about an order.

Preferred channel: an approved WhatsApp template. A business-initiated message
to the owner's phone needs one, because the customer's inbound message does
not open a 24-hour service window for a different number.

Fallback: while a staff phone has ITS OWN window open (it wrote to the bot in
the last 24 hours) Meta allows free-form messages to it, so the alert goes out
as free text with reply buttons. Otherwise the alert fails closed and ERPNext
records that manual follow-up is required. The bot never claims an alert that
Meta did not accept.
"""
import hashlib
import os

from app import erpnext
from app.outbound_status import has_accepted, record_outbound, window_open
from app.router import STAFF
from app.whatsapp import enviar_botones, enviar_mensaje, enviar_plantilla


def _texto_libre(nombre: str, so: dict, auto: bool, motivos: str, detalle: str) -> str:
    estado = "confirmado automáticamente" if auto else "pendiente de revisión"
    icono = "✅" if auto else "🟡"
    lineas = [
        f"{icono} Pedido {estado}",
        f"Pedido: {nombre}",
        f"Cliente: {so.get('customer_name') or so.get('customer') or 'Cliente'}",
        f"Items: {detalle}",
        f"Total: {float(so.get('grand_total') or 0):,.2f}",
        f"Entrega: {so.get('delivery_date') or 'Sin fecha'}",
    ]
    if not auto:
        lineas.append(f"Motivo: {(motivos or 'Sin observaciones')[:300]}")
        lineas.append(f"Respondé 'confirmar {nombre}' o 'ver {nombre}'.")
    # Interactive bodies are capped at 1024 characters by Meta.
    return "\n".join(lineas)[:1024]


def notificar_equipo(
    nombre: str, so: dict, auto: bool, motivos: str = ""
) -> bool:
    """Return True only when Meta accepts at least one staff notification."""
    detalle = "; ".join(
        f"{i['qty']:g} x {i.get('item_name') or i['item_code']}"
        for i in so.get("items", [])
    )[:1000] or "Sin líneas"
    variable = (
        "WHATSAPP_STAFF_CONFIRMED_TEMPLATE"
        if auto
        else "WHATSAPP_STAFF_PENDING_TEMPLATE"
    )
    plantilla = os.getenv(variable, "").strip()
    idioma = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "es_AR").strip() or "es_AR"

    if not STAFF:
        print(f"[staff-notify] {nombre}: TELEFONOS_EQUIPO vacío")
        erpnext.add_comment(
            "Sales Order",
            nombre,
            "Alerta al equipo no enviada: TELEFONOS_EQUIPO no está configurado.",
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
    texto = _texto_libre(nombre, so, auto, motivos, detalle)
    botones = (
        None
        if auto
        else [
            {"id": f"ok:{nombre}", "title": "Confirmar"},
            {"id": f"ver:{nombre}", "title": "Ver detalle"},
        ]
    )

    enviados = 0
    sin_canal = 0
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
            if plantilla:
                result = enviar_plantilla(
                    telefono,
                    plantilla,
                    idioma,
                    parametros,
                    acciones,
                )
            elif window_open(telefono):
                result = (
                    enviar_botones(telefono, texto, botones)
                    if botones
                    else enviar_mensaje(telefono, texto)
                )
            else:
                sin_canal += 1
                print(
                    f"[staff-notify] {nombre}: falta {variable} y la ventana de "
                    f"24 h de {recipient_tag} está cerrada"
                )
                continue
            enviados += 1
            wamid = result["messages"][0]["id"]
            try:
                record_outbound(wamid, purpose, order_name=nombre)
            except Exception as tracking_error:  # noqa: BLE001
                # The API acceptance is still real. Record the observability
                # gap without causing a duplicate send.
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

    if sin_canal:
        erpnext.add_comment(
            "Sales Order",
            nombre,
            f"Alerta al equipo no enviada: falta configurar {variable} y ningún "
            "teléfono del equipo escribió al bot en las últimas 24 h. "
            "Requiere seguimiento manual.",
        )
    else:
        erpnext.add_comment(
            "Sales Order",
            nombre,
            "Alerta al equipo no enviada; requiere seguimiento manual.",
        )
    return False
