"""¿Esta excepción ya la autorizó el dueño de antemano, o la decide él ahora?

"Necesito 5 kg de leche. Hoy no hay reparto, ¿pero me lo pueden traer?"

Esa pregunta tiene dos respuestas posibles y NINGUNA la puede dar el modelo:

  * el dueño dejó configurado que fuera de día de reparto SÍ se entrega, tal
    día, a tal hora y con tal cargo. Entonces la oferta ya existe y el agente
    la repite tal cual, sin molestar a nadie;
  * no hay nada configurado que cubra el caso. Entonces se abre una
    DecisionRequest (app/solicitudes.py) y decide una persona.

WHY THIS IS NOT A PROMPT
A model asked "can you make an exception?" will eventually say yes: it is the
agreeable answer, and a customer only has to insist. So there is no judgement
here, only comparisons against values the owner set. The model may ASK for the
exception and REPEAT what was decided; it never decides.

AND WHAT IF NOBODY ANSWERS?
Then the request expires (app/solicitudes.py) and the customer still deserves
an answer with something in it. ``evaluar_respaldo`` computes that answer the
same way: the next NORMAL delivery day the owner configured, or — when there is
no round to put them on — a pickup at the shop, if he enabled one. It is the
same kind of comparison against the same kind of value; no model is asked what
would be reasonable.

FAIL CLOSED
Anything missing, unparseable or contradictory means "not pre-authorized",
which routes the case to a person. That is the direction where a wrong answer
costs one WhatsApp message instead of a delivery nobody can make.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta

from app import entrega, erpnext

# El horizonte: una excepción es "el próximo día habilitado", no una fecha
# cualquiera del futuro. Sin tope, un pedido podría quedar prometido para
# dentro de dos meses porque el dueño habilitó un solo día de la semana.
HORIZONTE_DIAS = 7



@dataclass(frozen=True)
class Oferta:
    """Lo que se le puede ofrecer al cliente, ya decidido por configuración."""

    fecha: str
    hora: str
    cargo: float
    metodo: str = "entrega"

    def como_dict(self) -> dict:
        return {
            "fecha": self.fecha,
            "hora": self.hora,
            "cargo": self.cargo,
            "metodo": self.metodo,
        }


@dataclass(frozen=True)
class Evaluacion:
    preautorizada: bool
    oferta: Oferta | None
    motivo: str


# WHERE THESE VALUES COME FROM
# The OWNER's store first, then the bootstrap environment, then a safe default
# — app/limites.py resolves all three and normalizes the result, behind the
# same two-step confirmation code every other setting needs. Read on EVERY
# call, so a change he confirms applies to the next message with nothing
# restarted; and it fails soft, so a value that cannot be read reads as "not
# configured" and this module then answers "not pre-authorized".


def reglas():
    """The owner's delivery rules as they stand right now. Never raises."""
    from app import limites

    try:
        return limites.entrega()
    except Exception as exc:
        print(f"[excepciones] reglas de entrega no legibles ({type(exc).__name__})")
        return limites.Entrega()


# The named readers below are the readable surface for the readiness report,
# the management tools and the tests. The evaluators do NOT use them: they take
# one ``reglas()`` snapshot each, so a decision cannot be assembled from two
# different versions of the configuration.


def activa() -> bool:
    return reglas().excepcion_activa


def dias_habilitados() -> list[int]:
    """Weekday numbers the owner pre-authorized, in ISO Monday-0 form."""
    return list(reglas().excepcion_dias)


def hora_configurada() -> str:
    """The configured time, or '' when it is missing or not a real time."""
    return reglas().excepcion_hora


# --- The fallback: the NORMAL round, and the shop counter. ------------------
# These are not exception values. The normal delivery round and the pickup
# counter only ever produce an offer AFTER a request expired unanswered, so
# they never compete with the deterministic path or with a person's decision.


def dias_reparto() -> list[int]:
    """The weekdays the normal delivery round goes out."""
    return list(reglas().dias_reparto)


def hora_reparto() -> str:
    """The time the normal round is promised for."""
    return reglas().hora_reparto


def retiro_activo() -> bool:
    return reglas().retiro_activo


def dias_retiro() -> list[int]:
    return list(reglas().retiro_dias)


def hora_retiro() -> str:
    return reglas().retiro_hora


def cargo_configurado() -> float | None:
    return reglas().excepcion_cargo


def minimo_configurado() -> float:
    """Order total below which no exception is pre-authorized. 0 means none."""
    return reglas().excepcion_minimo


def cuenta_cargo() -> str:
    """The account head a delivery charge is booked against, or ''.

    Without it a fee cannot be written into the order at all: a charge lives in
    a Sales Taxes and Charges row against an account, and guessing one would
    make the customer's total wrong in the accounts. Empty means the automatic
    path stops before confirming and asks a person to add the charge.

    Environment only, and never through the management agent — see
    app/limites.py::cuenta_cargo.
    """
    from app import limites

    return limites.cuenta_cargo()


def descripcion_cargo() -> str:
    return os.getenv("ENTREGA_CARGO_DESCRIPCION", "").strip() or "Envío fuera de día"


def _hoy() -> date:
    from app import policy

    return policy._hoy_del_negocio()


def _proxima_fecha(dias: list[int], hoy: date, *, desde: int = 0) -> str:
    """The next configured day inside the horizon, no earlier than ``desde``.

    ``desde=0`` includes today, which is what a live exception wants: the
    customer is writing now and the owner said today is fine. ``desde=1``
    excludes it, which is what the expiry fallback wants: that request sat
    unanswered for hours, and today's round may already have left.
    """
    for adelanto in range(max(0, desde), HORIZONTE_DIAS + 1):
        candidato = hoy + timedelta(days=adelanto)
        if candidato.weekday() in dias:
            return candidato.isoformat()
    return ""


def evaluar_entrega(sales_order: dict, *, hoy: date | None = None) -> Evaluacion:
    """Is an off-schedule delivery for THIS order already authorized?

    Every condition is a comparison against something the owner configured:
    the switch, the days, the time, the fee, the minimum order, and the normal
    delivery zone — because an exception is about the DAY, never about driving
    somewhere there is no route to.
    """
    # ONE snapshot for the whole evaluation. Reading each value separately
    # would be five Redis round-trips AND five chances for the owner to change
    # something mid-decision — an offer that pairs last week's days with
    # today's time is not a rule anybody configured.
    cfg = reglas()

    if not cfg.excepcion_activa:
        return Evaluacion(False, None, "el dueño no habilitó entregas fuera de día")

    dias = list(cfg.excepcion_dias)
    if not dias:
        return Evaluacion(False, None, "no hay días de excepción configurados")
    hora = cfg.excepcion_hora
    if not hora:
        return Evaluacion(False, None, "no hay hora de excepción configurada")
    cargo = cfg.excepcion_cargo
    if cargo is None:
        return Evaluacion(False, None, "no hay cargo de excepción configurado")

    try:
        total = float(sales_order.get("grand_total") or 0)
    except (TypeError, ValueError):
        return Evaluacion(False, None, "el total del pedido no se pudo leer")
    minimo = cfg.excepcion_minimo
    if minimo > 0 and total < minimo:
        return Evaluacion(
            False,
            None,
            "el pedido no llega al mínimo configurado para una excepción",
        )

    # An exception moves the DAY, not the map: outside the delivery zones there
    # is no route at all, and no fee makes one appear.
    en_zona, motivo_zona = _en_zona(sales_order)
    if not en_zona:
        return Evaluacion(False, None, motivo_zona or "la dirección no está en zona")

    try:
        fecha = _proxima_fecha(dias, hoy or _hoy())
    except erpnext.ERPNextError:
        return Evaluacion(False, None, "no pude establecer la fecha de hoy")
    if not fecha:
        return Evaluacion(
            False, None, "ningún día habilitado cae en los próximos días"
        )

    return Evaluacion(True, Oferta(fecha=fecha, hora=hora, cargo=cargo), "")


def texto_oferta(oferta: Oferta, moneda: str = "") -> str:
    """The offer as the customer reads it. Built here, not by a model."""
    from app.formato import pesos

    cargo = (
        "sin cargo extra"
        if oferta.cargo <= 0
        else f"con un cargo de {pesos(oferta.cargo, 2)} {moneda}".strip()
    )
    return f"{oferta.fecha} a las {oferta.hora}, {cargo}"


def _en_zona(sales_order: dict) -> tuple[bool, str]:
    """Can this address be delivered to at all? Never raises."""
    try:
        return entrega.autorizada(sales_order)
    except Exception as exc:
        print(f"[excepciones] zona no verificable ({type(exc).__name__})")
        return False, "no pude verificar la zona de entrega"


def evaluar_respaldo(sales_order: dict, *, hoy: date | None = None) -> Evaluacion:
    """What can be offered INSTEAD, now that nobody answered in time.

    An expired request must not leave the customer with "write to me again":
    they already wrote, and waiting was our side's failure. So this returns a
    CONCRETE second offer, and it is arithmetic on the owner's own values, not
    a judgement:

      1. the next NORMAL delivery day (``ENTREGA_DIAS`` / ``ENTREGA_HORA``),
         because the customer asked to be delivered to and a normal round keeps
         that intent. It still needs an address inside the delivery zones: the
         exception moved the day, and so does this;
      2. otherwise a pickup at the shop (``RETIRO_LOCAL_*``), which needs no
         route and no zone — the customer comes to us.

    Both carry NO fee. A normal round day is the ordinary price, and a pickup
    has nothing to charge for; that also keeps the offer independent of
    ``ENTREGA_CARGO_CUENTA``, so accepting it can never stall on a missing
    account. Today is excluded on purpose (see ``_proxima_fecha``).

    Fails closed: when nothing can be computed, ``preautorizada`` is False and
    ``motivo`` lists every reason, so the person who has to pick up the case
    reads what is missing instead of guessing.
    """
    try:
        dia = hoy or _hoy()
    except erpnext.ERPNextError:
        return Evaluacion(False, None, "no pude establecer la fecha de hoy")

    # One snapshot, for the same reason as evaluar_entrega: the round and the
    # counter are compared against each other here, so they have to be the
    # owner's configuration as of one instant.
    cfg = reglas()
    motivos: list[str] = []

    dias = list(cfg.dias_reparto)
    hora = cfg.hora_reparto
    if not dias:
        motivos.append("no hay días de reparto configurados (ENTREGA_DIAS)")
    elif not hora:
        motivos.append("no hay hora de reparto configurada (ENTREGA_HORA)")
    else:
        fecha = _proxima_fecha(dias, dia, desde=1)
        if not fecha:
            motivos.append("ningún día de reparto cae en los próximos días")
        else:
            en_zona, motivo_zona = _en_zona(sales_order)
            if en_zona:
                return Evaluacion(
                    True, Oferta(fecha=fecha, hora=hora, cargo=0.0), ""
                )
            motivos.append(motivo_zona or "la dirección no está en zona")

    if not cfg.retiro_activo:
        motivos.append("el dueño no habilitó el retiro en el local")
        return Evaluacion(False, None, "; ".join(motivos))

    dias_r = list(cfg.retiro_dias)
    hora_r = cfg.retiro_hora
    if not dias_r:
        motivos.append("no hay días de retiro configurados (RETIRO_LOCAL_DIAS)")
    elif not hora_r:
        motivos.append("no hay hora de retiro configurada (RETIRO_LOCAL_HORA)")
    else:
        fecha_r = _proxima_fecha(dias_r, dia, desde=1)
        if fecha_r:
            return Evaluacion(
                True,
                Oferta(fecha=fecha_r, hora=hora_r, cargo=0.0, metodo="retiro"),
                "",
            )
        motivos.append("ningún día de retiro cae en los próximos días")
    return Evaluacion(False, None, "; ".join(motivos))
