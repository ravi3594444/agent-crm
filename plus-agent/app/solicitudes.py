"""DecisionRequest: the sales side asks, a HUMAN decides, nothing waits.

THE SHAPE OF THE PROBLEM
A customer asks for something the deterministic rules do not allow — delivery
on a day there is no round, a discount, a different date. Three things must be
true at once:

  * the webhook must answer NOW. A question for a person cannot hold a request
    open, a worker busy or a distributed lock taken;
  * both agents must stay available for every other conversation. A pending
    decision belongs to an ORDER, not to a session;
  * the two models must not talk to each other. What crosses between the sales
    side and the management side is a RECORD with fields, never prose one model
    wrote for the other to interpret.

So a DecisionRequest is a durable document, and every step appends an event to
it. The customer is told immediately that a person was asked. The manager gets a
structured summary and answers with an exact command. Nothing in this module is
an LLM tool.

WHERE IT LIVES
In ERPNext, as append-only Comments on the Sales Order:

    [solicitud] {"id": "...", "evento": "creada", "estado": "pendiente", ...}

Same reason as app/confirmacion.py: Redis is a cache and cannot be the source of
truth for a promise made to a customer. A flush must not lose a pending
decision, and it must not resurrect one either. Each event carries the FULL
snapshot, so reading is "take the last event", not a fold that could go wrong
halfway. Redis holds an index for fast reads and the expiry schedule; both are
rebuilt from ERPNext when they are empty.

THE STOCK QUESTION, ANSWERED HONESTLY
A draft waiting for a person holds the units it asked for — otherwise two
customers get promised the same milk. But it cannot hold them for ever, so the
hold EXPIRES with the request (a manager-configurable timeout), the expiry is
part of the durable record, and app/policy.py stops counting a draft whose hold
has lapsed. When a request dies, the draft is marked so ERPNext itself stops
reserving, and that is only reported as "released" if a re-read proves it. And
the customer is never told the stock is reserved: they are told it will be
re-checked when the answer comes, because that is what actually happens.

AN EXPIRY IS STILL AN ANSWER
Nobody answering is OUR failure, not the customer's, so "write to me again"
is not an acceptable ending: they already wrote. When a request expires the
original dies for good — VENCIDA is terminal, its hold is released, and no
later decision revives it — and a SECOND, separate request is opened carrying a
concrete offer computed from the owner's configuration (app/excepciones.py
``evaluar_respaldo``): the next normal delivery day, or a pickup at the shop.
New id, new expiry, its own event trail, and still no promise — the customer
has to accept it in so many words, and acceptance re-reads and re-validates
everything under the lock exactly like any other offer. A fallback never gets a
fallback of its own, so there is no chain; and when nothing can be computed
nothing is offered, which is the direction that never promises a delivery
nobody can make.

NO DRAFT LIVES WITHOUT A DEADLINE
"Counts against stock" and "waiting for somebody" are NOT the same question
here. ERPNext counts every live draft by default; app/policy.py only ever
SUBTRACTS the ones whose hold has lapsed, and it learns which those are from
``vencimientos``. So a request state that reports no expiry does not stop
holding units — it holds them for ever. REVISION_HUMANA used to be exactly
that: the customer accepted, something had moved, a person was asked, and the
draft sat there reserving milk with nothing that could ever release it.

Hence ``CON_PLAZO``, which is deliberately WIDER than ``ABIERTOS``:

  * ABIERTOS  — somebody still has to answer. Drives every DECISION path
    (which manager command applies, whether an offer can be accepted).
  * CON_PLAZO — has a live deadline the sweep must honour. Drives the index,
    ``vencimientos`` and the sweep.

REVISION_HUMANA is in the second and not the first, on purpose: "confirmar
<pedido>" must keep meaning "submit this draft" while a person is reviewing it,
not "approve the exception again". The review's deadline is the owner's own
number (REVISION_TIMEOUT_HORAS), it is written into the durable record like
every other expiry, and when it lapses the draft is closed so ERPNext itself
stops reserving.

WHICH MAKES ONE RULE ABSOLUTE
A TERMINAL state is, by definition, a request with no deadline. So recording
one for an order that is still a live draft is the bug — it hands the units
back to nobody. A terminal write is therefore only licensed when the draft is
gone: either it is no longer a draft, or ``soltar_reserva`` PROVED the release
by re-reading it. When neither holds, REVISION_HUMANA is written again with a
nearer deadline. It is the one re-entrant state in the module, and that is what
makes "a live draft always bears a deadline" true by construction instead of by
luck.

CUSTOMER TEXT IS DATA
Whatever the customer wrote travels in one field, quoted, and is shown to the
manager as a quotation. It is never a line of the management agent's prompt and
never an instruction to anything. See ``citar``.
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app import erpnext
from app.outbound_status import cliente as _redis
from app.outbound_status import digest_recipiente

MARCA = "[solicitud]"
VERSION = 1

# Types of decision a request can carry. The type decides which terms matter
# and which text the customer and the manager read.
TIPO_ENTREGA = "entrega_excepcion"
TIPOS = (TIPO_ENTREGA,)

# States. A request is either waiting for somebody, or finished.
PENDIENTE = "pendiente"
ESPERANDO_CLIENTE = "esperando_cliente"
CUMPLIDA = "cumplida"
RECHAZADA = "rechazada"
RECHAZADA_CLIENTE = "rechazada_cliente"
VENCIDA = "vencida"
REVISION_HUMANA = "revision_humana"
# How a human review ENDS. Two states, because the two endings are not the same
# fact: somebody dealt with it, or nobody did and the draft had to be closed.
REVISION_RESUELTA = "revision_resuelta"
REVISION_VENCIDA = "revision_vencida"

# Waiting for somebody to answer. Drives the DECISION paths only.
ABIERTOS = frozenset({PENDIENTE, ESPERANDO_CLIENTE})
# Carrying a live deadline the sweep has to honour. Wider than ABIERTOS: a
# human review is nobody's pending decision, but its draft is still reserving
# stock and something must eventually let go of it. See the module docstring.
CON_PLAZO = ABIERTOS | frozenset({REVISION_HUMANA})
TERMINALES = frozenset(
    {
        CUMPLIDA,
        RECHAZADA,
        RECHAZADA_CLIENTE,
        VENCIDA,
        REVISION_RESUELTA,
        REVISION_VENCIDA,
    }
)

# Decisions a human can take on a pending request.
APROBADA = "aprobada"
CONTRAOFERTA = "contraoferta"
RETIRO = "retiro"
# Not a human decision: the deterministic offer that replaces a request nobody
# answered. It is recorded in the same field so "ver <pedido>" and the audit
# trail read the same way, and it can only ever be written by _respaldar.
RESPALDO = "respaldo_automatico"
DECIDE_EL_SISTEMA = "el sistema (regla configurada por el dueño)"

CLAVE_INDICE = "wa:{inbound}:solicitudes"
CACHE_TTL_SEGUNDOS = 30 * 24 * 60 * 60
# Most drafts never carry a decision request, and app/policy.py asks about all
# of them on every stock check. Remembering "this one has none" keeps that from
# costing an ERPNext read per draft per check. It is deliberately short-lived,
# and a request being created writes the real entry over it at once; a stale
# negative can only make a draft look like it still holds its units, which is
# the direction that never oversells.
SIN_SOLICITUD = "-"
SIN_SOLICITUD_TTL_SEGUNDOS = 600
MAX_EVENTOS = 60
MAX_RECONSTRUCCION = 200
# How many pages of history ONE rebuild may read. The index is rebuilt from the
# newest end, so the first page already carries the requests most likely to
# still be live; the budget exists so a store with years of events cannot make
# a restart hang on a background thread.
MAX_PAGINAS_RECONSTRUCCION = 25
# ... and when even that budget ran out, the rebuild is INCOMPLETE, not done.
# The orders it could not reach have no deadline at all, so it is retried this
# soon instead of waiting for the next restart.
REINTENTO_RECONSTRUCCION_SEGUNDOS = 600.0
# vencimientos() asks about many drafts in one read. When that read comes back
# truncated the missing orders are resolved ONE BY ONE, bounded by this, so a
# single order with thousands of events cannot starve every other draft of an
# answer. See vencimientos().
MAX_RESCATES_VENCIMIENTOS = 20
# ERPNext has no "rejected draft" docstatus; "Closed" is the durable state its
# own get_reserved_qty does not count. Same constant as app/decisiones.py.
_ESTADO_SIN_RESERVA = "Closed"
# ... and the state it came from, so an accepted fallback can be confirmed on
# the same document instead of asking the customer to order again.
_ESTADO_BORRADOR = "Draft"
# How soon to try again when the draft could not be closed. Short, because the
# units are still held; not zero, because a 60-second sweep would otherwise
# spin on the same failing order.
REINTENTO_REVISION_SEGUNDOS = 900.0

_JSON = re.compile(re.escape(MARCA) + r"\s*(\{.*\})\s*$", re.DOTALL)


@dataclass(frozen=True)
class Solicitud:
    """One decision a person has to take, with everything needed to take it."""

    id: str
    pedido: str
    tipo: str
    estado: str
    cliente: str
    cliente_nombre: str
    resumen_items: str
    total: float
    moneda: str
    creada_en: float
    vence_en: float
    solicitado: dict = field(default_factory=dict)
    ofrecido: dict = field(default_factory=dict)
    decision: str = ""
    decidida_por: str = ""
    decidida_en: float = 0.0
    motivo: str = ""
    nota_cliente: str = ""
    evento: str = "creada"
    sello: float = 0.0
    cantidades: dict = field(default_factory=dict)
    reabierta_en: float = 0.0
    # A fallback offer: computed after ``origen`` expired unanswered. It is a
    # request in its own right, and the flag is what stops a second one and
    # what makes acceptance re-open the draft its predecessor closed.
    es_respaldo: bool = False
    origen: str = ""

    @property
    def abierta(self) -> bool:
        """Somebody still has to answer. NOT the same as "holds stock"."""
        return self.estado in ABIERTOS

    @property
    def con_plazo(self) -> bool:
        """Has a live deadline: the sweep must reach it and it may hold stock."""
        return self.estado in CON_PLAZO

    @property
    def en_revision(self) -> bool:
        return self.estado == REVISION_HUMANA

    def vencida(self, ahora: float | None = None) -> bool:
        """Past its deadline while it still had one."""
        return self.con_plazo and (ahora or time.time()) >= self.vence_en

    def como_dict(self) -> dict:
        return {
            "v": VERSION,
            "id": self.id,
            "pedido": self.pedido,
            "tipo": self.tipo,
            "estado": self.estado,
            "cliente": self.cliente,
            "cliente_nombre": self.cliente_nombre,
            "resumen_items": self.resumen_items,
            "total": self.total,
            "moneda": self.moneda,
            "creada_en": self.creada_en,
            "vence_en": self.vence_en,
            "solicitado": self.solicitado,
            "ofrecido": self.ofrecido,
            "decision": self.decision,
            "decidida_por": self.decidida_por,
            "decidida_en": self.decidida_en,
            "motivo": self.motivo,
            "nota_cliente": self.nota_cliente,
            "evento": self.evento,
            "sello": self.sello,
            "cantidades": self.cantidades,
            "reabierta_en": self.reabierta_en,
            "es_respaldo": self.es_respaldo,
            "origen": self.origen,
        }


# ---------------------------------------------------------------------------
# Configuration: the approval timeout is the OWNER's number (app/limites.py).
# ---------------------------------------------------------------------------


def timeout_horas() -> float:
    """How long a pending decision — and its stock hold — may live."""
    from app import limites

    try:
        return limites.configuracion().timeout_aprobacion
    except Exception as exc:
        print(f"[solicitudes] timeout no legible ({type(exc).__name__})")
        # Not a guess at a business number: the shortest sane hold, so a
        # misconfiguration cannot park stock for ever.
        return 4.0


def revision_horas() -> float:
    """How long a draft may wait for a PERSON to review it.

    Same contract as ``timeout_horas``: never raises, and an unreadable
    configuration falls back to a bounded number rather than to no bound at
    all. This is the only thing standing between a failed revalidation and a
    draft that reserves stock for ever.
    """
    from app import limites

    try:
        return limites.configuracion().timeout_revision
    except Exception as exc:
        print(f"[solicitudes] plazo de revisión no legible ({type(exc).__name__})")
        return float(limites.LIMITES["REVISION_TIMEOUT_HORAS"].default)


# ---------------------------------------------------------------------------
# Quoting customer text. A prompt-injection attempt has to read as a quotation.
# ---------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def citar(texto: object, tope: int = 400) -> str:
    """Customer words, safe to show a person and useless as an instruction.

    Control characters go, length is capped, every line is prefixed so it reads
    as a quotation, and the caller labels it as customer text. Nothing here is
    ever concatenated into a system prompt: the management agent receives the
    request's FIELDS, and this string only ever travels inside a quotation in a
    deterministic message.
    """
    limpio = _CONTROL.sub(" ", str(texto or "")).strip()
    limpio = re.sub(r"\s+", " ", limpio)[:tope]
    if not limpio:
        return ""
    return "\n".join(f"> {linea}" for linea in limpio.splitlines())


# ---------------------------------------------------------------------------
# The durable record.
# ---------------------------------------------------------------------------


def _ahora() -> float:
    return time.time()


def _sello_utc(momento: float) -> str:
    return datetime.fromtimestamp(momento, UTC).isoformat(timespec="seconds")


def _nuevo_id(pedido: str) -> str:
    return f"DR-{digest_recipiente(pedido)[:8]}-{secrets.token_hex(3)}"


def _clave_cache(pedido: str) -> str:
    return f"wa:{{inbound}}:solicitud:{digest_recipiente(pedido)}"


def _parsear(contenido: str) -> dict | None:
    """One event out of an ERPNext comment, however ERPNext stored it."""
    texto = re.sub(r"<[^>]+>", " ", html.unescape(str(contenido or "")))
    encontrado = _JSON.search(texto)
    if not encontrado:
        return None
    try:
        datos = json.loads(encontrado.group(1))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return datos if isinstance(datos, dict) else None


def _desde_dict(datos: dict) -> Solicitud | None:
    try:
        return Solicitud(
            id=str(datos["id"]),
            pedido=str(datos["pedido"]),
            tipo=str(datos.get("tipo") or TIPO_ENTREGA),
            estado=str(datos["estado"]),
            cliente=str(datos.get("cliente") or ""),
            cliente_nombre=str(datos.get("cliente_nombre") or ""),
            resumen_items=str(datos.get("resumen_items") or ""),
            total=float(datos.get("total") or 0),
            moneda=str(datos.get("moneda") or ""),
            creada_en=float(datos.get("creada_en") or 0),
            vence_en=float(datos.get("vence_en") or 0),
            solicitado=dict(datos.get("solicitado") or {}),
            ofrecido=dict(datos.get("ofrecido") or {}),
            decision=str(datos.get("decision") or ""),
            decidida_por=str(datos.get("decidida_por") or ""),
            decidida_en=float(datos.get("decidida_en") or 0),
            motivo=str(datos.get("motivo") or ""),
            nota_cliente=str(datos.get("nota_cliente") or ""),
            evento=str(datos.get("evento") or ""),
            sello=float(datos.get("sello") or 0),
            cantidades=dict(datos.get("cantidades") or {}),
            reabierta_en=float(datos.get("reabierta_en") or 0),
            es_respaldo=bool(datos.get("es_respaldo") or False),
            origen=str(datos.get("origen") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _escribir(solicitud: Solicitud) -> bool:
    """Append one event durably, then cache it. True only when durable.

    registrar_comentario RAISES on failure, and the caller must treat a False
    as "this did not happen": a decision that exists only in Redis is exactly
    what this module refuses to build.
    """
    cuerpo = json.dumps(solicitud.como_dict(), ensure_ascii=False, separators=(",", ":"))
    try:
        erpnext.registrar_comentario("Sales Order", solicitud.pedido, f"{MARCA} {cuerpo}")
    except Exception as exc:
        print(
            f"[solicitudes] {solicitud.pedido}: evento {solicitud.evento} NO durable "
            f"({type(exc).__name__})"
        )
        return False
    _cachear(solicitud)
    return True


def _cachear(solicitud: Solicitud) -> None:
    cuerpo = json.dumps(solicitud.como_dict(), ensure_ascii=False, separators=(",", ":"))
    try:
        cliente = _redis()
        cliente.set(_clave_cache(solicitud.pedido), cuerpo, ex=CACHE_TTL_SEGUNDOS)
        if solicitud.con_plazo:
            cliente.zadd(CLAVE_INDICE, {solicitud.pedido: solicitud.vence_en})
        else:
            cliente.zrem(CLAVE_INDICE, solicitud.pedido)
    except Exception as exc:
        print(f"[solicitudes] {solicitud.pedido}: caché no guardada ({type(exc).__name__})")


def _leer_cache(pedido: str) -> Solicitud | str | None:
    """The cached state, the SIN_SOLICITUD sentinel, or None for "not cached"."""
    try:
        crudo = _redis().get(_clave_cache(pedido))
    except Exception as exc:
        print(f"[solicitudes] {pedido}: caché no legible ({type(exc).__name__})")
        return None
    if not crudo:
        return None
    if isinstance(crudo, bytes):
        crudo = crudo.decode()
    if crudo == SIN_SOLICITUD:
        return SIN_SOLICITUD
    try:
        datos = json.loads(crudo)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return _desde_dict(datos) if isinstance(datos, dict) else None


def _desde_cache(pedido: str) -> Solicitud | None:
    valor = _leer_cache(pedido)
    return valor if isinstance(valor, Solicitud) else None


def _cachear_sin_solicitud(pedido: str) -> None:
    try:
        _redis().set(
            _clave_cache(pedido), SIN_SOLICITUD, ex=SIN_SOLICITUD_TTL_SEGUNDOS
        )
    except Exception as exc:
        print(f"[solicitudes] {pedido}: caché negativa no guardada ({type(exc).__name__})")


def _desde_erpnext(pedido: str) -> Solicitud | None:
    """The NEWEST event recorded on the order, or None.

    Ordered `creation desc` and taken from the front. It used to ask for the
    oldest ``MAX_EVENTOS`` and keep the last of those, which is the newest event
    only while an order has fewer than 60 events in its life. A review that
    retries, or a request that was offered, accepted and re-offered, passes that
    in a day — and then the "current" state of the order was a state it had left
    hours earlier: an expired request read as pending, a resolved review read as
    still open. Frappe's own ordering decides which event is last, not the size
    of the page it happened to fit in.
    """
    try:
        filas = erpnext.policy_get_list(
            "Comment",
            filters=[
                ["reference_doctype", "=", "Sales Order"],
                ["reference_name", "=", pedido],
                ["content", "like", f"%{MARCA}%"],
            ],
            fields=["content", "creation"],
            limit=MAX_EVENTOS,
            order_by="creation desc",
        )
    except Exception as exc:
        print(f"[solicitudes] {pedido}: no pude leer los eventos ({type(exc).__name__})")
        return None
    for fila in filas:
        if not isinstance(fila, dict):
            continue
        datos = _parsear(str(fila.get("content") or ""))
        if not datos:
            continue
        candidata = _desde_dict(datos)
        if candidata is not None:
            # Newest first, so the first one that parses IS the current state.
            # An unparseable newer event is skipped rather than treated as
            # "no request": a corrupt row must not resurrect an old state
            # silently, and the older event it falls back to still carries a
            # deadline, which is the direction that keeps the hold bounded.
            return candidata
    return None


def leer(pedido: str) -> Solicitud | None:
    """The current state of the order's decision request, or None.

    Cache first, ERPNext second. A cache miss is normal after a restart; a
    cache hit is never allowed to outlive the durable record because every
    write goes to ERPNext first.
    """
    pedido = str(pedido or "").strip()
    if not pedido:
        return None
    desde_cache = _desde_cache(pedido)
    if desde_cache is not None:
        return desde_cache
    durable = _desde_erpnext(pedido)
    if durable is not None:
        _cachear(durable)
    return durable


def _cantidades(so: dict) -> dict:
    """Quantity per product and warehouse, to detect a changed order later."""
    cantidades: dict[str, float] = {}
    for item in so.get("items") or []:
        if not isinstance(item, dict):
            continue
        codigo = str(item.get("item_code") or "").strip()
        deposito = str(item.get("warehouse") or "").strip()
        if not codigo:
            continue
        try:
            qty = float(item.get("stock_qty") or item.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        clave = f"{codigo}@{deposito}"
        cantidades[clave] = cantidades.get(clave, 0.0) + qty
    return cantidades


def crear(
    so: dict,
    *,
    tipo: str = TIPO_ENTREGA,
    solicitado: dict,
    nota_cliente: str = "",
) -> Solicitud | None:
    """Open a decision request for this order. Returns None if nothing was written.

    Idempotent per order: an order that already has an OPEN request keeps it,
    so a customer repeating themselves cannot produce two questions for the
    manager or two stock holds.
    """
    from app import notificar

    pedido = str(so.get("name") or "").strip()
    if not pedido:
        return None
    if tipo not in TIPOS:
        print(f"[solicitudes] {pedido}: tipo desconocido {tipo!r}")
        return None

    existente = leer(pedido)
    if existente is not None and existente.en_revision:
        # A review is not `abierta`, so without this a customer writing back
        # would REPLACE it with a fresh PENDIENTE: the reason a person was
        # asked would be lost, the deadline would be re-armed as an offer's,
        # and "confirmar <pedido>" would start meaning "approve the exception"
        # again. There is one record per order; the person has to finish first.
        print(f"[solicitudes] {pedido}: en revisión humana, no abro otra solicitud")
        return None
    if existente is not None and existente.abierta and not existente.vencida():
        return existente

    ahora = _ahora()
    horas = timeout_horas()
    solicitud = Solicitud(
        id=_nuevo_id(pedido),
        pedido=pedido,
        tipo=tipo,
        estado=PENDIENTE,
        cliente=str(so.get("customer") or ""),
        cliente_nombre=str(so.get("customer_name") or so.get("customer") or ""),
        resumen_items=notificar.renglones(so)[:500],
        total=float(so.get("grand_total") or 0),
        moneda=str(so.get("currency") or ""),
        creada_en=ahora,
        vence_en=ahora + horas * 3600.0,
        solicitado=dict(solicitado or {}),
        nota_cliente=citar(nota_cliente),
        evento="creada",
        sello=ahora,
        cantidades=_cantidades(so),
    )
    if not _escribir(solicitud):
        return None
    return solicitud


def registrar(
    solicitud: Solicitud, evento: str, **cambios: object
) -> Solicitud | None:
    """Append the next event. Returns the new state, or None if not durable."""
    ahora = _ahora()
    siguiente = replace(solicitud, evento=evento, sello=ahora, **cambios)  # type: ignore[arg-type]
    if not _escribir(siguiente):
        return None
    return siguiente


# ---------------------------------------------------------------------------
# The stock hold: durable, bounded, and never claimed without proof.
# ---------------------------------------------------------------------------


def vencimientos(pedidos: list[str]) -> dict[str, float]:
    """{order: hold expiry} for the orders that have one, in ONE ERPNext read.

    app/policy.py calls this while deciding whether a draft still holds the
    units it asked for. A draft whose request has lapsed must stop competing
    with live orders even if the sweep has not run yet, so the answer comes
    from the durable record rather than from a timer somewhere.

    An unreadable answer returns nothing rather than guessing: the caller then
    treats those drafts as still holding stock, which is the direction that
    never oversells.
    """
    nombres = [str(p).strip() for p in pedidos if str(p or "").strip()]
    if not nombres:
        return {}
    pendientes: dict[str, float] = {}
    faltan: list[str] = []
    for nombre in nombres:
        cacheada = _leer_cache(nombre)
        if cacheada is None:
            faltan.append(nombre)
        elif isinstance(cacheada, Solicitud) and cacheada.con_plazo:
            pendientes[nombre] = cacheada.vence_en
    if not faltan:
        return pendientes

    tope = MAX_EVENTOS * max(1, len(faltan))
    try:
        filas = erpnext.policy_get_list(
            "Comment",
            filters=[
                ["reference_doctype", "=", "Sales Order"],
                ["reference_name", "in", faltan],
                ["content", "like", f"%{MARCA}%"],
            ],
            fields=["content", "reference_name", "creation"],
            limit=tope,
            order_by="creation desc",
        )
    except Exception as exc:
        print(f"[solicitudes] vencimientos no legibles ({type(exc).__name__})")
        return pendientes
    ultimas: dict[str, Solicitud] = {}
    for fila in filas:
        if not isinstance(fila, dict):
            continue
        datos = _parsear(str(fila.get("content") or ""))
        candidata = _desde_dict(datos) if datos else None
        if candidata is None:
            continue
        # Newest first, so the FIRST event seen for an order is its current
        # state. Ordered `creation asc`, a truncated page gave every order the
        # newest of its OLDEST events — a lapsed request read as pending, and a
        # closed one read as still holding stock.
        ultimas.setdefault(candidata.pedido, candidata)
    for nombre, candidata in ultimas.items():
        _cachear(candidata)
        if candidata.con_plazo:
            pendientes[nombre] = candidata.vence_en

    # THE SHARED BUDGET IS NOT SHARED FAIRLY, SO ABSENCE IS NOT AN ANSWER.
    # The page holds `tope` rows for ALL the orders asked about. One order that
    # has been retried a thousand times fills it on its own, and every other
    # draft then comes back with no rows at all — which used to be recorded as
    # "this order has no request" for ten minutes. That is a lie in the one
    # direction that matters: it drops a live hold's deadline, so app/policy.py
    # stops seeing the units as held and the sweep never gets the order back.
    #
    # A full page means the answer was cut, so the orders it did not mention
    # are UNKNOWN, not empty. They are resolved one at a time — a per-order read
    # has its own per-order limit, so no other order's history can crowd it out
    # — and the rescue itself is bounded. Whatever is still unresolved is simply
    # not reported: the caller then treats those drafts as still holding stock,
    # which is the direction that never oversells.
    truncada = len(filas) >= tope
    sin_eventos = [nombre for nombre in faltan if nombre not in ultimas]
    if not truncada:
        for nombre in sin_eventos:
            _cachear_sin_solicitud(nombre)
        return pendientes

    print(
        f"[solicitudes] vencimientos: la consulta de {len(faltan)} pedido(s) "
        f"volvió cortada en {tope} eventos; resuelvo "
        f"{min(len(sin_eventos), MAX_RESCATES_VENCIMIENTOS)} de "
        f"{len(sin_eventos)} uno por uno"
    )
    for nombre in sin_eventos[:MAX_RESCATES_VENCIMIENTOS]:
        candidata = _desde_erpnext(nombre)
        if candidata is None:
            # Unreadable or genuinely absent — indistinguishable here, and only
            # one of those is safe to remember. Neither is cached.
            continue
        _cachear(candidata)
        if candidata.con_plazo:
            pendientes[nombre] = candidata.vence_en
    return pendientes


def soltar_reserva(pedido: str) -> tuple[bool, str]:
    """Make ERPNext itself stop reserving a draft nobody will fulfil.

    Returns (proven released, what to say). "Proven" means the document was
    re-read afterwards and really is in a state ERPNext does not count. The
    "Closed draft" behaviour is not verified against a live ERPNext build, so
    the claim is only made when the re-read agrees; otherwise the caller says
    the stock will be re-checked instead of saying it was freed.
    """
    try:
        actual = erpnext.policy_get_doc("Sales Order", pedido)
    except Exception as exc:
        print(f"[solicitudes] {pedido}: no pude leer el pedido ({type(exc).__name__})")
        return False, "no pude verificar si el borrador dejó de comprometer stock"
    if int(actual.get("docstatus") or 0) != 0:
        return False, "el pedido ya no es un borrador"
    from app import policy

    if policy.sin_reserva(actual.get("status")):
        # Already out of the way — a fallback offer lives on a draft its
        # predecessor closed, and re-closing it would be a write for nothing.
        return True, "el borrador ya estaba cerrado y no compromete stock"
    try:
        erpnext.policy_update_status("Sales Order", pedido, _ESTADO_SIN_RESERVA)
    except Exception as exc:
        print(f"[solicitudes] {pedido}: no pude cerrar el borrador ({type(exc).__name__})")
        return False, "no pude cerrar el borrador; sigue comprometiendo stock"
    try:
        confirmado = erpnext.policy_get_doc("Sales Order", pedido)
    except Exception as exc:
        print(f"[solicitudes] {pedido}: relectura falló ({type(exc).__name__})")
        return False, "cerré el borrador pero no pude comprobarlo"
    if policy.sin_reserva(confirmado.get("status")):
        return True, "el borrador quedó cerrado y ya no compromete stock"
    return False, "ERPNext no dejó el borrador cerrado; sigue comprometiendo stock"


def reabrir_borrador(pedido: str) -> tuple[bool, str]:
    """Undo ``soltar_reserva``, so an accepted fallback can be confirmed.

    A fallback offer is made on a draft that was deliberately Closed when its
    predecessor expired — that is what stopped it holding stock while nobody
    was promised anything. Accepting the fallback has to put the document back
    where the ordinary rules apply, and the ordinary rules are what run next:
    ``revalidar`` re-checks the stock that was NOT held in the meantime, so
    re-opening promises nothing by itself.

    Fails closed, and only claims success when a re-read agrees.
    """
    from app import policy

    try:
        actual = erpnext.policy_get_doc("Sales Order", pedido)
    except Exception as exc:
        print(f"[solicitudes] {pedido}: no pude leer el pedido ({type(exc).__name__})")
        return False, "no pude leer el pedido para reabrirlo"
    if int(actual.get("docstatus") or 0) != 0:
        return False, "el pedido ya no es un borrador"
    if not policy.sin_reserva(actual.get("status")):
        return True, ""
    try:
        erpnext.policy_update_status("Sales Order", pedido, _ESTADO_BORRADOR)
    except Exception as exc:
        print(f"[solicitudes] {pedido}: no pude reabrir el borrador ({type(exc).__name__})")
        return False, "no pude reabrir el borrador que había quedado cerrado"
    try:
        confirmado = erpnext.policy_get_doc("Sales Order", pedido)
    except Exception as exc:
        print(f"[solicitudes] {pedido}: relectura falló ({type(exc).__name__})")
        return False, "reabrí el borrador pero no pude comprobarlo"
    if policy.sin_reserva(confirmado.get("status")):
        return False, "ERPNext dejó el borrador cerrado; no lo puedo confirmar"
    return True, ""


# ---------------------------------------------------------------------------
# Expiry sweep. Its own thread in app/main.py, so nothing waits on it.
# ---------------------------------------------------------------------------


def _indice_pendientes(ahora: float) -> list[str]:
    try:
        cliente = _redis()
        vencidos = cliente.zrangebyscore(CLAVE_INDICE, "-inf", f"{ahora:.3f}")
    except Exception as exc:
        print(f"[solicitudes] índice no legible ({type(exc).__name__})")
        return []
    return [v.decode() if isinstance(v, bytes) else str(v) for v in vencidos or []]


def _indice_vacio() -> bool:
    try:
        return int(_redis().zcard(CLAVE_INDICE)) == 0
    except Exception:
        return True


# monotonic() deadline after which an incomplete rebuild is worth retrying.
# In-process on purpose: a restart rebuilds anyway, because the index is empty.
_reconstruccion_incompleta_hasta: float = 0.0


def reconstruccion_incompleta() -> bool:
    """Whether the last rebuild ran out of pages and is still owed a retry."""
    return time.monotonic() < _reconstruccion_incompleta_hasta


def reconstruir_indice() -> int:
    """Rebuild the expiry index from ERPNext after a flush or a restart.

    Redis cannot answer "was I emptied?", so this is asked of the system of
    record: the open requests are found again and rescheduled. Without it, a
    flush would leave pending decisions with no expiry at all, and their drafts
    would hold stock for ever — the exact failure this module exists to avoid.

    IT USED TO REBUILD FROM THE WRONG END OF HISTORY
    One page of 200 events ordered `creation asc` is the OLDEST 200 events this
    system ever wrote. Past that — a few weeks of ordinary traffic — the rebuild
    reconstructed nothing but long-closed requests from the first days of the
    deployment and NOT ONE live one. Every draft parked on a real pending
    decision came back from the restart with no deadline at all, and a draft
    with no deadline holds its units until somebody notices by hand. The bug got
    worse the longer the system ran correctly.

    So it reads from the NEWEST end, and pages backwards. Newest-first also
    settles which event counts: the first one seen for an order is its current
    state, where the old code kept the last one and therefore the newest of the
    OLDEST events it had read.

    Truncation is reported rather than hidden. Reaching the page budget means
    there are orders this rebuild could not reach, so the run is marked
    incomplete and ``tick`` comes back for it — a partial index that looks
    finished is the same permanent hold with an extra step.
    """
    global _reconstruccion_incompleta_hasta
    ultimas: dict[str, Solicitud] = {}
    truncada = False
    for pagina in range(MAX_PAGINAS_RECONSTRUCCION):
        try:
            filas = erpnext.policy_get_list(
                "Comment",
                filters=[
                    ["reference_doctype", "=", "Sales Order"],
                    ["content", "like", f"%{MARCA}%"],
                ],
                fields=["content", "reference_name", "creation"],
                limit=MAX_RECONSTRUCCION,
                order_by="creation desc",
                start=pagina * MAX_RECONSTRUCCION,
            )
        except Exception as exc:
            print(
                f"[solicitudes] no pude reconstruir el índice "
                f"({type(exc).__name__}); página {pagina + 1}"
            )
            # Whatever was read so far is still worth indexing — it is the
            # newest end — but the rebuild is not finished.
            truncada = True
            break
        for fila in filas:
            if not isinstance(fila, dict):
                continue
            datos = _parsear(str(fila.get("content") or ""))
            candidata = _desde_dict(datos) if datos else None
            if candidata is not None:
                # Newest first: the first event seen for an order is current.
                ultimas.setdefault(candidata.pedido, candidata)
        if len(filas) < MAX_RECONSTRUCCION:
            break
    else:
        truncada = True

    recuperadas = 0
    for candidata in ultimas.values():
        _cachear(candidata)
        if candidata.con_plazo:
            recuperadas += 1
    if truncada:
        _reconstruccion_incompleta_hasta = (
            time.monotonic() + REINTENTO_RECONSTRUCCION_SEGUNDOS
        )
        print(
            f"[solicitudes] reconstrucción INCOMPLETA: leí "
            f"{MAX_PAGINAS_RECONSTRUCCION * MAX_RECONSTRUCCION} eventos y hay "
            f"más. {recuperadas} plazo(s) recuperado(s) de "
            f"{len(ultimas)} pedido(s); reintento en "
            f"{REINTENTO_RECONSTRUCCION_SEGUNDOS / 60:g} min. Los pedidos que "
            f"no alcancé siguen reservando stock sin plazo."
        )
    else:
        _reconstruccion_incompleta_hasta = 0.0
    return recuperadas


def tick(ahora: float | None = None) -> int:
    """Expire what has run out of time. Returns how many were closed.

    Never raises: it runs on a background thread and a Redis or ERPNext hiccup
    must not stop the loop that will try again a minute later.
    """
    momento = ahora or _ahora()
    # An index that is merely NON-EMPTY is not proof the rebuild finished: a
    # truncated one leaves the orders it never reached with no deadline, and
    # they would wait for the next restart to get one.
    if _indice_vacio() or reconstruccion_incompleta():
        reconstruir_indice()
    cerradas = 0
    for pedido in _indice_pendientes(momento)[:50]:
        try:
            if _vencer(pedido, momento):
                cerradas += 1
        except Exception as exc:
            print(f"[solicitudes] {pedido}: vencimiento falló ({type(exc).__name__})")
    return cerradas


def _vencer(pedido: str, ahora: float) -> bool:
    """Close what ran out of time, and answer the customer with something.

    Everything durable happens under the order's lock, in this order: release
    the hold, record the expiry, then compute and record the fallback. A repeat
    is safe at every point — the state is re-read inside the lock, releasing an
    already-closed draft writes nothing, and an expiry that did not become
    durable leaves the request open for the next tick instead of half-done.

    The notices go out AFTERWARDS, outside the lock, through the durable queue:
    a person's phone must never be on the critical path of a state change.
    """
    from app.locks import CoordinationError, distributed_lock

    try:
        with distributed_lock(f"solicitud:{pedido}", lease_seconds=60, wait_seconds=5):
            solicitud = leer(pedido)
            if solicitud is None or not solicitud.con_plazo:
                try:
                    _redis().zrem(CLAVE_INDICE, pedido)
                except Exception:
                    pass
                return False
            if not solicitud.vencida(ahora):
                return False
            if solicitud.en_revision:
                # A different ending, and it must not reach _respaldar: the
                # customer already accepted and was already told a person
                # would look at it. Offering them a machine-picked date on top
                # of that would be the system talking to itself.
                return _vencer_revision(solicitud, ahora)
            liberado, detalle = soltar_reserva(pedido)
            cerrada = registrar(
                solicitud,
                "vencida",
                estado=VENCIDA,
                motivo=f"sin respuesta en {timeout_horas():g} h; {detalle}",
                decidida_en=ahora,
            )
            if cerrada is None:
                # Not durable means it did not happen: the request is still
                # open, the next tick tries again, and nobody was told
                # anything. A second event on top of a failed one would be a
                # decision that exists only in this process.
                return False
            respaldo, sin_respaldo = _respaldar(cerrada, ahora)
    except CoordinationError:
        return False

    if respaldo is not None:
        _avisar_cliente_respaldo(respaldo)
        _avisar_equipo(
            respaldo,
            f"⏰ {pedido}: la solicitud {cerrada.id} venció sin respuesta. "
            f"{detalle.capitalize()}.\n"
            f"Le ofrecí automáticamente lo que ya estaba configurado: "
            f"{terminos_texto(respaldo.ofrecido, respaldo.moneda)} "
            f"(solicitud {respaldo.id}, vence {_sello_utc(respaldo.vence_en)} UTC).\n"
            f"Nada está confirmado hasta que el cliente acepte, y ahí se "
            f"revalida todo.",
        )
        return True

    _avisar_cliente_vencida(cerrada, liberado)
    _avisar_equipo(
        cerrada,
        f"⏰ {pedido}: la solicitud venció sin respuesta.\n"
        f"{detalle.capitalize()}.\n"
        f"No pude ofrecerle nada concreto en su lugar: {sin_respaldo}.\n"
        f"Si querés hacerlo igual, reabrí el pedido en ERPNext y confirmalo.",
    )
    return True


def _vencer_revision(solicitud: Solicitud, ahora: float) -> bool:
    """The review deadline ran out — or a person already dealt with it.

    Called with the order's lock held. Reads the CURRENT document first,
    because the manager's own commands act on the Sales Order directly
    (``confirmar`` submits it, ``cancelar`` cancels it) and this sweep must
    recognise their work instead of overwriting it. That makes a late command
    and a concurrent expiry two orderings of the same safe pair rather than a
    race: whichever runs second observes what the first did.

    Nothing here claims the stock was freed on its own account — ``soltar_reserva``
    is the only thing allowed to say that, and only when a re-read agrees.
    """
    pedido = solicitud.pedido
    estado_doc = _docstatus(pedido)
    if estado_doc is None:
        # Leave it in the index: the next tick asks again. Guessing here would
        # either close a draft somebody confirmed or free stock we cannot see.
        return False
    if estado_doc != 0:
        # Confirmed or cancelled by a person. The review is over and the draft
        # is no longer a draft, so there is nothing to release.
        return _revision_resuelta_por_persona(solicitud, estado_doc, ahora)

    liberado, detalle = soltar_reserva(pedido)
    if not liberado:
        # Did a person confirm it inside the release window? Then the draft is
        # gone for a good reason and the customer must not be told it is off.
        despues_doc = _docstatus(pedido)
        if despues_doc is not None and despues_doc != 0:
            return _revision_resuelta_por_persona(solicitud, despues_doc, ahora)
        return _revision_sin_soltar(solicitud, detalle, ahora)

    plazo = _plazo_horas(solicitud)
    cerrada = registrar(
        solicitud,
        "revision_vencida",
        estado=REVISION_VENCIDA,
        motivo=f"nadie la revisó en {plazo:g} h; {detalle}",
        decidida_en=ahora,
    )
    if cerrada is None:
        return False
    _encolar_cliente(
        cerrada, "revision_vencida", texto_revision_vencida_cliente(cerrada)
    )
    _avisar_equipo(
        cerrada,
        f"⏰ {pedido}: la revisión {solicitud.id} venció sin que nadie la mirara "
        f"({plazo:g} h).\n"
        f"Motivo original: {solicitud.motivo or 'sin detalle'}.\n"
        f"{detalle.capitalize()}.\n"
        f"Le avisé al cliente que no avanza. Si todavía se puede, hay que "
        f"rehacerlo con los datos del momento.",
    )
    return True


def _docstatus(pedido: str) -> int | None:
    """0 draft, 1 submitted, 2 cancelled — or None for "I could not tell".

    None is never 0. Collapsing an unreadable answer into "live draft" is how a
    document somebody confirmed gets closed underneath them.
    """
    try:
        so = erpnext.policy_get_doc("Sales Order", pedido)
    except Exception as exc:
        print(f"[solicitudes] {pedido}: relectura falló ({type(exc).__name__})")
        return None
    if not isinstance(so, dict):
        return None
    try:
        return int(so.get("docstatus") or 0)
    except (TypeError, ValueError):
        return None


def _plazo_horas(solicitud: Solicitud) -> float:
    """The plazo THIS review actually had, from its own record.

    Not revision_horas(): that re-enters app/limites.py on the sweep's hot path
    and would report today's configured number rather than the one that applied
    when the review opened.
    """
    if solicitud.decidida_en <= 0:
        return revision_horas()
    return max(0.0, (solicitud.vence_en - solicitud.decidida_en) / 3600.0)


def _revision_resuelta_por_persona(
    solicitud: Solicitud, estado_doc: int, ahora: float
) -> bool:
    resuelta = registrar(
        solicitud,
        "revision_resuelta",
        estado=REVISION_RESUELTA,
        motivo=(
            "una persona confirmó el pedido"
            if estado_doc == 1
            else "una persona canceló el pedido"
        ),
        decidida_en=ahora,
    )
    if resuelta is None:
        return False
    _avisar_equipo(
        resuelta,
        f"✅ {solicitud.pedido}: cierro la revisión {solicitud.id} porque el "
        f"pedido ya {'está confirmado' if estado_doc == 1 else 'fue cancelado'}. "
        f"El borrador ya no retiene stock.",
    )
    return True


def _revision_sin_soltar(solicitud: Solicitud, detalle: str, ahora: float) -> bool:
    """The draft is still live and we could not prove otherwise. Try again soon.

    Writing a terminal state here would be the very bug this deadline exists to
    prevent: terminal means no deadline, ``vencimientos`` would stop reporting
    the hold, and a draft ERPNext is still counting would reserve those units
    for ever with nothing left to act on it.

    So the review is re-armed instead. The units stay held — which is the
    direction that never oversells — and something always comes back for them.
    ``decidida_en`` is deliberately not touched, so the moment the review
    opened survives every retry and _plazo_horas keeps telling the truth.
    """
    reintentada = registrar(
        solicitud,
        "revision_humana",
        estado=REVISION_HUMANA,
        motivo=f"no pude cerrar el borrador; {detalle}",
        vence_en=ahora + REINTENTO_REVISION_SEGUNDOS,
    )
    if reintentada is None:
        return False
    # Bucketed by the DAY of failure, so an ERPNext outage costs one message a
    # day instead of one every sweep.
    dia = int(max(0.0, ahora - solicitud.decidida_en) // 86400)
    _avisar_equipo(
        reintentada,
        evento=f"revision_sin_soltar:{dia}",
        texto=f"🚨 {solicitud.pedido}: venció la revisión {solicitud.id} y NO "
        f"pude cerrar el borrador — {detalle}. Sigue reservando stock, así que "
        f"lo dejo con plazo y reintento. Cerralo o confirmalo a mano en ERPNext.",
    )
    return True


def resolver_revision(pedido: str, por: str, motivo: str = "") -> bool:
    """A manager's own command closed the review. Idempotent by construction.

    Called from the authorized command handlers AFTER their action succeeded
    (app/aprobacion.py, app/decisiones.py). It is a no-op unless the order's
    request is actually in review, so calling it on every confirmation — or
    three times because the manager typed the word three times — costs one
    read and changes nothing.

    Never touches the Sales Order: the command that called it already did
    whatever it was going to do. This only ends the review, which is what
    takes the draft out of the expiry index.
    """
    from app.locks import CoordinationError, distributed_lock

    try:
        # Under the ORDER's lock, like every other state change: the sweep runs
        # _vencer_revision on the same record from its own thread, and without
        # this the two only converged by accident.
        with distributed_lock(f"solicitud:{pedido}", lease_seconds=30, wait_seconds=5):
            solicitud = leer(pedido)
            if solicitud is None or not solicitud.en_revision:
                return False
            resuelta = registrar(
                solicitud,
                "revision_resuelta",
                estado=REVISION_RESUELTA,
                motivo=motivo or f"resuelta a mano por {por}",
                decidida_por=por,
                decidida_en=_ahora(),
            )
    except CoordinationError:
        # The sweep holds it. It re-reads the order, sees what the manager just
        # did to it, and closes the review as resolved on its own.
        print(f"[solicitudes] {pedido}: revisión ocupada; la cierra el barrido")
        return False
    except Exception as exc:
        print(f"[solicitudes] {pedido}: cierre de revisión falló ({type(exc).__name__})")
        return False
    if resuelta is None:
        print(f"[solicitudes] {pedido}: cierre de revisión NO durable")
        return False
    try:
        erpnext.add_comment(
            "Sales Order",
            pedido,
            f"Revisión {solicitud.id} cerrada por {por}: {resuelta.motivo}. "
            f"El pedido deja de estar en revisión y su borrador ya no retiene "
            f"stock por ese motivo.",
        )
    except Exception as exc:
        print(f"[solicitudes] {pedido}: comentario de cierre falló ({type(exc).__name__})")
    return True


def _respaldar(vencida: Solicitud, ahora: float) -> tuple[Solicitud | None, str]:
    """The concrete second offer for a request nobody answered, or why not.

    Called with the order's lock already held and ``vencida`` already recorded
    as terminal — this never touches that record, so the expired request stays
    expired whatever happens here.

    Every refusal is a reason a person can read, and every one of them ends
    with the customer hearing the plain truth instead of an offer that might
    not hold.
    """
    from app import excepciones

    if vencida.es_respaldo:
        # A fallback offer that the CUSTOMER did not answer. Offering them a
        # third date nobody asked for would be a machine talking to itself.
        return None, "ya era la oferta de respaldo y el cliente no la contestó"

    try:
        so = erpnext.policy_get_doc("Sales Order", vencida.pedido)
    except Exception as exc:
        print(f"[solicitudes] {vencida.pedido}: relectura falló ({type(exc).__name__})")
        return None, "no pude releer el pedido"
    if not isinstance(so, dict):
        return None, "no pude releer el pedido"
    if int(so.get("docstatus") or 0) != 0:
        return None, "el pedido ya no es un borrador"

    # Nobody to accept it means nobody to offer it to. Checked BEFORE the
    # record is written, so a durable offer never exists with no way to answer.
    if not _telefono_cliente(vencida.pedido):
        return None, "el cliente no tiene teléfono cargado"

    evaluacion = excepciones.evaluar_respaldo(so)
    if not evaluacion.preautorizada or evaluacion.oferta is None:
        return None, evaluacion.motivo or "no hay una alternativa configurada"

    oferta = evaluacion.oferta.como_dict()
    respaldo = Solicitud(
        id=_nuevo_id(vencida.pedido),
        pedido=vencida.pedido,
        tipo=vencida.tipo,
        estado=ESPERANDO_CLIENTE,
        cliente=str(so.get("customer") or vencida.cliente),
        cliente_nombre=str(
            so.get("customer_name") or so.get("customer") or vencida.cliente_nombre
        ),
        resumen_items=vencida.resumen_items,
        total=float(so.get("grand_total") or 0),
        moneda=str(so.get("currency") or vencida.moneda),
        creada_en=ahora,
        vence_en=_vence_respaldo(
            ahora, evaluacion.oferta.fecha, evaluacion.oferta.hora
        ),
        solicitado=dict(vencida.solicitado),
        ofrecido=oferta,
        decision=RESPALDO,
        decidida_por=DECIDE_EL_SISTEMA,
        decidida_en=ahora,
        motivo=f"la solicitud {vencida.id} venció sin respuesta",
        nota_cliente=vencida.nota_cliente,
        evento="respaldo",
        sello=ahora,
        # Re-read, not inherited: what the fallback is measured against on
        # acceptance has to be the order as it stands NOW, since its
        # predecessor's hold is gone and anything could have moved.
        cantidades=_cantidades(so),
        es_respaldo=True,
        origen=vencida.id,
    )
    if not _escribir(respaldo):
        return None, "no pude registrar la oferta de respaldo en ERPNext"
    try:
        erpnext.add_comment(
            "Sales Order",
            vencida.pedido,
            f"La solicitud {vencida.id} venció sin respuesta y quedó cerrada. "
            f"Oferta de respaldo automática {respaldo.id}: "
            f"{terminos_texto(oferta, respaldo.moneda)}, calculada con la "
            f"configuración del dueño. No confirma nada: el cliente tiene que "
            f"aceptarla y ahí se revalida stock, precios y estado del pedido.",
        )
    except Exception as exc:
        print(f"[solicitudes] {vencida.pedido}: comentario de respaldo falló ({type(exc).__name__})")
    return respaldo, ""


def _vence_respaldo(ahora: float, fecha: str, hora: str) -> float:
    """When the fallback offer dies: the timeout, never past its own date.

    An offer for Thursday 18:00 must not still be acceptable on Thursday at
    19:00 — the customer would be answered "a person has to look at this"
    when the plain truth is that it lapsed. So the deadline is the earlier of
    the configured timeout and the moment the offer itself promises.
    """
    tope = ahora + max(0.5, timeout_horas()) * 3600.0
    momento = _momento_del_negocio(fecha, hora)
    return min(tope, momento) if momento > ahora else tope


def _momento_del_negocio(fecha: str, hora: str) -> float:
    """"2026-09-10", "18:00" -> that instant in the business timezone, or 0.0.

    0.0 means "unreadable", and every caller then falls back to the plain
    timeout rather than to a deadline it invented.
    """
    if not fecha or not hora:
        return 0.0
    zona = os.getenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires").strip()
    try:
        cuando = datetime.fromisoformat(f"{fecha}T{hora}").replace(tzinfo=ZoneInfo(zona))
    except Exception as exc:
        print(f"[solicitudes] fecha/hora de respaldo no interpretable ({type(exc).__name__})")
        return 0.0
    return cuando.timestamp()


# ---------------------------------------------------------------------------
# What the customer and the manager read. Deterministic text, every time.
# ---------------------------------------------------------------------------


def terminos_texto(datos: dict, moneda: str = "") -> str:
    """The agreed or requested terms, in one line, for a person to read."""
    from app.formato import pesos

    if not datos:
        return "sin cambios"
    partes: list[str] = []
    metodo = str(datos.get("metodo") or "")
    if metodo == "retiro":
        partes.append("retiro en el local")
    fecha = str(datos.get("fecha") or "")
    hora = str(datos.get("hora") or "")
    if fecha:
        partes.append(f"{fecha}{f' a las {hora}' if hora else ''}")
    elif hora:
        partes.append(f"a las {hora}")
    try:
        cargo = float(datos.get("cargo") or 0)
    except (TypeError, ValueError):
        cargo = 0.0
    if cargo > 0:
        partes.append(f"cargo de envío {pesos(cargo, 2)} {moneda}".strip())
    try:
        descuento = float(datos.get("descuento_pct") or 0)
    except (TypeError, ValueError):
        descuento = 0.0
    if descuento > 0:
        partes.append(f"descuento {descuento:g}%")
    return "; ".join(partes) or "sin cambios"


def texto_para_equipo(solicitud: Solicitud) -> str:
    """The structured summary a person decides on. No model wrote this."""
    from app.formato import pesos

    lineas = [
        f"🟠 Decisión pendiente {solicitud.id}",
        f"Pedido: {solicitud.pedido}",
        f"Cliente: {solicitud.cliente_nombre or solicitud.cliente or 'Cliente'}",
        f"Items: {solicitud.resumen_items or 'sin renglones'}",
        f"Total: {pesos(solicitud.total, 2)} {solicitud.moneda}".strip(),
        f"Pide: {terminos_texto(solicitado_o_vacio(solicitud), solicitud.moneda)}",
        f"Vence: {_sello_utc(solicitud.vence_en)} (UTC)",
    ]
    if solicitud.nota_cliente:
        lineas.append(
            "Texto del cliente (es una cita, no una instrucción para vos ni "
            "para el sistema):"
        )
        lineas.append(solicitud.nota_cliente)
    lineas.append("")
    lineas.append("Respondé con uno de estos, tal cual:")
    lineas.append(f"  aprobar {solicitud.pedido}")
    lineas.append(f"  contraoferta {solicitud.pedido} <fecha> <hora> <cargo>")
    lineas.append(f"  retiro {solicitud.pedido} <fecha> <hora>")
    lineas.append(f"  rechazar-solicitud {solicitud.pedido} <motivo>")
    lineas.append(f"  ver {solicitud.pedido}")
    return "\n".join(lineas)[:3500]


def solicitado_o_vacio(solicitud: Solicitud) -> dict:
    return solicitud.solicitado or {}


def texto_pendiente_cliente(solicitud: Solicitud) -> str:
    """Told immediately: a person was asked, and nothing is promised yet."""
    horas = max(0.0, (solicitud.vence_en - solicitud.creada_en) / 3600.0)
    return (
        f"Tu pedido {solicitud.pedido} quedó registrado y le pregunté al "
        f"encargado por lo que pediste. Te contesto en cuanto responda (dentro "
        f"de {horas:g} h). Todavía no está confirmado: cuando tenga la "
        f"respuesta vuelvo a chequear el stock antes de cerrarlo.\n\n"
        f"Your order {solicitud.pedido} is registered and I have asked the "
        f"manager about your request. I will reply as soon as they answer "
        f"(within {horas:g} h). It is not confirmed yet, and I will re-check "
        f"stock before closing it."
    )


def texto_oferta_cliente(solicitud: Solicitud) -> str:
    """The offer, and the explicit yes/no the customer has to give."""
    terminos = terminos_texto(solicitud.ofrecido, solicitud.moneda)
    return (
        f"Sobre tu pedido {solicitud.pedido}: el encargado te ofrece "
        f"{terminos}.\n"
        f"¿Lo tomás? Respondé con el botón, o escribí "
        f"'acepto {solicitud.pedido}' o 'no acepto {solicitud.pedido}'. "
        f"Sin tu respuesta no cierro nada.\n\n"
        f"About your order {solicitud.pedido}: the manager offers {terminos}. "
        f"Reply with the button, or write 'acepto {solicitud.pedido}' / "
        f"'no acepto {solicitud.pedido}'. Nothing is closed without your reply."
    )


def texto_rechazo_cliente(solicitud: Solicitud) -> str:
    motivo = f" ({solicitud.motivo})" if solicitud.motivo else ""
    return (
        f"Sobre tu pedido {solicitud.pedido}: no vamos a poder hacer lo que "
        f"pediste{motivo}. Si querés, lo dejamos para un día de reparto normal."
        f"\n\nAbout your order {solicitud.pedido}: we cannot do what you asked"
        f"{motivo}. We can schedule it for a normal delivery day instead."
    )


def texto_vencida_cliente(solicitud: Solicitud) -> str:
    """Last resort: nobody answered AND nothing could be offered instead.

    Reached only when ``_respaldar`` could compute no safe alternative — no
    configured round, no pickup, an order that is no longer a draft. Asking
    them to write again is not a good answer, and it is the only honest one
    left: inventing a date here is exactly what this module exists to prevent.
    """
    return (
        f"Sobre tu pedido {solicitud.pedido}: no llegué a tener una respuesta "
        f"del encargado, así que por ahora no queda confirmado. Escribime y lo "
        f"volvemos a ver con el stock del momento.\n\n"
        f"About your order {solicitud.pedido}: I did not get an answer in time, "
        f"so it is not confirmed. Message me and we will look at it again with "
        f"current stock."
    )


def texto_respaldo_cliente(solicitud: Solicitud) -> str:
    """Nobody answered, so here is what we CAN do — a date, and a yes/no.

    The waiting was our failure, so the customer is not sent away to write
    again: they get the alternative the owner already authorized, in the same
    message, with the same explicit acceptance every other offer needs.
    """
    terminos = terminos_texto(solicitud.ofrecido, solicitud.moneda)
    retiro = str(solicitud.ofrecido.get("metodo") or "entrega") == "retiro"
    puede = (
        "podés pasar a buscarlo por el local"
        if retiro
        else "te lo puedo llevar en el próximo reparto normal"
    )
    puede_en = (
        "you can pick it up at the shop"
        if retiro
        else "I can bring it on the next normal delivery round"
    )
    return (
        f"Sobre tu pedido {solicitud.pedido}: no llegué a tener la respuesta "
        f"del encargado sobre lo que pediste, así que eso queda sin efecto. "
        f"Perdón por la espera.\n"
        f"Lo que sí {puede}: {terminos}.\n"
        f"¿Lo tomás? Respondé 'acepto {solicitud.pedido}' o "
        f"'no acepto {solicitud.pedido}'. Sin tu respuesta no cierro nada, y "
        f"cuando aceptes vuelvo a chequear el stock antes de confirmarlo.\n\n"
        f"About your order {solicitud.pedido}: I did not get the manager's "
        f"answer about what you asked for, so that is off. Sorry for the wait. "
        f"What I can do: {puede_en} — {terminos}. Reply "
        f"'acepto {solicitud.pedido}' or 'no acepto {solicitud.pedido}'. "
        f"Nothing is closed without your reply, and I re-check stock before "
        f"confirming."
    )


def texto_revision_vencida_cliente(solicitud: Solicitud) -> str:
    """We said a person would look at it, and nobody did.

    The customer accepted in good faith and was told to wait for us. Leaving
    them with silence would be the worst of the endings, so they are told
    plainly that it is not going ahead — and nothing was charged, because
    nothing was ever confirmed.
    """
    return (
        f"Sobre tu pedido {solicitud.pedido}: te había dicho que lo revisaba "
        f"una persona y no llegamos a hacerlo, así que no lo dejo agendado y no "
        f"queda nada a tu nombre. Perdón. Cuando quieras lo armamos de nuevo "
        f"con el stock del momento.\n\n"
        f"About your order {solicitud.pedido}: I said a person would review it "
        f"and we did not get to it, so it is not scheduled and nothing is "
        f"charged. Sorry. Message me and we will put it together again with "
        f"current stock."
    )


def texto_respaldo_vencido_cliente(solicitud: Solicitud) -> str:
    """The fallback offer ran out. Says the offer lapsed, NOT who was silent.

    It is tempting to write "I did not get your reply" — usually true, and it
    reads better than the passive. But it is not knowable here: a review that
    could not be recorded (``_revision_sin_registro``) leaves the record at
    ESPERANDO_CLIENTE precisely because the write that would have said
    otherwise failed, and this sweep then closes it as unanswered. A customer
    who DID accept would be told they had not, which is the one thing this
    module never does. Whose silence it was stays in the team notice, where a
    person can reconcile it against the order.
    """
    return (
        f"Sobre tu pedido {solicitud.pedido}: se venció el plazo de esa opción "
        f"({terminos_texto(solicitud.ofrecido, solicitud.moneda)}), así que no "
        f"queda agendada y no hay nada confirmado a tu nombre. Cuando quieras, "
        f"escribime y lo armamos con el stock del momento.\n\n"
        f"About your order {solicitud.pedido}: that option has run out, so it is "
        f"not scheduled and nothing is confirmed in your name. Message me "
        f"whenever you like and we will put it together with current stock."
    )


# ---------------------------------------------------------------------------
# Notices. Always through the durable queue (app/avisos.py), never inline:
# a decision must not wait on Meta, and a notice must not be lost.
# ---------------------------------------------------------------------------


def _telefono_cliente(pedido: str) -> str:
    from app import telefono as telefonos

    try:
        so = erpnext.policy_get_doc("Sales Order", pedido)
        cliente = erpnext.policy_get_doc("Customer", str(so.get("customer") or ""))
    except Exception as exc:
        print(f"[solicitudes] {pedido}: teléfono no legible ({type(exc).__name__})")
        return ""
    return telefonos.normalizar(cliente.get("mobile_no")) or ""


def _encolar_cliente(solicitud: Solicitud, evento: str, texto: str) -> bool:
    from app import avisos

    tel = _telefono_cliente(solicitud.pedido)
    if not tel:
        try:
            erpnext.add_comment(
                "Sales Order",
                solicitud.pedido,
                f"Aviso de solicitud ({evento}) no enviado: el cliente no tiene "
                "teléfono cargado. Requiere contacto manual.",
            )
        except Exception:
            pass
        return False
    try:
        return avisos.encolar(f"{evento}:{solicitud.id}", solicitud.pedido, tel, texto)
    except Exception as exc:
        print(f"[solicitudes] {solicitud.pedido}: aviso no encolado ({type(exc).__name__})")
        return False


def _avisar_cliente_vencida(solicitud: Solicitud, liberado: bool) -> bool:
    del liberado  # the customer is told the same either way: nothing is promised
    texto = (
        texto_respaldo_vencido_cliente(solicitud)
        if solicitud.es_respaldo
        else texto_vencida_cliente(solicitud)
    )
    return _encolar_cliente(solicitud, "solicitud_vencida", texto)


def _avisar_cliente_respaldo(solicitud: Solicitud) -> bool:
    """The concrete second offer. Keyed on the NEW id, so it is sent once."""
    return _encolar_cliente(
        solicitud, "solicitud_respaldo", texto_respaldo_cliente(solicitud)
    )


def _avisar_equipo(solicitud: Solicitud, texto: str, *, evento: str = "") -> bool:
    """One team notice, deduplicated per (event, order).

    ``evento`` is explicit rather than always ``solicitud.evento`` because the
    caller sometimes has to say something ABOUT a record without having
    written a new event onto it — an escalation when a write failed, for
    instance. Keyed on the record's own event, that notice would collide with
    the one already sent for that event and be dropped as a duplicate, which
    is how an escalation goes missing.
    """
    from app import avisos

    return avisos.encolar_equipo(
        f"solicitud_equipo:{evento or solicitud.evento}", solicitud.pedido, texto
    )


def parsear_terminos(texto: str, *, con_cargo: bool = True) -> dict | None:
    """"2026-09-04 18:00 1500" -> the terms, or None if it is not unambiguous.

    Deterministic on purpose. A manager writing prose gets the request summary
    and the exact commands back (see app/main.py), never a model's guess at
    what they meant: these fields become a price and a delivery date somebody
    has to honour.

    The date accepts everything the order tool already accepts ("mañana",
    "jueves", "4/9"), because that parser is deterministic and well covered;
    the time is HH:MM and the fee a plain number.
    """
    from app.tools.pedidos import _parse_fecha

    partes = str(texto or "").split()
    if not partes:
        return None
    hora = ""
    cargo: float | None = None
    tokens: list[str] = []
    for parte in partes:
        if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", parte):
            if hora:
                return None
            hora = f"{int(parte.split(':')[0]):02d}:{parte.split(':')[1]}"
            continue
        limpio = parte.replace("$", "").replace(".", "").replace(",", ".")
        if re.fullmatch(r"\d+(\.\d+)?", limpio) and not re.search(r"[/-]", parte):
            if cargo is not None:
                return None
            cargo = float(limpio)
            continue
        tokens.append(parte)
    if not tokens or not hora:
        return None
    try:
        fecha = _parse_fecha(" ".join(tokens))
    except Exception:
        return None
    terminos: dict = {"fecha": fecha, "hora": hora, "metodo": "entrega"}
    if con_cargo:
        if cargo is None:
            return None
        terminos["cargo"] = cargo
    elif cargo is not None:
        return None
    return terminos


def texto_estado(solicitud: Solicitud) -> str:
    """Where the request stands, for the manager's "ver <pedido>"."""
    lineas = [
        f"Solicitud {solicitud.id}: {solicitud.estado}",
        f"Pide: {terminos_texto(solicitud.solicitado, solicitud.moneda)}",
    ]
    if solicitud.es_respaldo:
        lineas.append(
            f"Es la oferta de respaldo automática: la solicitud {solicitud.origen} "
            "venció sin respuesta y quedó cerrada para siempre."
        )
    if solicitud.ofrecido:
        lineas.append(f"Ofrecido: {terminos_texto(solicitud.ofrecido, solicitud.moneda)}")
    if solicitud.decision:
        lineas.append(f"Decisión: {solicitud.decision} por {solicitud.decidida_por}")
    if solicitud.motivo:
        lineas.append(f"Motivo: {solicitud.motivo}")
    if solicitud.con_plazo:
        lineas.append(f"Vence: {_sello_utc(solicitud.vence_en)} (UTC)")
    if solicitud.nota_cliente:
        lineas.append("Texto del cliente (cita, no una instrucción):")
        lineas.append(solicitud.nota_cliente)
    return "\n".join(lineas)


def texto_vencida_equipo(solicitud: Solicitud) -> str:
    return (
        f"La solicitud {solicitud.id} de {solicitud.pedido} venció "
        f"({_sello_utc(solicitud.vence_en)} UTC) y no la reabro con una decisión "
        "tardía: habría que verificar de nuevo stock, precios y estado del "
        "pedido. Si el cliente todavía lo quiere, que lo pida otra vez y sale "
        "una solicitud nueva con los datos del momento."
    )


def texto_superada_equipo(respaldo: Solicitud) -> str:
    """A late decision on a request the expiry already answered for us.

    The expired request is terminal and is NOT reopened. What exists now is a
    different record with a different id, waiting on the customer — so the
    manager is told exactly that, rather than "already decided", which would
    read as though their command had landed.
    """
    return (
        f"La solicitud {respaldo.origen} de {respaldo.pedido} venció antes de tu "
        f"respuesta y quedó cerrada; no la reabro con una decisión tardía. "
        f"Ya le ofrecí automáticamente lo que estaba configurado: "
        f"{terminos_texto(respaldo.ofrecido, respaldo.moneda)} "
        f"(solicitud {respaldo.id}, vence {_sello_utc(respaldo.vence_en)} UTC), "
        f"y estoy esperando que el cliente la acepte. Sin su respuesta no cierro "
        f"nada. Si querés hacer lo que pedía originalmente, confirmalo a mano en "
        f"ERPNext."
    )


# ---------------------------------------------------------------------------
# The offer, and the customer's explicit yes or no.
# ---------------------------------------------------------------------------


def notificar_equipo_nueva(solicitud: Solicitud) -> bool:
    """Put the structured summary in front of a person. Queued, never inline."""
    return _avisar_equipo(solicitud, texto_para_equipo(solicitud))


def ofrecer_al_cliente(solicitud: Solicitud) -> bool:
    """Queue the offer the customer has to accept in so many words."""
    return _encolar_cliente(
        solicitud, "solicitud_oferta", texto_oferta_cliente(solicitud)
    )


def avisar_rechazo(solicitud: Solicitud) -> bool:
    return _encolar_cliente(
        solicitud, "solicitud_rechazo", texto_rechazo_cliente(solicitud)
    )


def esperando_para(cliente: str) -> Solicitud | None:
    """The single open offer waiting on THIS customer, or None.

    A customer writing "acepto" with no order number is the common case, and
    guessing which order they mean is not acceptable. So this answers only when
    there is exactly one, and the caller asks for the number otherwise.
    """
    cliente = str(cliente or "").strip()
    if not cliente:
        return None
    try:
        abiertos = _redis().zrangebyscore(CLAVE_INDICE, "-inf", "+inf")
    except Exception as exc:
        print(f"[solicitudes] índice no legible ({type(exc).__name__})")
        return None
    encontradas: list[Solicitud] = []
    for crudo in abiertos or []:
        pedido = crudo.decode() if isinstance(crudo, bytes) else str(crudo)
        candidata = leer(pedido)
        if (
            candidata is not None
            and candidata.estado == ESPERANDO_CLIENTE
            and candidata.cliente == cliente
            and not candidata.vencida()
        ):
            encontradas.append(candidata)
    return encontradas[0] if len(encontradas) == 1 else None


def _es_su_pedido(solicitud: Solicitud, telefono_cliente: str) -> bool:
    """Only the customer the order belongs to can accept its offer."""
    from app import telefono as telefonos

    esperado = telefonos.normalizar(telefono_cliente)
    if not esperado:
        return False
    return esperado == _telefono_cliente(solicitud.pedido)


def rechazar_cliente(pedido: str, telefono_cliente: str) -> str:
    """The customer turns the offer down. Deterministic, and it frees the stock."""
    from app.locks import CoordinationError, distributed_lock

    try:
        with distributed_lock(f"solicitud:{pedido}", lease_seconds=60, wait_seconds=10):
            solicitud = leer(pedido)
            if solicitud is None or solicitud.estado != ESPERANDO_CLIENTE:
                return _sin_oferta(pedido, solicitud)
            if not _es_su_pedido(solicitud, telefono_cliente):
                print(f"[solicitudes] {pedido}: respuesta de otro número, ignorada")
                return "No encontré una oferta tuya pendiente."
            liberado, detalle = soltar_reserva(pedido)
            cerrada = registrar(
                solicitud,
                "rechazada_cliente",
                estado=RECHAZADA_CLIENTE,
                motivo="el cliente no aceptó la oferta",
                decidida_en=_ahora(),
            )
            if cerrada is None:
                return (
                    "No pude registrar tu respuesta. Escribime de nuevo en un momento."
                )
    except CoordinationError:
        return "Estoy procesando algo de este pedido. Escribime de nuevo en un momento."

    del liberado
    _avisar_equipo(
        cerrada,
        f"🙅 {pedido}: el cliente no aceptó la oferta "
        f"({terminos_texto(cerrada.ofrecido, cerrada.moneda)}). {detalle.capitalize()}.",
    )
    return (
        f"Listo, no avanzo con {pedido}. Si querés, lo dejamos para un día de "
        "reparto normal.\n\n"
        f"Understood, {pedido} will not go ahead. We can schedule it for a "
        "normal delivery day instead."
    )


def _sin_oferta(pedido: str, solicitud: Solicitud | None) -> str:
    if solicitud is None:
        return f"No tengo una oferta pendiente para {pedido}."
    if solicitud.estado == PENDIENTE:
        return (
            f"Todavía no tengo la respuesta del encargado sobre {pedido}. "
            "Te escribo en cuanto la tenga."
        )
    if solicitud.estado == CUMPLIDA:
        return f"{pedido} ya quedó confirmado con lo que acordamos."
    if solicitud.estado == REVISION_HUMANA:
        return (
            f"Sobre {pedido} está mirándolo una persona antes de cerrarlo. Te "
            "contestamos en cuanto lo revise."
        )
    if solicitud.estado in (REVISION_VENCIDA, REVISION_RESUELTA, VENCIDA):
        return (
            f"Sobre {pedido} ya no tengo nada pendiente para cerrar. Si lo "
            "querés igual, escribime y lo armamos con el stock del momento."
        )
    # Never the raw state name: "ya está cerrada (rechazada_cliente)" is
    # internal vocabulary, and the customer did not ask about a state machine.
    return (
        f"Sobre {pedido} no me quedó nada pendiente de tu parte. Si necesitás "
        "algo más, decime."
    )


def aceptar_cliente(pedido: str, telefono_cliente: str) -> str:
    """The customer accepts. Re-check EVERYTHING, then confirm the order.

    This is the only place in the workflow that submits, and it runs under the
    order's lock with the document re-read from ERPNext: the offer may be hours
    old, and stock, prices, quantities and the order's own state can all have
    moved since. Nothing here is reachable from an LLM.
    """
    from app import erpnext as erp
    from app.locks import CoordinationError, distributed_lock

    try:
        with distributed_lock(f"solicitud:{pedido}", lease_seconds=90, wait_seconds=10):
            solicitud = leer(pedido)
            if solicitud is None or solicitud.estado != ESPERANDO_CLIENTE:
                return _sin_oferta(pedido, solicitud)
            if not _es_su_pedido(solicitud, telefono_cliente):
                print(f"[solicitudes] {pedido}: respuesta de otro número, ignorada")
                return "No encontré una oferta tuya pendiente."
            if solicitud.vencida():
                liberado, detalle = soltar_reserva(pedido)
                del liberado
                vencida = registrar(
                    solicitud,
                    "vencida",
                    estado=VENCIDA,
                    motivo=f"el cliente contestó tarde; {detalle}",
                    decidida_en=_ahora(),
                )
                _avisar_equipo(
                    vencida or solicitud,
                    f"⏰ {pedido}: el cliente aceptó después del vencimiento. No lo "
                    "confirmé; si todavía se puede, hay que rehacerlo con los datos "
                    "del momento.",
                )
                return (
                    f"Pasó el plazo de la oferta de {pedido}, así que no la puedo "
                    "cerrar. Escribime y lo vemos de nuevo con el stock de ahora."
                )

            if solicitud.es_respaldo:
                # This offer was made on a draft that was deliberately closed
                # when its predecessor expired, so nothing was held while the
                # customer thought about it. Re-open it FIRST and re-validate
                # afterwards: revalidar is what proves the units are still
                # there, and it refuses a closed order outright.
                reabierto, por_que = reabrir_borrador(pedido)
                if not reabierto:
                    return _a_revision(solicitud, [por_que])

            try:
                so = erp.policy_get_doc("Sales Order", pedido)
            except Exception as exc:
                print(f"[solicitudes] {pedido}: relectura falló ({type(exc).__name__})")
                return (
                    "No pude verificar el pedido en este momento. Escribime de "
                    "nuevo en un rato."
                )

            problemas = revalidar(so, solicitud)
            if problemas:
                return _a_revision(solicitud, problemas)

            aplicado, detalle_aplicado = _aplicar_terminos(pedido, solicitud)
            if not aplicado:
                return _a_revision(solicitud, [detalle_aplicado])

            try:
                erp.submit_doc("Sales Order", pedido)
            except Exception as exc:
                print(f"[solicitudes] {pedido}: submit falló ({type(exc).__name__})")
                try:
                    actual = erp.policy_get_doc("Sales Order", pedido)
                except Exception:
                    actual = {}
                if int(actual.get("docstatus") or 0) != 1:
                    return _a_revision(
                        solicitud, ["no pude confirmar el pedido en ERPNext"]
                    )
                so = actual

            confirmada = registrar(
                solicitud,
                "cumplida",
                estado=CUMPLIDA,
                motivo="el cliente aceptó la oferta",
                decidida_en=_ahora(),
            )
    except CoordinationError:
        return "Estoy procesando algo de este pedido. Escribime de nuevo en un momento."

    _cerrar_confirmado(pedido, solicitud, confirmada)
    return (
        f"¡Listo! {pedido} quedó confirmado con lo que acordamos: "
        f"{terminos_texto(solicitud.ofrecido, solicitud.moneda)}. Te mando el "
        "detalle enseguida."
    )


def _cerrar_confirmado(pedido: str, solicitud: Solicitud, confirmada: Solicitud | None) -> None:
    """Audit, durable confirmation record, customer notice, manager notice."""
    from app import avisos, confirmacion, notificar

    try:
        erpnext.add_comment(
            "Sales Order",
            pedido,
            f"Solicitud {solicitud.id} cumplida: el cliente aceptó "
            f"{terminos_texto(solicitud.ofrecido, solicitud.moneda)} y el pedido se "
            f"confirmó después de revalidar stock, precios, cantidades y estado "
            f"bajo el lock. Decidió {solicitud.decidida_por}.",
        )
    except Exception as exc:
        print(f"[solicitudes] {pedido}: comentario final falló ({type(exc).__name__})")
    try:
        confirmacion.registrar(pedido, f"solicitud {solicitud.id} aceptada por el cliente")
    except Exception as exc:
        print(f"[solicitudes] {pedido}: marca durable falló ({type(exc).__name__})")
    try:
        completo = erpnext.policy_get_doc("Sales Order", pedido)
    except Exception:
        completo = {"name": pedido}
    try:
        avisos.confirmacion_cliente(completo)
    except Exception as exc:
        print(f"[solicitudes] {pedido}: confirmación al cliente no encolada ({type(exc).__name__})")
    try:
        notificar.notificar_confirmacion(completo, "solicitud aprobada y aceptada")
    except Exception as exc:
        print(f"[solicitudes] {pedido}: aviso al equipo falló ({type(exc).__name__})")
    if confirmada is None:
        print(f"[solicitudes] {pedido}: el cierre de la solicitud no quedó durable")


def _a_revision(solicitud: Solicitud, problemas: list[str]) -> str:
    """The customer said yes but the world moved. Nobody is told a half-truth.

    This is the one exit from the workflow that leaves a LIVE draft behind: the
    order keeps its lines and ERPNext keeps counting them. So the review gets
    its own fresh deadline from the owner's configuration — never the reviewed
    offer's, which is usually minutes away and often already past — and the
    state goes into the expiry index like any other hold.

    Called with the order's lock held, from aceptar_cliente only.
    """
    ahora = _ahora()
    detalle = "; ".join(problemas)[:500]
    horas = revision_horas()
    en_revision = registrar(
        solicitud,
        "revision_humana",
        estado=REVISION_HUMANA,
        motivo=detalle,
        decidida_en=ahora,
        vence_en=ahora + horas * 3600.0,
    )
    if en_revision is None:
        # No durable record means no deadline, and no deadline on a live draft
        # is the permanent stock commitment this whole path exists to avoid.
        # Closing the draft now is the fail-closed direction: a person can
        # always reopen it, and nobody was promised anything.
        return _revision_sin_registro(solicitud, detalle)
    _avisar_equipo(
        en_revision,
        f"⚠️ {solicitud.pedido}: el cliente aceptó la oferta pero NO lo confirmé. "
        f"Cambió algo desde la decisión: {detalle}. El pedido sigue en borrador; "
        f"revisalo y, si corresponde, confirmalo con 'confirmar {solicitud.pedido}'."
        f"\nTenés {horas:g} h: pasado ese plazo cierro el borrador para que deje "
        f"de retener stock, y le aviso al cliente.",
    )
    return (
        f"Gracias por confirmar. Sobre {solicitud.pedido} necesito revisarlo con "
        "una persona antes de cerrarlo: cambió algo desde la oferta. Te "
        "contestamos a la brevedad.\n\n"
        f"Thanks for confirming. I need a person to review {solicitud.pedido} "
        "before closing it: something changed since the offer. We will get back "
        "to you shortly."
    )


def _revision_sin_registro(solicitud: Solicitud, detalle: str) -> str:
    """The review could not be recorded, so the draft must not keep the units.

    Without a durable record there is no deadline, and without a deadline this
    draft would reserve stock until somebody noticed by hand. So the hold goes
    now and a person is told loudly. The order itself is untouched and still
    amendable in ERPNext — what is released is only ERPNext's reservation.
    """
    liberado, como = soltar_reserva(solicitud.pedido)
    del liberado
    print(f"[solicitudes] {solicitud.pedido}: revisión NO durable; solté la reserva")
    # The durable record still reads ESPERANDO_CLIENTE, because the write that
    # would have moved it is the one that failed — so the sweep will reach this
    # order again later and close it as an offer that ran out. That is why
    # texto_respaldo_vencido_cliente says the OFFER lapsed and never that the
    # customer was silent: this is the one path where they demonstrably were
    # not. Dropping the index entry here would only hide it in the common case
    # (reconstruir_indice rebuilds it from ERPNext anyway), so the wording is
    # the fix and the later message stays true.
    _avisar_equipo(
        solicitud,
        evento="revision_sin_registro",
        texto=f"🚨 {solicitud.pedido}: el cliente aceptó, algo había cambiado "
        f"({detalle}) y NO pude registrar la revisión en ERPNext. Cerré el "
        f"borrador para que no retenga stock sin plazo: {como}. "
        f"Está sin confirmar y sin revisión abierta — miralo a mano.",
    )
    return (
        f"Gracias por confirmar. Sobre {solicitud.pedido} tengo un problema "
        "para registrarlo y necesito que lo vea una persona. No queda nada "
        "confirmado a tu nombre; te contestamos a la brevedad.\n\n"
        f"Thanks for confirming. I hit a problem recording {solicitud.pedido} "
        "and a person has to look at it. Nothing is confirmed in your name; we "
        "will get back to you shortly."
    )


# ---------------------------------------------------------------------------
# Revalidation. The same rules as the automatic path, re-run on the CURRENT
# document — never a second implementation that could drift from app/policy.py.
# ---------------------------------------------------------------------------


def revalidar(so: dict, solicitud: Solicitud) -> list[str]:
    """What is wrong with confirming this order NOW. Empty means nothing is.

    The human approval covers exactly one thing — the delivery exception it was
    asked about. It does not cover stock that sold out since, a price somebody
    edited, a quantity that changed, a discount nobody agreed to, or an order
    that is no longer a draft. Each of those is checked here against the
    document as it stands, with app/policy.py's own helpers.
    """
    from app import inventario, policy

    problemas: list[str] = []

    estado = int(so.get("docstatus") or 0)
    if estado != 0:
        return [
            "el pedido ya no es un borrador"
            if estado == 1
            else "el pedido está cancelado"
        ]
    if policy.sin_reserva(so.get("status")):
        return [f"el pedido está {so.get('status')} y ya no reserva stock"]

    cantidades_ahora = _cantidades(so)
    if solicitud.cantidades and cantidades_ahora != solicitud.cantidades:
        problemas.append("cambiaron los renglones o las cantidades del pedido")

    try:
        total_ahora = float(so.get("grand_total") or 0)
    except (TypeError, ValueError):
        total_ahora = -1.0
    if total_ahora < 0:
        problemas.append("no pude leer el total del pedido")
    elif solicitud.total and abs(total_ahora - solicitud.total) > 0.01:
        problemas.append("cambió el total del pedido")

    aprobado_pct = 0.0
    try:
        aprobado_pct = float(solicitud.ofrecido.get("descuento_pct") or 0) / 100.0
    except (TypeError, ValueError):
        problemas.append("el descuento acordado no se pudo leer")
    try:
        efectivo = policy.descuento_efectivo(so)
    except Exception as exc:
        print(f"[solicitudes] {solicitud.pedido}: descuento no medible ({type(exc).__name__})")
        problemas.append("no pude medir el descuento del pedido")
    else:
        if efectivo > aprobado_pct + 0.000001:
            problemas.append(
                f"el pedido tiene {efectivo * 100:.2f}% de descuento y se aprobó "
                f"{aprobado_pct * 100:g}%"
            )

    dia = policy.dia_del_pedido(so)
    if dia is None:
        problemas.append("no pude establecer la fecha del pedido")
    else:
        for item in so.get("items") or []:
            if not isinstance(item, dict):
                continue
            codigo = str(item.get("item_code") or "producto")
            try:
                if not policy.precio_de_lista(
                    item, dia, permitir_descuento=aprobado_pct > 0
                ):
                    problemas.append(f"el precio de {codigo} quedó fuera de lista")
            except Exception:
                problemas.append(f"no pude verificar el precio de {codigo}")

    combinadas: dict[tuple[str, str], float] = {}
    for item in so.get("items") or []:
        if not isinstance(item, dict):
            continue
        codigo = str(item.get("item_code") or "").strip()
        deposito = str(item.get("warehouse") or "").strip()
        if not codigo or not deposito:
            problemas.append("un renglón perdió el producto o el depósito")
            continue
        try:
            qty = policy.cantidad_en_stock_uom(item)
        except Exception:
            problemas.append(f"cantidad inválida en {codigo}")
            continue
        combinadas[(codigo, deposito)] = combinadas.get((codigo, deposito), 0.0) + qty

    for (codigo, deposito), qty in combinadas.items():
        fresco, sin_confianza = inventario.confiable(codigo, deposito)
        if not fresco:
            problemas.append(sin_confianza or f"stock de {codigo} sin verificar")
            continue
        try:
            if not policy.hay_stock_para(
                codigo,
                qty,
                deposito,
                excluir=solicitud.pedido,
                company=str(so.get("company") or ""),
                desde=str(so.get("creation") or ""),
            ):
                problemas.append(f"ya no hay stock de {codigo}")
        except Exception as exc:
            print(f"[solicitudes] {solicitud.pedido}: stock no verificable causa={exc}")
            problemas.append(f"no pude verificar el stock de {codigo}")

    # The day matters whichever way the goods move: a pickup somebody agreed to
    # last Thursday is as stale as a delivery, and confirming it would put a
    # past date on the order.
    metodo = str(solicitud.ofrecido.get("metodo") or "entrega")
    fecha = str(solicitud.ofrecido.get("fecha") or "")
    if not fecha:
        problemas.append(
            "la oferta no dice qué día se retira"
            if metodo == "retiro"
            else "la oferta no dice qué día se entrega"
        )
    else:
        try:
            from datetime import date as _date

            if _date.fromisoformat(fecha) < policy._hoy_del_negocio():
                problemas.append("la fecha acordada ya pasó")
        except Exception:
            problemas.append("la fecha acordada no se pudo interpretar")
    return problemas


def _aplicar_terminos(pedido: str, solicitud: Solicitud) -> tuple[bool, str]:
    """Write the agreed terms onto the order, or say why it cannot be done.

    Date and document discount are real Sales Order fields and are written
    deterministically, with the saved value verified. A delivery FEE is not: a
    stock ERPNext carries charges in a Taxes and Charges row against an account
    head this system cannot invent, so the fee is only written when the owner
    configured which account to use. Without it the order is NOT confirmed and
    a person is asked to add the charge — inventing a total is worse than
    waiting.
    """
    from app import excepciones

    fecha = str(solicitud.ofrecido.get("fecha") or "")
    try:
        descuento = float(solicitud.ofrecido.get("descuento_pct") or 0)
    except (TypeError, ValueError):
        return False, "el descuento acordado no se pudo leer"
    try:
        cargo = float(solicitud.ofrecido.get("cargo") or 0)
    except (TypeError, ValueError):
        return False, "el cargo acordado no se pudo leer"

    try:
        erpnext.policy_aplicar_terminos(
            "Sales Order",
            pedido,
            # Written for a pickup too: delivery_date is the day the goods
            # leave, and leaving the old one there dates the order to a day
            # nobody agreed to — the exception's date, which has often passed.
            delivery_date=fecha,
            descuento_pct=descuento if descuento > 0 else None,
        )
    except Exception as exc:
        print(f"[solicitudes] {pedido}: no pude aplicar los términos ({type(exc).__name__})")
        return False, "no pude escribir la fecha o el descuento acordados en el pedido"

    if cargo <= 0:
        return True, ""
    cuenta = excepciones.cuenta_cargo()
    if not cuenta:
        return False, (
            "hay un cargo de envío acordado y falta configurar ENTREGA_CARGO_CUENTA, "
            "así que no lo escribo en el pedido"
        )
    try:
        erpnext.policy_agregar_cargo(
            pedido, cuenta, excepciones.descripcion_cargo(), cargo
        )
    except Exception as exc:
        print(f"[solicitudes] {pedido}: no pude agregar el cargo ({type(exc).__name__})")
        return False, "no pude agregar el cargo de envío al pedido"
    return True, ""
