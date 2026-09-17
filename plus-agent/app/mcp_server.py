"""MCP: las herramientas del agente de gerencia, para cualquier harness.

QUÉ ES ESTO, EN UNA LÍNEA
-------------------------
El dueño puede conectar n8n, Claude Code, Claude Desktop o el harness que sea
contra este servidor y ESE harness pasa a ser el agente de gerencia: las mismas
herramientas, la misma credencial de ERPNext, los mismos límites. No es una API
nueva del CRM — es la misma superficie que ya tiene el agente por WhatsApp,
hablada en un protocolo que las automatizaciones entienden.

NO HAY UN SEGUNDO REGISTRO DE HERRAMIENTAS, Y ÉSA ES LA DECISIÓN
----------------------------------------------------------------
`_catalogo()` no enumera nada: lee `graph.TOOLS_GERENCIA`. Una lista escrita a
mano acá se desincronizaría —y la desincronización es silenciosa y siempre en
la dirección peligrosa: alguien saca una herramienta de gerencia y la copia de
este lado sigue sirviéndola—. Con una sola lista, agregar una herramienta al
agente la agrega acá, y sacarla la saca. No existe la versión "de MCP" de nada.

UN TOKEN ES UN TELÉFONO. NO HAY TOKEN ANÓNIMO.
----------------------------------------------
`MCP_TOKENS` tiene el formato `<token>:<teléfono>,<token>:<teléfono>` y lo
parsea el MISMO `dashboard.entradas_de_tokens` que el panel, con sus mismas
reglas duras (mínimo 32 caracteres, nada ambiguo entra). El teléfono que sale
de ahí es el que viaja en el `RunnableConfig`, así que cada herramienta vuelve
a pasar por `require_management` y por `router.es_equipo`: un token para un
número que no está en `TELEFONOS_EQUIPO` se autentica y no puede leer nada.

El panel además acepta un token COMPARTIDO y anónimo para mirar. Acá no, a
propósito: estas herramientas leen la facturación, los márgenes y la deuda de
cada cliente, y algunas le escriben al dueño por WhatsApp. Una identidad que no
nombra a nadie no se puede auditar ni revocar.

LO QUE ESTO NO ABRE
-------------------
Ninguna herramienta de `TOOLS_GERENCIA` hace Submit, cancela un documento
confirmado, toca un Item Price ni cambia un límite. Las dos que PROPONEN
(`proponer_limite`, `proponer_accion`) siguen terminando en un código de cuatro
o seis dígitos que sale por WhatsApp al dueño y que el router determinista de
`app/main.py` aplica — ese código nunca entra en el contexto de un modelo, y
tampoco vuelve por acá. Así que lo peor que puede hacer un token robado es leer
el negocio y hacerle sonar el teléfono al dueño. Es malo; no es una venta.

SIN `MCP_TOKENS` ESTO NO EXISTE
-------------------------------
Sin tokens configurados el endpoint HTTP contesta 503 y el servidor stdio se
niega a arrancar. No hay un interruptor aparte que alguien pueda dejar en `true`
sin haber configurado la autenticación: configurar la autenticación ES
encenderlo.
"""
from __future__ import annotations

import contextlib
import hmac
import json
import os
import sys
from typing import Any

# La versión del protocolo que hablamos. Un cliente que pide otra recibe ÉSTA
# en el `initialize`, que es lo que manda la especificación: el servidor
# responde con la versión que va a usar y el cliente decide si sigue.
PROTOCOL_VERSION = "2025-06-18"

# Versiones que también sabemos contestar si el cliente las pide. La respuesta
# se hace ECO de la que pidió cuando está acá; si no, ofrecemos la nuestra.
PROTOCOL_VERSIONS_SOPORTADAS = ("2025-06-18", "2025-03-26", "2024-11-05")

SERVER_NAME = "plus-agent"


def server_title() -> str:
    """El nombre que ve el harness. Sale del negocio, no del código.

    Decía «Plus Agent — Lácteos Plus», o sea el nombre de UN cliente horneado
    en un archivo. Lo que un dueño ve en la lista de servidores de su Claude
    Desktop tiene que ser SU negocio.
    """
    from app.conversacion import negocio

    return f"Plus Agent — {negocio()}"

# Cuerpo máximo de una petición. El webhook tiene su propio límite; éste es
# para que un POST enorme no ocupe memoria antes de que se rechace el token.
MAX_BODY_BYTES = 256 * 1024

# Techo del texto que devuelve una herramienta. Las herramientas ya recortan
# (`ejecutar_reporte` manda 40 filas), así que esto es un cinturón: un resultado
# gigante no debería poder llenar el contexto del harness que llamó.
MAX_RESULTADO = 60_000

# Códigos de error de JSON-RPC 2.0. Un error de PROTOCOLO (método que no
# existe, parámetros mal formados) viaja acá; un error de EJECUCIÓN de una
# herramienta NO —ése vuelve como resultado con `isError: true`, para que el
# modelo del otro lado lo lea y reintente en vez de romper la conversación.
# Es la misma razón por la que `graph.handle_tool_errors` no deja propagar.
ERROR_PARSE = -32700
ERROR_PETICION_INVALIDA = -32600
ERROR_METODO = -32601
ERROR_PARAMETROS = -32602

# El centinela que separa «no vino `params`» de «vino `params: null`». Un
# `.get("params")` pelado los hace indistinguibles, y la especificación dice
# opcional, no anulable.
_SIN_PARAMS = object()
ERROR_INTERNO = -32603


class MCPNoConfigurado(RuntimeError):
    """No hay ni un token válido: este servidor no tiene con qué autenticar."""


# ---------------------------------------------------------------- identidad

def tokens() -> dict[str, str]:
    """`MCP_TOKENS` -> {token: teléfono}, con el parser del panel.

    Reusar `dashboard.entradas_de_tokens` no es economía de líneas: es que las
    reglas de un token —el mínimo de 32 caracteres, que dos entradas con el
    mismo token se caigan las dos, que el teléfono se normalice igual que en el
    webhook— se escriban UNA vez. Un segundo parser con las mismas reglas
    escritas de nuevo es un segundo parser que se va a portar distinto el día
    que alguien arregle una de las dos.
    """
    from app import dashboard

    pares, problemas = dashboard.entradas_de_tokens(os.getenv("MCP_TOKENS", ""))
    for motivo in problemas:
        _avisar(f"MCP_TOKENS: {motivo}")
    return pares


# Lo último que se avisó, para no repetir el aviso en cada petición.
_AVISADOS: set[str] = set()


def _avisar(mensaje: str) -> None:
    """Un aviso por texto distinto. NUNCA imprime un token.

    Va a stderr y no a stdout: en el transporte stdio, stdout es el canal del
    protocolo y un `print` ahí corrompe el mensaje que el cliente está leyendo.
    """
    if mensaje in _AVISADOS:
        return
    _AVISADOS.add(mensaje)
    print(f"[mcp] {mensaje}", file=sys.stderr, flush=True)


def quien(header: str) -> str | None:
    """El teléfono detrás de un `Authorization: Bearer …`, o None.

    Compara en tiempo constante y sin cortar el recorrido, igual que
    `dashboard.quien`: cuánto tarda la respuesta no dice en qué posición de la
    lista estaba el token.
    """
    if not header.startswith("Bearer "):
        return None
    entregado = header.removeprefix("Bearer ").strip().encode()
    if not entregado:
        return None
    encontrado: str | None = None
    for token, numero in tokens().items():
        if hmac.compare_digest(entregado, token.encode()):
            encontrado = numero
    return encontrado


def hay_acceso_configurado() -> bool:
    """¿Hay al menos un token válido? Sin esto, 503 y ningún dato."""
    return bool(tokens())


# ------------------------------------------------------------- herramientas

def _catalogo() -> dict[str, Any]:
    """Las herramientas de gerencia, por nombre. La lista es la del agente.

    El import es perezoso a propósito: `app.graph` construye el checkpointer de
    Redis y los modelos AL IMPORTAR, así que un test del protocolo no debería
    pagarlo sólo por preguntar qué forma tiene un error de JSON-RPC.
    """
    from app import graph

    return {herramienta.name: herramienta for herramienta in graph.TOOLS_GERENCIA}


def descriptor(herramienta: Any) -> dict:
    """Una herramienta de LangChain, como la describe MCP.

    `tool_call_schema` —y no `args_schema`— es el que YA excluye el
    `RunnableConfig` inyectado. Publicar `args_schema` le ofrecería al harness
    un parámetro `config` que el modelo del otro lado intentaría llenar, y que
    es justamente el que no puede venir de afuera: ahí viaja la identidad.
    """
    esquema = herramienta.tool_call_schema.model_json_schema()
    # `title` lo pone pydantic con el nombre de la función y no aporta nada al
    # cliente; la descripción ya viaja aparte.
    esquema.pop("title", None)
    esquema.pop("description", None)
    return {
        "name": herramienta.name,
        "description": (herramienta.description or "").strip(),
        "inputSchema": esquema,
    }


def listar() -> list[dict]:
    return [descriptor(h) for h in _catalogo().values()]


def ejecutar(nombre: str, argumentos: dict, telefono: str) -> str:
    """Corre UNA herramienta con la identidad de `telefono`.

    Las tres cosas que pasan acá y en ningún otro lado de este archivo:

    1. `manager_scope()` — la credencial de ERPNext de gerencia, la misma que
       usa `graph.responder_gerencia`. Si no está configurada, levanta antes de
       tocar nada.
    2. `actor_scope="management"` y `actor_phone` en el `configurable`. Es lo
       único que `require_management` mira, y lo vuelve a contrastar contra
       `TELEFONOS_EQUIPO`. Ningún argumento de herramienta acepta un teléfono,
       así que esto no se puede falsificar desde el cuerpo de la petición.
    3. El `thread_id` lleva el prefijo `mcp:` para que una conversación por
       WhatsApp y una automatización no compartan hilo.
    """
    from app import erpnext

    herramienta = _catalogo().get(nombre)
    if herramienta is None:
        raise KeyError(nombre)
    with erpnext.manager_scope():
        salida = herramienta.invoke(
            dict(argumentos or {}),
            config={
                "configurable": {
                    "thread_id": f"mcp:{_etiqueta(telefono)}",
                    "actor_scope": "management",
                    "actor_phone": telefono,
                    "inbound_message_id": "",
                }
            },
        )
    texto = salida if isinstance(salida, str) else json.dumps(salida, default=str)
    return texto[:MAX_RESULTADO]


def _etiqueta(telefono: str) -> str:
    import hashlib

    return hashlib.sha256(telefono.encode()).hexdigest()[:10]


# ------------------------------------------------------------------ JSON-RPC

def _resultado(ident: Any, valor: dict) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "result": valor}


def _error(ident: Any, codigo: int, mensaje: str) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": codigo, "message": mensaje}}


def _initialize(peticion: dict) -> dict:
    pedida = str((peticion.get("params") or {}).get("protocolVersion") or "")
    version = pedida if pedida in PROTOCOL_VERSIONS_SOPORTADAS else PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        # `listChanged: false` es la verdad: la lista sale de TOOLS_GERENCIA,
        # que es una constante de módulo. No hay nada que notificar.
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {
            "name": SERVER_NAME,
            "title": server_title(),
            "version": _version(),
        },
    }


def _version() -> str:
    """La versión que se anuncia. `MCP_SERVER_VERSION` si alguien la fija."""
    return os.getenv("MCP_SERVER_VERSION", "1.0.0").strip() or "1.0.0"


def _tools_call(peticion: dict, telefono: str) -> dict:
    parametros = peticion.get("params") or {}
    nombre = str(parametros.get("name") or "")
    argumentos = parametros.get("arguments") or {}
    if not isinstance(argumentos, dict):
        return _error(peticion.get("id"), ERROR_PARAMETROS, "arguments must be an object")
    try:
        texto = ejecutar(nombre, argumentos, telefono)
    except KeyError:
        # Herramienta inexistente: el protocolo lo trata como un error de
        # ejecución y no de JSON-RPC, para que el modelo del otro lado lo lea.
        # NO se enumera el catálogo, por el mismo motivo que
        # `graph.ToolNodeSinInventario`: un inventario recitado es un mapa.
        return _resultado(
            peticion.get("id"),
            {
                "content": [{"type": "text", "text": "That tool does not exist here."}],
                "isError": True,
            },
        )
    except Exception as exc:  # cualquier fallo vuelve como resultado, no como excepción
        _avisar(f"{nombre} falló ({type(exc).__name__})")
        return _resultado(
            peticion.get("id"),
            {
                "content": [
                    {
                        "type": "text",
                        # Sin cuerpo de ERPNext, sin traza y sin secretos: el
                        # que lee esto es un modelo que no es nuestro.
                        "text": f"That tool failed ({type(exc).__name__}). "
                                "Nothing was changed.",
                    }
                ],
                "isError": True,
            },
        )
    return _resultado(
        peticion.get("id"),
        {"content": [{"type": "text", "text": texto}], "isError": False},
    )


def despachar(peticion: object, telefono: str) -> dict | None:
    """Un mensaje JSON-RPC -> la respuesta, o None si era una notificación.

    Devolver None no es "no pasó nada": es que el mensaje NO llevaba `id`, y a
    un mensaje sin `id` la especificación prohíbe contestarle. El transporte
    HTTP traduce ese None a un 202 sin cuerpo.
    """
    if not isinstance(peticion, dict) or peticion.get("jsonrpc") != "2.0":
        return _error(None, ERROR_PETICION_INVALIDA, "invalid JSON-RPC 2.0 message")
    metodo = peticion.get("method")
    if not isinstance(metodo, str):
        # Sin `method` esto es una RESPUESTA, no una petición. Un servidor que
        # no manda peticiones no espera respuestas: se ignora.
        return None
    ident = peticion.get("id")
    es_notificacion = "id" not in peticion

    # `params` TIENE QUE SER UN OBJETO, y se comprueba acá y no en cada handler.
    # La especificación permite omitirlo, pero si viene tiene que ser un objeto
    # (o un array, que nosotros no aceptamos). Los handlers hacían
    # `(peticion.get("params") or {}).get(...)`, que con un array o un string
    # —truthy y sin `.get`— levanta AttributeError; y como nadie lo atrapaba,
    # salía por el transporte: 500 por HTTP, y en stdio se lleva el servidor
    # puesto. Una sola comprobación acá cubre initialize, tools/call y lo que
    # se agregue después.
    #
    # Y UN `"params": null` EXPLÍCITO NO ES LO MISMO QUE OMITIRLO. Con
    # `.get("params")` los dos daban `None` y los dos salteaban la validación,
    # así que el mismo cuerpo malformado se contestaba distinto según el
    # método: `initialize` lo aceptaba como si nada y `tools/call` devolvía un
    # RESULTADO con `isError` —que del otro lado se lee como «la herramienta
    # corrió y falló»— en vez de un error de protocolo. `null` no es un objeto
    # en ninguna de las dos, así que se rechaza en las dos, acá.
    parametros = peticion.get("params", _SIN_PARAMS)
    if parametros is not _SIN_PARAMS and not isinstance(parametros, dict):
        if es_notificacion:
            return None
        return _error(ident, ERROR_PARAMETROS, "params must be an object")

    if metodo == "initialize":
        return None if es_notificacion else _resultado(ident, _initialize(peticion))
    if metodo.startswith("notifications/"):
        return None
    if metodo == "ping":
        return None if es_notificacion else _resultado(ident, {})
    if metodo == "tools/list":
        if es_notificacion:
            return None
        return _resultado(ident, {"tools": listar()})
    if metodo == "tools/call":
        if es_notificacion:
            return None
        return _tools_call(peticion, telefono)
    if es_notificacion:
        return None
    return _error(ident, ERROR_METODO, f"method not found: {metodo}")


def despachar_lote(cuerpo: object, telefono: str) -> tuple[list[dict], bool]:
    """Uno o varios mensajes -> (respuestas, hubo_alguna_peticion).

    JSON-RPC permite un array. El segundo valor es lo que necesita el
    transporte HTTP para elegir entre 200 con cuerpo y 202 sin cuerpo.
    """
    mensajes = cuerpo if isinstance(cuerpo, list) else [cuerpo]
    respuestas = [r for r in (despachar(m, telefono) for m in mensajes) if r is not None]
    return respuestas, bool(respuestas)


# ------------------------------------------------------------ transporte HTTP

def origenes_permitidos() -> set[str]:
    """`MCP_ALLOWED_ORIGINS`, exactos. Nunca un comodín.

    La especificación pide validar `Origin` por el ataque de DNS rebinding: una
    página cualquiera que el dueño tenga abierta puede hacerle POST a un
    servidor local y, si el servidor no mira el origen, la página termina
    leyendo el negocio con el token del navegador. Los clientes que NO son un
    navegador (n8n, Claude Code, un script) no mandan `Origin`, así que esto no
    les cuesta nada: sólo se exige cuando el header viene.
    """
    crudo = os.getenv("MCP_ALLOWED_ORIGINS", "")
    return {o.strip().rstrip("/") for o in crudo.split(",") if o.strip() and o.strip() != "*"}


def origen_aceptable(origin: str) -> bool:
    return not origin or origin.rstrip("/") in origenes_permitidos()


class MCPTransport:
    """El endpoint `Streamable HTTP`, montado en /mcp. Sin sesiones.

    POR QUÉ SIN SESIÓN. `Mcp-Session-Id` es opcional y sirve para que un
    servidor con estado reconozca a un cliente entre peticiones. Acá no hay
    estado que reconocer: cada llamada trae su token, resuelve un teléfono y
    corre una herramienta. Un servidor sin sesión es además el que sobrevive a
    que el contenedor se reinicie en medio de un workflow de n8n, que es
    exactamente lo que pasa en producción.

    POR QUÉ CONTESTA JSON Y NO SSE. La especificación deja elegir: un POST
    puede responderse con un `application/json` único o con un stream de
    eventos. Un stream sirve para mandar progreso mientras la herramienta
    corre; nuestras herramientas devuelven una vez y ya. Un SSE de un solo
    evento es la misma respuesta con más piezas que se pueden romper.
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        metodo = scope["method"]

        origen = headers.get("origin", "")

        async def responder(codigo: int, cuerpo: object | None, extra: list | None = None):
            cabeceras = [
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
                (b"vary", b"Origin"),
                (b"mcp-protocol-version", PROTOCOL_VERSION.encode()),
            ]
            # VALIDAR EL ORIGEN Y NO DEVOLVERLO NO SIRVE DE NADA. Sin este
            # header el navegador descarta la respuesta aunque el servidor la
            # haya aceptado, así que un origen que SÍ está en la lista fallaba
            # igual —en el preflight y en el POST—, y se veía como si la
            # validación lo estuviera rechazando. Se hacen las dos mitades o
            # ninguna: la lista decide, y lo que decide se dice.
            #
            # Se hace eco del origen exacto que llegó, nunca `*`: con
            # credenciales de por medio un comodín es la lista vacía al revés.
            if origen and origen.rstrip("/") in origenes_permitidos():
                cabeceras.append((b"access-control-allow-origin", origen.encode()))
            if extra:
                cabeceras.extend(extra)
            datos = b""
            if cuerpo is not None:
                cabeceras.append((b"content-type", b"application/json; charset=utf-8"))
                datos = json.dumps(cuerpo, allow_nan=False).encode()
            await send({"type": "http.response.start", "status": codigo, "headers": cabeceras})
            await send({"type": "http.response.body", "body": datos})

        if not origen_aceptable(headers.get("origin", "")):
            await responder(403, {"error": "origin not allowed"})
            return
        if not hay_acceso_configurado():
            # 503 y NO 401: el problema no es el token que trajo el cliente, es
            # que este servidor no tiene con qué comparar ninguno.
            await responder(503, {"error": "MCP is not configured on this server"})
            return
        if metodo == "OPTIONS":
            await responder(204, None, [
                (b"allow", b"POST, OPTIONS"),
                (b"access-control-allow-methods", b"POST, OPTIONS"),
                (b"access-control-allow-headers",
                 b"Authorization, Content-Type, MCP-Protocol-Version"),
                (b"access-control-max-age", b"600"),
            ])
            return
        if metodo != "POST":
            # No ofrecemos el canal de eventos que el cliente abriría con GET:
            # este servidor no manda nada que el cliente no haya pedido.
            await responder(405, {"error": "method not allowed"}, [(b"allow", b"POST, OPTIONS")])
            return

        telefono = quien(headers.get("authorization", ""))
        if telefono is None:
            await responder(401, {"error": "unauthorized"},
                            [(b"www-authenticate", b'Bearer realm="plus-agent"')])
            return

        cuerpo = b""
        while True:
            evento = await receive()
            if evento["type"] != "http.request":
                break
            cuerpo += evento.get("body", b"")
            if len(cuerpo) > MAX_BODY_BYTES:
                await responder(413, {"error": "body too large"})
                return
            if not evento.get("more_body"):
                break
        try:
            mensaje = json.loads(cuerpo or b"null")
        except (json.JSONDecodeError, UnicodeDecodeError):
            await responder(400, _error(None, ERROR_PARSE, "invalid JSON"))
            return

        from starlette.concurrency import run_in_threadpool

        # Las herramientas son SÍNCRONAS —httpx y redis bloqueantes— y esto
        # corre dentro del mismo proceso que atiende el webhook de WhatsApp.
        # Ejecutarlas en el hilo del bucle de eventos congelaría los mensajes
        # de los clientes mientras un workflow de n8n pide un reporte.
        respuestas, hubo_peticion = await run_in_threadpool(
            despachar_lote, mensaje, telefono
        )
        if not hubo_peticion:
            # Sólo notificaciones: la especificación pide 202 y ningún cuerpo.
            await responder(202, None)
            return
        unico = isinstance(mensaje, dict)
        await responder(200, respuestas[0] if unico and respuestas else respuestas)


def install_mcp(application) -> None:
    """Monta el endpoint. Una línea en `app/main.py`, igual que el panel."""
    application.mount("/mcp", MCPTransport())


# ----------------------------------------------------------- transporte stdio

def _telefono_de_stdio() -> str:
    """Quién es el que arrancó el servidor por stdio.

    Un proceso lanzado por Claude Code no trae headers, así que el token viaja
    por entorno (`MCP_TOKEN`) y se resuelve contra la MISMA tabla que el HTTP.
    Una segunda forma de decir quién sos —un `MCP_STDIO_PHONE`, por ejemplo—
    sería una identidad que no se puede revocar sacando una línea del `.env`.
    """
    telefono = quien(f"Bearer {os.getenv('MCP_TOKEN', '').strip()}")
    if telefono is None:
        raise MCPNoConfigurado(
            "MCP_TOKEN no coincide con ninguna entrada de MCP_TOKENS"
        )
    return telefono


@contextlib.contextmanager
def _canal_apartado():
    """Aparta el stdout REAL para el protocolo y manda todo lo demás a stderr.

    ESTE ARCHIVO NO ESCRIBE UN `print` A SECAS, PERO NO ALCANZA, y por qué no
    alcanza está medido: `_catalogo()` importa `app.graph` TARDE, recién cuando
    alguien pide `tools/list`, y esa importación arrastra medio proyecto —
    `limites`, `agenda`, `solicitudes`, `notificar`…— que entre todos tienen
    ~370 `print()` de diagnóstico, escritos para el contenedor del agente, donde
    stdout es el log y está bien que vayan ahí. Acá stdout ES el canal del
    protocolo. Uno solo de esos 370 que se dispare durante la importación sale
    ANTES de la respuesta JSON-RPC, en el mismo stream, y el cliente no puede
    parsear nada.

    No es hipotético: con este archivo intacto, un `tools/list` imprimía
    «[limites] no pude leer el almacén, uso el entorno para el negocio» como
    primera línea de stdout. Un módulo que no sabe que existe MCP rompía el
    servidor MCP.

    Por eso el arreglo va acá y no en los 370 llamados: mientras corre el
    servidor, `sys.stdout` ES stderr, y las respuestas se escriben al descriptor
    apartado, al que nadie más tiene referencia. Cualquier `print` futuro de
    cualquier módulo ya nace del lado correcto.
    """
    protocolo = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield protocolo
    finally:
        sys.stdout = protocolo


def main() -> int:
    """El servidor stdio: JSON por línea, protocolo por stdout, avisos por stderr.

    LO QUE NUNCA VA A STDOUT. En este transporte stdout ES el canal del
    protocolo: un `print` de diagnóstico se mete en medio de un mensaje y el
    cliente lo lee como JSON roto. Todo lo que no sea una respuesta sale por
    stderr (`_avisar`), este archivo no tiene un solo `print` a secas, y los de
    los módulos que se importan acá adentro los desvía `_canal_apartado`.
    """
    with _canal_apartado() as protocolo:
        try:
            telefono = _telefono_de_stdio()
        except MCPNoConfigurado as exc:
            _avisar(str(exc))
            return 2
        for linea in sys.stdin:
            linea = linea.strip()
            if not linea:
                continue
            try:
                mensaje = json.loads(linea)
            except json.JSONDecodeError:
                respuesta: dict | None = _error(None, ERROR_PARSE, "invalid JSON")
                protocolo.write(json.dumps(respuesta) + "\n")
                protocolo.flush()
                continue
            respuestas, _ = despachar_lote(mensaje, telefono)
            for respuesta in respuestas:
                protocolo.write(json.dumps(respuesta, allow_nan=False) + "\n")
            protocolo.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
