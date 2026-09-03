"""Las herramientas con las que el DUEÑO cambia sus ajustes, por WhatsApp.

Dos familias, un mismo camino: los límites de auto-confirmación
(app/policy.py) y las reglas de entrega (app/excepciones.py). Las dos se
proponen y se confirman igual, se auditan igual, y ninguna la aplica el modelo.

LO QUE NO SE TOCA POR ACÁ
La cuenta contable del cargo de envío (ENTREGA_CARGO_CUENTA). Es un account
head real de ERPNext: un nombre equivocado no rompe el bot, le desbalancea la
contabilidad al dueño, y un modelo interpretando "poneme la cuenta de fletes"
no puede verificar que exista. No está en ningún registro, así que ninguna
herramienta puede escribirla; se configura en el servidor. Sin ella, un cargo
simplemente no se escribe en el pedido y lo agrega una persona.

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

from app import limites, policy
from app.formato import pesos
from app.runtime_context import RuntimeContextError, require_management

_SIN_PERMISO = (
    "Ese número no está autorizado para ver ni cambiar los límites. "
    "No cambié nada."
)


def _mostrar(fila: dict) -> str:
    valor = fila["valor"]
    if valor == limites.NINGUNO:
        valor = "sin configurar"
    elif fila["unidad"] == "$":
        try:
            valor = pesos(float(valor))
        except (TypeError, ValueError):
            pass
    elif fila["unidad"] == "%":
        valor = f"{valor}%"
    elif fila["unidad"] == "sí/no":
        valor = "sí" if valor == "true" else "no"
    elif fila["unidad"] == "días":
        valor = valor.replace(",", ", ")
    origen = {
        "dueño": "lo fijaste vos",
        "arranque": "valor de arranque",
        "default": "default del sistema",
    }.get(fila["origen"], fila["origen"])
    linea = f"*{fila['alias']}*: {valor}  ({origen})"
    if fila["problema"]:
        linea += f"\n   ⚠️ mal configurado: {fila['problema']}"
    # He should know the ceiling is not the only thing standing between a new
    # customer and an automatic order: the address has to check out too.
    if fila["nombre"] == "AUTO_CONFIRM_MAX_CLIENTE_NUEVO":
        if not policy.CLIENTE_NUEVO_HABILITADO:
            linea += (
                "\n   ℹ️ todavía sin efecto: hasta que el sistema verifique la "
                "dirección y la zona de entrega, un cliente nuevo siempre "
                "espera a una persona"
            )
        elif fila["valor"] not in ("", "0"):
            linea += (
                "\n   ℹ️ sólo cuando la dirección del pedido cae en una zona de "
                "reparto configurada (o ya se le entregó ahí antes); si no, el "
                "pedido queda en borrador igual"
            )
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
        filas = [f for f in limites.resumen() if f["nombre"] in limites.LIMITES]
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
        "antes de aplicarlo. Las reglas de entrega se ven con «reglas de "
        "entrega»."
    )


@tool
def proponer_limite(limite: str, valor: str, config: RunnableConfig) -> str:
    """Prepara un cambio de UN ajuste y pide confirmación. NO lo aplica.

    Sirve para los límites de auto-confirmación ("tope", "colchón de stock",
    "cantidad por producto", "cliente nuevo", "deuda", "descuentos", "plazo de
    revisión") y también para las reglas de entrega ("días de reparto", "hora
    de reparto", "entregas fuera de día", "días fuera de día", "hora fuera de
    día", "cargo fuera de día", "mínimo fuera de día", "retiro en el local",
    "días de retiro", "hora de retiro").

    Pasá `limite` y `valor` TAL COMO los dijo el dueño, sin interpretarlos ni
    convertirlos: los días, las horas, los sí/no y la plata los valida Python.
    Si el nombre es ambiguo te lo va a decir con las opciones — preguntale cuál
    en vez de elegir vos. Un cambio por vez.
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
    defi = limites.TODOS.get(cambio["limite"])
    alias = defi.alias[0] if defi else cambio["limite"]
    return (
        f"Listo: *{alias}* pasó de {cambio['anterior']} a {cambio['nuevo']}. "
        "Rige desde el próximo pedido, sin reiniciar nada. "
        f"Queda registrado a tu nombre ({cambio['ts']})."
    )


@tool
def ver_reglas_de_entrega(config: RunnableConfig) -> str:
    """Muestra las reglas de entrega vigentes: reparto, excepciones y retiro.

    Usala cuando el dueño pregunta qué días se reparte, si se entrega fuera de
    día, cuánto se cobra por eso, o si se puede retirar por el local — y antes
    de proponerle un cambio.
    """
    try:
        require_management(config)
    except RuntimeContextError:
        return _SIN_PERMISO
    try:
        filas = [f for f in limites.resumen() if f["nombre"] in limites.ENTREGA]
    except limites.LimiteError as exc:
        return (
            f"No pude leer las reglas de entrega ({exc}). Mientras no se puedan "
            "leer, no se ofrece ninguna entrega fuera de día ni retiro."
        )
    cuerpo = "\n".join(_mostrar(fila) for fila in filas)
    cuenta = limites.cuenta_cargo()
    nota = (
        f"\nCuenta contable del cargo: {cuenta} (se configura en el servidor)."
        if cuenta
        else "\n⚠️ Sin cuenta contable configurada: un cargo de envío no se "
        "escribe en el pedido y queda para que lo agregue una persona."
    )
    return (
        "Reglas de entrega:\n"
        f"{cuerpo}\n{nota}\n\n"
        "Para cambiar una, decime cuál y el valor nuevo. Te pido confirmación "
        "antes de aplicarla."
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
        defi = limites.TODOS.get(nombre)
        lineas.append(
            f"· {entrada.get('ts', '?')} — {defi.alias[0] if defi else nombre or '?'}: "
            f"{entrada.get('anterior', '?')} → {entrada.get('nuevo', '?')} "
            f"(desde {entrada.get('telefono', '?')})"
        )
    return "Últimos cambios de límites:\n" + "\n".join(lineas)
