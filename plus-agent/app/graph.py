"""Two agents, two permission scopes, one runtime.

  agente_clientes  -> WhatsApp customers. Untrusted input. Narrow tools.
                      Writes drafts only.
  agente_gerencia  -> the owner and staff. Trusted. Broad READ across the
                      whole system. Still cannot submit anything.

They use DIFFERENT ERPNext API credentials for submit, and the customer
agent is additionally scoped to ONE customer by `config.configurable`
(ver app/tools/alcance.py), que el modelo no puede leer ni escribir.

TRES COSAS QUE ESTABAN MAL ACÁ
1. `RedisSaver.from_conn_string(...)` está decorado con @contextmanager:
   devuelve un context manager, NO un RedisSaver. Pasarlo como
   `checkpointer=` reventaba al primer mensaje. Además hay que llamar
   `setup()` una vez para crear los índices de Redis, y el `finally` del
   context manager cierra la conexión al salir — así que tampoco servía
   `with ... as saver`. Se construye directo.
2. El system prompt se mandaba dentro de `messages` en CADA invoke sobre un
   thread con checkpointer, así que se acumulaba uno nuevo por turno en el
   estado persistido: más tokens en cada mensaje, para siempre. Ahora va por
   `prompt=`, que se aplica en cada llamada al modelo sin quedar en el
   estado.
3. `.content` de un mensaje de Anthropic puede ser una lista de bloques, no
   un string. `texto[:4096]` sobre una lista devolvía una lista y Meta
   rechazaba el envío en silencio. Ahora se extrae el texto.
"""

from __future__ import annotations

import os
from datetime import date

from langchain.chat_models import init_chat_model
from langgraph.checkpoint.redis import RedisSaver
from langgraph.prebuilt import create_react_agent

from app import log
from app.prompts import SYSTEM_ES_AR
from app.prompts_gerencia import SYSTEM_GERENCIA
from app.tools.captura import (
    confirmar_entrega,
    contar_stock,
    redactar_mensaje_cliente,
    registrar_venta_offline,
)
from app.tools.catalogo import (
    buscar_producto,
    consultar_stock,
    estado_pedido,
    pedido_habitual,
)
from app.tools.gerencia import (
    cobranzas_vencidas,
    ejecutar_reporte,
    ficha_cliente,
    pedidos_pendientes,
    stock_bajo,
    ventas_del_periodo,
)
from app.tools.pedidos import crear_lead, crear_pedido, escalar_a_humano

_log = log.get("graph")

TOOLS_CLIENTES = [
    buscar_producto,
    consultar_stock,
    estado_pedido,
    pedido_habitual,
    crear_lead,
    crear_pedido,
    escalar_a_humano,
]

TOOLS_GERENCIA = [
    pedidos_pendientes,
    ventas_del_periodo,
    stock_bajo,
    cobranzas_vencidas,
    ficha_cliente,
    ejecutar_reporte,
    buscar_producto,
    consultar_stock,
    estado_pedido,
    escalar_a_humano,
    # offline capture — how reality gets back into the system
    registrar_venta_offline,
    contar_stock,
    confirmar_entrega,
    redactar_mensaje_cliente,
]

# El límite de turnos protege dos cosas: el costo (un loop de tool calls
# puede quemar tokens sin techo) y el tiempo de respuesta.
MAX_PASOS = int(os.getenv("AGENTE_MAX_PASOS", "12"))


def _construir_checkpointer() -> RedisSaver:
    """RedisSaver listo para usar, con los índices creados.

    `from_conn_string` es un @contextmanager y no sirve acá: cierra la
    conexión al salir del bloque.
    """
    saver = RedisSaver(redis_url=os.environ["REDIS_URL"])
    saver.setup()  # crea los índices de búsqueda; idempotente
    return saver


def _prompt_clientes(state, config=None):
    """El system prompt se arma en cada llamada al modelo y NO se persiste.

    El contexto del cliente viene por config, igual que el código de cliente
    que usan las herramientas, así que no puede quedar desfasado del hilo.
    """
    conf = (config or {}).get("configurable") or {}
    system = SYSTEM_ES_AR.format(
        NEGOCIO=os.getenv("NOMBRE_NEGOCIO", "la empresa"),
        CONTEXTO_CLIENTE=conf.get("contexto_cliente", ""),
        HORARIO=os.getenv("HORARIO_ATENCION", "lunes a viernes de 8 a 17"),
        HOY=date.today().isoformat(),
    )
    return [("system", system), *state["messages"]]


def _prompt_gerencia(state, config=None):
    conf = (config or {}).get("configurable") or {}
    system = SYSTEM_GERENCIA.format(
        NEGOCIO=os.getenv("NOMBRE_NEGOCIO", "la empresa"),
        USUARIO=conf.get("telefono", ""),
        HOY=date.today().isoformat(),
    )
    return [("system", system), *state["messages"]]


_agentes: dict[str, object] = {}


def _agente(cual: str):
    """Los agentes se construyen la primera vez que se usan, no al importar.

    POR QUÉ LAZY
    Construirlos en el import abría la conexión a Redis y creaba los índices
    como efecto secundario de `import app.graph`. Eso hacía dos cosas malas:
    el contenedor moría al arrancar si Redis todavía no estaba listo (en vez
    de reintentar en el primer mensaje), y ningún test podía importar nada de
    la app sin un Redis de verdad. Los tests ahora corren sin infraestructura.
    """
    if cual in _agentes:
        return _agentes[cual]

    checkpointer = _construir_checkpointer()
    if cual == "clientes":
        # Cheap+fast for the high-volume customer bot.
        agente = create_react_agent(
            model=init_chat_model(
                os.getenv("LLM_MODEL_CLIENTES", "anthropic:claude-haiku-4-5"),
                temperature=0.3,
            ),
            tools=TOOLS_CLIENTES,
            prompt=_prompt_clientes,
            checkpointer=checkpointer,
        )
    else:
        # Stronger model for analysis.
        agente = create_react_agent(
            model=init_chat_model(
                os.getenv("LLM_MODEL_GERENCIA", "anthropic:claude-sonnet-4-5"),
                temperature=0.1,
            ),
            tools=TOOLS_GERENCIA,
            prompt=_prompt_gerencia,
            checkpointer=checkpointer,
        )
    _agentes[cual] = agente
    _log.info("agente '%s' construido", cual)
    return agente


def texto_de(mensaje) -> str:
    """Saca el texto de un mensaje del modelo.

    `.content` puede ser un string o una lista de bloques
    (`[{"type":"text","text":...}, {"type":"thinking",...}]`). Mandarle la
    lista a Meta es un 400 silencioso.
    """
    contenido = getattr(mensaje, "content", mensaje)
    if isinstance(contenido, str):
        return contenido.strip()
    if isinstance(contenido, list):
        partes = []
        for bloque in contenido:
            if isinstance(bloque, str):
                partes.append(bloque)
            elif isinstance(bloque, dict) and bloque.get("type") == "text":
                partes.append(bloque.get("text") or "")
        return "\n".join(p for p in partes if p).strip()
    return str(contenido).strip()


def _invocar(cual: str, mensaje: str, thread: str, configurable: dict) -> str:
    salida = _agente(cual).invoke(
        {"messages": [("user", mensaje)]},
        config={
            "configurable": {"thread_id": thread, **configurable},
            "recursion_limit": MAX_PASOS * 2,
        },
    )
    mensajes = salida.get("messages") or []
    if not mensajes:
        _log.error("el agente no devolvió mensajes para el hilo %s", thread)
        return ""
    return texto_de(mensajes[-1])


def responder_cliente(
    mensaje: str,
    thread_id: str,
    contexto_cliente: str,
    cliente_code: str,
    telefono: str,
) -> str:
    """`cliente_code` sale del teléfono, no del modelo. Es el límite de
    autorización: las herramientas lo leen de config."""
    return _invocar(
        "clientes",
        mensaje,
        f"cli:{thread_id}",
        {
            "alcance": "cliente",
            "cliente_code": cliente_code,
            "telefono": telefono,
            "contexto_cliente": contexto_cliente,
        },
    )


def responder_gerencia(mensaje: str, thread_id: str, usuario: str) -> str:
    return _invocar(
        "gerencia",
        mensaje,
        f"ger:{thread_id}",
        {"alcance": "gerencia", "telefono": usuario},
    )
