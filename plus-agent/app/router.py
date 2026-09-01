"""One webhook, two agents.

Route by phone number:
  - the owner and staff  -> management agent (broad read, trusted)
  - everyone else        -> customer agent   (narrow, untrusted input)

This split is a SECURITY boundary, not a convenience. If the customer-facing
bot had full system read, one prompt injection would hand a stranger the
customer list, margins and supplier prices.

La comparación es en forma canónica (app/telefono.py). Antes era
`telefono.lstrip("+")` contra el crudo de la variable de entorno, así que
`+5493511234567` en TELEFONOS_EQUIPO no matcheaba `5493511234567` de Meta
si alguien lo escribía con espacios, con 0 o con 15 — y el dueño quedaba
ruteado como cliente, sin acceso a sus propias herramientas.
"""

from __future__ import annotations

import os

from app import log, telefono

_log = log.get("router")


def _cargar_equipo() -> list[str]:
    crudos = [t.strip() for t in os.getenv("TELEFONOS_EQUIPO", "").split(",") if t.strip()]
    normalizados = []
    for t in crudos:
        n = telefono.normalizar(t)
        if not n:
            _log.warning("TELEFONOS_EQUIPO: no pude interpretar %r, lo ignoro", t)
            continue
        if n not in normalizados:
            normalizados.append(n)
    if not normalizados:
        _log.warning(
            "TELEFONOS_EQUIPO está vacío: nadie puede aprobar pedidos ni usar el "
            "agente de gestión, y no se envían notificaciones."
        )
    return normalizados


# Lista ordenada y sin duplicados: el orden importa porque notificar.py
# manda al primero cuando NOTIFICAR_SOLO_PRIMERO=true. Un `set` hacía que
# ese "primero" fuera uno al azar en cada arranque.
STAFF: list[str] = _cargar_equipo()


def es_equipo(numero: str) -> bool:
    n = telefono.normalizar(numero)
    return bool(n) and n in STAFF


def recargar() -> None:
    """Para los tests, y para recargar la lista sin rebuildear la imagen."""
    global STAFF
    STAFF = _cargar_equipo()
