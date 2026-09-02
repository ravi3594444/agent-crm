"""Las herramientas con las que el DUEÑO cambia los límites, por WhatsApp.

DÓNDE ESTÁ EL LÍMITE DE LO QUE PUEDE HACER EL LLM
El agente de gerencia interpreta lo que el dueño escribió («subime el tope a
50 mil») y llama a `proponer_limite`. Nada cambia todavía: Python valida el
número, guarda el cambio como PENDIENTE y devuelve un código de cuatro
dígitos. El límite se mueve únicamente cuando el dueño escribe ese código y el
agente llama a `confirmar_limite`. Así ningún malentendido del modelo, y
ninguna instrucción escondida en un mensaje, mueve un límite por su cuenta.

Y una vez cambiado, el LLM sigue sin decidir nada: app/policy.py lee los
números y decide, en Python, adentro del lock.

Cada herramienta verifica de nuevo que quien habla sea del equipo
(require_management), aunque el router ya lo haya hecho.
"""
from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app import limites
from app.formato import pesos
from app.runtime_context import RuntimeContextError, require_management

_SIN_PERMISO = (
    "Ese número no está autorizado para ver ni cambiar los límites. "
    "No cambié nada."
)


def _mostrar(fila: dict) -> str:
    valor = fila["valor"]
    if fila["unidad"] == "$":
        try:
            valor = pesos(float(valor))
        except (TypeError, ValueError):
            pass
    elif fila["unidad"] == "%":
        valor = f"{valor}%"
    elif fila["unidad"] == "sí/no":
        valor = "sí" if valor == "true" else "no"
    origen = {
        "dueño": "lo fijaste vos",
        "arranque": "valor de arranque",
        "default": "default del sistema",
    }.get(fila["origen"], fila["origen"])
    linea = f"*{fila['alias']}*: {valor}  ({origen})"
    if fila["problema"]:
        linea += f"\n   ⚠️ mal configurado: {fila['problema']}"
    return linea


@tool
def ver_limites(config: RunnableConfig) -> str:
    """Muestra los límites vigentes de auto-confirmación y de dónde salen.

    Usala cuando el dueño pregunta qué límites hay, cuánto es el tope, cuánto
    colchón de stock se guarda, o antes de proponerle un cambio.
    """
    try:
        require_management(config)
    except RuntimeContextError:
        return _SIN_PERMISO
    try:
        filas = limites.resumen()
    except limites.LimiteError as exc:
        return (
            f"No pude leer los límites ({exc}). Mientras no se puedan leer, "
            "ningún pedido se auto-confirma: todos quedan pendientes."
        )
    cuerpo = "\n".join(_mostrar(fila) for fila in filas)
    return (
        "Límites de auto-confirmación:\n"
        f"{cuerpo}\n\n"
        "Para cambiar uno, decime cuál y el valor nuevo. Te pido confirmación "
        "antes de aplicarlo."
    )


@tool
def proponer_limite(limite: str, valor: str, config: RunnableConfig) -> str:
    """Prepara un cambio de UN límite y pide confirmación. NO lo aplica.

    `limite` es como lo dijo el dueño ("tope", "colchón de stock", "cantidad
    por producto", "cliente nuevo", "deuda", "descuentos"). `valor` es el
    número o sí/no que dijo, sin interpretarlo ni convertirlo.
    Devolvé al dueño el código tal como viene: lo tiene que escribir él.
    """
    try:
        actor = require_management(config)
    except RuntimeContextError:
        return _SIN_PERMISO
    try:
        propuesta = limites.proponer(limite, valor, actor.actor_phone)
    except limites.LimiteError as exc:
        return f"No cambié nada: {exc}."
    return (
        f"Cambio preparado, todavía sin aplicar:\n"
        f"*{propuesta['alias']}*: {propuesta['anterior']} → {propuesta['nuevo']}\n\n"
        f"Si es correcto, escribí *{propuesta['codigo']}* para confirmarlo. "
        "Si no contestás, en 10 minutos se descarta solo."
    )


@tool
def confirmar_limite(codigo: str, config: RunnableConfig) -> str:
    """Aplica el cambio de límite pendiente, con el código que escribió el dueño.

    Usala SOLO cuando el dueño escribió el código de cuatro dígitos. No
    inventes ni adivines un código, y no la uses para confirmar pedidos.
    """
    try:
        actor = require_management(config)
    except RuntimeContextError:
        return _SIN_PERMISO
    try:
        cambio = limites.aplicar(codigo, actor.actor_phone)
    except limites.LimiteError as exc:
        return f"No apliqué nada: {exc}."
    alias = limites.LIMITES[cambio["limite"]].alias[0]
    return (
        f"Listo: *{alias}* pasó de {cambio['anterior']} a {cambio['nuevo']}. "
        "Rige desde el próximo pedido, sin reiniciar nada. "
        f"Queda registrado a tu nombre ({cambio['ts']})."
    )


@tool
def historial_limites(config: RunnableConfig) -> str:
    """Muestra los últimos cambios de límites: qué, cuándo y desde qué número."""
    try:
        require_management(config)
    except RuntimeContextError:
        return _SIN_PERMISO
    try:
        entradas = limites.auditoria(10)
    except limites.LimiteError as exc:
        return f"No pude leer el historial ({exc})."
    if not entradas:
        return "Todavía nadie cambió un límite; están todos en su valor inicial."
    lineas = []
    for entrada in entradas:
        nombre = str(entrada.get("limite") or "")
        defi = limites.LIMITES.get(nombre)
        lineas.append(
            f"· {entrada.get('ts', '?')} — {defi.alias[0] if defi else nombre or '?'}: "
            f"{entrada.get('anterior', '?')} → {entrada.get('nuevo', '?')} "
            f"(desde {entrada.get('telefono', '?')})"
        )
    return "Últimos cambios de límites:\n" + "\n".join(lineas)
