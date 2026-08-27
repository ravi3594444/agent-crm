"""Morning briefing. Cron at 07:00 America/Argentina/Buenos_Aires.

This is the single feature that makes the owner FEEL the system is working
for him. He wakes up and the business has already reported in.
"""
import os

from app.graph import responder_gerencia
from app.router import STAFF
from app.whatsapp import enviar_mensaje

PROMPT = (
    "Preparame el resumen de la mañana en 5 líneas como máximo: "
    "pedidos pendientes de confirmar, ventas de los últimos 7 días, "
    "alertas de stock bajo y cobranzas vencidas. "
    "Arrancá con 'Buen día'. Si algo necesita acción hoy, decilo primero."
)


def enviar_briefing() -> None:
    for telefono in STAFF:
        try:
            texto = responder_gerencia(
                PROMPT, thread_id=f"briefing:{telefono}", usuario=telefono
            )
            enviar_mensaje(telefono, texto)
        except Exception as e:  # noqa: BLE001
            print(f"briefing failed for {telefono}: {e}")


if __name__ == "__main__":
    enviar_briefing()
