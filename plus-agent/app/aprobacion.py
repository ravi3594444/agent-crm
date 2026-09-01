"""Button taps -> real ERPNext actions.

Only phones on the staff list can approve. A stranger who somehow guesses a
button payload gets nothing.

EL SILENCIO QUE ESTO ARREGLA
Antes, tocar [Rechazar] agregaba un comentario y listo. El cliente —al que
le habíamos dicho "te confirmo en unos minutos"— no se enteraba nunca de
nada. Esperaba su leche para siempre. Ese es exactamente el fallo que el
README declara como el peor de todos.

Ahora rechazar avisa al cliente, y el pedido queda cancelado o en borrador
según lo que el dueño decida en el sistema.
"""

from __future__ import annotations

from app import erpnext, formato, log
from app.router import es_equipo
from app.whatsapp import enviar_mensaje

_log = log.get("aprobacion")

ACCIONES = ("ok", "no", "ver")


def manejar_boton(reply_id: str, telefono: str) -> str:
    if not es_equipo(telefono):
        _log.warning("intento de aprobación desde %s (no está en el equipo)", telefono)
        return "No tenés permiso para aprobar pedidos."
    if ":" not in reply_id:
        return "No entendí esa acción."

    accion, nombre = reply_id.split(":", 1)
    if accion not in ACCIONES or not nombre:
        return "Acción desconocida."

    if accion == "ok":
        return _confirmar(nombre, telefono)
    if accion == "no":
        return _rechazar(nombre, telefono)
    return _ver(nombre)


def _confirmar(nombre: str, telefono: str) -> str:
    try:
        erpnext.submit_doc("Sales Order", nombre)
    except erpnext.ERPNextError as e:
        return f"No pude confirmar {nombre}: {e}"
    erpnext.add_comment("Sales Order", nombre, f"Confirmado por WhatsApp desde {telefono}.")
    avisado = _avisar_cliente(
        nombre,
        lambda so: (
            f"¡Confirmado! Tu pedido {nombre} está en preparación. "
            f"Entrega prevista: {so.get('delivery_date')}. ¡Gracias!"
        ),
    )
    if avisado:
        return f"✅ {nombre} confirmado. Ya le avisé al cliente."
    return (
        f"✅ {nombre} confirmado, pero NO pude avisarle al cliente "
        f"(¿teléfono cargado en su ficha?). Avisale vos."
    )


def _rechazar(nombre: str, telefono: str) -> str:
    erpnext.add_comment("Sales Order", nombre, f"Rechazado por WhatsApp desde {telefono}.")
    avisado = _avisar_cliente(
        nombre,
        lambda so: (
            f"Hola! Sobre tu pedido {nombre}: no vamos a poder cumplirlo como "
            f"estaba. En un rato te escribe alguien del equipo para arreglarlo. "
            f"Perdón por la molestia."
        ),
    )
    cola = "Ya le avisé al cliente." if avisado else "NO pude avisarle al cliente — avisale vos."
    return f"❌ {nombre} marcado como rechazado. Queda en borrador para revisar. {cola}"


def _ver(nombre: str) -> str:
    try:
        so = erpnext.get_doc("Sales Order", nombre)
    except erpnext.ERPNextError as e:
        _log.warning("no pude leer %s: %s", nombre, e)
        return f"No pude abrir {nombre} ahora. Probá de nuevo en un momento."
    detalle = "\n".join(
        f"  · {float(i.get('qty') or 0):g} x {i.get('item_name') or i['item_code']} "
        f"= {formato.pesos(float(i.get('amount') or 0))}"
        for i in so.get("items", [])
    )
    return (
        f"{nombre} — {so.get('customer_name') or so.get('customer')}\n{detalle}\n"
        f"Total {formato.pesos(float(so.get('grand_total') or 0))} · "
        f"entrega {so.get('delivery_date')}"
    )


def _avisar_cliente(nombre: str, armar_texto) -> bool:
    """Devuelve True si el mensaje salió. El llamador se lo dice al dueño:
    si el aviso no salió, tiene que saberlo en el momento."""
    try:
        so = erpnext.get_doc("Sales Order", nombre)
        cliente = erpnext.get_doc("Customer", so["customer"])
        tel = cliente.get("mobile_no")
        if not tel:
            _log.warning("el cliente %s no tiene mobile_no", so["customer"])
            return False
        return enviar_mensaje(tel, armar_texto(so))
    except Exception as e:
        _log.error("no pude avisarle al cliente de %s: %s", nombre, e)
        return False
