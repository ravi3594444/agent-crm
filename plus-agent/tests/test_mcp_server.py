"""MCP: que la puerta nueva no sea más ancha que la que ya había.

Lo que se prueba acá no es «el protocolo se parece a la especificación» —eso lo
dice un cliente de verdad—, sino las cuatro cosas que, si se rompen, abren algo:

1. El catálogo que publica MCP es EXACTAMENTE `graph.TOOLS_GERENCIA`. No una
   copia, no un subconjunto elegido a mano.
2. El `RunnableConfig` inyectado NO viaja en el esquema. Ahí va la identidad, y
   un parámetro publicado es un parámetro que el modelo del otro lado llena.
3. El teléfono del token llega a la herramienta, así que `require_management`
   sigue decidiendo. Un token válido para un número que no es del equipo se
   autentica y no lee nada.
4. Sin `MCP_TOKENS` no hay superficie.
"""
from __future__ import annotations

import json

import pytest
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app import mcp_server

TOKEN = "t" * 40
DUENO = "5491133334444"
AJENO = "5491199998888"


@pytest.fixture
def con_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_TOKENS", f"{TOKEN}:{DUENO}")
    monkeypatch.setenv("TELEFONOS_EQUIPO", DUENO)
    return TOKEN


# --------------------------------------------------------------- identidad

def test_a_valid_token_resolves_to_its_phone(con_token):
    assert mcp_server.quien(f"Bearer {TOKEN}") == DUENO


def test_an_unknown_token_resolves_to_nobody(con_token):
    assert mcp_server.quien("Bearer " + "x" * 40) is None


def test_a_token_without_the_bearer_scheme_is_not_a_token(con_token):
    assert mcp_server.quien(TOKEN) is None


def test_a_token_shorter_than_the_minimum_never_enters(monkeypatch):
    """El mínimo lo pone el parser del panel. Si MCP lo relajara, esto lo dice."""
    corto = "a" * 8
    monkeypatch.setenv("MCP_TOKENS", f"{corto}:{DUENO}")
    assert mcp_server.tokens() == {}
    assert mcp_server.quien(f"Bearer {corto}") is None


def test_without_tokens_there_is_no_surface(monkeypatch):
    monkeypatch.setenv("MCP_TOKENS", "")
    assert mcp_server.hay_acceso_configurado() is False


# ---------------------------------------------------------------- catálogo

def test_the_published_catalogue_is_exactly_the_management_agent_s(con_token):
    """No hay un segundo registro. Si alguien escribe una lista acá, esto cae."""
    from app import graph

    publicadas = {h["name"] for h in mcp_server.listar()}
    del_agente = {h.name for h in graph.TOOLS_GERENCIA}
    assert publicadas == del_agente


def test_the_customer_only_cancel_tool_is_not_reachable(con_token):
    """`dar_de_baja_pedido` es del cliente sobre SU borrador, y de nadie más."""
    assert "dar_de_baja_pedido" not in {h["name"] for h in mcp_server.listar()}


# Los nombres con los que se dice QUIÉN SOS. Ninguno puede ser un parámetro
# publicado: un parámetro es un campo que el modelo del otro lado llena, y la
# identidad la pone el servidor con el teléfono del token.
CAMPOS_DE_IDENTIDAD = {
    "config", "actor_phone", "actor_scope", "telefono", "phone",
    "customer_code", "thread_id",
}


def test_no_published_schema_has_a_parameter_that_carries_identity(con_token):
    """La identidad la pone el token, nunca el cuerpo de la petición.

    ESTE TEST NO PRUEBA `tool_call_schema` CONTRA `args_schema`. Se escribió
    creyendo que sí, y la mutación lo desmintió: en esta versión de LangChain
    los dos excluyen el `RunnableConfig` inyectado, así que cambiar uno por el
    otro no rompe nada y el test no medía lo que decía medir.

    Lo que sí puede pasar —y es lo que esto ataja— es que alguien escriba una
    herramienta de gerencia que ACEPTE un teléfono como argumento. Ahí la
    segunda puerta (`require_management` sobre el número del token) deja de
    valer, porque el número lo eligió el que llamó. La mutación que lo mata es
    agregarle `telefono: str = ""` a cualquier herramienta de TOOLS_GERENCIA.
    """
    for herramienta in mcp_server.listar():
        publicados = set(herramienta["inputSchema"].get("properties", {}))
        colision = publicados & CAMPOS_DE_IDENTIDAD
        assert not colision, f"{herramienta['name']} publica {colision}"


def test_every_published_tool_describes_itself(con_token):
    """Una herramienta sin descripción es una que el modelo elige a ciegas."""
    for herramienta in mcp_server.listar():
        assert herramienta["description"].strip(), herramienta["name"]
        assert herramienta["inputSchema"]["type"] == "object"


# ------------------------------------------------------- la identidad viaja

@tool
def _espia(config: RunnableConfig, dato: str = "") -> str:
    """Herramienta de prueba: devuelve el configurable que le llegó."""
    return json.dumps(dict((config or {}).get("configurable") or {}))


@pytest.fixture
def catalogo_espia(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mcp_server, "_catalogo", lambda: {"_espia": _espia})
    return _espia


def test_the_phone_from_the_token_reaches_the_tool(con_token, catalogo_espia):
    """Consumidor 1 de `telefono`: `actor_phone`, que es lo que autoriza."""
    recibido = json.loads(mcp_server.ejecutar("_espia", {}, DUENO))
    assert recibido["actor_phone"] == DUENO
    assert recibido["actor_scope"] == "management"


def test_an_mcp_run_does_not_share_a_thread_with_whatsapp(con_token, catalogo_espia):
    """Consumidor 2 de `telefono`: el `thread_id`.

    Son DOS consumidores del mismo valor, así que van dos tests: uno que cae si
    se pierde `actor_phone` y otro que cae si el hilo deja de estar separado.
    `graph.responder_gerencia` usa el prefijo `ger:`; si esto lo usara también,
    una automatización de n8n escribiría dentro de la conversación del dueño.
    """
    recibido = json.loads(mcp_server.ejecutar("_espia", {}, DUENO))
    assert recibido["thread_id"].startswith("mcp:")
    assert not recibido["thread_id"].startswith("ger:")


def test_the_phone_is_never_taken_from_the_arguments(con_token, catalogo_espia):
    """Un cuerpo de petición no puede decir quién es.

    Si `ejecutar` dejara que `arguments` pisara el `configurable`, este teléfono
    ajeno ganaría. Los argumentos van a la herramienta; la identidad, al config.
    """
    recibido = json.loads(
        mcp_server.ejecutar("_espia", {"actor_phone": AJENO, "dato": "x"}, DUENO)
    )
    assert recibido["actor_phone"] == DUENO


def test_a_phone_outside_the_team_authenticates_and_still_reads_nothing(monkeypatch):
    """La segunda puerta. El token es válido; `require_management` no lo conoce."""
    monkeypatch.setenv("MCP_TOKENS", f"{TOKEN}:{AJENO}")
    monkeypatch.setenv("TELEFONOS_EQUIPO", DUENO)
    assert mcp_server.quien(f"Bearer {TOKEN}") == AJENO

    respuesta = mcp_server.despachar(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "estado_del_sistema", "arguments": {}}},
        AJENO,
    )
    texto = respuesta["result"]["content"][0]["text"]
    assert "autorizado" in texto or "authorized" in texto


# ----------------------------------------------------------------- JSON-RPC

def test_initialize_answers_with_a_version_and_only_the_tools_capability(con_token):
    respuesta = mcp_server.despachar(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": mcp_server.PROTOCOL_VERSION}},
        DUENO,
    )
    resultado = respuesta["result"]
    assert resultado["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert set(resultado["capabilities"]) == {"tools"}
    assert resultado["serverInfo"]["name"] == mcp_server.SERVER_NAME


def test_initialize_offers_our_version_when_the_client_asks_for_an_unknown_one(con_token):
    respuesta = mcp_server.despachar(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "1999-01-01"}},
        DUENO,
    )
    assert respuesta["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION


def test_a_notification_is_never_answered(con_token):
    """Sin `id` no se contesta: contestarle rompe al cliente que la mandó."""
    assert mcp_server.despachar(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, DUENO
    ) is None


def test_a_batch_of_only_notifications_leaves_nothing_to_send(con_token):
    respuestas, hubo = mcp_server.despachar_lote(
        [{"jsonrpc": "2.0", "method": "notifications/initialized"},
         {"jsonrpc": "2.0", "method": "notifications/cancelled"}],
        DUENO,
    )
    assert respuestas == []
    assert hubo is False


def test_an_unknown_method_is_a_protocol_error(con_token):
    respuesta = mcp_server.despachar({"jsonrpc": "2.0", "id": 7, "method": "no/such"}, DUENO)
    assert respuesta["error"]["code"] == mcp_server.ERROR_METODO


def test_a_message_that_is_not_json_rpc_2_is_refused(con_token):
    respuesta = mcp_server.despachar({"id": 1, "method": "ping"}, DUENO)
    assert respuesta["error"]["code"] == mcp_server.ERROR_PETICION_INVALIDA


# ------------------------------------------- un fallo vuelve como resultado

def test_an_unknown_tool_is_an_error_result_and_not_a_catalogue(con_token):
    """Igual que `graph.ToolNodeSinInventario`: no se recita el inventario."""
    respuesta = mcp_server.despachar(
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
         "params": {"name": "submit_sales_order", "arguments": {}}},
        DUENO,
    )
    resultado = respuesta["result"]
    assert resultado["isError"] is True
    texto = resultado["content"][0]["text"]
    for nombre in (h["name"] for h in mcp_server.listar()):
        assert nombre not in texto


def test_a_tool_that_raises_comes_back_as_a_result_without_the_detail(
    con_token, monkeypatch
):
    """Una excepción que sale por JSON-RPC rompe el turno del harness.

    Y el texto no lleva el cuerpo de ERPNext ni la traza: del otro lado hay un
    modelo que no es nuestro.
    """
    @tool
    def _rompe(config: RunnableConfig) -> str:
        """Falla siempre."""
        raise RuntimeError("clave secreta en el cuerpo de ERPNext")

    monkeypatch.setattr(mcp_server, "_catalogo", lambda: {"_rompe": _rompe})
    respuesta = mcp_server.despachar(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
         "params": {"name": "_rompe", "arguments": {}}},
        DUENO,
    )
    assert respuesta["result"]["isError"] is True
    texto = respuesta["result"]["content"][0]["text"]
    assert "secreta" not in texto
    assert "RuntimeError" in texto


def test_arguments_that_are_not_an_object_are_a_parameter_error(con_token):
    respuesta = mcp_server.despachar(
        {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
         "params": {"name": "estado_del_sistema", "arguments": ["no"]}},
        DUENO,
    )
    assert respuesta["error"]["code"] == mcp_server.ERROR_PARAMETROS


def test_a_huge_result_is_cut_before_it_reaches_the_harness(con_token, monkeypatch):
    @tool
    def _largo(config: RunnableConfig) -> str:
        """Devuelve más de lo que cabe."""
        return "x" * (mcp_server.MAX_RESULTADO + 5_000)

    monkeypatch.setattr(mcp_server, "_catalogo", lambda: {"_largo": _largo})
    texto = mcp_server.ejecutar("_largo", {}, DUENO)
    assert len(texto) == mcp_server.MAX_RESULTADO


# ------------------------------------------------------------------ origen

def test_a_browser_origin_that_is_not_allowed_is_refused(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://panel.example")
    assert mcp_server.origen_aceptable("https://evil.example") is False
    assert mcp_server.origen_aceptable("https://panel.example") is True


def test_a_client_that_is_not_a_browser_sends_no_origin_and_is_allowed(monkeypatch):
    """n8n, Claude Code y un script no mandan `Origin`. El token es su puerta."""
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "")
    assert mcp_server.origen_aceptable("") is True


def test_a_wildcard_origin_is_not_an_origin(monkeypatch):
    """`*` en la lista no habilita a nadie.

    El filtro que descarta el `*` en `origenes_permitidos` es redundante por
    construcción —la comparación es de pertenencia exacta, y ningún navegador
    manda `Origin: *`— y la mutación lo confirmó: sacarlo no rompe ningún test.
    Se queda porque dice la intención en el lugar donde alguien escribiría el
    comodín. Lo que este test protege de verdad es la pertenencia exacta:
    mutar `origen_aceptable` a `return True` lo mata.
    """
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "*")
    assert mcp_server.origen_aceptable("https://evil.example") is False


# ------------------------------------------------------------ CORS del endpoint

def _llamar_transporte(metodo: str, cabeceras: dict, cuerpo: bytes = b""):
    """Corre `MCPTransport` como ASGI y devuelve (código, headers, cuerpo)."""
    import asyncio

    transporte = mcp_server.MCPTransport()
    eventos = [{"type": "http.request", "body": cuerpo, "more_body": False}]
    salida: dict = {}

    async def receive():
        return eventos.pop(0) if eventos else {"type": "http.disconnect"}

    async def send(mensaje):
        if mensaje["type"] == "http.response.start":
            salida["codigo"] = mensaje["status"]
            salida["headers"] = {
                k.decode().lower(): v.decode() for k, v in mensaje["headers"]
            }
        else:
            salida["cuerpo"] = mensaje.get("body", b"")

    scope = {
        "type": "http",
        "method": metodo,
        "path": "/",
        "headers": [(k.encode(), v.encode()) for k, v in cabeceras.items()],
    }
    asyncio.run(transporte(scope, receive, send))
    return salida


def test_un_origen_permitido_recibe_el_header_que_lo_habilita(con_token, monkeypatch):
    """Validar el origen y NO devolverlo no sirve de nada.

    Sin `access-control-allow-origin` el navegador descarta la respuesta aunque
    el servidor la haya aceptado, así que un origen que SÍ está en la lista
    fallaba igual —en el preflight y en el POST— y se veía como si la validación
    lo estuviera rechazando. Son dos mitades de una sola cosa.

    MUTACIÓN: sacar el `cabeceras.append((b"access-control-allow-origin", …))`.
    Caen éste y el del POST, y ningún otro.
    """
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://panel.example")

    respuesta = _llamar_transporte("OPTIONS", {"origin": "https://panel.example"})

    assert respuesta["codigo"] == 204
    assert respuesta["headers"]["access-control-allow-origin"] == "https://panel.example"


def test_el_POST_tambien_lo_devuelve_y_no_solo_el_preflight(con_token, monkeypatch):
    """Un preflight que pasa y un POST que no es el mismo fallo, más tarde."""
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://panel.example")

    respuesta = _llamar_transporte(
        "POST",
        {"origin": "https://panel.example", "authorization": f"Bearer {TOKEN}",
         "content-type": "application/json"},
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode(),
    )

    assert respuesta["codigo"] == 200
    assert respuesta["headers"]["access-control-allow-origin"] == "https://panel.example"


def test_un_cliente_sin_origen_no_recibe_el_header_y_funciona_igual(
    con_token, monkeypatch
):
    """n8n, Claude Code y un script no mandan `Origin` y no necesitan CORS.

    Devolverles un `access-control-allow-origin` vacío sería peor que no
    mandarlo: el header existiría sin nombrar a nadie.
    """
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://panel.example")

    respuesta = _llamar_transporte(
        "POST",
        {"authorization": f"Bearer {TOKEN}", "content-type": "application/json"},
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode(),
    )

    assert respuesta["codigo"] == 200
    assert "access-control-allow-origin" not in respuesta["headers"]


def test_un_origen_de_afuera_se_frena_y_no_se_le_habilita_nada(con_token, monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://panel.example")

    respuesta = _llamar_transporte("OPTIONS", {"origin": "https://evil.example"})

    assert respuesta["codigo"] == 403
    assert "access-control-allow-origin" not in respuesta["headers"]


def test_sin_token_configurado_el_endpoint_no_existe(monkeypatch):
    """503 y NO 401: el problema no es el token que trajo el cliente."""
    monkeypatch.setenv("MCP_TOKENS", "")

    respuesta = _llamar_transporte("POST", {"authorization": "Bearer lo-que-sea"})

    assert respuesta["codigo"] == 503


def test_un_GET_no_abre_ningun_canal_de_eventos(con_token):
    """No ofrecemos SSE iniciado por el servidor: este servidor no manda nada
    que el cliente no haya pedido."""
    respuesta = _llamar_transporte("GET", {"authorization": f"Bearer {TOKEN}"})

    assert respuesta["codigo"] == 405


def test_sin_token_valido_es_401_y_lo_dice_con_el_header_del_protocolo(con_token):
    respuesta = _llamar_transporte("POST", {"authorization": "Bearer " + "z" * 40})

    assert respuesta["codigo"] == 401
    assert respuesta["headers"]["www-authenticate"].startswith("Bearer")


def test_el_titulo_que_ve_el_harness_es_el_negocio_del_dueno(con_token, monkeypatch):
    """Lo que el dueño lee en la lista de servidores de su harness.

    Estaba escrito «Plus Agent — Lácteos Plus»: el nombre de UN cliente, en un
    archivo, en el único campo de todo el protocolo que una persona ve con los
    ojos. Instalado en otro negocio, el dueño conectaba su CRM y le aparecía el
    nombre de otra empresa.

    Se prueba por `despachar` y no llamando a `server_title()`: lo que importa
    no es que la función arme la cadena, es que `initialize` la mande. Y el
    valor esperado se escribe acá, no se lee de `mcp_server` — un assert contra
    la constante del módulo pasa igual con la constante equivocada.

    MUTACIÓN: devolver la cadena fija de antes. Cae éste y sólo éste.
    """
    monkeypatch.setenv("NOMBRE_NEGOCIO", "Ferretería Rivadavia")

    respuesta = mcp_server.despachar(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": mcp_server.PROTOCOL_VERSION}},
        DUENO,
    )

    titulo = respuesta["result"]["serverInfo"]["title"]
    assert "Ferretería Rivadavia" in titulo
    assert "Lácteos" not in titulo


@pytest.mark.parametrize("params", [[1, 2], "hola", 7, True, None])
def test_un_params_que_no_es_objeto_es_un_error_y_no_una_excepcion(con_token, params):
    """Lo que entra por la red no tiene por qué ser lo que la spec promete.

    Los handlers hacían `(peticion.get("params") or {}).get(...)`. Con un array
    o un string —truthy y sin `.get`— eso levanta AttributeError, y como el
    despacho no lo atrapaba salía por el transporte: 500 por HTTP, y en stdio
    se lleva el servidor puesto. Un cliente mal escrito podía apagar el
    endpoint de gerencia.

    MUTACIÓN: sacarle a `despachar` la comprobación de que `params` es un
    objeto. Cae ésta y sólo ésta.
    """
    respuesta = mcp_server.despachar(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params},
        DUENO,
    )
    assert respuesta["error"]["code"] == mcp_server.ERROR_PARAMETROS
    assert "result" not in respuesta


def test_omitir_params_sigue_siendo_valido(con_token):
    """La otra mitad del centinela, sin la cual se cumple rechazando todo.

    La spec dice `params` OPCIONAL. `initialize` sin `params` tiene que
    contestar un resultado, no un error — es lo que manda un cliente que no
    negocia nada.

    MUTACIÓN: `peticion.get("params", _SIN_PARAMS)` -> `peticion.get("params",
    None)`, o sea volver a tratar la ausencia como un `null` rechazable. Cae
    ésta y sólo ésta.
    """
    respuesta = mcp_server.despachar(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, DUENO
    )

    assert "error" not in respuesta
    assert respuesta["result"]["serverInfo"]


def test_un_tools_call_con_params_null_es_un_error_de_protocolo(con_token):
    """Y NO un resultado con `isError`, que es lo que devolvía.

    `params` es obligatorio en `tools/call` —lleva el `name`—, así que
    `"params": null` es un cuerpo malformado. Contestarlo con
    `{"content": [...], "isError": true}` le dice al modelo del otro lado que
    la herramienta CORRIÓ y falló: va a reintentar con otros argumentos en vez
    de arreglar la petición. El mismo `null` en `initialize` pasaba de largo.

    MUTACIÓN: en `despachar`, `peticion.get("params", _SIN_PARAMS)` ->
    `peticion.get("params")`. Cae ésta, la celda `None` de la parametrizada de
    arriba, y ninguna otra.
    """
    respuesta = mcp_server.despachar(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": None}, DUENO
    )

    assert respuesta["error"]["code"] == mcp_server.ERROR_PARAMETROS
    assert "result" not in respuesta


def test_un_params_malo_en_una_notificacion_no_contesta_nada(con_token):
    """Y sigue sin contestarle a algo sin `id`, que la spec prohíbe.

    El arreglo no puede convertir una notificación en una respuesta: eso es
    otro bug, y más difícil de ver.

    MUTACIÓN: devolver `_error(...)` sin mirar `es_notificacion`. Cae ésta y
    sólo ésta.
    """
    assert mcp_server.despachar(
        {"jsonrpc": "2.0", "method": "initialize", "params": [1, 2]}, DUENO
    ) is None
