"""Two agents, two permission scopes, one runtime.

  agente_clientes  -> WhatsApp customers. Untrusted input. Narrow tools.
                      Writes drafts only.
  agente_gerencia  -> the owner and staff. Trusted. Broad READ across the
                      whole system. Still cannot submit anything.

They use DIFFERENT ERPNext API credentials, so the permission boundary is
enforced by ERPNext itself — not by which prompt happened to load.
"""
import os

from langgraph.checkpoint.redis import RedisSaver
from langgraph.prebuilt import ToolNode, create_react_agent

from app import erpnext, modelos
from app.conversacion import (
    business_today,
    prompt_clientes,
    prompt_gerencia,
    recortar_historial,
    texto_plano,
)
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
from app.tools.configuracion import (
    confirmar_limite,
    historial_limites,
    proponer_limite,
    ver_limites,
)
from app.tools.gerencia import (
    cobranzas_vencidas,
    ejecutar_reporte,
    ficha_cliente,
    pedidos_pendientes,
    stock_bajo,
    ventas_del_periodo,
)
from app.tools.pedidos import (
    crear_cliente,
    crear_lead,
    crear_pedido,
    escalar_a_humano,
    pedir_excepcion_de_entrega,
)

TOOLS_CLIENTES = [
    buscar_producto, consultar_stock, estado_pedido, pedido_habitual,
    # crear_cliente da de alta al REMITENTE con el teléfono del webhook: no
    # acepta un teléfono como argumento, así que ningún mensaje puede pedir
    # el alta de otra persona.
    crear_cliente, crear_lead, crear_pedido, escalar_a_humano,
    # Pide una excepción de entrega. NO decide: o el dueño la dejó autorizada
    # de antemano, o abre una solicitud para una persona (app/solicitudes.py).
    pedir_excepcion_de_entrega,
]

TOOLS_GERENCIA = [
    pedidos_pendientes, ventas_del_periodo, stock_bajo,
    cobranzas_vencidas, ficha_cliente, ejecutar_reporte,
    buscar_producto, consultar_stock, estado_pedido,
    escalar_a_humano,
    # offline capture — how reality gets back into the system
    registrar_venta_offline, contar_stock, confirmar_entrega,
    redactar_mensaje_cliente,
    # the owner's own limits: read them out, propose a change, confirm it.
    # NEVER in TOOLS_CLIENTES — a customer cannot be allowed near these.
    ver_limites, proponer_limite, confirmar_limite, historial_limites,
]

# from_conn_string() is a CONTEXT MANAGER, not a constructor — using it
# directly hands you a generator, not a saver. Construct directly instead,
# and setup() is mandatory: it creates the Redis indices.
# NOTE: needs both RedisJSON and RediSearch, not a plain Redis server.
try:
    _conversation_ttl_days = int(os.getenv("CONVERSATION_TTL_DAYS", "30"))
except ValueError as exc:
    raise RuntimeError("CONVERSATION_TTL_DAYS debe ser un entero positivo") from exc
if _conversation_ttl_days <= 0:
    raise RuntimeError("CONVERSATION_TTL_DAYS debe ser un entero positivo")
_checkpointer = RedisSaver(
    redis_url=os.environ["REDIS_URL"],
    ttl={
        "default_ttl": _conversation_ttl_days * 24 * 60,
        "refresh_on_read": True,
    },
)
_checkpointer.setup()

# Cheap+fast for the high-volume customer bot; stronger model for analysis.
# Both are Qwen on one DashScope key (app/modelos.py). Missing configuration
# raises here, at import: there is deliberately no fallback provider.
_modelo_clientes = modelos.construir("clientes")
_modelo_gerencia = modelos.construir("gerencia")

# A raising tool leaves an AIMessage with no matching ToolMessage, which
# permanently breaks that conversation thread — on WhatsApp that means one
# customer can never be replied to again until someone clears Redis by hand.
# Always turn a tool failure into a normal tool result instead.
_ERROR_MSG = (
    "Hubo un error tecnico con esa herramienta. No inventes un resultado: "
    "pedile disculpas al cliente y usa escalar_a_humano."
)

# The system prompt is built per call (prompt=) and never stored in the
# checkpoint; the model only sees a bounded tail of the thread
# (pre_model_hook=). See app/conversacion.py for why.
agente_clientes = create_react_agent(
    model=_modelo_clientes,
    tools=ToolNode(TOOLS_CLIENTES, handle_tool_errors=_ERROR_MSG),
    prompt=prompt_clientes,
    pre_model_hook=recortar_historial,
    checkpointer=_checkpointer,
)
agente_gerencia = create_react_agent(
    model=_modelo_gerencia,
    tools=ToolNode(TOOLS_GERENCIA, handle_tool_errors=_ERROR_MSG),
    prompt=prompt_gerencia,
    pre_model_hook=recortar_historial,
    checkpointer=_checkpointer,
)


# Kept as aliases for callers/tests that import them from here.
_texto = texto_plano
_business_today = business_today


def responder_cliente(
    mensaje: str,
    thread_id: str,
    contexto_cliente: str = "",
    *,
    customer_code: str = "",
    inbound_message_id: str = "",
    actor_phone: str = "",
) -> str:
    """Run one customer turn with server-authenticated values hidden from the LLM.

    ``contexto_cliente`` remains accepted while callers migrate, but is
    deliberately not interpolated: it previously contained phone, ERP customer
    code and group. Tools receive those values only through RunnableConfig,
    and the system prompt reads ``customer_code`` from the same config.
    """
    del contexto_cliente
    with erpnext.customer_scope():
        out = agente_clientes.invoke(
            {"messages": [("user", mensaje)]},
            config={
                "configurable": {
                    "thread_id": f"cli:{thread_id}",
                    "actor_scope": "customer",
                    "customer_code": customer_code,
                    "actor_phone": actor_phone,
                    "inbound_message_id": inbound_message_id,
                }
            },
        )
    return texto_plano(out["messages"][-1])


def responder_gerencia(
    mensaje: str,
    thread_id: str,
    usuario: str,
    *,
    inbound_message_id: str = "",
) -> str:
    with erpnext.manager_scope():
        out = agente_gerencia.invoke(
            {"messages": [("user", mensaje)]},
            config={
                "configurable": {
                    "thread_id": f"ger:{thread_id}",
                    "actor_scope": "management",
                    "actor_phone": usuario,
                    "inbound_message_id": inbound_message_id,
                }
            },
        )
    return texto_plano(out["messages"][-1])
