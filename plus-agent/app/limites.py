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

import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from redis.exceptions import RedisError

from app import erpnext, locks

# Marca de los comentarios de auditoría en ERPNext. Redis no puede contestar
# «¿me borraron?»: un almacén vacío es idéntico a uno recién instalado. La
# copia durable de cada cambio vive en ERPNext, así que un almacén vacío CON
# cambios registrados es pérdida de datos, no una instalación nueva.
MARCA_DURABLE = "[limite]"
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


@dataclass(frozen=True)
class Definicion:
    nombre: str
    alias: tuple[str, ...]
    significado: str
    unidad: str
    default: str
    minimo: float = 0.0
    maximo: float = 0.0
    booleano: bool = False


# Los seis. `maximo` no es una preferencia: arriba de eso el número es un error
# de tipeo, no una decisión, y aplicarlo sería peor que rechazarlo.
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
        booleano=True,
    ),
}

_VERDADEROS = frozenset({"true", "si", "sí", "1", "on", "yes", "y"})
_FALSOS = frozenset({"false", "no", "0", "off", "n"})


@dataclass(frozen=True)
class Configuracion:
    """Los seis límites ya validados, para una evaluación."""

    tope: float
    tope_qty_por_producto: float
    buffer: float  # fracción 0..0.95, ya dividida por 100
    tope_cliente_nuevo: float
    tope_deuda: float
    descuentos_aprueban: bool
    tope_descuento_pct: float  # fracción 0..0.5, ya dividida por 100


def _texto(valor: object) -> str:
    if isinstance(valor, bytes):
        return valor.decode("utf-8", "replace")
    return "" if valor is None else str(valor)


_durable_cache: tuple[float, bool] | None = None


def _hubo_cambios_durables() -> bool:
    """Si ERPNext recuerda que alguna vez se configuró un límite.

    Es la única pregunta que Redis no puede contestar sobre sí mismo. La
    respuesta se cachea un minuto: si es «sí» el sistema ya está fallando
    cerrado, y si es «no» es porque nunca se configuró nada y no hay nada que
    perder.
    """
    global _durable_cache
    ahora = time.monotonic()
    if _durable_cache and _durable_cache[0] > ahora:
        return _durable_cache[1]
    try:
        filas = erpnext.policy_get_list(
            "Comment",
            filters=[
                ["reference_doctype", "=", "Company"],
                ["content", "like", f"%{MARCA_DURABLE}%"],
            ],
            fields=["name"],
            limit=1,
        )
    except erpnext.ERPNextError as exc:
        raise LimiteError(
            "no pude verificar en ERPNext si los límites se configuraron antes"
        ) from exc
    hubo = bool(filas)
    _durable_cache = (ahora + DURABLE_CACHE_SEGUNDOS, hubo)
    return hubo


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
    return LIMITES[nombre].default, "default"


def _numero(defi: Definicion, crudo: str) -> float:
    try:
        valor = float(str(crudo).strip().replace(",", "."))
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


def validar(nombre: str, crudo: str) -> str:
    """Normaliza un valor para ese límite, o levanta LimiteError."""
    defi = LIMITES[nombre]
    if defi.booleano:
        return "true" if _bool(defi, crudo) else "false"
    return f"{_numero(defi, crudo):g}"


def configuracion() -> Configuracion:
    """Los límites vigentes AHORA. Levanta LimiteError si algo no cierra."""
    almacen = _almacen()
    crudos = {nombre: _resolver(nombre, almacen)[0] for nombre in LIMITES}
    buffer_pct = _numero(LIMITES["STOCK_BUFFER_PCT"], crudos["STOCK_BUFFER_PCT"])
    return Configuracion(
        tope=_numero(LIMITES["AUTO_CONFIRM_MAX"], crudos["AUTO_CONFIRM_MAX"]),
        tope_qty_por_producto=_numero(
            LIMITES["AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO"],
            crudos["AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO"],
        ),
        buffer=buffer_pct / 100.0,
        tope_cliente_nuevo=_numero(
            LIMITES["AUTO_CONFIRM_MAX_CLIENTE_NUEVO"],
            crudos["AUTO_CONFIRM_MAX_CLIENTE_NUEVO"],
        ),
        tope_deuda=_numero(
            LIMITES["AUTO_CONFIRM_MAX_DEBT"], crudos["AUTO_CONFIRM_MAX_DEBT"]
        ),
        descuentos_aprueban=_bool(
            LIMITES["AUTO_CONFIRM_DESCUENTOS_APRUEBAN"],
            crudos["AUTO_CONFIRM_DESCUENTOS_APRUEBAN"],
        ),
        tope_descuento_pct=_numero(
            LIMITES["AUTO_CONFIRM_MAX_DESCUENTO_PCT"],
            crudos["AUTO_CONFIRM_MAX_DESCUENTO_PCT"],
        )
        / 100.0,
    )


def resumen() -> list[dict]:
    """Cada límite con su valor vigente y de dónde salió, para el dueño."""
    almacen = _almacen()
    filas = []
    for nombre, defi in LIMITES.items():
        crudo, origen = _resolver(nombre, almacen)
        try:
            valor = validar(nombre, crudo)
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
    for nombre, defi in LIMITES.items():
        if buscado == nombre.lower() or buscado in defi.alias:
            return defi
    for nombre, defi in LIMITES.items():
        if buscado in nombre.lower() or any(buscado in a for a in defi.alias):
            return defi
    conocidos = ", ".join(defi.alias[0] for defi in LIMITES.values())
    raise LimiteError(f"no conozco el límite «{nombre_o_alias}». Hay: {conocidos}")


def _ahora() -> str:
    zona = os.getenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires").strip()
    try:
        return datetime.now(ZoneInfo(zona)).isoformat(timespec="seconds")
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now().isoformat(timespec="seconds")


def _codigo() -> str:
    return f"{secrets.randbelow(9000) + 1000}"


def vigente(nombre: str) -> str:
    """El valor vigente de un límite, tal como se guardaría."""
    crudo, _ = _resolver(nombre, _almacen())
    try:
        return validar(nombre, crudo)
    except LimiteError:
        return crudo


def proponer(nombre_o_alias: str, valor_crudo: str, telefono: str) -> dict:
    """Valida un cambio y lo deja PENDIENTE de confirmación. No cambia nada.

    El código vuelve al dueño y tiene que volver escrito por él: así ningún
    malentendido del LLM mueve un límite por su cuenta.
    """
    if not telefono:
        raise LimiteError("no sé quién pide el cambio")
    defi = definicion(nombre_o_alias)
    nuevo = validar(defi.nombre, valor_crudo)
    anterior = vigente(defi.nombre)
    codigo = _codigo()
    propuesta = {
        "codigo": codigo,
        "limite": defi.nombre,
        "alias": defi.alias[0],
        "anterior": anterior,
        "nuevo": nuevo,
        "telefono": telefono,
        "ts": _ahora(),
    }
    try:
        locks.conexion().setex(
            f"{CLAVE_PROPUESTA}:{telefono}",
            PROPUESTA_TTL_SEGUNDOS,
            json.dumps(propuesta, ensure_ascii=False),
        )
    except (locks.CoordinationError, RedisError) as exc:
        raise LimiteError("no pude registrar el cambio para confirmarlo") from exc
    return propuesta


def aplicar(codigo: str, telefono: str) -> dict:
    """Aplica el cambio pendiente de ESE teléfono si el código coincide."""
    if not telefono:
        raise LimiteError("no sé quién confirma el cambio")
    clave = f"{CLAVE_PROPUESTA}:{telefono}"
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

    if str(propuesta.get("codigo")) != str(codigo or "").strip():
        raise LimiteError("ese código no es el del cambio pendiente")

    nombre = str(propuesta.get("limite") or "")
    if nombre not in LIMITES:
        raise LimiteError("el cambio pendiente apunta a un límite que no existe")
    nuevo = validar(nombre, str(propuesta.get("nuevo")))
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
        cliente.delete(clave)
    except (locks.CoordinationError, RedisError) as exc:
        raise LimiteError("no pude guardar el cambio") from exc
    print(
        f"[limites] {nombre}: {anterior} -> {nuevo} por {telefono} ({entrada['ts']})"
    )
    return entrada


def _auditar_en_erpnext(entrada: dict) -> None:
    """Deja el cambio anotado en ERPNext, que es lo que sobrevive a todo.

    Sirve para dos cosas: el dueño puede leer el historial en el sistema donde
    vive su contabilidad, y app/limites.py puede distinguir «nunca se
    configuró» de «se perdió el almacén». Si no se puede escribir, el cambio
    NO se aplica: prefiero no mover el límite antes que moverlo sin registro.
    """
    global _durable_cache
    texto = (
        f"{MARCA_DURABLE} {entrada['limite']}: {entrada['anterior']} -> "
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
