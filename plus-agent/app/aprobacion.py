"""Button taps -> real ERPNext actions.

Only phones on the staff list can approve. A stranger who somehow guesses a
button payload gets nothing.
"""
from app import erpnext
from app.router import es_equipo
from app.whatsapp import enviar_mensaje


def manejar_boton(reply_id: str, telefono: str) -> str:
    if not es_equipo(telefono):
        return "No tenés permiso para aprobar pedidos."
    if ":" not in reply_id:
        return "No entendí esa acción."

    accion, nombre = reply_id.split(":", 1)

    if accion == "ok":
        try:
            erpnext.submit_doc("Sales Order", nombre)
        except erpnext.ERPNextError as e:
            return f"No pude confirmar {nombre}: {e}"
        erpnext.add_comment(
            "Sales Order", nombre, f"Confirmado por WhatsApp desde {telefono}."
        )
        _avisar_cliente(nombre)
        return f"✅ {nombre} confirmado. Ya le avisé al cliente."

    if accion == "no":
        erpnext.add_comment(
            "Sales Order", nombre, f"Rechazado por WhatsApp desde {telefono}."
        )
        return f"❌ {nombre} marcado como rechazado. Queda en borrador para revisar."

    if accion == "ver":
        so = erpnext.get_doc("Sales Order", nombre)
        detalle = "\n".join(
            f"  · {i['qty']:g} x {i.get('item_name') or i['item_code']} "
            f"= ${i.get('amount', 0):,.0f}"
            for i in so.get("items", [])
        )
        return (
            f"{nombre} — {so.get('customer_name') or so['customer']}\n{detalle}\n"
            f"Total ${so.get('grand_total', 0):,.0f} · entrega {so.get('delivery_date')}"
        )

    return "Acción desconocida."


def _avisar_cliente(nombre: str) -> None:
    try:
        so = erpnext.get_doc("Sales Order", nombre)
        cliente = erpnext.get_doc("Customer", so["customer"])
        tel = cliente.get("mobile_no")
        if tel:
            enviar_mensaje(
                tel,
                f"¡Confirmado! Tu pedido {nombre} está en preparación. "
                f"Entrega prevista: {so.get('delivery_date')}. ¡Gracias!",
            )
    except Exception as e:  # noqa: BLE001
        print(f"customer notify failed for {nombre}: {e}")
