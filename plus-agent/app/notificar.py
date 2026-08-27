"""One-tap approval. The other half of removing the wait.

Instead of "open ERPNext, find the order, review, submit", the owner gets a
WhatsApp with buttons. He taps Confirmar from his lock screen. Two seconds.
"""
import os

from app.router import STAFF
from app.whatsapp import enviar_botones, enviar_mensaje


def notificar_equipo(
    nombre: str, so: dict, auto: bool, motivos: str = ""
) -> None:
    detalle = "\n".join(
        f"  · {i['qty']:g} x {i.get('item_name') or i['item_code']}"
        for i in so.get("items", [])
    )
    cabecera = (
        f"✅ Pedido auto-confirmado {nombre}"
        if auto
        else f"🔔 Pedido {nombre} — necesita tu OK"
    )
    cuerpo = (
        f"{cabecera}\n"
        f"{so.get('customer_name') or so.get('customer')}\n"
        f"{detalle}\n"
        f"Total: ${so.get('grand_total', 0):,.0f} · entrega {so.get('delivery_date')}"
    )
    if motivos:
        cuerpo += f"\n\nPor qué: {motivos}"

    for telefono in STAFF:
        try:
            if auto:
                enviar_mensaje(telefono, cuerpo)
            else:
                enviar_botones(
                    telefono,
                    cuerpo,
                    [
                        {"id": f"ok:{nombre}", "title": "Confirmar"},
                        {"id": f"no:{nombre}", "title": "Rechazar"},
                        {"id": f"ver:{nombre}", "title": "Ver detalle"},
                    ],
                )
        except Exception as e:  # noqa: BLE001
            print(f"notify failed {telefono}: {e}")
        if os.getenv("NOTIFICAR_SOLO_PRIMERO", "true").lower() == "true":
            break
