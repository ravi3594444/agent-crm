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
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

from app import entrega, erpnext

# El horizonte: una excepción es "el próximo día habilitado", no una fecha
# cualquiera del futuro. Sin tope, un pedido podría quedar prometido para
# dentro de dos meses porque el dueño habilitó un solo día de la semana.
HORIZONTE_DIAS = 7

_DIAS = {
    "lunes": 0,
    "martes": 1,
    "miercoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sabado": 5,
    "domingo": 6,
}
_HORA = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


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


def _sin_tildes(texto: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", str(texto).lower())
        if unicodedata.category(char) != "Mn"
    ).strip()


def _bandera(variable: str) -> bool:
    return os.getenv(variable, "").strip().lower() in {
        "true",
        "1",
        "si",
        "sí",
        "yes",
    }


def _dias(variable: str) -> list[int]:
    """Weekday numbers from one variable, in ISO Monday-0 form."""
    dias: list[int] = []
    for parte in os.getenv(variable, "").split(","):
        indice = _DIAS.get(_sin_tildes(parte))
        if indice is not None and indice not in dias:
            dias.append(indice)
    return sorted(dias)


def _hora_de(variable: str) -> str:
    """One variable's time, or '' when it is missing or not a real time."""
    encontrado = _HORA.match(os.getenv(variable, "").strip())
    if not encontrado:
        return ""
    return f"{int(encontrado.group(1)):02d}:{encontrado.group(2)}"


def activa() -> bool:
    return _bandera("ENTREGA_EXCEPCION_ACTIVA")


def dias_habilitados() -> list[int]:
    """Weekday numbers the owner pre-authorized, in ISO Monday-0 form."""
    return _dias("ENTREGA_EXCEPCION_DIAS")


def hora_configurada() -> str:
    """The configured time, or '' when it is missing or not a real time."""
    return _hora_de("ENTREGA_EXCEPCION_HORA")


# --- The fallback: the NORMAL round, and the shop counter. ------------------
# These are not exception values. ENTREGA_DIAS/ENTREGA_HORA are the ordinary
# delivery rounds, and RETIRO_LOCAL_* is the counter the customer can come to.
# They only ever produce an offer AFTER a request expired unanswered, so they
# never compete with the deterministic path or with a person's decision.


def dias_reparto() -> list[int]:
    """The weekdays the normal delivery round goes out."""
    return _dias("ENTREGA_DIAS")


def hora_reparto() -> str:
    """The time the normal round is promised for."""
    return _hora_de("ENTREGA_HORA")


def retiro_activo() -> bool:
    return _bandera("RETIRO_LOCAL_ACTIVO")


def dias_retiro() -> list[int]:
    return _dias("RETIRO_LOCAL_DIAS")


def hora_retiro() -> str:
    return _hora_de("RETIRO_LOCAL_HORA")


def _numero(variable: str) -> float | None:
    crudo = os.getenv(variable, "").strip()
    if not crudo:
        return None
    try:
        valor = float(crudo.replace(",", "."))
    except (TypeError, ValueError):
        return None
    return valor if valor >= 0 else None


def cargo_configurado() -> float | None:
    return _numero("ENTREGA_EXCEPCION_CARGO")


def minimo_configurado() -> float:
    """Order total below which no exception is pre-authorized. 0 means none."""
    valor = _numero("ENTREGA_EXCEPCION_MIN_TOTAL")
    return valor if valor is not None else 0.0


def cuenta_cargo() -> str:
    """The account head a delivery charge is booked against, or ''.

    Without it a fee cannot be written into the order at all: a charge lives in
    a Sales Taxes and Charges row against an account, and guessing one would
    make the customer's total wrong in the accounts. Empty means the automatic
    path stops before confirming and asks a person to add the charge.
    """
    return os.getenv("ENTREGA_CARGO_CUENTA", "").strip()


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
    if not activa():
        return Evaluacion(False, None, "el dueño no habilitó entregas fuera de día")

    dias = dias_habilitados()
    if not dias:
        return Evaluacion(False, None, "no hay días de excepción configurados")
    hora = hora_configurada()
    if not hora:
        return Evaluacion(False, None, "no hay hora de excepción configurada")
    cargo = cargo_configurado()
    if cargo is None:
        return Evaluacion(False, None, "no hay cargo de excepción configurado")

    try:
        total = float(sales_order.get("grand_total") or 0)
    except (TypeError, ValueError):
        return Evaluacion(False, None, "el total del pedido no se pudo leer")
    minimo = minimo_configurado()
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

    motivos: list[str] = []

    dias = dias_reparto()
    hora = hora_reparto()
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

    if not retiro_activo():
        motivos.append("el dueño no habilitó el retiro en el local")
        return Evaluacion(False, None, "; ".join(motivos))

    dias_r = dias_retiro()
    hora_r = hora_retiro()
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
