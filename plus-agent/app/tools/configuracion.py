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
50 mil») y llama a `proponer_limite`. Nada cambia: Python valida el número y
guarda el cambio como PENDIENTE.

El código de cuatro dígitos NO vuelve por acá. Python se lo manda al dueño
directamente a su número (app/notificar.py::pedir_codigo_de_ajuste), y el
agente no lo ve nunca. Un código devuelto en el resultado de una herramienta es
un código que el modelo leyó, y un modelo que lo leyó puede confirmar el cambio
él mismo en el mismo turno: los dos pasos dejan de ser dos. Por eso tampoco
existe una herramienta para confirmar — el que aplica el cambio es el router
determinista de app/main.py cuando el dueño escribe esos cuatro dígitos desde
un número del equipo, en un webhook firmado, antes de que ningún modelo lea el
mensaje.

Así ningún malentendido del modelo, y ninguna instrucción escondida en un
mensaje, mueve un límite por su cuenta.

Y una vez cambiado, el LLM sigue sin decidir nada: app/policy.py lee los
números y decide, en Python, adentro del lock.

Cada herramienta verifica de nuevo que quien habla sea del equipo
(require_management), aunque el router ya lo haya hecho.
"""
from __future__ import annotations

from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field

from app import ajustes, idioma, limites, policy
from app.formato import pesos
from app.runtime_context import RuntimeContextError, require_management


def _sin_permiso() -> str:
    """La negativa de este módulo, en el idioma que fijó el dueño.

    Era una constante de módulo, y una constante se evalúa al importar —o sea
    antes de que haya un idioma que consultar—. Como función se resuelve cuando
    se contesta, que es cuando se sabe a quién (mismo arreglo que en
    app/tools/operaciones.py).
    """
    return idioma.t("ajustes.sin_permiso", idioma.gerencia())


def _mostrar(fila: dict, lengua: str) -> str:
    valor = fila["valor"]
    if fila["origen"] == limites.PERDIDO:
        # Not "sin configurar": he DID configure it, and the store lost it.
        # Nothing is in effect — not the .env either — see limites.resumen().
        valor = idioma.t("ajustes.sin_valor_vigente", lengua)
    elif valor == limites.NINGUNO:
        valor = idioma.t("ajustes.sin_configurar", lengua)
    elif fila["unidad"] == "$":
        try:
            valor = pesos(float(valor))
        except (TypeError, ValueError):
            pass
    elif fila["unidad"] == "%":
        valor = f"{valor}%"
    elif fila["unidad"] == "sí/no":
        valor = idioma.t("ajustes.si" if valor == "true" else "ajustes.no", lengua)
    elif fila["unidad"] == "días":
        # Los días se guardan y se muestran como están: son el valor que
        # parsea app/excepciones.py, no prosa de esta herramienta.
        valor = valor.replace(",", ", ")
    clave_origen = {
        "dueño": "ajustes.origen_dueno",
        "arranque": "ajustes.origen_arranque",
        "default": "ajustes.origen_default",
        limites.PERDIDO: "ajustes.origen_perdido",
    }.get(fila["origen"])
    origen = idioma.t(clave_origen, lengua) if clave_origen else fila["origen"]
    linea = f"*{fila['alias']}*: {valor}  ({origen})"
    # A lost row carries the same problem as every other lost row; the tool
    # says it once, at the end, instead of ten times here.
    if fila["problema"] and fila["origen"] != limites.PERDIDO:
        # El sangrado va ACÁ y no en el catálogo: idioma.t() hace .strip(),
        # así que un texto que empieza con espacios los pierde.
        linea += "\n   " + idioma.t(
            "ajustes.mal_configurado", lengua, problema=fila["problema"]
        )
    # He should know the ceiling is not the only thing standing between a new
    # customer and an automatic order: the address has to check out too.
    if fila["nombre"] == "AUTO_CONFIRM_MAX_CLIENTE_NUEVO":
        if not policy.CLIENTE_NUEVO_HABILITADO:
            linea += "\n   " + idioma.t("ajustes.cliente_nuevo_sin_efecto", lengua)
        elif fila["valor"] not in ("", "0"):
            linea += "\n   " + idioma.t("ajustes.cliente_nuevo_zona", lengua)
    return linea


# TRES LECTURAS DE LOS AJUSTES DEL DUEÑO, UNA HERRAMIENTA
# -------------------------------------------------------
# Misma regla que se escribió hoy arriba de `informe` en app/tools/gerencia.py:
# detrás de un `Literal` van LECTURAS de un mismo tema, nunca una escritura.
# «Qué tengo configurado» es un solo tema y tenía tres puertas —los límites de
# plata, las reglas de entrega y el historial— con la misma guarda y la misma
# negativa. Lo que degrada la elección es el solapamiento: presentar cinco
# candidatos donde alcanzan dos cuesta ~6 puntos de acierto (arXiv:2605.24660),
# y «¿qué tengo puesto?» daba en tres.
#
# `proponer_limite` NO entra acá y la línea es dura: propone un CAMBIO, y el
# cambio se confirma con un código de cuatro dígitos que el modelo no ve nunca.
# Una herramienta que lee y escribe detrás de un enum es una donde una rama
# equivocada escribe.


@tool
def ver_ajustes(
    config: RunnableConfig,
    que: Annotated[
        Literal["limites", "entrega", "historial"],
        Field(
            description=(
                "Qué parte mirar. "
                "limites = los topes de auto-confirmación (monto, cantidad por "
                "producto, colchón de stock, clientes nuevos, deuda, descuentos). "
                "entrega = dónde y qué días se reparte, entrega fuera de día, "
                "cargo y retiro por el local. "
                "historial = los últimos cambios, qué se cambió y desde qué número."
            )
        ),
    ] = "limites",
) -> str:
    """Lo que el dueño tiene configurado hoy. Sólo lectura: no cambia nada.

    Ejemplos de cómo se mapea lo que dice:
    - «¿cuánto es el tope?» / «¿qué límites tengo?» -> que=limites
    - «¿a Alta Gracia llego?» / «¿qué días reparto?» -> que=entrega
    - «¿qué cambié la semana pasada?» -> que=historial

    Usala también ANTES de proponer un cambio, para decirle de qué número sale.
    """
    if que == "entrega":
        return _ver_reglas_de_entrega(config)
    if que == "historial":
        return _historial_limites(config)
    return _ver_limites(config)


def _ver_limites(config: RunnableConfig) -> str:
    """Los límites vigentes de auto-confirmación y de dónde salen."""
    try:
        require_management(config)
    except RuntimeContextError:
        return _sin_permiso()
    lengua = idioma.gerencia()
    try:
        filas = [f for f in limites.resumen() if f["nombre"] in limites.LIMITES]
    except limites.LimiteError as exc:
        # El motivo también, no sólo la frase de alrededor: `limites.motivo` lo
        # resuelve por la clave del LimiteError, y en español devuelve el mismo
        # `str(exc)` de siempre.
        return idioma.t(
            "ajustes.limites_ilegibles", lengua, motivo=limites.motivo(exc, lengua)
        )
    cuerpo = "\n".join(_mostrar(fila, lengua) for fila in filas)
    return (
        idioma.t("ajustes.limites_titulo", lengua)
        + f"\n{cuerpo}\n\n"
        + idioma.t("ajustes.limites_pie", lengua)
    )


@tool
def proponer_limite(
    limite: Annotated[
        str,
        Field(description="Qué ajuste, con el nombre que usó el dueño («tope», «colchón "
                          "de stock», «localidades de reparto»). Si es ambiguo la "
                          "herramienta te da las opciones: preguntale cuál."),
    ],
    valor: Annotated[
        str,
        Field(description="El valor TAL COMO lo dijo, sin convertir ni redondear. Las "
                          "listas van completas y separadas por comas, porque reemplazan "
                          "a la anterior."),
    ],
    config: RunnableConfig,
) -> str:
    """Prepara un cambio de UN ajuste y pide confirmación. NO lo aplica.

    Sirve para los límites de auto-confirmación ("tope", "colchón de stock",
    "cantidad por producto", "cliente nuevo", "deuda", "descuentos", "plazo de
    revisión") y también para las reglas de entrega ("localidades de
    reparto", "códigos postales", "días de reparto", "hora de reparto",
    "entregas fuera de día", "días fuera de día", "hora fuera de día", "cargo
    fuera de día", "mínimo fuera de día", "retiro en el local", "días de
    retiro", "hora de retiro").

    Las localidades y los códigos postales son LISTAS separadas por comas
    ("Villa Allende, Córdoba"): pasá la lista completa tal como la dijo, porque
    reemplaza a la anterior, no se le agrega.

    Pasá `limite` y `valor` TAL COMO los dijo el dueño, sin interpretarlos ni
    convertirlos: los días, las horas, los sí/no y la plata los valida Python.
    Si el nombre es ambiguo te lo va a decir con las opciones — preguntale cuál
    en vez de elegir vos. Un cambio por vez.

    El código de confirmación se lo manda el sistema al dueño por separado. Vos
    no lo recibís y no lo podés aplicar: decile que conteste con el código que
    le llegó.
    """
    try:
        actor = require_management(config)
    except RuntimeContextError:
        return _sin_permiso()
    # UNA sola implementación, en app/ajustes.py: la comparte con el ruteo
    # determinista que atiende los comandos exactos de idioma. El estado —la
    # propuesta, su código, su vencimiento, su huella y la auditoría durable—
    # sigue siendo el de app/limites.py.
    return ajustes.preparar(limite, valor, actor.actor_phone)


def _ver_reglas_de_entrega(config: RunnableConfig) -> str:
    """Las reglas de entrega vigentes: zonas, reparto, excepciones y retiro."""
    try:
        require_management(config)
    except RuntimeContextError:
        return _sin_permiso()
    lengua = idioma.gerencia()
    try:
        filas = [f for f in limites.resumen() if f["nombre"] in limites.ENTREGA]
    except limites.LimiteError as exc:
        return idioma.t(
            "ajustes.entrega_ilegible", lengua, motivo=limites.motivo(exc, lengua)
        )
    cuerpo = "\n".join(_mostrar(fila, lengua) for fila in filas)
    # The owner has to hear this in words, because it is the one thing he needs
    # in order to repair it: the store lost his rules, the .env is NOT what the
    # system is running on, and nothing is offered until he sets them again.
    perdidas = any(fila["origen"] == limites.PERDIDO for fila in filas)
    alerta = ("\n" + idioma.t("ajustes.entrega_perdida", lengua)) if perdidas else ""
    cuenta = limites.cuenta_cargo()
    nota = "\n" + (
        idioma.t("ajustes.cuenta_cargo", lengua, cuenta=cuenta)
        if cuenta
        else idioma.t("ajustes.sin_cuenta_cargo", lengua)
    )
    return (
        idioma.t("ajustes.entrega_titulo", lengua)
        + f"\n{cuerpo}{alerta}\n{nota}\n\n"
        + idioma.t("ajustes.entrega_pie", lengua)
    )


def _historial_limites(config: RunnableConfig) -> str:
    """Los últimos cambios de ajustes: qué, cuándo y desde qué número."""
    try:
        require_management(config)
    except RuntimeContextError:
        return _sin_permiso()
    lengua = idioma.gerencia()
    try:
        entradas = limites.auditoria(10)
    except limites.LimiteError as exc:
        return idioma.t(
            "ajustes.historial_ilegible", lengua, motivo=limites.motivo(exc, lengua)
        )
    if not entradas:
        return idioma.t("ajustes.historial_vacio", lengua)
    lineas = []
    for entrada in entradas:
        nombre = str(entrada.get("limite") or "")
        defi = limites.TODOS.get(nombre)
        # El alias del ajuste, la fecha, los dos valores y el teléfono son
        # DATOS: se interpolan tal cual y se leen igual en los dos idiomas.
        lineas.append(idioma.t(
            "ajustes.linea_historial", lengua,
            ts=entrada.get("ts", "?"),
            ajuste=defi.alias[0] if defi else nombre or "?",
            anterior=entrada.get("anterior", "?"),
            nuevo=entrada.get("nuevo", "?"),
            telefono=entrada.get("telefono", "?"),
        ))
    return idioma.t("ajustes.historial_titulo", lengua) + "\n" + "\n".join(lineas)
