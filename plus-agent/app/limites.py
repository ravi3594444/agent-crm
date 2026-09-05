"""Los límites de auto-confirmación: los fija el DUEÑO, no el código.

QUIÉN DECIDE QUÉ
  El dueño (una persona) fija los números desde WhatsApp.
  El agente de gerencia (LLM) interpreta lo que escribió y le cuenta qué pasó.
  app/policy.py —Python determinista— es lo ÚNICO que decide si un pedido se
  auto-confirma, y lee estos números él mismo en cada evaluación, incluida la
  revalidación final adentro del lock. El LLM no participa de esa decisión.

DÓNDE VIVEN
  En Redis, el mismo de los locks de negocio (ver locks.conexion). Las
  variables de entorno quedan como valor de ARRANQUE: sirven para levantar el
  sistema, no para configurarlo. El orden de resolución es

      Redis (lo que fijó el dueño)  ->  variable de entorno  ->  default

  y el default de cada límite reproduce el comportamiento de hoy: con nada
  configurado, esta etapa no cambia qué pedidos se confirman solos. Sólo un
  cambio explícito y confirmado por el dueño afloja algo.

SI NO SE PUEDE LEER
  Nunca se adivina un número. LimiteError -> el pedido queda pendiente con un
  motivo que dice qué límite hay que arreglar.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from redis.exceptions import RedisError

from app import erpnext, locks
from app import telefono as telefono_mod

# Marca de los comentarios de auditoría en ERPNext. Redis no puede contestar
# «¿me borraron?»: un almacén vacío es idéntico a uno recién instalado. La
# copia durable de cada cambio vive en ERPNext, así que un almacén vacío CON
# cambios registrados es pérdida de datos, no una instalación nueva.
MARCA_DURABLE = "[limite]"
# Marca APARTE para las reglas de entrega, y en esto está TODO el punto de
# haber separado los dos registros. _hubo_cambios_durables() le pregunta a
# ERPNext por MARCA_DURABLE para distinguir «nunca se configuró» de «se perdió
# el almacén», y un almacén vacío CON cambios registrados hace que
# configuracion() levante — o sea, que no se confirme solo ningún pedido.
# Compartir la marca significaba que un cambio de días de reparto de hoy dejaba
# armado ese fusible para siempre, y un flush de Redis el mes que viene frenaba
# las ventas por un cambio de horario. Son dos hechos distintos y se anotan
# distinto.
MARCA_DURABLE_ENTREGA = "[entrega]"
DURABLE_CACHE_SEGUNDOS = 60.0

CLAVE_VALORES = "plus-agent:limites"
CLAVE_AUDITORIA = "plus-agent:limites:auditoria"
CLAVE_PROPUESTA = "plus-agent:limites:propuesta"

# Un cambio de límite se confirma o se cae solo. Diez minutos es tiempo de
# sobra para leer el mensaje y contestar, y poco para que quede colgado.
PROPUESTA_TTL_SEGUNDOS = 600
AUDITORIA_MAXIMA = 500


class LimiteError(RuntimeError):
    """Un límite falta, no se puede leer, o no es un número creíble."""


# What KIND of value a setting holds. Each kind has exactly one validator and
# one normal form, so "validated" never means "the model thought it looked ok".
NUMERO = "numero"
BOOLEANO = "booleano"
DIAS = "dias"
HORA = "hora"
# Las zonas de reparto. Son dos listas y no una porque app/entrega.py las
# evalúa por separado: con las dos cargadas la dirección necesita LOS DOS
# datos permitidos, y con una sola manda esa sobre su dato.
LOCALIDADES = "localidades"
CODIGOS_POSTALES = "codigos_postales"

# The normal form for "nothing configured". An EMPTY string cannot mean that:
# _resolver treats "" in the store as "unset" and falls through to the
# bootstrap environment, so "borrá los días de reparto" would silently restore
# whatever the .env said. This sentinel stores, resolves and reads back as
# "none", which is what the owner actually asked for.
NINGUNO = "-"
_NINGUNO_DICHO = frozenset({"-", "ninguno", "ninguna", "nada", "no", "vacio", "vacío"})

# Where a delivery value comes from when the store was WIPED: not the owner,
# not the bootstrap environment, not a default — NOTHING is in effect, because
# entrega() offers nothing in that state. resumen() reports the row this way so
# readiness and ver_reglas_de_entrega say what the system will actually do,
# instead of showing .env values it will not offer.
PERDIDO = "perdido"
PROBLEMA_ENTREGA_PERDIDA = (
    "las reglas de entrega se perdieron del almacén y ERPNext tiene cambios "
    "registrados: no rige ningún valor, tampoco el del .env, y no se ofrece "
    "reparto, entrega fuera de día ni retiro hasta que las vuelvas a fijar"
)


@dataclass(frozen=True)
class Definicion:
    nombre: str
    alias: tuple[str, ...]
    significado: str
    unidad: str
    default: str
    minimo: float = 0.0
    maximo: float = 0.0
    tipo: str = NUMERO
    # Only a setting marked opcional may hold NINGUNO. A ceiling cannot be
    # "none"; a list of delivery days can.
    opcional: bool = False

    @property
    def booleano(self) -> bool:
        return self.tipo == BOOLEANO


# Los que fija el dueño. `maximo` no es una preferencia: arriba de eso el
# número es un error de tipeo, no una decisión, y aplicarlo sería peor que
# rechazarlo.
LIMITES: dict[str, Definicion] = {
    "AUTO_CONFIRM_MAX": Definicion(
        nombre="AUTO_CONFIRM_MAX",
        alias=("monto maximo", "monto máximo", "tope", "tope maximo", "monto"),
        significado="Pedido más grande que se puede confirmar sin que lo mire nadie",
        unidad="$",
        default="0",
        maximo=100_000_000.0,
    ),
    "AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO": Definicion(
        nombre="AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO",
        alias=(
            "cantidad maxima por producto",
            "cantidad máxima por producto",
            "cantidad por producto",
            "maximo por producto",
        ),
        significado=(
            "Lo máximo de UN producto que puede llevarse un pedido automático, "
            "en la unidad de stock del producto (litro, kilo, unidad)"
        ),
        unidad="unidad de stock",
        default="0",
        maximo=1_000_000.0,
    ),
    "STOCK_BUFFER_PCT": Definicion(
        nombre="STOCK_BUFFER_PCT",
        alias=("colchon de stock", "colchón de stock", "colchon", "buffer"),
        significado="Margen de stock que se guarda para las ventas todavía no cargadas",
        unidad="%",
        default="20",
        maximo=95.0,
    ),
    "AUTO_CONFIRM_MAX_CLIENTE_NUEVO": Definicion(
        nombre="AUTO_CONFIRM_MAX_CLIENTE_NUEVO",
        alias=("tope cliente nuevo", "cliente nuevo", "clientes nuevos"),
        significado=(
            "Tope para un cliente sin historial suficiente. Con 0, un cliente "
            "nuevo siempre espera a una persona"
        ),
        unidad="$",
        default="0",
        maximo=100_000_000.0,
    ),
    "AUTO_CONFIRM_MAX_DEBT": Definicion(
        nombre="AUTO_CONFIRM_MAX_DEBT",
        alias=("deuda tolerada", "deuda", "deuda vencida"),
        significado="Deuda vencida que se tolera antes de que lo mire una persona",
        unidad="$",
        default="0",
        maximo=100_000_000.0,
    ),
    "AUTO_CONFIRM_MAX_DESCUENTO_PCT": Definicion(
        nombre="AUTO_CONFIRM_MAX_DESCUENTO_PCT",
        alias=(
            "descuento maximo",
            "descuento máximo",
            "tope de descuento",
            "maximo descuento",
        ),
        significado=(
            "Descuento máximo —renglón y pedido SUMADOS— que puede "
            "auto-confirmarse cuando la aprobación de descuentos está en no"
        ),
        unidad="%",
        default="5",
        # Un tope de más de la mitad del precio no es una decisión de negocio
        # que pueda tomar un sistema sin nadie mirando.
        maximo=50.0,
    ),
    "APROBACION_TIMEOUT_HORAS": Definicion(
        nombre="APROBACION_TIMEOUT_HORAS",
        alias=(
            "timeout de aprobacion",
            "timeout de aprobación",
            "espera de aprobacion",
            "espera de aprobación",
            "horas para decidir",
        ),
        significado=(
            "Cuánto espera una decisión pendiente antes de vencer. Es también "
            "lo que un borrador esperando respuesta puede retener stock: "
            "vencido el plazo, el pedido deja de competir con los pedidos vivos"
        ),
        unidad="h",
        default="4",
        # Más de una semana no es una espera, es un pedido olvidado que
        # retiene stock que otro cliente podía llevarse.
        maximo=168.0,
    ),
    "REVISION_TIMEOUT_HORAS": Definicion(
        nombre="REVISION_TIMEOUT_HORAS",
        alias=(
            "plazo de revision",
            "plazo de revisión",
            "revision manual",
            "revisión manual",
            "plazo para revisar",
        ),
        significado=(
            "Cuánto puede quedar un pedido esperando que una persona lo revise "
            "—después de que el cliente aceptó y algo había cambiado— antes de "
            "que el borrador se cierre y deje de retener stock"
        ),
        unidad="h",
        default="24",
        # A review nobody does is a draft holding units another customer could
        # have had. A week is already generous; more is a forgotten order.
        maximo=168.0,
    ),
    "AUTO_CONFIRM_DESCUENTOS_APRUEBAN": Definicion(
        nombre="AUTO_CONFIRM_DESCUENTOS_APRUEBAN",
        alias=("descuentos", "aprobar descuentos", "descuentos aprueban"),
        significado=(
            "Si está en sí, cualquier descuento —del pedido o de un renglón— "
            "pasa por una persona. En no, un descuento puede auto-confirmarse "
            "mientras el precio no supere la lista"
        ),
        unidad="sí/no",
        default="true",
        tipo=BOOLEANO,
    ),
}

_VERDADEROS = frozenset({"true", "si", "sí", "1", "on", "yes", "y"})
_FALSOS = frozenset({"false", "no", "0", "off", "n"})


# ---------------------------------------------------------------------------
# Las reglas de ENTREGA: mismo dueño, mismo código de dos pasos, misma
# auditoría durable. Registro aparte a propósito.
# ---------------------------------------------------------------------------
#
# WHY A SECOND REGISTRY AND NOT NINE MORE ENTRIES IN LIMITES
# configuracion() validates EVERY entry of LIMITES and raises on the first bad
# one, and app/policy.py calls it once per order LINE and again inside the
# submit lock. Put a delivery day in there and a typo in "martes" stops every
# customer's order from confirming — a delivery-schedule mistake would become
# an outage. These settings are read by app/excepciones.py instead, on their
# own, where a bad value fails closed as "no exception is pre-authorized" and
# affects nothing else.
#
# Everything the owner touches is shared: definicion(), validar(), vigente(),
# resumen(), proponer() and aplicar() all work over TODOS, so a delivery
# setting changes through exactly the same two-step confirmation code and lands
# in the same append-only audit — in Redis and in the durable ERPNext comment.

ENTREGA: dict[str, Definicion] = {
    # Las zonas van PRIMERO porque son la regla que gatea a todas las demás:
    # sin ninguna lista, app/entrega.py no entrega nada solo, no importa qué
    # días ni a qué hora esté configurado. Hasta ahora salían únicamente del
    # entorno, así que "permití reparto en tal ciudad" era la única regla de
    # entrega que el dueño NO podía cambiar por WhatsApp — tenía que editar el
    # .env y reiniciar. Ahora pasa por el mismo propose+código de cuatro
    # dígitos que el resto.
    "ZONAS_ENTREGA_LOCALIDADES": Definicion(
        nombre="ZONAS_ENTREGA_LOCALIDADES",
        alias=(
            "localidades de reparto",
            "localidades",
            "zonas de reparto",
            "zonas",
            "ciudades de reparto",
            "ciudades",
        ),
        significado=(
            "Las localidades donde se reparte sin que lo mire una persona. Con "
            "los códigos postales también cargados, la dirección necesita LOS "
            "DOS datos permitidos"
        ),
        unidad="localidades",
        default=NINGUNO,
        tipo=LOCALIDADES,
        opcional=True,
    ),
    "ZONAS_ENTREGA_CP": Definicion(
        nombre="ZONAS_ENTREGA_CP",
        alias=(
            "codigos postales",
            "códigos postales",
            "codigo postal",
            "código postal",
            "cp de reparto",
            "cp",
        ),
        significado=(
            "Los códigos postales donde se reparte sin que lo mire una "
            "persona. Con las localidades también cargadas, la dirección "
            "necesita LOS DOS datos permitidos"
        ),
        unidad="códigos postales",
        default=NINGUNO,
        tipo=CODIGOS_POSTALES,
        opcional=True,
    ),
    "ENTREGA_DIAS": Definicion(
        nombre="ENTREGA_DIAS",
        alias=("días de reparto", "dias de reparto", "días de entrega", "dias de entrega"),
        significado=(
            "Los días en que sale el reparto normal. Es lo que se le ofrece a "
            "un cliente cuando una solicitud vence sin que nadie la conteste"
        ),
        unidad="días",
        default=NINGUNO,
        tipo=DIAS,
        opcional=True,
    ),
    "ENTREGA_HORA": Definicion(
        nombre="ENTREGA_HORA",
        alias=("hora de reparto", "hora de entrega", "horario de reparto"),
        significado="La hora que se promete para el reparto normal",
        unidad="hh:mm",
        default=NINGUNO,
        tipo=HORA,
        opcional=True,
    ),
    "ENTREGA_EXCEPCION_ACTIVA": Definicion(
        nombre="ENTREGA_EXCEPCION_ACTIVA",
        alias=("entregas fuera de día", "entregas fuera de dia", "excepciones de entrega"),
        significado=(
            "Si está en sí, se puede entregar un día sin reparto sin que lo "
            "mire nadie, siempre que el resto de la configuración cierre"
        ),
        unidad="sí/no",
        default="false",
        tipo=BOOLEANO,
    ),
    "ENTREGA_EXCEPCION_DIAS": Definicion(
        nombre="ENTREGA_EXCEPCION_DIAS",
        alias=("días fuera de día", "dias fuera de dia", "días de excepción", "dias de excepcion"),
        significado="Los días en que sí se entrega fuera del reparto normal",
        unidad="días",
        default=NINGUNO,
        tipo=DIAS,
        opcional=True,
    ),
    "ENTREGA_EXCEPCION_HORA": Definicion(
        nombre="ENTREGA_EXCEPCION_HORA",
        alias=("hora fuera de día", "hora fuera de dia", "hora de excepción", "hora de excepcion"),
        significado="La hora que se promete para una entrega fuera de día",
        unidad="hh:mm",
        default=NINGUNO,
        tipo=HORA,
        opcional=True,
    ),
    "ENTREGA_EXCEPCION_CARGO": Definicion(
        nombre="ENTREGA_EXCEPCION_CARGO",
        alias=("cargo fuera de día", "cargo fuera de dia", "cargo de excepción", "cargo de envío"),
        significado=(
            "Lo que se cobra por una entrega fuera de día. Sólo se escribe en "
            "el pedido si además está configurada la cuenta contable"
        ),
        unidad="$",
        default=NINGUNO,
        opcional=True,
        maximo=10_000_000.0,
    ),
    "ENTREGA_EXCEPCION_MIN_TOTAL": Definicion(
        nombre="ENTREGA_EXCEPCION_MIN_TOTAL",
        alias=("mínimo fuera de día", "minimo fuera de dia", "mínimo de excepción", "pedido mínimo"),
        significado=(
            "Total mínimo del pedido para que una entrega fuera de día esté "
            "pre-autorizada. Con 0 no hay mínimo"
        ),
        unidad="$",
        default="0",
        maximo=100_000_000.0,
    ),
    "RETIRO_LOCAL_ACTIVO": Definicion(
        nombre="RETIRO_LOCAL_ACTIVO",
        alias=("retiro en el local", "retiro por el local", "retiros"),
        significado=(
            "Si está en sí, cuando no hay reparto al que subir un pedido se le "
            "puede ofrecer al cliente que lo pase a buscar"
        ),
        unidad="sí/no",
        default="false",
        tipo=BOOLEANO,
    ),
    "RETIRO_LOCAL_DIAS": Definicion(
        nombre="RETIRO_LOCAL_DIAS",
        alias=("días de retiro", "dias de retiro"),
        significado="Los días en que se puede pasar a buscar un pedido por el local",
        unidad="días",
        default=NINGUNO,
        tipo=DIAS,
        opcional=True,
    ),
    "RETIRO_LOCAL_HORA": Definicion(
        nombre="RETIRO_LOCAL_HORA",
        alias=("hora de retiro", "horario de retiro"),
        significado="La hora a la que se puede pasar a buscar un pedido",
        unidad="hh:mm",
        default=NINGUNO,
        tipo=HORA,
        opcional=True,
    ),
}

# Everything the owner can set, in one mapping. LIMITES stays separate above
# because only it feeds configuracion().
TODOS: dict[str, Definicion] = {**LIMITES, **ENTREGA}

# La cuenta contable NO se toca por WhatsApp. Es un account head real de
# ERPNext: escribir el nombre equivocado no rompe el bot, desbalancea la
# contabilidad del dueño, y ningún modelo interpretando "poneme la cuenta de
# fletes" puede verificar que exista. Se sigue configurando por entorno.
CUENTA_CARGO = "ENTREGA_CARGO_CUENTA"

# Accent-free and lowercase, so "Miércoles" and "miercoles" are one day. The
# normal form stored is this spelling, which is also what app/excepciones.py
# parses — one vocabulary, so a value cannot validate here and fail there.
_ORDEN_DIAS = (
    "lunes",
    "martes",
    "miercoles",
    "jueves",
    "viernes",
    "sabado",
    "domingo",
)
_DIAS_SEMANA = {nombre: indice for indice, nombre in enumerate(_ORDEN_DIAS)}
_HORA_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _sin_tildes(texto: object) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", str(texto or "").lower())
        if unicodedata.category(char) != "Mn"
    ).strip()


@dataclass(frozen=True)
class Configuracion:
    """Los límites ya validados, para una evaluación."""

    tope: float
    tope_qty_por_producto: float
    buffer: float  # fracción 0..0.95, ya dividida por 100
    tope_cliente_nuevo: float
    tope_deuda: float
    descuentos_aprueban: bool
    tope_descuento_pct: float  # fracción 0..0.5, ya dividida por 100
    # Horas que espera una decisión pendiente, y que su borrador puede retener
    # stock. Lo lee app/solicitudes.py; nunca es 0 (un plazo de 0 vencería
    # todo al instante y un plazo infinito congelaría el stock).
    timeout_aprobacion: float = 4.0
    # Lo mismo para un pedido que quedó esperando que lo revise una persona.
    # Sin plazo, ese borrador retendría stock para siempre: es la única salida
    # del flujo que no la tenía.
    timeout_revision: float = 24.0


def _texto(valor: object) -> str:
    if isinstance(valor, bytes):
        return valor.decode("utf-8", "replace")
    return "" if valor is None else str(valor)


_durable_cache: tuple[float, bool] | None = None
_durable_cache_entrega: tuple[float, bool] | None = None


def _consultar_marca(marca: str, queja: str) -> bool:
    """¿Hay en ERPNext algún comentario de auditoría con esa marca?

    Sin cachear: los dos que preguntan tienen su propio caché, porque una marca
    puede estar y la otra no y ésa es exactamente la distinción que importa.
    """
    try:
        filas = erpnext.policy_get_list(
            "Comment",
            filters=[
                ["reference_doctype", "=", "Company"],
                ["content", "like", f"%{marca}%"],
            ],
            fields=["name"],
            limit=1,
        )
    except erpnext.ERPNextError as exc:
        raise LimiteError(queja) from exc
    return bool(filas)


def _hubo_cambios_durables() -> bool:
    """Si ERPNext recuerda que alguna vez se configuró un LÍMITE.

    Es la única pregunta que Redis no puede contestar sobre sí mismo. La
    respuesta se cachea un minuto: si es «sí» el sistema ya está fallando
    cerrado, y si es «no» es porque nunca se configuró nada y no hay nada que
    perder.

    Pregunta SÓLO por MARCA_DURABLE. Un cambio de reglas de entrega no arma
    este fusible: no es un límite y no decide si un pedido se confirma solo.
    """
    global _durable_cache
    ahora = time.monotonic()
    if _durable_cache and _durable_cache[0] > ahora:
        return _durable_cache[1]
    hubo = _consultar_marca(
        MARCA_DURABLE,
        "no pude verificar en ERPNext si los límites se configuraron antes",
    )
    _durable_cache = (ahora + DURABLE_CACHE_SEGUNDOS, hubo)
    return hubo


def _hubo_cambios_durables_entrega() -> bool:
    """Si ERPNext recuerda que alguna vez se configuró una regla de ENTREGA.

    NUNCA levanta: una regla de entrega que no se puede resolver cuesta un
    mensaje de WhatsApp, no una venta que no cierra, y ésa es la asimetría
    entera entre los dos registros.

    «No pude averiguarlo» devuelve True, o sea se trata como almacén perdido.
    No es pesimismo: lo único que hace ese True es NO habilitar el entorno de
    arranque, y todo lo que hay en Entrega sólo puede ensanchar lo que el
    sistema ofrece por su cuenta. Fallar para el otro lado sería restaurar un
    día de reparto que el dueño borró.
    """
    global _durable_cache_entrega
    ahora = time.monotonic()
    if _durable_cache_entrega and _durable_cache_entrega[0] > ahora:
        return _durable_cache_entrega[1]
    try:
        hubo = _consultar_marca(MARCA_DURABLE_ENTREGA, "entrega no verificable")
    except LimiteError:
        print(
            "[limites] no pude verificar en ERPNext si las reglas de entrega "
            "se configuraron antes: no habilito el entorno de arranque"
        )
        return True
    _durable_cache_entrega = (ahora + DURABLE_CACHE_SEGUNDOS, hubo)
    return hubo


def _reglas_de_entrega_perdidas(almacen: dict[str, str]) -> bool:
    """An EMPTY delivery store with [entrega] changes on record: it was wiped.

    ONE question, asked by entrega() — which decides — and by resumen() and
    vigente() — which show — so what the owner is shown cannot disagree with
    what the system will do. Falling back to the bootstrap environment in this
    state would restore whatever the .env says: a day he removed, an exception
    he turned off. A silent WIDENING, so nothing is in effect until he sets
    the rules again.
    """
    sin_reglas_del_dueno = not any(almacen.get(nombre, "").strip() for nombre in ENTREGA)
    return sin_reglas_del_dueno and _hubo_cambios_durables_entrega()


def _almacen() -> dict[str, str]:
    """Lo que fijó el dueño. Falla cerrada si Redis no contesta.

    No cae al entorno cuando Redis está caído: eso convertiría una caída en un
    aflojamiento silencioso de un límite que el dueño había apretado. Y si el
    almacén aparece VACÍO pero ERPNext tiene cambios registrados, entonces se
    perdieron los datos: tampoco se cae al entorno, porque el valor de arranque
    puede ser más flojo que el que había fijado el dueño.
    """
    try:
        crudo = locks.conexion().hgetall(CLAVE_VALORES)
    except (locks.CoordinationError, RedisError) as exc:
        raise LimiteError("no pude leer los límites configurados") from exc
    valores = {_texto(k): _texto(v) for k, v in (crudo or {}).items()}
    if not valores and _hubo_cambios_durables():
        raise LimiteError(
            "los límites que configuró el dueño no están en el almacén, y "
            "ERPNext tiene cambios registrados: hay que restaurarlos antes de "
            "que algo se confirme solo"
        )
    return valores


def _resolver(nombre: str, almacen: dict[str, str]) -> tuple[str, str]:
    """(valor, origen). origen: 'dueño' | 'arranque' | 'default'."""
    fijado = almacen.get(nombre, "").strip()
    if fijado:
        return fijado, "dueño"
    del_entorno = os.getenv(nombre, "").strip()
    if del_entorno:
        return del_entorno, "arranque"
    return TODOS[nombre].default, "default"


# "1.500" is fifteen hundred pesos to an Argentine owner and one-and-a-half to
# float(). app/solicitudes.py::parsear_terminos already strips the dots when
# the manager types a delivery fee, so a money SETTING has to read the same
# way or the same three keystrokes mean two things. The rule is deliberately
# narrow — groups of exactly three digits — so "1.5" stays 1.5 and a percentage
# or a number of hours is never touched.
_MILES = re.compile(r"^\d{1,3}(\.\d{3})+$")
# Which units a "." can be a THOUSANDS separator in. An owner writing "1.000"
# means a thousand pesos and a thousand litres, but "1.5" hours and "1.5" per
# cent are decimals — and in es-AR nobody groups a percentage. Getting this
# wrong on a ceiling is a 1000x error in the direction that oversells, so the
# rule is a list of units rather than a guess about the digits.
_CON_MILES = frozenset({"$", "unidad de stock"})


def _numero(defi: Definicion, crudo: str, *, tecleado: bool = True) -> float:
    """El número, o LimiteError.

    ``tecleado`` dice que este texto lo escribió una PERSONA, que es la única
    vez que se puede aplicar la regla de miles — porque esa regla NO es
    idempotente. "1,125" es un peso doce; normaliza a "1.125"; y volver a
    normalizar ESO lee el punto como separador de miles y da 1125. Corriendo
    dos veces, una en proponer() y otra en aplicar(), le muestra al dueño un
    número y guarda otro mil veces más grande. Así que un valor que ya está en
    forma normal se lee como float y nunca se re-agrupa.
    """
    texto = str(crudo).strip().replace("$", "").strip()
    if tecleado and defi.unidad in _CON_MILES:
        entero, coma, decimales = texto.partition(",")
        if _MILES.match(entero):
            entero = entero.replace(".", "")
        texto = f"{entero},{decimales}" if coma else entero
    try:
        valor = float(texto.replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise LimiteError(
            f"«{defi.alias[0]}» no es un número: {crudo!r}"
        ) from exc
    if valor != valor or valor in (float("inf"), float("-inf")):
        raise LimiteError(f"«{defi.alias[0]}» no es un número usable: {crudo!r}")
    if valor < defi.minimo:
        raise LimiteError(
            f"«{defi.alias[0]}» no puede ser menor que {defi.minimo:g}"
        )
    if valor > defi.maximo:
        raise LimiteError(
            f"«{defi.alias[0]}» {valor:g} es imposible: el máximo es "
            f"{defi.maximo:g} {defi.unidad}".strip()
        )
    return valor


def _bool(defi: Definicion, crudo: str) -> bool:
    normal = str(crudo).strip().lower()
    if normal in _VERDADEROS:
        return True
    if normal in _FALSOS:
        return False
    raise LimiteError(
        f"«{defi.alias[0]}» tiene que ser sí o no, no {crudo!r}"
    )


def _dias(defi: Definicion, crudo: str) -> str:
    """"Martes y viernes" -> "martes,viernes". Deterministic, no judgement.

    Separators are commas, whitespace and the word "y", because that is how a
    person writes a list. Anything that is not a weekday is refused by name:
    silently dropping it would schedule a round the owner did not ask for.
    """
    texto = _sin_tildes(crudo).replace(" y ", ",")
    partes = [parte for parte in re.split(r"[,\s]+", texto) if parte]
    if not partes:
        raise LimiteError(f"«{defi.alias[0]}» está vacío: decime qué días")
    elegidos: set[str] = set()
    for parte in partes:
        if parte not in _DIAS_SEMANA:
            raise LimiteError(
                f"«{parte}» no es un día de la semana. Van así: "
                f"{', '.join(_ORDEN_DIAS)}"
            )
        elegidos.add(parte)
    return ",".join(dia for dia in _ORDEN_DIAS if dia in elegidos)


def _partes_de_lista(crudo: str) -> list[str]:
    """Lo que una persona escribe como lista: comas, y la palabra "y".

    NO se corta por espacios: "Villa Allende" es UNA localidad. Por eso esto
    no es `_dias`, que sí puede cortar por espacios porque ningún día de la
    semana lleva uno.
    """
    texto = str(crudo or "")
    # " y " sólo como separador entre elementos, nunca dentro de una palabra.
    texto = re.sub(r"\s+y\s+", ",", texto, flags=re.IGNORECASE)
    return [parte.strip() for parte in texto.split(",") if parte.strip()]


def _localidades(defi: Definicion, crudo: str) -> str:
    """"Villa Allende y Cordoba" -> "Villa Allende, Cordoba".

    Se guarda COMO LO ESCRIBIÓ el dueño, porque esto se le muestra de vuelta
    en `ver_reglas_de_entrega` y en el pedido de confirmación. La comparación
    contra la dirección de un pedido la normaliza app/entrega.py, que ya lo
    hacía cuando esto salía del entorno: acá no hay criterio, hay una lista.
    """
    partes = _partes_de_lista(crudo)
    if not partes:
        raise LimiteError(
            f"«{defi.alias[0]}» está vacío: decime en qué localidades repartís"
        )
    elegidas: list[str] = []
    vistas: set[str] = set()
    for parte in partes:
        limpia = " ".join(parte.split())
        # Dedup sin tildes y sin caso: "Córdoba" y "cordoba" son la misma.
        clave = re.sub(r"[^a-z0-9]+", " ", _sin_tildes(limpia)).strip()
        if not clave:
            raise LimiteError(
                f"«{parte}» no es una localidad: no tiene ni una letra ni un número"
            )
        if clave not in vistas:
            vistas.add(clave)
            elegidas.append(limpia)
    return ", ".join(elegidas)


def _codigos_postales(defi: Definicion, crudo: str) -> str:
    """"5000, x5105abc" -> "5000, X5105ABC". Alfanumérico, en mayúsculas.

    Misma forma normal que usa app/entrega.py::normalizar_cp para comparar, así
    que lo que se guarda es exactamente lo que se va a comparar.
    """
    partes: list[str] = []
    for grupo in _partes_de_lista(crudo):
        partes.extend(p for p in re.split(r"\s+", grupo) if p)
    if not partes:
        raise LimiteError(
            f"«{defi.alias[0]}» está vacío: decime qué códigos postales"
        )
    elegidos: list[str] = []
    for parte in partes:
        limpio = re.sub(r"[^A-Z0-9]+", "", parte.upper())
        if not limpio:
            raise LimiteError(f"«{parte}» no es un código postal")
        if limpio not in elegidos:
            elegidos.append(limpio)
    return ", ".join(elegidos)


def _hora(defi: Definicion, crudo: str) -> str:
    """"9", "9:30", "09:30" -> "09:30". Refuses anything that is not a time."""
    texto = _sin_tildes(crudo).replace(".", ":").replace("hs", "").replace("h", "").strip()
    if re.fullmatch(r"\d{1,2}", texto):
        texto = f"{texto}:00"
    encontrado = _HORA_RE.match(texto)
    if not encontrado:
        raise LimiteError(
            f"«{defi.alias[0]}» tiene que ser una hora tipo 08:00, no {crudo!r}"
        )
    return f"{int(encontrado.group(1)):02d}:{encontrado.group(2)}"


def validar(nombre: str, crudo: str, *, tecleado: bool = True) -> str:
    """Normaliza un valor para ese ajuste, o levanta LimiteError.

    The normal form is what gets stored, shown back to the owner and written
    into the audit, so every kind has exactly one — and a value that validates
    here is a value app/excepciones.py can read without re-interpreting it.

    ``tecleado=False`` re-lee un valor que YA está en forma normal: una
    propuesta pendiente, o lo que el dueño confirmó hace un mes y está en el
    almacén. Todas las clases de acá son idempotentes menos la plata; ver
    ``_numero``.
    """
    defi = TODOS[nombre]
    if defi.opcional and _sin_tildes(crudo) in _NINGUNO_DICHO:
        return NINGUNO
    if defi.tipo == BOOLEANO:
        return "true" if _bool(defi, crudo) else "false"
    if defi.tipo == DIAS:
        return _dias(defi, crudo)
    if defi.tipo == HORA:
        return _hora(defi, crudo)
    if defi.tipo == LOCALIDADES:
        return _localidades(defi, crudo)
    if defi.tipo == CODIGOS_POSTALES:
        return _codigos_postales(defi, crudo)
    # 12 significant digits, not the default 6: at :g an owner who sets a
    # 1234567 ceiling gets "1.23457e+06" stored, shown back to him and audited,
    # and reads back as 1234570. Every money limit here reaches seven digits.
    return f"{_numero(defi, crudo, tecleado=tecleado):.12g}"


def configuracion() -> Configuracion:
    """Los límites vigentes AHORA. Levanta LimiteError si algo no cierra."""
    almacen = _almacen()
    # LIMITES only, deliberately: see the note above ENTREGA. A malformed
    # delivery day must not be able to stop an order from confirming.
    crudos = {nombre: _resolver(nombre, almacen) for nombre in LIMITES}

    def _num(nombre: str) -> float:
        """El número de ese límite, respetando de dónde salió el texto.

        ESTE es el camino que decide si un pedido se auto-confirma, así que es
        el que más importa que no re-agrupe: un "1.125" que el dueño confirmó
        está guardado en forma normal, y leerlo como miles acá ensancharía el
        tope por mil sin que nadie haya cambiado nada.
        """
        crudo, origen = crudos[nombre]
        return _numero(LIMITES[nombre], crudo, tecleado=origen != "dueño")

    return Configuracion(
        tope=_num("AUTO_CONFIRM_MAX"),
        tope_qty_por_producto=_num("AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO"),
        buffer=_num("STOCK_BUFFER_PCT") / 100.0,
        tope_cliente_nuevo=_num("AUTO_CONFIRM_MAX_CLIENTE_NUEVO"),
        tope_deuda=_num("AUTO_CONFIRM_MAX_DEBT"),
        descuentos_aprueban=_bool(
            LIMITES["AUTO_CONFIRM_DESCUENTOS_APRUEBAN"],
            crudos["AUTO_CONFIRM_DESCUENTOS_APRUEBAN"][0],
        ),
        tope_descuento_pct=_num("AUTO_CONFIRM_MAX_DESCUENTO_PCT") / 100.0,
        timeout_aprobacion=_timeout(
            _num("APROBACION_TIMEOUT_HORAS"), "APROBACION_TIMEOUT_HORAS"
        ),
        timeout_revision=_timeout(
            _num("REVISION_TIMEOUT_HORAS"), "REVISION_TIMEOUT_HORAS"
        ),
    )


def _timeout(horas: float, nombre: str) -> float:
    """Un plazo de 0 vencería todo al instante: vuelve al default del límite."""
    return horas if horas > 0 else float(LIMITES[nombre].default)


@dataclass(frozen=True)
class Entrega:
    """Las reglas de entrega vigentes AHORA, ya normalizadas.

    Read per operation by app/excepciones.py, so a change the owner confirms
    applies to the next message with nothing restarted.

    FAILS SOFT, ON PURPOSE. Every field here can only ever WIDEN what the
    system offers by itself, so "unreadable" has to mean "offer nothing" — an
    empty day list, an empty time, a disabled switch, no fee. That is the same
    direction app/excepciones.py already fails in, and it is why a typo in a
    delivery day costs one WhatsApp message instead of stopping every order:
    unlike Configuracion, nothing here raises.
    """

    dias_reparto: tuple[int, ...] = ()
    hora_reparto: str = ""
    excepcion_activa: bool = False
    excepcion_dias: tuple[int, ...] = ()
    excepcion_hora: str = ""
    excepcion_cargo: float | None = None
    # None means "configured but unreadable", and it is NOT the same as 0.
    # Every other field here can only WIDEN what the system offers by itself,
    # so failing soft on them offers less. The minimum is the one field that
    # NARROWS — it is what stops a $200 order earning a free off-day trip — so
    # losing it would fail OPEN. None therefore blocks the exception outright.
    excepcion_minimo: float | None = 0.0
    retiro_activo: bool = False
    retiro_dias: tuple[int, ...] = ()
    retiro_hora: str = ""


def _bruto(nombre: str, almacen: dict[str, str]) -> tuple[str, bool]:
    """(normalized value or '', whether it could be read at all).

    Validation happens on the way IN (validar, behind the confirmation code),
    but a bootstrap environment variable never went through it and a stored
    value can predate a rule. So it is re-normalized here and a bad one reads
    as absent rather than raising into the deterministic path.

    The second element exists because "unset" and "unreadable" are the same
    thing for a widening setting and OPPOSITE things for a narrowing one — see
    Entrega.excepcion_minimo. Callers that do not care use ``_crudo``.
    """
    crudo, origen = _resolver(nombre, almacen)
    try:
        # Lo que fijó el dueño ya está en forma normal; el entorno de arranque
        # lo escribió una persona y conserva la lectura tecleada.
        valor = validar(nombre, crudo, tecleado=origen != "dueño")
    except LimiteError as exc:
        print(f"[limites] {nombre} no usable: {exc}")
        return "", False
    return ("" if valor == NINGUNO else valor), True


def _crudo(nombre: str, almacen: dict[str, str]) -> str:
    """The normalized value of one delivery setting, or '' if it is unusable."""
    return _bruto(nombre, almacen)[0]


def _indices(valor: str) -> tuple[int, ...]:
    return tuple(
        sorted(
            _DIAS_SEMANA[parte]
            for parte in valor.split(",")
            if parte in _DIAS_SEMANA
        )
    )


def _plata(valor: str) -> float | None:
    if not valor:
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def entrega() -> Entrega:
    """Las reglas de entrega vigentes. Nunca levanta: ver Entrega."""
    try:
        almacen = _almacen()
    except LimiteError as exc:
        # A store that cannot be read must not be talked into an offer. Same
        # rule as the limits: the difference is that here it costs an
        # exception nobody was promised, not an order that cannot confirm.
        print(f"[limites] reglas de entrega no legibles: {exc}")
        return Entrega()
    # An empty delivery store plus changes on record means the store was WIPED,
    # and falling back to the bootstrap environment here would restore whatever
    # the .env says — a day he removed, an exception he turned off. That is a
    # silent WIDENING of what the system offers on its own, so it offers
    # nothing instead and a person is asked. Unlike the limits this never
    # raises: see _hubo_cambios_durables_entrega. resumen() and vigente() ask
    # the SAME question, so readiness and the owner's ver_reglas_de_entrega
    # report these rows as lost rather than as .env values.
    if _reglas_de_entrega_perdidas(almacen):
        print(
            "[limites] las reglas de entrega no están en el almacén y ERPNext "
            "tiene cambios registrados: no ofrezco nada por mi cuenta"
        )
        return Entrega()
    # Read through _bruto, not _crudo: a minimum nobody can parse must not
    # read as "no minimum". See Entrega.excepcion_minimo.
    minimo_texto, minimo_legible = _bruto("ENTREGA_EXCEPCION_MIN_TOTAL", almacen)
    if minimo_legible:
        minimo = _plata(minimo_texto)
        minimo = 0.0 if minimo is None else minimo
    else:
        minimo = None
    return Entrega(
        dias_reparto=_indices(_crudo("ENTREGA_DIAS", almacen)),
        hora_reparto=_crudo("ENTREGA_HORA", almacen),
        excepcion_activa=_crudo("ENTREGA_EXCEPCION_ACTIVA", almacen) == "true",
        excepcion_dias=_indices(_crudo("ENTREGA_EXCEPCION_DIAS", almacen)),
        excepcion_hora=_crudo("ENTREGA_EXCEPCION_HORA", almacen),
        excepcion_cargo=_plata(_crudo("ENTREGA_EXCEPCION_CARGO", almacen)),
        excepcion_minimo=minimo,
        retiro_activo=_crudo("RETIRO_LOCAL_ACTIVO", almacen) == "true",
        retiro_dias=_indices(_crudo("RETIRO_LOCAL_DIAS", almacen)),
        retiro_hora=_crudo("RETIRO_LOCAL_HORA", almacen),
    )


def zonas() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(códigos postales, localidades) donde se reparte. Nunca levanta.

    Misma resolución y mismo fusible que `entrega()`: almacén -> entorno ->
    default, y con el almacén de entrega VACÍO y cambios registrados en
    ERPNext devuelve las dos listas vacías. Eso hace que app/entrega.py
    conteste SIN_ZONAS y ningún pedido se entregue solo — que es lo correcto
    cuando se perdió la configuración: un flush de Redis no puede ensanchar
    la zona de reparto de vuelta a lo que decía el .env.

    Se lee en cada llamada, así que un cambio confirmado rige en el próximo
    pedido sin reiniciar nada.
    """
    try:
        almacen = _almacen()
    except LimiteError as exc:
        print(f"[limites] zonas de reparto no legibles: {exc}")
        return (), ()
    if _reglas_de_entrega_perdidas(almacen):
        print(
            "[limites] las zonas de reparto no están en el almacén y ERPNext "
            "tiene cambios registrados: no entrego nada por mi cuenta"
        )
        return (), ()
    def _partes(nombre: str) -> tuple[str, ...]:
        valor = _crudo(nombre, almacen)
        return tuple(p.strip() for p in valor.split(",") if p.strip())

    return _partes("ZONAS_ENTREGA_CP"), _partes("ZONAS_ENTREGA_LOCALIDADES")


def cuenta_cargo() -> str:
    """La cuenta contable del cargo de envío. SÓLO por entorno.

    Deliberately not in any registry, so no natural-language path can reach
    it: it is a real ERPNext account head, a wrong name silently unbalances
    the owner's books rather than breaking the bot, and no model interpreting
    "poneme la cuenta de fletes" can check that the account exists. Without it
    a fee is simply never written and a person is asked to add the charge —
    which app/solicitudes.py already does.
    """
    return os.getenv(CUENTA_CARGO, "").strip()


def resumen() -> list[dict]:
    """Cada límite con su valor vigente y de dónde salió, para el dueño.

    The delivery rows after a wipe read as LOST — valor "", origen PERDIDO and
    the problem spelled out — because that is the state entrega() decides in,
    and this list is what readiness and ver_reglas_de_entrega show. Resolving
    them from the .env here told him he had a round the system would not run,
    and hid the one thing he needed to know: that his rules were gone.
    """
    almacen = _almacen()
    entrega_perdida = _reglas_de_entrega_perdidas(almacen)
    filas = []
    for nombre, defi in TODOS.items():
        if entrega_perdida and nombre in ENTREGA:
            valor, origen, problema = "", PERDIDO, PROBLEMA_ENTREGA_PERDIDA
        else:
            crudo, origen = _resolver(nombre, almacen)
            try:
                valor = validar(nombre, crudo, tecleado=origen != "dueño")
                problema = ""
            except LimiteError as exc:
                valor = crudo
                problema = str(exc)
        filas.append(
            {
                "nombre": nombre,
                "alias": defi.alias[0],
                "significado": defi.significado,
                "unidad": defi.unidad,
                "valor": valor,
                "origen": origen,
                "problema": problema,
            }
        )
    return filas


def definicion(nombre_o_alias: str) -> Definicion:
    """Encuentra el límite por su nombre técnico o por como lo dice el dueño."""
    buscado = str(nombre_o_alias or "").strip().lower()
    if not buscado:
        raise LimiteError("no me dijiste qué límite")
    for nombre, defi in TODOS.items():
        if buscado == nombre.lower() or buscado in defi.alias:
            return defi
    # Substring fallback, and it must be UNAMBIGUOUS. "hora" alone matches the
    # approval timeout, the review deadline and four delivery times; picking
    # the first one in dict order would let a vague word from the model move a
    # setting the owner never mentioned. Asking is the fail-closed answer.
    parecidos = [
        defi
        for defi in TODOS.values()
        if buscado in defi.nombre.lower() or any(buscado in a for a in defi.alias)
    ]
    if len(parecidos) == 1:
        return parecidos[0]
    if parecidos:
        opciones = ", ".join(f"«{defi.alias[0]}»" for defi in parecidos)
        raise LimiteError(
            f"«{nombre_o_alias}» puede ser varias cosas: {opciones}. Decime cuál"
        )
    conocidos = ", ".join(defi.alias[0] for defi in TODOS.values())
    raise LimiteError(f"no conozco el ajuste «{nombre_o_alias}». Hay: {conocidos}")


def _ahora() -> str:
    zona = os.getenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires").strip()
    try:
        return datetime.now(ZoneInfo(zona)).isoformat(timespec="seconds")
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now().isoformat(timespec="seconds")


def _codigo() -> str:
    return f"{secrets.randbelow(9000) + 1000}"


def vigente(nombre: str) -> str:
    """El valor vigente de un límite, tal como se guardaría.

    A lost delivery rule has NO value in effect — not the .env's either — so it
    reads as NINGUNO: the «anterior» a proposal shows and the audit records is
    what entrega() is actually working with.
    """
    almacen = _almacen()
    if nombre in ENTREGA and _reglas_de_entrega_perdidas(almacen):
        return NINGUNO
    crudo, origen = _resolver(nombre, almacen)
    try:
        return validar(nombre, crudo, tecleado=origen != "dueño")
    except LimiteError:
        return crudo


def _tag(telefono: str) -> str:
    """Hash corto, para el log. El número entero no va a stdout."""
    return hashlib.sha256(str(telefono or "").encode()).hexdigest()[:10]


def _clave_propuesta(telefono: str) -> str:
    """La clave de la propuesta, en forma canónica.

    Proponer un cambio y confirmarlo con el código llegan por caminos
    distintos: la herramienta de gerencia (con el teléfono del contexto) y el
    router determinista de app/main.py (con el del webhook). Si cada uno
    normaliza distinto, el código correcto no encuentra nada que aplicar.
    """
    return f"{CLAVE_PROPUESTA}:{telefono_mod.normalizar(telefono) or telefono}"


def _huella_propuesta(telefono: str, limite: str, nuevo: str) -> str:
    """Identidad del cambio: quién, qué ajuste y qué valor YA normalizado.

    El valor entra normalizado a propósito: «20», « 20 » y «20%» son el MISMO
    cambio, y lo son recién después de validar(). Comparar lo tecleado haría
    que tres maneras de escribir lo mismo fueran tres cambios distintos, que es
    justo lo que esto existe para que no pase.
    """
    crudo = json.dumps(
        [telefono_mod.normalizar(telefono) or telefono, limite, nuevo],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(crudo.encode()).hexdigest()[:16]


def _vencida(propuesta: dict, ahora: float | None = None) -> bool:
    """El vencimiento va ADENTRO, no sólo en el TTL.

    Un TTL que no corrió —un Redis restaurado de un backup, un reloj movido— no
    puede revivir un código de ayer.
    """
    try:
        expira = float(propuesta.get("expira") or 0)
    except (TypeError, ValueError):
        return True
    return expira <= (time.time() if ahora is None else ahora)


def _propuesta_viva(telefono: str) -> dict | None:
    """El cambio que ese teléfono dejó esperando, con código y todo."""
    try:
        crudo = locks.conexion().get(_clave_propuesta(telefono))
    except (locks.CoordinationError, RedisError):
        return None
    if not crudo:
        return None
    try:
        propuesta = json.loads(_texto(crudo))
    except ValueError:
        return None
    if not isinstance(propuesta, dict) or _vencida(propuesta):
        return None
    return propuesta


def proponer(nombre_o_alias: str, valor_crudo: str, telefono: str) -> dict:
    """Valida un cambio y lo deja PENDIENTE de confirmación. No cambia nada.

    El código vuelve al dueño y tiene que volver escrito por él: así ningún
    malentendido del LLM mueve un límite por su cuenta.

    PEDIR DOS VECES LO MISMO ES UN PEDIDO, NO DOS. Si ya hay un cambio idéntico
    esperando —mismo teléfono, mismo ajuste, mismo valor normalizado— se
    devuelve ESE, con SU código, y `repetida` en True. Antes cada llamada
    sorteaba un código nuevo y pisaba el anterior: si el turno se reintentaba
    —porque el resultado no se pudo cachear, porque Meta reentregó el
    mensaje— al dueño le llegaban dos mensajes con dos códigos y sólo el
    último servía. Contestar el primero, que es el que estaba mirando, no
    aplicaba nada.
    """
    if not telefono:
        raise LimiteError("no sé quién pide el cambio")
    defi = definicion(nombre_o_alias)
    nuevo = validar(defi.nombre, valor_crudo)
    anterior = vigente(defi.nombre)
    huella = _huella_propuesta(telefono, defi.nombre, nuevo)

    esperando = _propuesta_viva(telefono)
    if esperando and esperando.get("id") == huella:
        # El MISMO código, a propósito: el que él tiene en el teléfono.
        return {**esperando, "repetida": True}

    propuesta = {
        "id": huella,
        "codigo": _codigo(),
        "limite": defi.nombre,
        "alias": defi.alias[0],
        "anterior": anterior,
        "nuevo": nuevo,
        "telefono": telefono,
        "ts": _ahora(),
        "expira": time.time() + PROPUESTA_TTL_SEGUNDOS,
    }
    try:
        locks.conexion().setex(
            _clave_propuesta(telefono),
            PROPUESTA_TTL_SEGUNDOS,
            json.dumps(propuesta, ensure_ascii=False),
        )
    except (locks.CoordinationError, RedisError) as exc:
        raise LimiteError("no pude registrar el cambio para confirmarlo") from exc
    return {**propuesta, "repetida": False}


def pendiente(telefono: str) -> dict | None:
    """El cambio que ese teléfono dejó esperando confirmación, si hay uno.

    Lo usa el router determinista de app/main.py para saber si un mensaje de
    cuatro dígitos ES un código de confirmación. Sin esto tendría que llamar a
    aplicar() para averiguarlo, y un "no hay nada pendiente" no se distinguiría
    de un "ese código está mal": el primero es un mensaje cualquiera que le toca
    contestar al agente, el segundo es algo que el dueño tiene que saber.

    NUNCA devuelve el código. Falla cerrada: si no se puede leer, no hay nada.
    Un cambio vencido no está pendiente, aunque el TTL no haya corrido.
    """
    if not telefono:
        return None
    propuesta = _propuesta_viva(telefono)
    if propuesta is None:
        return None
    return {k: v for k, v in propuesta.items() if k != "codigo"}


def descartar(telefono: str) -> None:
    """Tira el cambio pendiente de ese teléfono. Para cuando el código no llegó.

    Un cambio que espera un código que el dueño nunca vio no se puede confirmar
    y sí puede confundirlo diez minutos después. Mejor no dejarlo.
    """
    if not telefono:
        return
    try:
        locks.conexion().delete(_clave_propuesta(telefono))
    except (locks.CoordinationError, RedisError) as exc:
        print(f"[limites] no pude descartar la propuesta ({type(exc).__name__})")


def aplicar(codigo: str, telefono: str) -> dict:
    """Aplica el cambio pendiente de ESE teléfono si el código coincide.

    Se mira primero y se RECLAMA después. Un código equivocado no consume nada
    —mirar no borra—, y el que sí coincide se lleva la propuesta con un GETDEL,
    que es una sola operación: dos entregas del mismo mensaje, o dos workers a
    la vez, encuentran uno el cambio y el otro nada. Sin eso, el mismo código
    contestado dos veces escribía dos veces la auditoría y dos comentarios en
    ERPNext, y el historial contaba dos cambios donde el dueño hizo uno.
    """
    if not telefono:
        raise LimiteError("no sé quién confirma el cambio")
    limpio = str(codigo or "").strip()
    clave = _clave_propuesta(telefono)
    try:
        crudo = locks.conexion().get(clave)
    except (locks.CoordinationError, RedisError) as exc:
        raise LimiteError("no pude leer el cambio pendiente") from exc
    if not crudo:
        raise LimiteError("no hay ningún cambio esperando confirmación")
    try:
        propuesta = json.loads(_texto(crudo))
    except ValueError as exc:
        raise LimiteError("el cambio pendiente quedó ilegible") from exc

    if str(propuesta.get("codigo")) != limpio:
        raise LimiteError("ese código no es el del cambio pendiente")
    if _vencida(propuesta):
        raise LimiteError(
            "ese código ya venció. No cambié nada: pedime el cambio de nuevo"
        )
    # Atado al teléfono: el código que le llegó a uno no lo aplica otro, aunque
    # los dos estén en la lista del equipo.
    if telefono_mod.normalizar(propuesta.get("telefono")) != telefono_mod.normalizar(
        telefono
    ):
        raise LimiteError("ese código no es de este número")

    # El reclamo. A partir de acá la propuesta es de este turno y de nadie más.
    try:
        reclamado = locks.conexion().getdel(clave)
    except (locks.CoordinationError, RedisError) as exc:
        raise LimiteError("no pude leer el cambio pendiente") from exc
    if not reclamado:
        raise LimiteError("ese cambio ya se confirmó")
    try:
        propuesta = json.loads(_texto(reclamado))
    except ValueError as exc:
        raise LimiteError("el cambio pendiente quedó ilegible") from exc
    # Entre el vistazo y el reclamo pudo entrar otra propuesta: la que se
    # aplica es la que el código nombra, nunca la que quedó en su lugar.
    if str(propuesta.get("codigo")) != limpio:
        raise LimiteError("ese código no es el del cambio pendiente")

    nombre = str(propuesta.get("limite") or "")
    if nombre not in TODOS:
        raise LimiteError("el cambio pendiente apunta a un ajuste que no existe")
    # tecleado=False: proponer() ya normalizó esto. Re-agruparlo es el error
    # de mil veces que describe el docstring de _numero.
    nuevo = validar(nombre, str(propuesta.get("nuevo")), tecleado=False)
    anterior = vigente(nombre)

    entrada = {
        "ts": _ahora(),
        "telefono": telefono,
        "limite": nombre,
        "anterior": anterior,
        "nuevo": nuevo,
    }
    # The durable copy goes in FIRST, and a failure here cancels the change.
    # An audit that only lives in Redis disappears with Redis, and then a wiped
    # store looks like a brand-new install.
    _auditar_en_erpnext(entrada)
    try:
        cliente = locks.conexion()
        cliente.hset(CLAVE_VALORES, nombre, nuevo)
        cliente.rpush(CLAVE_AUDITORIA, json.dumps(entrada, ensure_ascii=False))
        cliente.ltrim(CLAVE_AUDITORIA, -AUDITORIA_MAXIMA, -1)
    except (locks.CoordinationError, RedisError) as exc:
        raise LimiteError("no pude guardar el cambio") from exc
    print(
        f"[limites] {nombre}: {anterior} -> {nuevo} "
        f"por {_tag(telefono)} ({entrada['ts']})"
    )
    return entrada


def _auditar_en_erpnext(entrada: dict) -> None:
    """Deja el cambio anotado en ERPNext, que es lo que sobrevive a todo.

    Sirve para dos cosas: el dueño puede leer el historial en el sistema donde
    vive su contabilidad, y app/limites.py puede distinguir «nunca se
    configuró» de «se perdió el almacén». Si no se puede escribir, el cambio
    NO se aplica: prefiero no mover el límite antes que moverlo sin registro.
    """
    global _durable_cache, _durable_cache_entrega
    entrega_cambio = entrada["limite"] in ENTREGA
    marca = MARCA_DURABLE_ENTREGA if entrega_cambio else MARCA_DURABLE
    texto = (
        f"{marca} {entrada['limite']}: {entrada['anterior']} -> "
        f"{entrada['nuevo']} · lo cambió {entrada['telefono']} "
        f"el {entrada['ts']}"
    )
    try:
        erpnext.registrar_comentario(
            "Company", erpnext.default_company(), texto
        )
    except erpnext.ERPNextError as exc:
        raise LimiteError(
            "no pude registrar el cambio en ERPNext, así que no lo apliqué"
        ) from exc
    if entrega_cambio:
        _durable_cache_entrega = None
    else:
        _durable_cache = None


def auditoria(maximo: int = 10) -> list[dict]:
    """Los últimos cambios, del más nuevo al más viejo."""
    try:
        crudos = locks.conexion().lrange(CLAVE_AUDITORIA, -max(1, maximo), -1)
    except (locks.CoordinationError, RedisError) as exc:
        raise LimiteError("no pude leer el historial de cambios") from exc
    entradas = []
    for crudo in reversed(list(crudos or [])):
        try:
            entradas.append(json.loads(_texto(crudo)))
        except ValueError:
            continue
    return entradas
