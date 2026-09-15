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
from pydantic import ValidationError

from app import erpnext, modelos, pasos
from app.conversacion import (
    business_today,
    prompt_clientes,
    prompt_gerencia,
    recortar_historial,
    texto_plano,
)

# QUIÉN puede llamar a QUÉ vive en app/tools/registro.py, con los comentarios
# que explican cada permiso. Se movió cuando apareció el segundo canal
# (app/voz/): dos canales que arman su propia lista se desincronizan en la
# primera herramienta nueva, y este módulo no se puede importar sin Redis. Se
# re-exportan porque media suite las importa de acá.
#
# Una herramienta NUEVA se agrega en registro.py, no acá. Este merge lo probó:
# #48 sumó memoria, CRM y gerencia a las listas de este archivo y el conflicto
# salió justo en estas líneas — que es exactamente lo que el módulo existe para
# hacer ruidoso en vez de silencioso.
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


# LAS HERRAMIENTAS DE OTRO SERVIDOR MCP, SI EL DUEÑO CONFIGURÓ UNO
# ----------------------------------------------------------------
# `TOOLS_GERENCIA` de arriba NO SE TOCA, y ésa es la decisión importante de este
# bloque. Lo de afuera va a una lista APARTE, y sólo esa lista arma el agente.
#
# Si en cambio se le sumaran a `TOOLS_GERENCIA`, se llevarían puestas dos cosas
# calladas: `mcp_server._catalogo()` lee esa constante, así que NUESTRO endpoint
# MCP pasaría a re-publicar las herramientas de un tercero —incluido su
# `erpnext_doc_submit`— con nuestro token y nuestra autenticación; y el test que
# afirma «el catálogo publicado es exactamente el del agente» seguiría en verde,
# porque los dos lados se moverían juntos. Una superficie de terceros
# reexportada por nuestra puerta es lo peor de las dos: parece nuestra.
#
# `MCP_EXTERNOS` vacío = no-op, sin un import de más.
#
# NUNCA a TOOLS_CLIENTES, y el motivo está MEDIDO contra el servidor real, no
# leído en un README: `erpnext_sales_order_create` declara
# `items[].required = ["item_code", "qty", "rate"]`. El PRECIO lo pone el
# modelo. No hay resolución de lista de precios del otro lado, así que
# `policy._precio_autorizado` —que filtra Item Prices por price_list, currency y
# uom— queda fuera de ese camino. Con un desconocido escribiendo del otro lado,
# una herramienta donde el precio es un argumento del modelo es la regla 1 al
# revés.
#
# Un fallo del servidor externo NO puede tumbar el agente: si no levanta, se
# avisa y se sigue con las herramientas propias. Un ERP de terceros caído es un
# martes; un agente que no contesta el WhatsApp es el negocio parado.
def _con_externas() -> list:
    try:
        from app import mcp_cliente

        externas = mcp_cliente.cargar(TOOLS_GERENCIA)
        if not externas:
            # COPIA, no la misma lista. Sin servidores externos el contenido es
            # idéntico y la tentación es devolver la constante; entonces las dos
            # son el MISMO objeto y un `TOOLS_AGENTE_GERENCIA.append(...)` de
            # alguna sesión futura le agregaría una herramienta a lo que
            # `mcp_server` publica, sin tocar una línea de ese archivo. Lo
            # encontró su propio test, que fallaba con esto puesto.
            return list(TOOLS_GERENCIA)
        print(mcp_cliente.resumen(TOOLS_GERENCIA))
        return TOOLS_GERENCIA + externas
    except Exception as exc:  # el agente arranca igual, con lo suyo
        print(f"[mcp] no pude cargar los servidores externos ({type(exc).__name__})")
        return list(TOOLS_GERENCIA)


# Lo que se le monta al agente. `TOOLS_GERENCIA` sigue siendo lo que este repo
# escribió y lo que `app/mcp_server.py` publica.
TOOLS_AGENTE_GERENCIA = _con_externas()

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



# UN VALOR DE ENUM EQUIVOCADO NO ES UNA HERRAMIENTA ROTA
# -----------------------------------------------------
# `_ERROR_MSG` manda a escalar_a_humano, y para una herramienta que falló de
# verdad está bien. Pero desde que cinco informes son `informe(que=…)` y tres
# lecturas de ajustes son `ver_ajustes(que=…)`, hay una falla nueva que NO es
# una herramienta rota: el modelo llama bien y escribe mal el valor.
#
# Y es la falla probable, no una rara. El `Literal` no lo garantiza nadie en la
# red: Gemini no tiene `strict` en su capa compatible con OpenAI y lo ignora en
# silencio, así que el enum es una SUGERENCIA para el modelo y la validación
# real es la de pydantic, acá. Peor: con herramientas en castellano, la falla
# medida más común es que el modelo escriba el valor en el idioma del usuario
# —`que="ventas del día"` en vez de `que="ventas"`— aunque haya entendido todo
# bien (arXiv:2601.05366, «parameter value language mismatch»).
#
# Sin esto, ese error se convertía en «esa herramienta falló, escalá a una
# persona»: un dueño preguntando «¿cómo venimos?» terminaba esperando a un
# humano por un guión bajo. Con esto vuelve la lista de valores válidos y el
# modelo reintenta. No se enumera NINGUNA herramienta: sólo los valores del
# parámetro de la que ya llamó, que ya estaban en su propio esquema.
def _valores_esperados(exc: ValidationError) -> tuple[str, str] | None:
    for error in exc.errors():
        if error.get("type") != "literal_error":
            continue
        campo = ".".join(str(x) for x in error.get("loc", ())) or "ese parámetro"
        esperado = str((error.get("ctx") or {}).get("expected", "")).strip()
        if esperado:
            return campo, esperado
    return None


def _error_de_herramienta(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        detalle = _valores_esperados(exc)
        if detalle:
            campo, esperado = detalle
            return (
                f"El valor de «{campo}» no es uno de los que acepta esa "
                f"herramienta. Los únicos válidos son: {esperado}. Llamala de "
                "nuevo con uno de ésos, copiado tal cual —sin traducirlo, sin "
                "acentos y sin mayúsculas—. No le muestres este mensaje a nadie "
                "ni le hables de parámetros."
            )
    return _ERROR_MSG



# The system prompt is built per call (prompt=) and never stored in the
# checkpoint; the model only sees a bounded tail of the thread
# (pre_model_hook=). See app/conversacion.py for why.
agente_clientes = create_react_agent(
    model=_modelo_clientes,
    tools=ToolNodeSinInventario(TOOLS_CLIENTES, handle_tool_errors=_error_de_herramienta),
    prompt=prompt_clientes,
    pre_model_hook=recortar_historial,
    checkpointer=_checkpointer,
)
agente_gerencia = create_react_agent(
    model=_modelo_gerencia,
    tools=ToolNodeSinInventario(
        TOOLS_AGENTE_GERENCIA, handle_tool_errors=_error_de_herramienta
    ),
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
        return pasos.correr(
            agente_clientes,
            mensaje,
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
            modelo=_modelo_clientes,
            armar_prompt=prompt_clientes,
            rol="clientes",
        )


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
        return pasos.correr(
            agente_gerencia,
            mensaje,
            config=_config(
                {
                    "thread_id": f"ger:{thread_id}",
                    "actor_scope": "management",
                    "actor_phone": telefono,
                    "inbound_message_id": inbound_message_id,
                },
                callbacks,
            ),
            modelo=_modelo_gerencia,
            armar_prompt=prompt_gerencia,
            rol="gerencia",
        )
