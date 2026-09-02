"""Two agents, two permission scopes, one runtime.

  agente_clientes  -> WhatsApp customers. Untrusted input. Narrow tools.
                      Writes drafts only.
  agente_gerencia  -> the owner and staff. Trusted. Broad READ across the
                      whole system. Still cannot submit anything.

They use DIFFERENT ERPNext API credentials, so the permission boundary is
enforced by ERPNext itself — not by which prompt happened to load.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain.chat_models import init_chat_model
from langgraph.checkpoint.redis import RedisSaver
from langgraph.prebuilt import ToolNode, create_react_agent

from app import erpnext
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
_modelo_clientes = init_chat_model(
    os.getenv("LLM_MODEL_CLIENTES", "google_genai:gemini-2.5-flash"),
    temperature=0.3,
)
_modelo_gerencia = init_chat_model(
    os.getenv("LLM_MODEL_GERENCIA", "google_genai:gemini-2.5-pro"),
    temperature=0.1,
)

# A raising tool leaves an AIMessage with no matching ToolMessage, which
# permanently breaks that conversation thread — on WhatsApp that means one
# customer can never be replied to again until someone clears Redis by hand.
# Always turn a tool failure into a normal tool result instead.
_ERROR_MSG = (
    "Hubo un error tecnico con esa herramienta. No inventes un resultado: "
    "pedile disculpas al cliente y usa escalar_a_humano."
)

agente_clientes = create_react_agent(
    model=_modelo_clientes,
    tools=ToolNode(TOOLS_CLIENTES, handle_tool_errors=_ERROR_MSG),
    checkpointer=_checkpointer,
)
agente_gerencia = create_react_agent(
    model=_modelo_gerencia,
    tools=ToolNode(TOOLS_GERENCIA, handle_tool_errors=_ERROR_MSG),
    checkpointer=_checkpointer,
)


def _texto(msg) -> str:
    """Providers differ: some return a string, Gemini returns a list of
    content blocks. Always hand WhatsApp plain text."""
    c = getattr(msg, "content", msg)
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        partes = [
            b.get("text", "")
            for b in c
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in partes if p).strip() or str(c)
    return str(c)


def _business_today() -> str:
    zone_name = os.getenv(
        "BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires"
    ).strip()
    try:
        return datetime.now(ZoneInfo(zone_name)).date().isoformat()
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RuntimeError("BUSINESS_TIMEZONE inválida") from exc


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
    code and group. Tools receive those values only through RunnableConfig.
    """
    del contexto_cliente
    minimal_context = (
        "Cliente con cuenta registrada en ERPNext."
        if customer_code
        else (
            "Remitente sin cuenta de cliente registrada. Si quiere comprar, "
            "registrá primero un contacto y derivá el alta comercial."
        )
    )
    system = SYSTEM_ES_AR.format(
        NEGOCIO=os.getenv("NOMBRE_NEGOCIO", "la empresa"),
        CONTEXTO_CLIENTE=minimal_context,
        HORARIO=os.getenv("HORARIO_ATENCION", "lunes a viernes de 8 a 17"),
        HOY=_business_today(),
    )
    with erpnext.customer_scope():
        out = agente_clientes.invoke(
            {"messages": [("system", system), ("user", mensaje)]},
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
    return _texto(out["messages"][-1])


def responder_gerencia(
    mensaje: str,
    thread_id: str,
    usuario: str,
    *,
    inbound_message_id: str = "",
) -> str:
    system = SYSTEM_GERENCIA.format(
        NEGOCIO=os.getenv("NOMBRE_NEGOCIO", "la empresa"),
        USUARIO="miembro autorizado del equipo",
        HOY=_business_today(),
    )
    with erpnext.manager_scope():
        out = agente_gerencia.invoke(
            {"messages": [("system", system), ("user", mensaje)]},
            config={
                "configurable": {
                    "thread_id": f"ger:{thread_id}",
                    "actor_scope": "management",
                    "actor_phone": usuario,
                    "inbound_message_id": inbound_message_id,
                }
            },
        )
    return _texto(out["messages"][-1])
