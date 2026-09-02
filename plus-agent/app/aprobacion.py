"""Button taps -> real ERPNext actions.

Only phones on the staff list can approve. A stranger who somehow guesses a
button payload gets nothing.
"""
import os

from app import erpnext
from app.formato import pesos
from app.outbound_status import has_accepted, record_outbound
from app.router import es_equipo
from app.whatsapp import enviar_plantilla


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
        try:
            actual = _leer_doc("Sales Order", nombre)
            ya_confirmado = actual.get("docstatus") == 1
            if not ya_confirmado and actual.get("docstatus") != 0:
                return f"No se puede confirmar {nombre} en su estado actual."
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
                    "Confirmado por un integrante autorizado mediante WhatsApp.",
                )
        except erpnext.ERPNextError as error:
            print(f"[approval] {nombre}: {type(error).__name__}")
            return f"No pude comprobar la confirmación de {nombre}. Revisalo en ERPNext."

        prefix = "ℹ️ Ya estaba confirmado." if ya_confirmado else f"✅ {nombre} confirmado."
        if _avisar_cliente(nombre):
            return (
                f"{prefix} Meta aceptó o ya tenía registrado el aviso al cliente; "
                "la entrega se controla con sus estados de WhatsApp."
            )
        return (
            f"{prefix} No pude enviar el aviso al cliente; "
            "contactalo manualmente."
        )

    if accion == "no":
        return (
            "La plantilla actual ya no permite rechazar borradores por WhatsApp, "
            f"porque ERPNext no tiene un estado de rechazo genérico. Revisá {nombre} "
            "en ERPNext; no cambié su estado."
        )

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


def _avisar_cliente(nombre: str) -> bool:
    """Use a template because approval may happen after the 24-hour window."""
    try:
        purpose = "customer_order_confirmation"
        if has_accepted(nombre, purpose):
            return True
        plantilla = os.getenv("WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE", "").strip()
        if not plantilla:
            print(
                f"[customer-notify] {nombre}: falta "
                "WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE"
            )
            erpnext.add_comment(
                "Sales Order",
                nombre,
                "Aviso de confirmación no enviado: falta la plantilla de WhatsApp.",
            )
            return False

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
        result = enviar_plantilla(
            tel,
            plantilla,
            os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "es_AR").strip() or "es_AR",
            [nombre, str(so.get("delivery_date") or "a coordinar")],
        )
        wamid = result["messages"][0]["id"]
        try:
            record_outbound(wamid, purpose, order_name=nombre)
        except Exception as tracking_error:  # noqa: BLE001
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
    except Exception as e:  # noqa: BLE001
        print(f"[customer-notify] {nombre}: falló ({type(e).__name__})")
        erpnext.add_comment(
            "Sales Order",
            nombre,
            "Aviso de confirmación no enviado; requiere seguimiento manual.",
        )
        return False
