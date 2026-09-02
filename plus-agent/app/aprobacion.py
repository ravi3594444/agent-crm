"""Button taps -> real ERPNext actions.

Only phones on the staff list can approve. A stranger who somehow guesses a
button payload gets nothing.
"""
import os

from app import erpnext, policy
from app.formato import pesos
from app.outbound_status import has_accepted, record_outbound, window_open
from app.router import es_equipo
from app.whatsapp import enviar_mensaje, enviar_plantilla


def _leer_doc(doctype: str, name: str) -> dict:
    """Approval runs outside the LLM and may use the policy credential."""
    getter = getattr(erpnext, "policy_get_doc", erpnext.get_doc)
    return getter(doctype, name)


def manejar_boton(reply_id: str, telefono: str) -> str:
    if not es_equipo(telefono):
        return "No tenés permiso para aprobar pedidos."
    if ":" not in reply_id:
        return "No entendí esa acción."

    accion, nombre = reply_id.split(":", 1)

    if accion == "ok":
        return confirmar_pedido(nombre, telefono)["detalle"]

    if accion == "no":
        # The customer was told the order was received and would be confirmed.
        # Rejecting must therefore tell them too — see app/decisiones.py.
        from app import decisiones

        resultado = decisiones.rechazar(nombre, telefono)
        cola = (
            "Ya le avisé al cliente."
            if resultado["aviso_cliente"]
            else "NO pude avisarle al cliente; contactalo vos."
        )
        return (
            f"❌ {nombre} rechazado. El borrador queda sin confirmar para que lo "
            f"revises o lo borres en ERPNext. {cola}"
        )

    if accion == "conteo":
        # A physical count is a claim about the real world; only a person can
        # make it. The submit uses the policy credential, never an LLM tool.
        from app import decisiones

        return decisiones.confirmar_conteo(nombre, telefono)["detalle"]

    if accion == "ver":
        try:
            so = _leer_doc("Sales Order", nombre)
        except erpnext.ERPNextError:
            return f"No pude abrir {nombre}. Revisalo en ERPNext."
        detalle = "\n".join(
            f"  · {i['qty']:g} x {i.get('item_name') or i['item_code']} "
            f"= {pesos(i.get('amount', 0))}"
            for i in so.get("items", [])
        )
        return (
            f"{nombre} — {so.get('customer_name') or so['customer']}\n{detalle}\n"
            f"Total {pesos(so.get('grand_total', 0))} · entrega {so.get('delivery_date')}"
        )

    return "Acción desconocida."


def confirmar_pedido(nombre: str, por: str) -> dict:
    """Confirm one order on behalf of an ALREADY AUTHENTICATED human manager.

    Moved out of manejar_boton unchanged so app/decisiones.py can offer it as
    the manual-path entry point without duplicating logic that is already
    proven against duplicate taps and submit timeouts that commit after the
    HTTP client gives up. Submission still uses the policy credential via
    erpnext.submit_doc; nothing here is reachable from an LLM tool.

    Returns {"ok", "aviso_cliente", "detalle"} — `detalle` is the text shown to
    the manager.
    """
    try:
        actual = _leer_doc("Sales Order", nombre)
        ya_confirmado = actual.get("docstatus") == 1
        if not ya_confirmado and actual.get("docstatus") != 0:
            return {
                "ok": False,
                "aviso_cliente": False,
                "detalle": f"No se puede confirmar {nombre} en su estado actual.",
            }
        if not ya_confirmado and policy.sin_reserva(actual.get("status")):
            # A rejected draft is left Closed so it stops holding stock.
            # ERPNext does not count a Closed order in reserved_qty even after
            # a submit, so submitting this one would promise units that no
            # reservation system can see, and it would never reach the
            # delivery queue either. Reopening it is a deliberate act.
            return {
                "ok": False,
                "aviso_cliente": False,
                "detalle": (
                    f"{nombre} está {actual.get('status')} — se rechazó antes y ya "
                    "no reserva stock. Si lo querés confirmar, reabrilo en ERPNext "
                    "(estado Draft) y volvé a tocar Confirmar."
                ),
            }
        if not ya_confirmado:
            try:
                erpnext.submit_doc("Sales Order", nombre)
            except erpnext.ERPNextError:
                # A timeout can happen after ERPNext committed. Re-read the
                # source of truth before reporting a failed confirmation.
                actual = _leer_doc("Sales Order", nombre)
                if actual.get("docstatus") != 1:
                    raise
            erpnext.add_comment(
                "Sales Order",
                nombre,
                f"Confirmado por un integrante autorizado mediante WhatsApp ({por}).",
            )
    except erpnext.ERPNextError as error:
        print(f"[approval] {nombre}: {type(error).__name__}")
        return {
            "ok": False,
            "aviso_cliente": False,
            "detalle": (
                f"No pude comprobar la confirmación de {nombre}. Revisalo en ERPNext."
            ),
        }

    prefix = "ℹ️ Ya estaba confirmado." if ya_confirmado else f"✅ {nombre} confirmado."
    if _avisar_cliente(nombre):
        return {
            "ok": True,
            "aviso_cliente": True,
            "detalle": (
                f"{prefix} Meta aceptó o ya tenía registrado el aviso al cliente; "
                "la entrega se controla con sus estados de WhatsApp."
            ),
        }
    return {
        "ok": True,
        "aviso_cliente": False,
        "detalle": (
            f"{prefix} No pude enviar el aviso al cliente; contactalo manualmente."
        ),
    }


def _avisar_cliente(nombre: str) -> bool:
    """Prefer a template: approval may happen after the 24-hour window.

    Without a configured template, a free-form confirmation is sent only while
    the customer's own window is still open (they wrote within 24 hours).
    """
    try:
        purpose = "customer_order_confirmation"
        if has_accepted(nombre, purpose):
            return True
        plantilla = os.getenv("WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE", "").strip()

        so = _leer_doc("Sales Order", nombre)
        cliente = _leer_doc("Customer", so["customer"])
        tel = cliente.get("mobile_no")
        if not tel:
            erpnext.add_comment(
                "Sales Order",
                nombre,
                "Aviso de confirmación no enviado: el cliente no tiene teléfono.",
            )
            return False
        entrega = str(so.get("delivery_date") or "a coordinar")
        if plantilla:
            result = enviar_plantilla(
                tel,
                plantilla,
                os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "es_AR").strip() or "es_AR",
                [nombre, entrega],
            )
        elif window_open(tel):
            result = enviar_mensaje(
                tel,
                f"✅ Tu pedido {nombre} quedó confirmado. Entrega: {entrega}. "
                "¡Gracias!",
            )
        else:
            print(
                f"[customer-notify] {nombre}: falta "
                "WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE y la ventana de 24 h cerró"
            )
            erpnext.add_comment(
                "Sales Order",
                nombre,
                "Aviso de confirmación no enviado: falta la plantilla de WhatsApp "
                "y la ventana de 24 h del cliente está cerrada. Avisarle manualmente.",
            )
            return False
        wamid = result["messages"][0]["id"]
        try:
            record_outbound(wamid, purpose, order_name=nombre)
        except Exception as tracking_error:
            print(
                f"[customer-notify] {nombre}: tracking falló "
                f"({type(tracking_error).__name__})"
            )
            erpnext.add_comment(
                "Sales Order",
                nombre,
                "Meta aceptó la confirmación, pero no se pudo guardar su "
                "seguimiento de entrega.",
            )
        erpnext.add_comment(
            "Sales Order",
            nombre,
            "Meta aceptó el aviso de confirmación para el cliente.",
        )
        return True
    except Exception as e:
        print(f"[customer-notify] {nombre}: falló ({type(e).__name__})")
        erpnext.add_comment(
            "Sales Order",
            nombre,
            "Aviso de confirmación no enviado; requiere seguimiento manual.",
        )
        return False
