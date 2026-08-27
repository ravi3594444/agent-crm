"""Two agents, two permission scopes, one runtime.

  agente_clientes  -> WhatsApp customers. Untrusted input. 6 narrow tools.
                      Writes drafts only.
  agente_gerencia  -> the owner and staff. Trusted. Broad READ across the
                      whole system. Still cannot submit anything.

They use DIFFERENT ERPNext API credentials, so the permission boundary is
enforced by ERPNext itself — not by which prompt happened to load.
"""
import os
from datetime import date

from langchain.chat_models import init_chat_model
from langgraph.checkpoint.redis import RedisSaver
from langgraph.prebuilt import create_react_agent

from app.prompts import SYSTEM_ES_AR
from app.prompts_gerencia import SYSTEM_GERENCIA
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
from app.tools.captura import (
    confirmar_entrega,
    contar_stock,
    redactar_mensaje_cliente,
    registrar_venta_offline,
)
from app.tools.pedidos import crear_lead, crear_pedido, escalar_a_humano

TOOLS_CLIENTES = [
    buscar_producto, consultar_stock, estado_pedido, pedido_habitual,
    crear_lead, crear_pedido, escalar_a_humano,
]

TOOLS_GERENCIA = [
    pedidos_pendientes, ventas_del_periodo, stock_bajo,
    cobranzas_vencidas, ficha_cliente, ejecutar_reporte,
    buscar_producto, consultar_stock, estado_pedido,
    escalar_a_humano,
    # offline capture — how reality gets back into the system
    registrar_venta_offline, contar_stock, confirmar_entrega,
    redactar_mensaje_cliente,
]

_checkpointer = RedisSaver.from_conn_string(os.environ["REDIS_URL"])

# Cheap+fast for the high-volume customer bot; stronger model for analysis.
_modelo_clientes = init_chat_model(
    os.getenv("LLM_MODEL_CLIENTES", "anthropic:claude-haiku-4-5"), temperature=0.3
)
_modelo_gerencia = init_chat_model(
    os.getenv("LLM_MODEL_GERENCIA", "anthropic:claude-sonnet-4-5"), temperature=0.1
)

agente_clientes = create_react_agent(
    model=_modelo_clientes, tools=TOOLS_CLIENTES, checkpointer=_checkpointer
)
agente_gerencia = create_react_agent(
    model=_modelo_gerencia, tools=TOOLS_GERENCIA, checkpointer=_checkpointer
)


def responder_cliente(mensaje: str, thread_id: str, contexto_cliente: str) -> str:
    system = SYSTEM_ES_AR.format(
        NEGOCIO=os.getenv("NOMBRE_NEGOCIO", "la empresa"),
        CONTEXTO_CLIENTE=contexto_cliente,
        HORARIO=os.getenv("HORARIO_ATENCION", "lunes a viernes de 8 a 17"),
    )
    out = agente_clientes.invoke(
        {"messages": [("system", system), ("user", mensaje)]},
        config={"configurable": {"thread_id": f"cli:{thread_id}"}},
    )
    return out["messages"][-1].content


def responder_gerencia(mensaje: str, thread_id: str, usuario: str) -> str:
    system = SYSTEM_GERENCIA.format(
        NEGOCIO=os.getenv("NOMBRE_NEGOCIO", "la empresa"),
        USUARIO=usuario,
        HOY=date.today().isoformat(),
    )
    out = agente_gerencia.invoke(
        {"messages": [("system", system), ("user", mensaje)]},
        config={"configurable": {"thread_id": f"ger:{thread_id}"}},
    )
    return out["messages"][-1].content
