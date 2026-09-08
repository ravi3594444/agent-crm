"""El registro de lo que las reglas HABRÍAN dicho. No confirma nada.

POR QUÉ EXISTE
En postura de lanzamiento `app/policy.py::evaluar` vuelve en su segunda línea:
con `AUTO_CONFIRM_MAX=0` no lee el historial, ni la deuda, ni el stock, y deja
en cada pedido el mismo comentario. Así el dueño no tiene con qué decidir su
primer tope: la única forma de saber cuánto se confirmaría solo sería
encenderlo y ver qué pasa, que es exactamente lo que nadie quiere hacer con
pedidos de verdad.

El modo sombra corre las reglas de verdad —las mismas, no una copia: ver
`policy._evaluar`— aparta las DOS puertas que son una postura (el tope del
dueño y el interruptor maestro de stock) y anota el resultado. Nada de esto
decide un pedido ni cambia una palabra de lo que se le dice a un cliente.

DÓNDE VIVE
En ERPNext, como un comentario en el Sales Order:

    [sombra] {"pasa_reglas": true, "motivos_reglas": [], ...}

Un pedido, un registro: el comentario ES la marca de idempotencia, así que el
barrido puede pasar mil veces y sólo anota los que todavía no tienen uno. Redis
guarda un contador por día para poder leer el número sin recorrer ERPNext, y se
RECONSTRUYE desde los comentarios cuando falta, igual que
`solicitudes.reconstruir_indice`.

MEJOR ESFUERZO, A PROPÓSITO
Se escribe con `erpnext.add_comment`, que se traga el error, y no con
`registrar_comentario`, que levanta. Es la diferencia entre una prueba y una
promesa: `[solicitud]` y `[confirmado-por-agente]` sostienen algo que se le
dijo a un cliente y no pueden perderse; un registro de sombra que se pierde
deja el número del día BAJO, nunca al revés, y jamás cambia lo que pasó con el
pedido. Un fallo acá se loguea y se sigue.
"""
from __future__ import annotations

import html
import json
import re
from datetime import UTC, date, datetime

from app import erpnext, locks, policy

MARCA = "[sombra]"

# Suficiente para encontrar el registro del pedido. Uno por pedido: si hay más
# de uno (dos barridos que corrieron a la vez), el más nuevo manda.
MAX_MARCAS = 5
# Cuántos borradores se anotan por ronda. El barrido corre cada 60 s y cada
# anotación es una evaluación completa contra ERPNext (historial, deuda, stock
# y precio por renglón): sin techo, una cola de doscientos borradores dejaría
# el hilo del barrido dentro de ERPNext durante minutos.
POR_RONDA = 10
CONTADOR_TTL_SEGUNDOS = 45 * 24 * 60 * 60

_JSON = re.compile(re.escape(MARCA) + r"\s*(\{.*\})\s*$", re.DOTALL)


def _clave_contador(dia: date) -> str:
    return f"plus-agent:sombra:{dia.isoformat()}"


def _ahora() -> datetime:
    return datetime.now(UTC)


def encendido() -> bool:
    """¿Está en sí el modo sombra? Ilegible = apagado, como cualquier límite.

    Fallar cerrado acá NO es no anotar por las dudas: es no anotar cuando no se
    puede probar que el dueño lo pidió. Un registro de menos no le hace nada a
    nadie.
    """
    from app import limites

    try:
        return limites.configuracion().sombra
    except Exception as exc:  # un límite ilegible no rompe el barrido
        print(f"[sombra] no pude leer si está encendido: {type(exc).__name__}: {exc}")
        return False


def _parsear(contenido: str) -> dict | None:
    """Un registro de sombra sacado de un comentario, como sea que ERPNext lo guardó.

    ERPNext mete etiquetas HTML y escapa entidades en el contenido de un
    comentario, así que el crudo no se parsea nunca. Mismo tratamiento que
    `solicitudes._parsear`.
    """
    texto = re.sub(r"<[^>]+>", " ", html.unescape(str(contenido or "")))
    encontrado = _JSON.search(texto)
    if not encontrado:
        return None
    try:
        datos = json.loads(encontrado.group(1))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return datos if isinstance(datos, dict) else None


def leer(pedido: str) -> dict | None:
    """El registro de sombra del pedido, o None si no tiene o no se pudo leer.

    None NO significa "no pasó las reglas": significa que no hay registro. El
    que llama no puede convertirlo en un número.
    """
    pedido = str(pedido or "").strip()
    if not pedido:
        return None
    try:
        filas = erpnext.policy_get_list(
            "Comment",
            filters=[
                ["reference_doctype", "=", "Sales Order"],
                ["reference_name", "=", pedido],
                ["content", "like", f"%{MARCA}%"],
            ],
            fields=["content", "creation"],
            limit=MAX_MARCAS,
            order_by="creation desc",
        )
    except Exception as exc:
        print(f"[sombra] {pedido}: no pude leer el registro: {type(exc).__name__}")
        return None
    for fila in filas:
        datos = _parsear(str(fila.get("content") or ""))
        if datos is not None:
            return datos
    return None


def ya_anotado(pedido: str) -> bool | None:
    """True/False si se pudo averiguar, None si ERPNext no contestó.

    Los tres estados son distintos y aplastar el None en False haría que el
    barrido anotara de nuevo un pedido que ya tiene su registro cada vez que
    ERPNext tose.
    """
    pedido = str(pedido or "").strip()
    if not pedido:
        return None
    try:
        filas = erpnext.policy_get_list(
            "Comment",
            filters=[
                ["reference_doctype", "=", "Sales Order"],
                ["reference_name", "=", pedido],
                ["content", "like", f"%{MARCA}%"],
            ],
            fields=["name"],
            limit=1,
        )
    except Exception as exc:
        print(f"[sombra] {pedido}: no pude ver si ya estaba anotado: {type(exc).__name__}")
        return None
    return bool(filas)


def anotar(pedido: str, sales_order: dict) -> bool:
    """Evalúa en sombra y deja UN registro en el pedido. True si quedó escrito.

    Mejor esfuerzo: un False no cambia nada de lo que pasó con el pedido ni de
    lo que se le dijo al cliente. No toma ningún lock y no somete nada — eso lo
    garantiza `policy.evaluar_sombra`, que es una lectura pura.
    """
    pedido = str(pedido or "").strip()
    if not pedido:
        return False
    try:
        sombra = policy.evaluar_sombra(sales_order)
    except Exception as exc:  # la evidencia nunca rompe el pedido
        print(f"[sombra] {pedido}: la evaluación falló: {type(exc).__name__}: {exc}")
        return False

    carga = {
        "pasa_reglas": sombra.pasa_reglas,
        "motivos_reglas": sombra.motivos_reglas,
        "motivos_postura": sombra.motivos_postura,
        "ilegible": sombra.ilegible,
        "total": sombra.total,
        "habitual": sombra.habitual,
        "tope_vigente": sombra.tope_vigente,
        "ts": _ahora().isoformat(),
    }
    texto = f"{MARCA} {json.dumps(carga, ensure_ascii=False, sort_keys=True)}"
    # `registrar_comentario` LEVANTA y `add_comment` se lo traga. Acá hace
    # falta el que levanta, y el try/except lo vuelve best-effort igual: sigue
    # sin cambiar una palabra de lo que se le dice a un cliente. La diferencia
    # es que ahora "no hubo excepción" sí significa "quedó escrito", así que el
    # contador y el True que devuelve esta función no cuentan un registro que
    # ERPNext rechazó. Contarlo de más es el error caro: infla la evidencia
    # sobre la que el dueño decide subir un límite.
    try:
        erpnext.registrar_comentario("Sales Order", pedido, texto)
    except Exception as exc:
        print(f"[sombra] {pedido}: no pude anotar el registro: {type(exc).__name__}")
        return False

    _sumar_al_dia(sombra.pasa_reglas)
    print(
        f"[sombra] {pedido} pasa_reglas={sombra.pasa_reglas} "
        f"reglas={len(sombra.motivos_reglas)} postura={len(sombra.motivos_postura)}"
    )
    return True


def _sumar_al_dia(paso: bool) -> None:
    """Contador barato por día. Redis es cache: si falla, el número se reconstruye."""
    dia = _ahora().date()
    campo = "pasan" if paso else "frenados"
    try:
        conexion = locks.conexion()
        conexion.hincrby(_clave_contador(dia), campo, 1)
        conexion.expire(_clave_contador(dia), CONTADOR_TTL_SEGUNDOS)
    except Exception as exc:
        print(f"[sombra] contador de {dia.isoformat()} no se pudo sumar: {type(exc).__name__}")


def contar(dia: date | None = None) -> dict[str, int] | None:
    """{'pasan': n, 'frenados': n} del día, o None si no se pudo saber.

    None es "no sé", no cero: el resumen tiene que poder decir «no pude leer»
    en vez de informar una autonomía de cero que nadie midió.
    """
    dia = dia or _ahora().date()
    try:
        crudo = locks.conexion().hgetall(_clave_contador(dia))
    except Exception as exc:
        print(f"[sombra] contador de {dia.isoformat()} ilegible: {type(exc).__name__}")
        return None
    if not crudo:
        return None
    cuenta = {"pasan": 0, "frenados": 0}
    for clave, valor in crudo.items():
        nombre = clave.decode() if isinstance(clave, bytes) else str(clave)
        if nombre not in cuenta:
            continue
        try:
            cuenta[nombre] = int(valor)
        except (TypeError, ValueError):
            return None
    return cuenta


def registros(pedidos: list[str]) -> dict[str, dict]:
    """Los registros de varios pedidos en UNA lectura. Los que falten, faltan.

    Para el resumen de autonomía (W3): recorrer pedido por pedido serían
    sesenta lecturas para un resumen de una semana.
    """
    unicos = [p for p in dict.fromkeys(str(x or "").strip() for x in pedidos) if p]
    if not unicos:
        return {}
    tope = MAX_MARCAS * len(unicos)
    try:
        filas = erpnext.policy_get_list(
            "Comment",
            filters=[
                ["reference_doctype", "=", "Sales Order"],
                ["reference_name", "in", unicos],
                ["content", "like", f"%{MARCA}%"],
            ],
            fields=["content", "reference_name", "creation"],
            limit=tope,
            order_by="creation desc",
        )
    except Exception as exc:
        print(f"[sombra] no pude leer los registros del lote: {type(exc).__name__}")
        return {}
    encontrados: dict[str, dict] = {}
    for fila in filas:
        nombre = str(fila.get("reference_name") or "").strip()
        if not nombre or nombre in encontrados:
            continue  # creation desc: el primero que aparece es el más nuevo
        datos = _parsear(str(fila.get("content") or ""))
        if datos is not None:
            encontrados[nombre] = datos
    return encontrados
