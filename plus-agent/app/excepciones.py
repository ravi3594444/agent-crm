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


def activa() -> bool:
    return os.getenv("ENTREGA_EXCEPCION_ACTIVA", "").strip().lower() in {
        "true",
        "1",
        "si",
        "sí",
        "yes",
    }


def dias_habilitados() -> list[int]:
    """Weekday numbers the owner pre-authorized, in ISO Monday-0 form."""
    dias: list[int] = []
    for parte in os.getenv("ENTREGA_EXCEPCION_DIAS", "").split(","):
        indice = _DIAS.get(_sin_tildes(parte))
        if indice is not None and indice not in dias:
            dias.append(indice)
    return sorted(dias)


def hora_configurada() -> str:
    """The configured time, or '' when it is missing or not a real time."""
    crudo = os.getenv("ENTREGA_EXCEPCION_HORA", "").strip()
    encontrado = _HORA.match(crudo)
    if not encontrado:
        return ""
    return f"{int(encontrado.group(1)):02d}:{encontrado.group(2)}"


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


def _proxima_fecha(dias: list[int], hoy: date) -> str:
    """The next pre-authorized day, today included, inside the horizon."""
    for adelanto in range(HORIZONTE_DIAS + 1):
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
    try:
        en_zona, motivo_zona = entrega.autorizada(sales_order)
    except Exception as exc:
        print(f"[excepciones] zona no verificable ({type(exc).__name__})")
        return Evaluacion(False, None, "no pude verificar la zona de entrega")
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
