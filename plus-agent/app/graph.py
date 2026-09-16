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
from app.tools.captura import (
    avisar_al_cliente,
    confirmar_entrega,
    contar_stock,
    registrar_venta_offline,
)
from app.tools.catalogo import (
    buscar_producto,
    consultar_stock,
    estado_pedido,
    pedido_habitual,
)
from app.tools.configuracion import (
    proponer_limite,
    ver_ajustes,
)
from app.tools.crm import (
    actualizar_cliente,
    actualizar_producto,
    anotar_en_ficha,
    armar_presupuesto,
    cambiar_precio,
    editar_borrador,
)
from app.tools.entrega import (
    condiciones_de_entrega,
)
from app.tools.gerencia import (
    ejecutar_reporte,
    ficha_cliente,
    informe,
)
from app.tools.gestion import (
    detalle_de_pedido,
    proponer_accion,
)
from app.tools.memoria import (
    anotar_dato,
    ver_memoria,
)
from app.tools.operaciones import (
    estado_del_sistema,
    ver_avisos_fallidos,
)
from app.tools.pedidos import (
    crear_cliente,
    crear_lead,
    crear_pedido,
    dar_de_baja_pedido,
    escalar_a_humano,
    pedir_excepcion_de_entrega,
    recordar,
)

TOOLS_CLIENTES = [
    buscar_producto, consultar_stock, estado_pedido, pedido_habitual,
    # crear_cliente da de alta al REMITENTE con el teléfono del webhook: no
    # acepta un teléfono como argumento, así que ningún mensaje puede pedir
    # el alta de otra persona.
    crear_cliente, crear_lead, crear_pedido, escalar_a_humano,
    # Las condiciones de entrega que configuró el dueño (app/limites.py, grupo
    # ENTREGA), en una sola herramienta. SÓLO LECTURA y sin un solo dato del
    # cliente que termine escrito en ninguna parte. Es lo que hace el negocio
    # EN GENERAL: no promete la entrega de un pedido —eso sigue siendo
    # `pedir_excepcion_de_entrega` más la decisión de una persona— y un ajuste
    # que falta sale como faltante, nunca como un «no repartimos».
    condiciones_de_entrega,
    # Pide una excepción de entrega. NO decide: o el dueño la dejó autorizada
    # de antemano, o abre una solicitud para una persona (app/solicitudes.py).
    pedir_excepcion_de_entrega,
    # Anota una fila en app/agenda.py para volver sobre un pedido más tarde.
    # PROPONE: el tipo lo fuerza Python a `seguimiento`, la fecha va acotada al
    # horizonte y el motivo se guarda como DATO que lee una persona del equipo.
    # Lo peor que puede causar es un mensaje al equipo que no hacía falta —
    # nunca una confirmación, un submit, una cancelación ni plata.
    recordar,
    # El cliente se da de baja su propio BORRADOR. No escribe nada privilegiado:
    # comprueba que el pedido es suyo, que es un borrador y que no hay una
    # decisión en curso, y anota una fila de app/agenda.py con la credencial de
    # CLIENTE. Quien cierra el borrador es el barrido, con la de política —
    # ninguna herramienta la alcanza. NUNCA en TOOLS_GERENCIA: el equipo cancela
    # por el router determinista, con código, y sobre pedidos confirmados.
    dar_de_baja_pedido,
]

TOOLS_GERENCIA = [
    # UNA herramienta para los cinco informes (pendientes, ventas, stock bajo,
    # cobranzas, autonomía). Eran cinco, y `cobranzas_vencidas` era literalmente
    # una de las siete consultas que `ejecutar_reporte` ya corre: dos
    # herramientas plausibles para «¿cuánto me deben?». Lo que degrada la
    # elección es el solapamiento, no la cantidad.
    informe, ficha_cliente, ejecutar_reporte,
    buscar_producto, consultar_stock, estado_pedido,
    escalar_a_humano,
    # offline capture — how reality gets back into the system
    registrar_venta_offline, contar_stock, confirmar_entrega,
    # ...y el mensaje al cliente, que ahora SALE —con el botón del dueño— en
    # vez de devolver un borrador con un hueco para copiar a mano.
    avisar_al_cliente,
    # the owner's own limits: read them out and PROPOSE a change. There is no
    # tool that confirms one, deliberately — the four-digit code never enters
    # this agent's context and the deterministic router in app/main.py is what
    # applies the change. An agent that could call both steps is one step.
    # NEVER in TOOLS_CLIENTES — a customer cannot be allowed near these.
    # ver_ajustes reads all THREE (limits, delivery rules, history) behind one
    # closed Literal; proponer_limite is the only one that writes, and it stays
    # its own tool — a read and a write behind one enum is one where the wrong
    # branch writes.
    ver_ajustes, proponer_limite,
    # ...y las treinta cosas que repite todo el tiempo y no quiere volver a
    # explicar (app/memoria.py). `ver_memoria` LEE detrás de un Literal;
    # `anotar_dato` ESCRIBE, así que va suelta: misma línea que ver_ajustes y
    # proponer_limite. Un dato no es un ajuste y NO lleva código de cuatro
    # dígitos — hacerle tipear un código para anotar «la panadería paga los
    # viernes» es exactamente la fricción de la que se queja.
    ver_memoria, anotar_dato,
    # read-only operational status. No writes, no retries, no secrets, and
    # NEVER in TOOLS_CLIENTES: these count queues and name the provider.
    estado_del_sistema, ver_avisos_fallidos,
    # ...and the owner's prose about ONE order, turned into ONE action that
    # already exists (app/acciones.py). Reading is done on the spot; anything
    # that writes is only PREPARED here and confirmed by him with a six-digit
    # code this agent never sees — the same shape as proponer_limite, and for
    # the same reason. NEVER in TOOLS_CLIENTES: a customer near these is a
    # customer deciding his own order.
    detalle_de_pedido, proponer_accion,
    # ...y lo que el dueño puede CAMBIAR (app/tools/crm.py). La línea no es
    # leer-contra-escribir —era demasiado ancha— sino IRREVERSIBLE × PLATA:
    # estas cinco se deshacen escribiendo de nuevo y no le cobran un peso a
    # nadie. Lo irreversible sigue afuera y sigue sin ser alcanzable: emitir usa
    # la credencial de política, cancelar un emitido no existe como herramienta,
    # los límites piden su código de cuatro dígitos, y `Item Price` no está en
    # `erpnext.DOCTYPES_EDITABLES` —la negativa es del cliente HTTP, no de la
    # buena conducta de un archivo—.
    #
    # NINGUNA pone un precio: `LineaSimple` no tiene `rate`, igual que
    # `pedidos.LineaPedido`. El precio lo resuelve ERPNext y lo verifica
    # `policy._precio_autorizado`. Una herramienta donde el precio es un
    # argumento del modelo convierte al modelo en la autoridad de precios.
    #
    # LA EXCEPCIÓN, Y LA DECIDIÓ EL DUEÑO: `cambiar_precio` escribe el precio de
    # LISTA. No es el precio de un renglón, pero tampoco es inocente —
    # `policy._precio_estandar` auto-confirma cuando el renglón coincide con la
    # lista, así que quien escribe la lista influye en lo que se confirma solo—.
    # Lo pidió explícitamente («no one can confirm everytime i need automated»)
    # y es su negocio.
    #
    # Lo que sí queda acotado, porque no depende de su permiso sino de cómo se
    # comporta un modelo: el único valor que el modelo aporta es el NÚMERO
    # —lista, moneda y unidad salen de `policy` y del `stock_uom` leído de
    # ERPNext—; ese número tiene que caer adentro de `PRECIO_CAMBIO_MAX_PCT`; y
    # hay UN cambio por producto por día, porque una banda por llamada no acota
    # una serie y el modelo puede llamar cinco veces en el mismo turno. Con la
    # banda en 0, que es el default, no escribe nada. La puerta genérica sigue
    # cerrada: `Item Price` no está en `erpnext.DOCTYPES_EDITABLES`.
    actualizar_cliente, anotar_en_ficha, armar_presupuesto,
    editar_borrador, actualizar_producto, cambiar_precio,
]

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
_HERRAMIENTA_INEXISTENTE = (
    "Esa herramienta no existe para esta conversación y no la vas a conseguir "
    "pidiéndola de nuevo. NO le muestres al cliente este mensaje, ni nombres "
    "herramientas, sistemas ni errores. Si lo que pide lo tiene que ver una "
    "persona, usá escalar_a_humano; si no, seguí con lo que sí podés hacer."
)


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
_ERROR_MSG = (
    "Esa herramienta falló y no devolvió nada. No inventes un resultado. Llamá a "
    "escalar_a_humano y decile al cliente, en UNA línea y con UNA sola disculpa, "
    "que eso lo va a ver el encargado. No le hables de herramientas, de sistemas "
    "ni de errores técnicos."
)

# LA MISMA FALLA, DEL LADO DEL DUEÑO, DICE OTRA COSA
# --------------------------------------------------
# `_ERROR_MSG` está escrito para el agente de CLIENTES —«decile al cliente»,
# «llamá a escalar_a_humano»— y lo usaban los dos. Medido en una conversación
# real del dueño: `contar_stock` falló contra ERPNext, el modelo leyó esta
# orden, y el dueño recibió una tarjeta «🙋 Un cliente necesita una persona /
# Cliente: cuenta no registrada / Tel: <su propio número>» diciéndole que
# alguien lo iba a mirar. Él ES ese alguien. Y como el mensaje tampoco dice que
# no se guardó nada, el modelo completó el hueco con lo que sonaba bien: «ya te
# anoté los 5 kg de leche», sobre una escritura que nunca ocurrió.
#
# Las dos mitades que cambian son las dos que estaban mal para este lado:
# «no se guardó nada» —que es lo único que impide la confirmación inventada— y
# «no escales», porque derivar al equipo a alguien que ES el equipo es mandarle
# un aviso sobre sí mismo. El nombre de la herramienta NO se nombra acá: si
# `escalar_a_humano` deja de existir mañana, esto sigue siendo cierto.
_ERROR_MSG_GERENCIA = (
    "Esa herramienta falló y NO GUARDÓ NADA. No inventes un resultado y no digas "
    "que quedó anotado, registrado, pendiente ni a medias: no quedó nada. "
    "Decíle en UNA línea qué no se pudo hacer, con palabras del negocio y sin "
    "jerga técnica, y ofrecele intentarlo de nuevo. NO lo derives a una persona "
    "del equipo: el que te está escribiendo ES el equipo."
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


def _error_de_herramienta_gerencia(exc: Exception) -> str:
    """Lo mismo para el agente del dueño, con la otra mitad del mensaje.

    El tratamiento del enum es idéntico a propósito —un valor mal escrito no es
    una herramienta rota de ningún lado del teléfono—; lo que cambia es sólo el
    texto de la falla de verdad. Son dos ToolNode distintos, así que el que
    decide cuál se usa es el agente y no un `if` sobre algo que el modelo
    escribe.
    """
    if isinstance(exc, ValidationError):
        detalle = _valores_esperados(exc)
        if detalle:
            return _error_de_herramienta(exc)
    return _ERROR_MSG_GERENCIA



# The system prompt is built per call (prompt=) and never stored in the
# checkpoint; the model only sees a bounded tail of the thread
# (pre_model_hook=). See app/conversacion.py for why.
TOOLNODE_CLIENTES = ToolNodeSinInventario(
    TOOLS_CLIENTES, handle_tool_errors=_error_de_herramienta
)
# CADA AGENTE CON SU MANEJADOR, y son dos objetos con nombre porque cuál le toca
# a cuál es justamente lo que estuvo mal: los dos usaban el de clientes, y el
# dueño terminó recibiendo una tarjeta de «un cliente necesita una persona»
# sobre sí mismo. Un `handle_tool_errors` compartido no se ve desde afuera del
# grafo compilado, y lo que no se puede mirar no se puede probar.
TOOLNODE_GERENCIA = ToolNodeSinInventario(
    TOOLS_AGENTE_GERENCIA, handle_tool_errors=_error_de_herramienta_gerencia
)

agente_clientes = create_react_agent(
    model=_modelo_clientes,
    tools=TOOLNODE_CLIENTES,
    prompt=prompt_clientes,
    pre_model_hook=recortar_historial,
    checkpointer=_checkpointer,
)
agente_gerencia = create_react_agent(
    model=_modelo_gerencia,
    tools=TOOLNODE_GERENCIA,
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
