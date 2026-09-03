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
from datetime import datetime
from zoneinfo import ZoneInfo

from app import entrega, erpnext
from app.formato import cantidad, pesos
from app.outbound_status import (
    claim_once,
    has_accepted,
    record_outbound,
    registrar_aviso_fallido,
    release_claim,
    window_open,
)
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
            except Exception as tracking_error:
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
    # Nobody received it: park it and make sure a person sees a task.
    registrar_aviso_fallido(
        "staff_order_confirmed" if auto else "staff_order_pending", nombre, texto
    )
    return False


# ---------------------------------------------------------------------------
# Stage 2e — the confirmed-order notice: exactly once per order.
#
# Both confirmation paths end here: policy.evaluar + submit in
# app/tools/pedidos.py (automatic) and aprobacion.confirmar_pedido (a human on
# the signed webhook). The first one to claim the order sends; the other finds
# the claim and does nothing. A claim whose send reaches nobody is released, so
# the other path (or a retry) can still notify.
# ---------------------------------------------------------------------------

CONFIRMACION_TTL_SEGUNDOS = 30 * 24 * 60 * 60


def _momento_negocio() -> str:
    zona = os.getenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires").strip()
    try:
        return datetime.now(ZoneInfo(zona)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M")


def _direccion_de_entrega(so: dict) -> str:
    """The delivery address as a person reads it; the Address name if unreadable."""
    nombre = entrega.nombre_direccion(so)
    if not nombre:
        return ""
    try:
        doc = erpnext.policy_get_doc("Address", nombre)
    except Exception:
        return nombre
    return entrega.texto_direccion(doc) if isinstance(doc, dict) else nombre


def _renglones(so: dict) -> str:
    partes = []
    for item in so.get("items") or []:
        if not isinstance(item, dict):
            continue
        unidad = item.get("uom") or item.get("stock_uom") or "u"
        partes.append(
            f"{cantidad(item.get('qty'))} {unidad} × "
            f"{item.get('item_name') or item.get('item_code') or 'producto'}"
        )
    return "; ".join(partes) or "sin renglones"


def texto_confirmacion(so: dict, fuente: str, momento: str | None = None) -> str:
    """What the manager reads: every field the client asked for, in order."""
    direccion = _direccion_de_entrega(so)
    entrega_txt = " — ".join(
        parte for parte in (direccion, str(so.get("delivery_date") or "")) if parte
    ) or "a coordinar"
    total = f"{pesos(so.get('grand_total'), 2)} {so.get('currency') or ''}".strip()
    return "\n".join(
        [
            f"✅ Pedido {so.get('name')} confirmado",
            f"Cliente: {so.get('customer_name') or so.get('customer') or 'Cliente'}",
            f"Items: {_renglones(so)}",
            f"Total: {total}",
            f"Entrega: {entrega_txt}",
            f"Origen: {fuente}",
            f"Confirmado: {momento or _momento_negocio()}",
        ]
    )[:3500]


def notificar_confirmacion(so: dict, fuente: str) -> bool:
    """Tell the human manager an order is confirmed — exactly once per order.

    ``fuente`` is "automática (política)" or "manual (confirmación humana)".
    Returns True when Meta accepted it for at least one staff phone, or when
    the order was already notified. Never raises.
    """
    nombre = str(so.get("name") or "").strip()
    if not nombre:
        return False
    try:
        if not claim_once(f"confirm-notice:{nombre}", CONFIRMACION_TTL_SEGUNDOS):
            return True
    except Exception as exc:
        # Cannot coordinate: a possible duplicate beats a certain silence.
        print(f"[staff-notify] {nombre}: claim no disponible ({type(exc).__name__})")

    momento = _momento_negocio()
    texto = texto_confirmacion(so, fuente, momento)
    plantilla = os.getenv("WHATSAPP_STAFF_CONFIRMED_TEMPLATE", "").strip()
    idioma = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "es_AR").strip() or "es_AR"
    parametros = [
        nombre,
        "Confirmado",
        str(so.get("customer_name") or so.get("customer") or "Cliente"),
        _renglones(so)[:1000],
        f"{float(so.get('grand_total') or 0):.2f} {so.get('currency') or ''}".strip(),
        " — ".join(
            p for p in (_direccion_de_entrega(so), str(so.get("delivery_date") or "")) if p
        )[:1000]
        or "a coordinar",
        f"Origen: {fuente}; confirmado {momento}"[:1000],
    ]

    if not STAFF:
        print(f"[staff-notify] {nombre}: TELEFONOS_EQUIPO vacío, sin aviso de confirmación")
        erpnext.add_comment(
            "Sales Order", nombre, "Aviso de confirmación al equipo no enviado: TELEFONOS_EQUIPO vacío."
        )
        release_claim(f"confirm-notice:{nombre}")
        registrar_aviso_fallido("manager_order_confirmed", nombre, texto)
        return False

    telefonos = sorted(STAFF)
    if os.getenv("NOTIFICAR_SOLO_PRIMERO", "true").lower() == "true":
        telefonos = telefonos[:1]
    enviados = 0
    for telefono in telefonos:
        tag = hashlib.sha256(telefono.encode()).hexdigest()[:12]
        purpose = f"manager_order_confirmed:{tag}"
        try:
            if has_accepted(nombre, purpose):
                enviados += 1
                continue
            if plantilla:
                result = enviar_plantilla(telefono, plantilla, idioma, parametros)
            elif window_open(telefono):
                result = enviar_mensaje(telefono, texto)
            else:
                print(
                    f"[staff-notify] {nombre}: sin plantilla de confirmación y ventana "
                    f"de 24 h de {tag} cerrada"
                )
                continue
            enviados += 1
            try:
                record_outbound(result["messages"][0]["id"], purpose, order_name=nombre)
            except Exception as tracking_error:
                print(f"[staff-notify] {nombre}: tracking falló ({type(tracking_error).__name__})")
        except Exception as exc:
            print(f"[staff-notify] {nombre}: aviso de confirmación falló ({type(exc).__name__})")

    if enviados:
        erpnext.add_comment(
            "Sales Order",
            nombre,
            f"Aviso de pedido confirmado ({fuente}) aceptado por Meta para {enviados} integrante(s).",
        )
        return True

    release_claim(f"confirm-notice:{nombre}")
    erpnext.add_comment(
        "Sales Order",
        nombre,
        "Aviso de pedido confirmado al equipo NO enviado (sin plantilla y sin ventana de "
        "24 h abierta, o Meta lo rechazó). Quedó en la lista de avisos pendientes.",
    )
    registrar_aviso_fallido("manager_order_confirmed", nombre, texto)
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
        registrar_aviso_fallido(f"exception:{asunto[:40]}", "", texto)
    return bool(enviados)


def _destinatarios(router_module) -> list[str]:
    """Staff phones, read at call time so a reload of TELEFONOS_EQUIPO applies."""
    staff = getattr(router_module, "STAFF", None) or []
    numeros = sorted(staff)
    if os.getenv("NOTIFICAR_SOLO_PRIMERO", "true").lower() == "true":
        return numeros[:1]
    return numeros


def pedir_confirmacion_conteo(telefono: str, nombre: str, texto: str) -> bool:
    """Ask the manager to confirm a count with one tap.

    He is by definition inside the 24-hour window — he just sent the count —
    so a free-form interactive message works and no template is needed. Never
    raises: if the button cannot be sent, the caller tells him to confirm it in
    ERPNext instead of pretending it is done.
    """
    from app import whatsapp

    try:
        whatsapp.enviar_botones(
            telefono,
            texto,
            [{"id": f"conteo:{nombre}", "title": "Confirmar conteo"}],
        )
        return True
    except Exception as exc:
        print(f"[staff-notify] botón de conteo {nombre} falló ({type(exc).__name__})")
        return False


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
