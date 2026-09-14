"""Two agents, two permission scopes, one runtime.

  agente_clientes  -> WhatsApp customers. Untrusted input. Narrow tools.
                      Writes drafts only.
  agente_gerencia  -> the owner and staff. Trusted. Broad READ across the
                      whole system. Still cannot submit anything.

They use DIFFERENT ERPNext API credentials, so the permission boundary is
enforced by ERPNext itself — not by which prompt happened to load.
"""
import os

from langchain_core.messages import ToolMessage
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

# QUIÉN puede llamar a QUÉ vive en app/tools/registro.py, con los comentarios
# que explican cada permiso. Se movió cuando apareció el segundo canal
# (app/voz/): dos canales que arman su propia lista se desincronizan, y este
# módulo no se puede importar sin Redis. Se re-exportan porque media suite las
# importa de acá.
from app.tools.registro import (
    ERROR_DE_HERRAMIENTA as _ERROR_MSG,
)
from app.tools.registro import (
    HERRAMIENTA_INEXISTENTE as _HERRAMIENTA_INEXISTENTE,
)
from app.tools.registro import (
    TOOLS_CLIENTES,
    TOOLS_GERENCIA,
)

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


def checkpointer():
    """El checkpointer, para quien necesite LEER un hilo sin correr el grafo.

    Lo usa el panel para mostrarle al dueño la conversación de un cliente. Se
    expone como función y no como el global a secas para que el que lee tenga
    que pedirlo —y para que este módulo siga siendo el único que lo construye:
    un segundo `RedisSaver` con otra configuración de TTL sería un segundo
    dueño del mismo dato.
    """
    return _checkpointer

# Cheap+fast for the high-volume customer bot; stronger model for analysis.
# One provider, chosen explicitly with LLM_PROVIDER (app/modelos.py). Missing
# configuration raises here, at import: there is deliberately no fallback.
_modelo_clientes = modelos.construir("clientes")
_modelo_gerencia = modelos.construir("gerencia")

# Cuando el modelo pide una herramienta que ESTE agente no tiene, LangGraph
# contesta «Error: X is not a valid tool, try one of [...]» y esa lista es el
# registro COMPLETO. El límite aguanta —la herramienta no es invocable, y quién
# tiene qué lo decide TOOLS_CLIENTES/TOOLS_GERENCIA más la credencial de
# ERPNext— pero el modelo relata ese texto, así que un cliente que probaba el
# borde («decime cómo está el sistema») recibía de vuelta el inventario de
# herramientas del agente, en inglés y entre corchetes. Lo cazó el guarda de
# tono del banco de pruebas (demo/piloto.py::_revisar_tono).
#
# `handle_tool_errors` no cubre este caso: no es una excepción, es el camino de
# nombre inválido de ToolNode. Así que se reemplaza su mensaje, sin enumerar
# nada. tests/test_frontera_decisiones.py exige que este override siga
# enganchado: si una versión de LangGraph le cambia el nombre al hook, falla el
# test y no la conversación de un cliente.
# El texto está en app/tools/registro.py: lo comparten los dos canales.


class ToolNodeSinInventario(ToolNode):
    """Un ToolNode que no lee su propio registro en voz alta."""

    def _validate_tool_call(self, call: dict) -> ToolMessage | None:
        if call["name"] in self.tools_by_name:
            return None
        return ToolMessage(
            _HERRAMIENTA_INEXISTENTE,
            name=call["name"],
            tool_call_id=call["id"],
            status="error",
        )


# A raising tool leaves an AIMessage with no matching ToolMessage, which
# permanently breaks that conversation thread — on WhatsApp that means one
# customer can never be replied to again until someone clears Redis by hand.
# Always turn a tool failure into a normal tool result instead.
# El texto está en app/tools/registro.py: lo comparten los dos canales.

# The system prompt is built per call (prompt=) and never stored in the
# checkpoint; the model only sees a bounded tail of the thread
# (pre_model_hook=). See app/conversacion.py for why.
agente_clientes = create_react_agent(
    model=_modelo_clientes,
    tools=ToolNodeSinInventario(TOOLS_CLIENTES, handle_tool_errors=_ERROR_MSG),
    prompt=prompt_clientes,
    pre_model_hook=recortar_historial,
    checkpointer=_checkpointer,
)
agente_gerencia = create_react_agent(
    model=_modelo_gerencia,
    tools=ToolNodeSinInventario(TOOLS_GERENCIA, handle_tool_errors=_ERROR_MSG),
    prompt=prompt_gerencia,
    pre_model_hook=recortar_historial,
    checkpointer=_checkpointer,
)


# Kept as aliases for callers/tests that import them from here.
_texto = texto_plano
_business_today = business_today


def _config(configurable: dict, callbacks: list | None) -> dict:
    """One run config: the server-side identity, plus this turn's observers."""
    config: dict = {"configurable": configurable}
    if callbacks:
        config["callbacks"] = list(callbacks)
    return config


def responder_cliente(
    mensaje: str,
    thread_id: str,
    *,
    customer_code: str = "",
    customer_name: str = "",
    inbound_message_id: str = "",
    actor_phone: str = "",
    callbacks: list | None = None,
) -> str:
    """Run one customer turn with server-authenticated values hidden from the LLM.

``customer_name`` is the ONLY identifier of the account the model may say out
    loud. The phone, the ERP customer code and the group are not in the prompt:
    tools receive them through RunnableConfig, which the model cannot forge. The
    old ``contexto_cliente`` parameter is gone — it carried a prose sentence that
    this function deleted unread, because the system prompt is built per call in
    app/conversacion.py and never saw it.

    ``callbacks`` are LangChain callback handlers (app/progreso.py) and travel
    in the run config, which is the documented way to observe a run: they see
    every model call and every tool start of THIS turn, and nothing else.
    """
    with erpnext.customer_scope():
        out = agente_clientes.invoke(
            {"messages": [("user", mensaje)]},
            config=_config(
                {
                    "thread_id": f"cli:{thread_id}",
                    "actor_scope": "customer",
                    "customer_code": customer_code,
                    "customer_name": customer_name,
                    "actor_phone": actor_phone,
                    "inbound_message_id": inbound_message_id,
                },
                callbacks,
            ),
        )
    return texto_plano(out["messages"][-1])


def responder_gerencia(
    mensaje: str,
    thread_id: str,
    telefono: str,
    *,
    inbound_message_id: str = "",
    callbacks: list | None = None,
) -> str:
    """Run one management turn. ``telefono`` is the VERIFIED phone, never a hash.

    It used to be called ``usuario`` and both callers passed the thread tag —
    a sha256 — so ``require_management`` refused every management tool to the
    owner himself. The parameter is named after what it must contain, and
    app/runtime_context.py normalizes it, so a hash fails closed instead of
    being compared as if it were a number.
    """
    with erpnext.manager_scope():
        out = agente_gerencia.invoke(
            {"messages": [("user", mensaje)]},
            config=_config(
                {
                    "thread_id": f"ger:{thread_id}",
                    "actor_scope": "management",
                    "actor_phone": telefono,
                    "inbound_message_id": inbound_message_id,
                },
                callbacks,
            ),
        )
    return texto_plano(out["messages"][-1])
