"""Herramientas de OTROS servidores MCP, adentro del agente de gerencia.

PARA QUÉ EXISTE ESTE ARCHIVO
----------------------------
`app/mcp_server.py` es la puerta de SALIDA: cualquier harness se conecta y usa
nuestras herramientas. Éste es la de ENTRADA: el agente de gerencia usa las
herramientas de un servidor MCP de terceros. El caso concreto es
`@casys/mcp-erpnext`, que trae 125 herramientas de ERPNext y nueve visores
interactivos que nadie de este lado va a escribir.

ESTA RAMA LAS CARGA TODAS, Y ESO INCLUYE LAS QUE MUEVEN PLATA
--------------------------------------------------------------
`MCP_EXTERNOS_BLOQUEAR` vacío = no se filtra nada, y ése es el default acá por
decisión del dueño: quiere la superficie entera, precios incluidos. Lo que eso
carga, medido contra el servidor real (v3.0.4): 125 herramientas, 35 que
escriben, y entre ellas `erpnext_doc_submit`, `erpnext_doc_cancel`,
`erpnext_doc_delete` y `erpnext_doc_create` —que es genérico sobre CUALQUIER
doctype, o sea que alcanza `Item Price`—.

Lo que decide qué puede pasar de verdad NO es esta lista: es **con qué usuario
de ERPNext arrancó ese contenedor**, porque un servidor MCP de terceros actúa
con UNA credencial y no tiene permisos por herramienta. Esa decisión vive en el
`.env` del otro contenedor, fuera del alcance de `app/readiness.py`, y por eso
`resumen()` existe: para que al menos se vea desde acá qué se cargó.

`MCP_EXTERNOS_BLOQUEAR` sigue estando por si se la quiere acotar sin tocar
código —`*_submit,*_cancel,*_delete` es la línea que la volvería reversible—,
pero vacío es vacío y no hay filtro escondido.

SIN SDK, Y NO POR AHORRAR UNA DEPENDENCIA
------------------------------------------
Esto habla el protocolo directo, con `httpx`. El intento anterior usaba
`langchain-mcp-adapters` sobre el SDK `mcp` de Python y **no conectaba**: el
modo HTTP de Casys 3.0.4 exige el protocolo `2026-07-28` y le contesta 400 a un
cliente que manda `2025-06-18`, que es lo que habla el SDK. Medido, no leído.

El contrato nuevo, aprendido preguntándole al servidor:
  · `MCP-Protocol-Version: <version>`
  · `Mcp-Method: <el mismo método del cuerpo>`
  · `Mcp-Name: <params.name>`, sólo en `tools/call`
  · `params._meta["io.modelcontextprotocol/protocolVersion"]` y
    `…/clientCapabilities`

SE MANDA EL SUPERCONJUNTO y no se negocia a mano: un servidor viejo ignora los
headers que no conoce y las claves de `_meta` que no conoce —`_meta` está
reservado para exactamente eso—, y uno nuevo los exige. Después del
`initialize` se usa la versión que el servidor devolvió.

De paso desaparecen 12 paquetes de la imagen y el bucle de eventos en un hilo
de fondo: `httpx` es síncrono y todo nuestro runtime también.
"""
from __future__ import annotations

import fnmatch
import json
import os
import selectors
import subprocess
import threading
import time
from typing import Any

import httpx
from langchain_core.tools import StructuredTool

# La que pedimos. El servidor contesta cuál va a usar y desde ahí se usa ésa.
PROTOCOLO_PREFERIDO = "2026-07-28"

TIMEOUT_SEGUNDOS = 60.0
MAX_RESULTADO = 60_000

_META_VERSION = "io.modelcontextprotocol/protocolVersion"
_META_CAPS = "io.modelcontextprotocol/clientCapabilities"


class MCPExternoError(RuntimeError):
    """La configuración de un servidor externo no se puede interpretar."""


# ------------------------------------------------------------ configuración

def servidores() -> dict[str, dict]:
    """`MCP_EXTERNOS` -> {nombre: {transporte, destino, token}}.

    Formato, una entrada por servidor separadas por coma:

        nombre=comando arg1 arg2        -> stdio
        nombre=http://host:puerto/mcp   -> http

    Se eligió por cómo se escribe en un `.env` de una línea. Un JSON acá sería
    más expresivo y se rompe al primer paste con comillas.
    """
    crudo = os.getenv("MCP_EXTERNOS", "").strip()
    if not crudo:
        return {}
    salida: dict[str, dict] = {}
    for posicion, entrada in enumerate(crudo.split(","), start=1):
        entrada = entrada.strip()
        if not entrada:
            continue
        nombre, _, destino = entrada.partition("=")
        nombre, destino = nombre.strip(), destino.strip()
        if not nombre or not destino:
            raise MCPExternoError(
                f"la entrada {posicion} de MCP_EXTERNOS no tiene «nombre=destino»"
            )
        if destino.startswith(("http://", "https://")):
            salida[nombre] = {"transporte": "http", "destino": destino}
        else:
            salida[nombre] = {"transporte": "stdio", "destino": destino}
    return salida


def token_de(nombre: str) -> str:
    """`MCP_EXTERNO_TOKEN_<NOMBRE>`: el bearer de ESE servidor, si lo pide.

    Casys autentica al cliente con `MCP_AUTH_TOKEN`. Es del servidor externo y
    no nuestro, así que va por servidor y no en una variable sola.
    """
    return os.getenv(f"MCP_EXTERNO_TOKEN_{nombre.upper()}", "").strip()


def patrones_bloqueados() -> list[str]:
    """`MCP_EXTERNOS_BLOQUEAR`: nombres que no se cargan. Acepta comodines.

    Vacío = no se bloquea nada, y es el default: este archivo no decide la
    política del negocio, la hace escribible en una línea que se lee.
    """
    crudo = os.getenv("MCP_EXTERNOS_BLOQUEAR", "")
    return [p.strip() for p in crudo.split(",") if p.strip()]


# Lo único que un proceso de terceros necesita del entorno para arrancar. Todo
# lo demás se pasa por `MCP_EXTERNO_ENV_<NOMBRE>`, que nombra las variables
# permitidas para ESE servidor.
_DEL_SISTEMA = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR")


def entorno_para(nombre: str) -> dict[str, str]:
    """El entorno de un servidor stdio. NO es `os.environ`.

    Era `env=dict(os.environ)`, o sea que cualquier comando configurado como
    servidor MCP arrancaba con TODAS nuestras credenciales adentro: las tres
    claves de ERPNext, el token de WhatsApp, la del modelo, la URL de Redis y
    los tokens del panel y de MCP. Un servidor MCP es código de otro que corre
    en nuestro proceso hijo; no tiene por qué ver nada de eso.

    Lo que pasa: seis variables del sistema —las que hacen falta para que un
    proceso arranque y sepa dónde está— y NADA más, salvo lo que el dueño
    nombre explícitamente en `MCP_EXTERNO_ENV_<NOMBRE>` (una lista de nombres
    de variable separados por coma). Nombres, no valores: el valor se sigue
    tomando del entorno, así que un secreto no se escribe dos veces.
    """
    salida = {k: os.environ[k] for k in _DEL_SISTEMA if k in os.environ}
    crudo = os.getenv(f"MCP_EXTERNO_ENV_{nombre.upper()}", "")
    for pedida in (v.strip() for v in crudo.split(",")):
        if pedida and pedida in os.environ:
            salida[pedida] = os.environ[pedida]
    return salida


def _bloqueada(nombre: str, patrones: list[str]) -> bool:
    return any(fnmatch.fnmatch(nombre, patron) for patron in patrones)


# ------------------------------------------------------- el cliente, directo

class ClienteMCP:
    """Un servidor MCP, hablado directo. Síncrono, sin SDK.

    No es un cliente completo del protocolo: hace `initialize`, `tools/list` y
    `tools/call`, que es todo lo que hace falta para usar herramientas ajenas.
    No implementa recursos, prompts, sampling ni elicitation — y cuando haga
    falta alguno, se agrega acá y no en doce paquetes.
    """

    def __init__(self, nombre: str, configuracion: dict):
        self.nombre = nombre
        self.transporte = configuracion["transporte"]
        self.destino = configuracion["destino"]
        self.protocolo = PROTOCOLO_PREFERIDO
        self._siguiente_id = 0
        self._candado = threading.Lock()
        self._http: httpx.Client | None = None
        self._proceso: subprocess.Popen | None = None

    # -- transporte -------------------------------------------------------

    def _cabeceras(self, metodo: str, nombre_herramienta: str = "") -> dict:
        cabeceras = {
            "Content-Type": "application/json",
            # Los DOS: la especificación deja al servidor elegir entre una
            # respuesta JSON y un stream de eventos, así que el cliente tiene
            # que declarar que acepta las dos.
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocolo,
            "Mcp-Method": metodo,
        }
        if nombre_herramienta:
            cabeceras["Mcp-Name"] = nombre_herramienta
        token = token_de(self.nombre)
        if token:
            cabeceras["Authorization"] = f"Bearer {token}"
        return cabeceras

    def _cuerpo(self, metodo: str, params: dict | None) -> dict:
        self._siguiente_id += 1
        parametros = dict(params or {})
        parametros["_meta"] = {
            _META_VERSION: self.protocolo,
            _META_CAPS: {},
        }
        return {
            "jsonrpc": "2.0",
            "id": self._siguiente_id,
            "method": metodo,
            "params": parametros,
        }

    def _por_http(self, cuerpo: dict, cabeceras: dict) -> dict:
        if self._http is None:
            self._http = httpx.Client(timeout=TIMEOUT_SEGUNDOS)
        respuesta = self._http.post(self.destino, json=cuerpo, headers=cabeceras)
        if respuesta.status_code >= 400:
            raise MCPExternoError(
                f"{self.nombre} rechazó {cuerpo['method']} "
                f"(estado {respuesta.status_code})"
            )
        texto = respuesta.text
        # Si contestó un stream de eventos en vez de JSON, el mensaje viaja en
        # la primera línea `data:`. Es la otra mitad del `Accept` de arriba.
        if texto.lstrip().startswith("event:") or texto.lstrip().startswith("data:"):
            for linea in texto.splitlines():
                if linea.startswith("data:"):
                    texto = linea[5:].strip()
                    break
        return json.loads(texto)

    def _matar(self) -> None:
        """Deja el proceso muerto y olvidado, para que el próximo lo rearranque."""
        proceso, self._proceso = self._proceso, None
        if proceso is None:
            return
        try:
            proceso.kill()
            proceso.wait(timeout=5)
        except Exception:
            # Ya lo estamos tirando: lo que falle acá no cambia nada.
            pass

    def _por_stdio(self, cuerpo: dict) -> dict:
        if self._proceso is None:
            partes = self.destino.split()
            self._proceso = subprocess.Popen(
                partes, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
                env=entorno_para(self.nombre),
            )
        proceso = self._proceso
        if proceso.stdin is None or proceso.stdout is None:
            raise MCPExternoError(f"{self.nombre} no tiene stdin/stdout")
        try:
            proceso.stdin.write(json.dumps(cuerpo) + "\n")
            proceso.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            # El hijo se murió. Sin esto el `Popen` muerto quedaba cacheado
            # para siempre y el servidor stdio no volvía hasta reiniciar el
            # contenedor entero; además el proceso nunca se recogía.
            self._matar()
            raise MCPExternoError(f"{self.nombre} cerró la entrada") from exc
        # El servidor puede escribir notificaciones antes de la respuesta; se
        # lee hasta encontrar la que lleva NUESTRO id.
        #
        # CON FECHA LÍMITE, y no sólo con un tope de mensajes. `readline()` no
        # tiene timeout: un proceso VIVO que deja de contestar —no uno que se
        # cerró, que devuelve ""— colgaba esta llamada para siempre, y con ella
        # todas las que esperan `self._candado`. El tope de 200 no ayudaba: se
        # trababa en el primero.
        limite = time.monotonic() + TIMEOUT_SEGUNDOS
        selector = selectors.DefaultSelector()
        selector.register(proceso.stdout, selectors.EVENT_READ)
        try:
            for _ in range(200):
                queda = limite - time.monotonic()
                if queda <= 0 or not selector.select(queda):
                    self._matar()
                    raise MCPExternoError(
                        f"{self.nombre} no contestó a {cuerpo['method']} en "
                        f"{TIMEOUT_SEGUNDOS:g}s"
                    )
                linea = proceso.stdout.readline()
                if not linea:
                    self._matar()
                    raise MCPExternoError(f"{self.nombre} cerró la salida")
                try:
                    mensaje = json.loads(linea)
                except json.JSONDecodeError:
                    continue
                if mensaje.get("id") == cuerpo["id"]:
                    return mensaje
        finally:
            selector.close()
        raise MCPExternoError(f"{self.nombre} no contestó a {cuerpo['method']}")

    def pedir(self, metodo: str, params: dict | None = None,
              nombre_herramienta: str = "") -> dict:
        """Un pedido, su respuesta. Serializado: un proceso stdio es uno solo."""
        with self._candado:
            cuerpo = self._cuerpo(metodo, params)
            if self.transporte == "http":
                mensaje = self._por_http(cuerpo, self._cabeceras(metodo, nombre_herramienta))
            else:
                mensaje = self._por_stdio(cuerpo)
        if "error" in mensaje:
            detalle = (mensaje["error"] or {}).get("message", "sin detalle")
            raise MCPExternoError(f"{self.nombre}: {detalle}")
        resultado = mensaje.get("result")
        if not isinstance(resultado, dict):
            raise MCPExternoError(f"{self.nombre} contestó algo que no es un resultado")
        return resultado

    # -- protocolo --------------------------------------------------------

    def conectar(self) -> None:
        resultado = self.pedir("initialize", {
            "protocolVersion": self.protocolo,
            "capabilities": {},
            "clientInfo": {"name": "plus-agent", "version": "1.0.0"},
        })
        # La versión que el servidor DIJO que va a usar, no la que pedimos.
        acordada = str(resultado.get("protocolVersion") or "").strip()
        if acordada:
            self.protocolo = acordada

    def listar(self) -> list[dict]:
        resultado = self.pedir("tools/list")
        herramientas = resultado.get("tools")
        return [h for h in herramientas or [] if isinstance(h, dict)]

    def llamar(self, nombre: str, argumentos: dict) -> str:
        resultado = self.pedir(
            "tools/call", {"name": nombre, "arguments": dict(argumentos or {})},
            nombre_herramienta=nombre,
        )
        partes = []
        for bloque in resultado.get("content") or []:
            if isinstance(bloque, dict) and bloque.get("type") == "text":
                partes.append(str(bloque.get("text") or ""))
        texto = "\n".join(partes) or json.dumps(resultado, default=str)
        if resultado.get("isError"):
            texto = f"[{nombre} falló] {texto}"
        return texto[:MAX_RESULTADO]


# ------------------------------------------------------------- las herramientas

def _envolver(cliente: ClienteMCP, descriptor: dict) -> StructuredTool:
    """Un descriptor de MCP -> una herramienta de LangChain.

    `args_schema` se pasa como el DICT que mandó el otro servidor: es JSON
    Schema y StructuredTool lo acepta así. Quien lea un esquema tiene que
    aguantar las dos formas —dict acá, modelo de pydantic en las nuestras—.
    """
    nombre = str(descriptor.get("name") or "")
    esquema = descriptor.get("inputSchema")
    if not isinstance(esquema, dict) or esquema.get("type") != "object":
        esquema = {"type": "object", "properties": {}}

    def _llamar(**argumentos: Any) -> str:
        try:
            return cliente.llamar(nombre, argumentos)
        except Exception as exc:
            # `Exception` Y NO `MCPExternoError`, que es lo que decía y estaba
            # mal: el servidor ajeno está del otro lado de la red, así que un
            # `httpx.ConnectError` —o un `JSONDecodeError` si contesta basura—
            # es el caso NORMAL de un contenedor caído, y ésos no son
            # `MCPExternoError`. Se escapaban, y una excepción que sale de una
            # herramienta deja un AIMessage sin su ToolMessage: ese hilo de
            # conversación no se puede volver a contestar hasta que alguien
            # borre Redis a mano. Mismo motivo que `graph.handle_tool_errors`.
            # Lo encontró su propio test.
            return f"Esa herramienta no contestó ({type(exc).__name__}). No cambié nada."

    return StructuredTool(
        name=nombre,
        description=str(descriptor.get("description") or "").strip(),
        args_schema=esquema,
        func=_llamar,
    )


_cargadas: list[StructuredTool] | None = None
_motivos: list[str] = []
_clientes: dict[str, ClienteMCP] = {}


def cargar(propias: list[Any] | None = None) -> list[StructuredTool]:
    """Las herramientas externas, una vez por proceso.

    ``propias`` son las que el agente ya tiene: un nombre repetido NO se carga.
    Sin esto, una herramienta ajena que se llame igual que una nuestra la tapa
    en el `ToolNode` —gana la última— y el agente creería estar llamando a la
    que tiene las guardas puestas.
    """
    global _cargadas
    if _cargadas is not None:
        return _cargadas

    configuracion = servidores()
    if not configuracion:
        _cargadas = []
        return _cargadas

    ya_existen = {h.name for h in (propias or [])}
    patrones = patrones_bloqueados()
    salida: list[StructuredTool] = []
    _motivos.clear()
    for nombre, ajustes in configuracion.items():
        cliente = ClienteMCP(nombre, ajustes)
        try:
            cliente.conectar()
            descriptores = cliente.listar()
        except Exception as exc:
            _motivos.append(f"{nombre}: no pude conectarme ({type(exc).__name__})")
            continue
        _clientes[nombre] = cliente
        for descriptor in descriptores:
            suyo = str(descriptor.get("name") or "")
            if not suyo:
                continue
            if suyo in ya_existen:
                _motivos.append(f"{suyo}: ya existe en el agente")
                continue
            if _bloqueada(suyo, patrones):
                _motivos.append(f"{suyo}: bloqueada por MCP_EXTERNOS_BLOQUEAR")
                continue
            salida.append(_envolver(cliente, descriptor))
            ya_existen.add(suyo)
    _cargadas = salida
    return _cargadas


def resumen(propias: list[Any] | None = None) -> str:
    """Qué se cargó y qué no. Para `readiness` y para el log de arranque.

    Existe porque lo que un servidor externo trae NO está escrito en este repo:
    lo decide su versión y sus `--categories`. Sin una línea que lo diga, el
    catálogo del agente cambia cuando alguien actualiza el otro contenedor y
    acá no se entera nadie.
    """
    configuracion = servidores()
    if not configuracion:
        return "MCP externos: ninguno configurado."
    herramientas = cargar(propias)
    lineas = [
        f"MCP externos: {len(configuracion)} servidor(es), "
        f"{len(herramientas)} herramientas cargadas."
    ]
    for nombre, ajustes in configuracion.items():
        cliente = _clientes.get(nombre)
        version = f" (protocolo {cliente.protocolo})" if cliente else ""
        lineas.append(f"  · {nombre}: {ajustes['destino']}{version}")
    escriben = [h.name for h in herramientas
                if any(v in h.name for v in ("submit", "cancel", "delete"))]
    if escriben:
        lineas.append(
            "  · IRREVERSIBLES cargadas: " + ", ".join(sorted(escriben)) +
            " — lo que puedan hacer lo decide la credencial de ERPNext de ESE "
            "servidor, no esta lista."
        )
    for motivo in _motivos:
        lineas.append(f"  · NO cargada — {motivo}")
    return "\n".join(lineas)
