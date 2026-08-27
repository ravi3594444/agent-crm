"""One webhook, two agents.

Route by phone number:
  - the owner and staff  -> management agent (broad read, trusted)
  - everyone else        -> customer agent   (narrow, untrusted input)

This split is a SECURITY boundary, not a convenience. If the customer-facing
bot had full system read, one prompt injection would hand a stranger the
customer list, margins and supplier prices.
"""
import os

STAFF = {t.strip() for t in os.getenv("TELEFONOS_EQUIPO", "").split(",") if t.strip()}


def es_equipo(telefono: str) -> bool:
    return telefono.lstrip("+") in {s.lstrip("+") for s in STAFF}
