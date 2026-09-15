"""El techo de pasos de UN turno, y qué se contesta cuando se llega a él.

POR QUÉ EXISTE ESTE ARCHIVO
---------------------------
`app/graph.py` arma el config del turno a mano y no pone `recursion_limit`.
Parece inocente: la biblioteca tendrá un default sensato. **Lo tiene, y en
LangGraph 1.2.11 vale 10007** (`langgraph._internal._config.DEFAULT_RECURSION_LIMIT`;
el 25 que se recuerda es el de `langchain_core`, que acá no interviene). Con un
superpaso cada tres por llamada al modelo, eso son ~3335 llamadas al modelo en
UN mensaje de WhatsApp.

No es teoría. Medido por `responder_cliente`, la puerta de verdad, con un modelo
guionado que siempre pide herramienta: **121 llamadas al modelo y 120 a
herramientas**, y paró porque se acabó el guion, no porque algo lo frenara. En
producción del otro lado hay un proveedor que cobra y que tarda —con la clave de
Gemini en free tier, entre 7 y 34 segundos por llamada, que es el problema #1 de
`CLAUDE.md`—, así que un modelo que entra en bucle se come la cuota y los
minutos mientras el cliente ve un «estoy consultando» y Meta reintenta el
mensaje, que es otro worker haciendo lo mismo de nuevo.

Un agente sin techo de pasos no es un agente con más capacidad: es uno al que
nadie le puso presupuesto.

LAS DOS SALIDAS DE LANGGRAPH, Y POR QUÉ HAY QUE ATAJAR LAS DOS
--------------------------------------------------------------
Medido, límite por límite, contra el grafo real de este repo:

    recursion_limit = 3n  -> n llamadas al modelo y un AIMessage cuyo texto es
                             "Sorry, need more steps to process this request."
    cualquier otro    -> n = floor(limite/3) llamadas y `GraphRecursionError`

La primera es la peor de las dos y es la que parece la buena: no levanta, no se
loguea, y esa frase en inglés sale tal cual por WhatsApp a un almacenero de
Buenos Aires. `_non_empty` no la ataja porque no está vacía. Así que acá se
atajan las dos, y el centinela se compara por texto: es una constante de la
biblioteca, y si una versión se lo cambia, `tests/test_pasos.py` lo dice.

(El `remaining_steps` de `create_react_agent` —el que produce el centinela— sólo
cubre el caso que cae justo en su ventana `< 2`. Con `pre_model_hook` una vuelta
gasta TRES superpasos, así que el contador salta por arriba de la ventana y sale
por la excepción. Por eso el número no se elige para caer en la salida linda: se
atajan las dos y listo.)

QUÉ SE CONTESTA CUANDO SE ACABA EL PRESUPUESTO
----------------------------------------------
No una disculpa. Lo que ya se averiguó.

Sin esto, `_generate_response` atrapa la excepción y manda «tuve un problema
técnico»: las diez herramientas que el turno ya corrió —el stock leído, el
precio buscado, la ficha del cliente— se tiran. Acá se hace una llamada MÁS al
modelo, UNA, **sin herramientas**, con el prompt de siempre y con lo que
devolvieron esas herramientas adelante, para que conteste con eso. Es lo que
hace cualquier harness decente cuando se queda sin presupuesto, y es la
diferencia entre un agente que contesta y uno que se disculpa.

Esa llamada no puede entrar en bucle porque no tiene herramientas que pedir. Y
si ELLA falla, se levanta: la disculpa de `_generate_response` sigue estando
abajo de todo, intacta.

LA TRAMPA DEL CIERRE
--------------------
El hilo quedó cortado justo después de que el modelo pidiera herramientas, así
que la cola tiene un `AIMessage` con `tool_calls` sin su `ToolMessage`. Mandarle
eso a un proveedor compatible con OpenAI es un 400, y sería un 400 en el peor
momento posible. Por eso el cierre **no replica el hilo**: se queda con la
conversación (lo que dijo la persona y lo que el agente ya le contestó en texto)
y mete lo que devolvieron las herramientas como UN dato, en un mensaje aparte.
Sin `tool_calls` en el payload no hay a qué quedarle colgando nada.

EL NÚMERO
---------
`PASOS_MAX_CLIENTES` y `PASOS_MAX_GERENCIA`, en LLAMADAS AL MODELO, que es lo
que se paga y lo que cuenta la línea de latencia (`modelo=8x1.2s`). Son dos y no
uno porque los dos agentes no se parecen: el de clientes tiene 12 herramientas y
un pedido bien atendido son tres o cuatro vueltas; el de gerencia puede tener
125 más de un servidor MCP externo y explora.

NO van por `app/limites.py`. Un límite de ahí lo cambia el dueño por WhatsApp
con su código de cuatro dígitos, y esto no es una regla del negocio: es cuánto
aguanta el proceso. Mezclarlo le agregaría una fila a `ver_ajustes` que él no
sabría en qué unidad está.
"""
from __future__ import annotations

import os

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError

from app.conversacion import recortar_historial, texto_plano

# El texto exacto que devuelve `create_react_agent` cuando se queda sin pasos
# por su propio camino (langgraph/prebuilt/chat_agent_executor.py). Es una
# constante de la biblioteca y viaja a WhatsApp si nadie la mira.
CENTINELA_SIN_PASOS = "Sorry, need more steps to process this request."

# Cuántos superpasos gasta UNA vuelta (hook + modelo + herramientas). Medido,
# no leído: con `pre_model_hook` puesto son tres. `tests/test_pasos.py` lo
# vuelve a medir contra el grafo real, así que una versión de LangGraph que lo
# cambie rompe un test y no la conversación de un cliente.
SUPERPASOS_POR_VUELTA = 3

# Cuántos resultados de herramienta se le muestran al cierre, y cuánto de cada
# uno. El cierre es UNA llamada y no puede convertirse en el turno entero de
# nuevo: un informe de gerencia devuelve páginas, y pegarlas todas sería pagar
# el contexto que el presupuesto acaba de negar.
_MAX_HALLAZGOS = 12
_MAX_CARACTERES_POR_HALLAZGO = 700

_INSTRUCCION_DE_CIERRE = (
    "[NOTA DEL SISTEMA — no es un mensaje de nadie y no se muestra]\n"
    "Se acabaron los pasos de este turno y ya no podés llamar herramientas. "
    "Contestá AHORA, en UN mensaje, con lo que ya averiguaste, que está más "
    "abajo. Reglas: no digas que te quedaste sin pasos, no hables de "
    "herramientas, de sistemas ni de errores, y no inventes nada que no esté "
    "en esos datos. Si lo que averiguaste no alcanza para contestar, decilo en "
    "una línea y ofrecé lo concreto que sí podés hacer o pasalo a una persona. "
    "Seguís hablando el idioma que te indican las reglas de arriba."
)

_SIN_HALLAZGOS = "(no llegaste a averiguar nada todavía)"


def _entero_positivo(nombre: str, por_defecto: str) -> int:
    """Lee un techo del entorno. Revienta al importar si no es un entero > 0.

    Un techo que cae en 0 porque alguien escribió «ocho» no es un techo: sería
    un turno de cero llamadas al modelo, o sea un agente mudo. Se falla acá,
    al arrancar el proceso, y no en el primer WhatsApp del día.
    """
    crudo = (os.getenv(nombre) or por_defecto).strip() or por_defecto
    try:
        valor = int(crudo)
    except ValueError as exc:
        raise RuntimeError(f"{nombre} debe ser un entero positivo") from exc
    if valor <= 0:
        raise RuntimeError(f"{nombre} debe ser un entero positivo")
    return valor


# Los dos techos, en llamadas al modelo.
PASOS_CLIENTES = _entero_positivo("PASOS_MAX_CLIENTES", "8")
PASOS_GERENCIA = _entero_positivo("PASOS_MAX_GERENCIA", "14")


def techo(rol: str) -> int:
    """Cuántas llamadas al modelo puede hacer un turno de este rol."""
    return PASOS_GERENCIA if rol == "gerencia" else PASOS_CLIENTES


def limite_de_recursion(llamadas: int) -> int:
    """Las llamadas al modelo que quiero, en los superpasos que pide LangGraph.

    `3n + 1` y no `3n` a propósito: los dos dan n llamadas, pero `3n` sale por
    el centinela en inglés y `3n + 1` por la excepción, que es la salida que se
    ve en el log. Las dos están atajadas igual — esto elige cuál es la normal.
    """
    return SUPERPASOS_POR_VUELTA * llamadas + 1


def _hallazgos(mensajes: list) -> str:
    """Lo que devolvieron las herramientas de este turno, como texto.

    De los ÚLTIMOS para atrás: si el modelo dio doce vueltas, lo que estaba
    averiguando al final es lo que contesta la pregunta. Se devuelve en el
    orden en que pasaron, que es como se lee.
    """
    tomados: list[str] = []
    for msg in reversed(mensajes):
        if not isinstance(msg, ToolMessage):
            continue
        cuerpo = texto_plano(msg).strip()
        if not cuerpo:
            continue
        if len(cuerpo) > _MAX_CARACTERES_POR_HALLAZGO:
            cuerpo = cuerpo[:_MAX_CARACTERES_POR_HALLAZGO] + "…"
        nombre = str(getattr(msg, "name", "") or "consulta")
        tomados.append(f"- {nombre}: {cuerpo}")
        if len(tomados) >= _MAX_HALLAZGOS:
            break
    if not tomados:
        return _SIN_HALLAZGOS
    return "\n".join(reversed(tomados))


def _conversacion(mensajes: list) -> list:
    """El hilo SIN nada que tenga `tool_calls` colgando y sin ToolMessages.

    El turno se cortó justo después de que el modelo pidiera herramientas, así
    que la cola tiene un `AIMessage` con `tool_calls` cuyo `ToolMessage` nunca
    llegó. Un proveedor compatible con OpenAI contesta 400 a eso. Acá no se
    intenta emparejar nada: se saca TODO lo que sea una llamada a herramienta o
    su respuesta, y queda la conversación, que es lo que el cierre necesita.
    """
    limpio = []
    for msg in mensajes:
        if isinstance(msg, ToolMessage):
            continue
        if isinstance(msg, AIMessage):
            if getattr(msg, "tool_calls", None):
                continue
            if not texto_plano(msg).strip():
                continue
            if texto_plano(msg).strip() == CENTINELA_SIN_PASOS:
                # El centinela de la biblioteca NO es algo que el agente dijo.
                continue
        limpio.append(msg)
    # Y ACOTADO, con la MISMA definición de cuánta historia ve el modelo que usa
    # el `pre_model_hook` del grafo (`CONVERSATION_MAX_MESSAGES`). El cierre se
    # saltea ese hook, así que sin esto le mandaría el hilo ENTERO —hasta 30
    # días de conversación— justo en la llamada que existe para acotar el gasto.
    # Se recorta DESPUÉS de limpiar: recortar primero gastaría el presupuesto de
    # mensajes en ToolMessages que acá no viajan, y dejaría al cierre sin la
    # conversación, que es lo único que viene a buscar.
    return recortar_historial({"messages": limpio})["llm_input_messages"] if limpio else []


def cerrar(mensajes: list, *, modelo, armar_prompt, config: dict) -> str:
    """UNA llamada al modelo, sin herramientas, que contesta con lo que hay.

    `armar_prompt` es `prompt_clientes` o `prompt_gerencia`: el mismo de
    siempre, así que el cierre habla con la misma identidad, las mismas reglas
    y el mismo idioma que el resto del turno. No hay un segundo prompt que
    mantener.

    Levanta si el modelo no contesta o contesta vacío. Eso es deliberado: la
    disculpa de `_generate_response` es el piso, y taparla con un texto armado
    acá sería inventarle al cliente una respuesta que nadie produjo.
    """
    cuerpo = _conversacion(mensajes)
    nota = f"{_INSTRUCCION_DE_CIERRE}\n\nLO QUE AVERIGUASTE:\n{_hallazgos(mensajes)}"
    entrada = armar_prompt({"messages": [*cuerpo, HumanMessage(content=nota)]}, config)
    salida = modelo.invoke(entrada, config=config)
    texto = texto_plano(salida).strip()
    if not texto:
        raise RuntimeError("el cierre del turno no devolvió texto")
    return texto


def correr(
    agente,
    mensaje: str,
    *,
    config: dict,
    modelo,
    armar_prompt,
    rol: str,
) -> str:
    """Un turno con techo. Devuelve el texto que se le manda a la persona.

    El techo se pone acá y no en `graph._config` porque el que lo pone es el
    mismo que sabe qué hacer cuando se llega: un `recursion_limit` suelto en un
    config convierte un bucle en una excepción, y una excepción en una disculpa.
    """
    llamadas = techo(rol)
    con_techo = {**config, "recursion_limit": limite_de_recursion(llamadas)}
    try:
        salida = agente.invoke({"messages": [("user", mensaje)]}, config=con_techo)
    except GraphRecursionError:
        print(f"[agent] techo de pasos rol={rol} llamadas={llamadas} camino=excepcion")
        return _cerrar_y_recordar(
            agente, _mensajes_del_hilo(agente, con_techo),
            modelo=modelo, armar_prompt=armar_prompt, config=config,
        )
    mensajes = list(salida.get("messages") or [])
    ultimo = mensajes[-1] if mensajes else None
    if ultimo is not None and texto_plano(ultimo).strip() == CENTINELA_SIN_PASOS:
        print(f"[agent] techo de pasos rol={rol} llamadas={llamadas} camino=centinela")
        return _cerrar_y_recordar(
            agente, mensajes,
            # PISA el centinela en vez de escribir debajo. Esa frase en inglés
            # la puso LangGraph y el grafo ya la guardó en el hilo: sin esto se
            # queda ahí los 30 días del checkpointer, y el modelo la lee en cada
            # turno siguiente como algo que dijo ÉL. Medido: el turno siguiente
            # veía «Sorry, need more steps to process this request.» entre sus
            # propios mensajes. `add_messages` reemplaza cuando el id coincide.
            reemplazar_id=getattr(ultimo, "id", None),
            modelo=modelo, armar_prompt=armar_prompt, config=config,
        )
    return texto_plano(ultimo) if ultimo is not None else ""


def _cerrar_y_recordar(agente, mensajes: list, *, reemplazar_id=None, **kw) -> str:
    """El cierre, y además queda ANOTADO en el hilo como algo que el agente dijo.

    Sin esto el cliente lee «tengo leche a $1000, ¿te mando 10?» y contesta «sí
    dale», y el turno siguiente arranca sin la oferta: el modelo ve un «sí dale»
    suelto y vuelve a preguntar todo. La respuesta salió por el camino de
    excepción, así que no la escribió nadie — el grafo sólo guarda lo que pasó
    por sus nodos.

    `reemplazar_id` es el id del mensaje que hay que PISAR, y sólo lo usa el
    camino del centinela: ahí el grafo SÍ escribió algo —su frase en inglés— y
    anotar debajo dejaría las dos. Sin id, se agrega.

    Si la escritura falla, el cliente igual recibe la respuesta: el hilo
    incompleto es un turno peor, y no contestar es el negocio parado.
    """
    texto = cerrar(mensajes, **kw)
    anotado = (
        AIMessage(content=texto, id=reemplazar_id)
        if reemplazar_id
        else AIMessage(content=texto)
    )
    try:
        agente.update_state(kw["config"], {"messages": [anotado]})
    except Exception as exc:  # la respuesta ya es del cliente
        print(f"[agent] no pude anotar el cierre en el hilo type={type(exc).__name__}")
    return texto


def _mensajes_del_hilo(agente, config: dict) -> list:
    """El hilo como quedó en el checkpoint, que es donde está lo averiguado.

    `GraphRecursionError` sale sin devolver estado, pero LangGraph ya escribió
    cada superpaso: el trabajo del turno está guardado aunque la llamada haya
    terminado en excepción. Si tampoco se puede leer, se cierra con lo que hay
    —nada— y el modelo lo dice en una línea, que sigue siendo mejor que una
    disculpa técnica.
    """
    try:
        estado = agente.get_state(config)
    except Exception as exc:
        print(f"[agent] no pude leer el hilo para cerrar type={type(exc).__name__}")
        return []
    valores = getattr(estado, "values", None) or {}
    return list(valores.get("messages") or [])
