"""Morning briefing. Cron at 07:00 America/Argentina/Buenos_Aires.

This is the single feature that makes the owner FEEL the system is working
for him. He wakes up and the business has already reported in.

Se corre como un job aparte, no dentro del webhook:
    docker compose run --rm agente python -m app.briefing
El cron está en deploy/crontab (ver README).
"""

from __future__ import annotations

import sys

from app import log, router
from app.graph import responder_gerencia
from app.whatsapp import enviar_mensaje

_log = log.get("briefing")

PROMPT = (
    "Preparame el resumen de la mañana en 5 líneas como máximo: "
    "pedidos pendientes de confirmar, ventas de los últimos 7 días, "
    "alertas de stock bajo y cobranzas vencidas. "
    "Arrancá con 'Buen día'. Si algo necesita acción hoy, decilo primero."
)


def enviar_briefing() -> int:
    """Devuelve la cantidad de briefings que salieron bien."""
    if not router.STAFF:
        _log.error("TELEFONOS_EQUIPO está vacío: no hay a quién mandarle el briefing")
        return 0

    enviados = 0
    for numero in router.STAFF:
        try:
            # thread_id distinto por día no hace falta: el briefing es un
            # hilo aparte del de conversación, y el checkpointer lo mantiene
            # corto porque siempre es la misma pregunta.
            texto = responder_gerencia(PROMPT, thread_id=f"briefing:{numero}", usuario=numero)
            if not texto:
                _log.error("briefing vacío para %s", numero)
                continue
            if enviar_mensaje(numero, texto):
                enviados += 1
        except Exception:
            _log.exception("briefing falló para %s", numero)
    _log.info("briefing enviado a %d de %d", enviados, len(router.STAFF))
    return enviados


if __name__ == "__main__":
    # Exit code distinto de 0 si no salió ninguno, para que el cron lo
    # reporte en lugar de fallar en silencio.
    sys.exit(0 if enviar_briefing() else 1)
