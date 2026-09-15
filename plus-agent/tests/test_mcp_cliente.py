"""El cliente de servidores MCP ajenos: que hable el protocolo que exigen.

POR QUÉ EL DOBLE ES UN SERVIDOR HTTP DE VERDAD Y NO UN MOCK
-----------------------------------------------------------
Lo que se prueba acá es CONFORMIDAD CON UN PROTOCOLO, y un mock que devuelve lo
que le pidan no puede estar en desacuerdo con el cliente sobre qué headers hay
que mandar — que es exactamente el bug que este archivo existe para que no
vuelva. El intento anterior usaba `langchain-mcp-adapters` sobre el SDK `mcp` y
NO CONECTABA contra Casys 3.0.4: su modo HTTP exige el protocolo `2026-07-28` y
le contesta 400 a un cliente que manda `2025-06-18`.

Así que el doble es un `ThreadingHTTPServer` que EXIGE el mismo contrato que se
midió preguntándole al servidor real:

    MCP-Protocol-Version: <version>      en toda petición
    Mcp-Method: <el método del cuerpo>   en toda petición
    Mcp-Name: <params.name>              sólo en tools/call
    params._meta["io.modelcontextprotocol/protocolVersion"]

Cada una de esas exigencias es un test, y cada test cae si el cliente deja de
mandar lo suyo. Los mensajes de error son los del servidor real, copiados de su
respuesta.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import mcp_cliente
from app.mcp_cliente import ClienteMCP, MCPExternoError

VERSION = "2026-07-28"
META_VERSION = "io.modelcontextprotocol/protocolVersion"

HERRAMIENTAS = [
    {"name": "erpnext_customer_list", "description": "Lista clientes.",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "erpnext_doc_submit", "description": "Emite cualquier documento.",
     "inputSchema": {"type": "object",
                     "properties": {"doctype": {"type": "string"},
                                    "name": {"type": "string"}}}},
]


class _Handler(BaseHTTPRequestHandler):
    """El contrato de Casys 3.0.4, tal como lo contestó el servidor real."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silencio en la salida de pytest
        pass

    def _responder(self, codigo: int, cuerpo: dict) -> None:
        datos = json.dumps(cuerpo).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def _falta(self, header: str, ident) -> None:
        self._responder(400, {"jsonrpc": "2.0", "id": ident, "error": {
            "code": -32020,
            "message": f"Missing required header '{header}'.",
        }})

    def do_POST(self):  # el nombre lo fija la stdlib
        largo = int(self.headers.get("Content-Length") or 0)
        peticion = json.loads(self.rfile.read(largo) or b"{}")
        ident = peticion.get("id")
        metodo = peticion.get("method")
        params = peticion.get("params") or {}
        self.server.vistas.append(dict(self.headers))  # type: ignore[attr-defined]

        if not self.headers.get("MCP-Protocol-Version"):
            return self._falta("MCP-Protocol-Version", ident)
        if self.headers.get("Mcp-Method") != metodo:
            return self._falta("Mcp-Method", ident)
        if (params.get("_meta") or {}).get(META_VERSION) is None:
            return self._responder(400, {"jsonrpc": "2.0", "id": ident, "error": {
                "code": -32020, "message": "Missing params._meta protocolVersion"}})

        if metodo == "initialize":
            return self._responder(200, {"jsonrpc": "2.0", "id": ident, "result": {
                "protocolVersion": VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "doble", "version": "3.0.4"},
            }})
        if metodo == "tools/list":
            return self._responder(200, {"jsonrpc": "2.0", "id": ident,
                                         "result": {"tools": HERRAMIENTAS}})
        if metodo == "tools/call":
            if self.headers.get("Mcp-Name") != params.get("name"):
                return self._falta("Mcp-Name", ident)
            return self._responder(200, {"jsonrpc": "2.0", "id": ident, "result": {
                "content": [{"type": "text", "text": f"ok:{params.get('name')}"}],
                "isError": False,
            }})
        return self._responder(200, {"jsonrpc": "2.0", "id": ident, "error": {
            "code": -32601, "message": "method not found"}})


@pytest.fixture
def servidor():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.vistas = []  # type: ignore[attr-defined]
    hilo = threading.Thread(target=srv.serve_forever, daemon=True)
    hilo.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def cliente(servidor):
    puerto = servidor.server_address[1]
    return ClienteMCP("erpnext", {
        "transporte": "http", "destino": f"http://127.0.0.1:{puerto}/mcp",
    })


@pytest.fixture(autouse=True)
def sin_cache(monkeypatch):
    mcp_cliente._cargadas = None
    mcp_cliente._clientes.clear()
    mcp_cliente._motivos.clear()
    monkeypatch.delenv("MCP_EXTERNOS_BLOQUEAR", raising=False)
    yield
    mcp_cliente._cargadas = None


# ------------------------------------------------------------ el protocolo

def test_el_handshake_completo_pasa_el_contrato_de_2026_07_28(cliente):
    """Si faltara UNO de los cuatro requisitos, el doble contesta 400.

    MUTACIÓN: sacar `"Mcp-Method": metodo` de `_cabeceras`. Cae éste y los que
    dependen de conectarse, y ninguno más.
    """
    cliente.conectar()
    assert cliente.protocolo == VERSION


def test_se_usa_la_version_que_dijo_el_servidor_y_no_la_que_pedimos(cliente):
    """El `initialize` es una negociación: contesta cuál va a usar.

    MUTACIÓN: no asignar `self.protocolo = acordada`. Cae éste y sólo éste.
    """
    cliente.protocolo = "2025-06-18"
    cliente.conectar()
    assert cliente.protocolo == VERSION


def test_tools_call_manda_el_header_con_el_nombre_de_la_herramienta(cliente, servidor):
    """`Mcp-Name` sólo lo exige `tools/call`, y el doble lo exige igual que Casys.

    MUTACIÓN: no pasar `nombre_herramienta` en `llamar`. Cae éste y sólo éste.
    """
    cliente.conectar()
    assert cliente.llamar("erpnext_customer_list", {}) == "ok:erpnext_customer_list"
    de_la_llamada = servidor.vistas[-1]
    assert de_la_llamada.get("Mcp-Name") == "erpnext_customer_list"


def test_el_meta_del_protocolo_viaja_en_cada_peticion(cliente, servidor):
    """Va en `params._meta`, no en el cuerpo raíz. El doble lo exige siempre."""
    cliente.conectar()
    cliente.listar()
    # Que las dos hayan pasado ya lo prueba: el doble corta con 400 si falta.
    assert len(servidor.vistas) == 2


def test_un_token_por_servidor_viaja_como_bearer(cliente, servidor, monkeypatch):
    """Casys autentica al cliente con su propio `MCP_AUTH_TOKEN`."""
    monkeypatch.setenv("MCP_EXTERNO_TOKEN_ERPNEXT", "secreto-largo")
    cliente.conectar()
    assert servidor.vistas[-1].get("Authorization") == "Bearer secreto-largo"


def test_sin_token_configurado_no_se_manda_un_authorization_vacio(cliente, servidor):
    cliente.conectar()
    assert "Authorization" not in servidor.vistas[-1]


# ------------------------------------------------------------ las herramientas

def test_se_cargan_TODAS_las_que_ofrece_el_servidor_incluidas_las_irreversibles(
    cliente, servidor, monkeypatch
):
    """Esta rama las carga todas, y eso es la decisión del dueño.

    `erpnext_doc_submit` está en la lista a propósito: lo que pueda hacer de
    verdad lo decide la credencial de ERPNext de ESE servidor, no este filtro.

    MUTACIÓN: filtrar `*_submit` por defecto. Cae éste y sólo éste.
    """
    puerto = servidor.server_address[1]
    monkeypatch.setenv("MCP_EXTERNOS", f"erpnext=http://127.0.0.1:{puerto}/mcp")

    nombres = [h.name for h in mcp_cliente.cargar([])]

    assert nombres == ["erpnext_customer_list", "erpnext_doc_submit"]


def test_lo_bloqueado_por_patron_no_llega_al_modelo(cliente, servidor, monkeypatch):
    """La línea que la volvería reversible, escrita en el `.env` y no en código."""
    puerto = servidor.server_address[1]
    monkeypatch.setenv("MCP_EXTERNOS", f"erpnext=http://127.0.0.1:{puerto}/mcp")
    monkeypatch.setenv("MCP_EXTERNOS_BLOQUEAR", "*_submit,*_cancel,*_delete")

    nombres = [h.name for h in mcp_cliente.cargar([])]

    assert nombres == ["erpnext_customer_list"]


def test_un_nombre_repetido_no_tapa_una_herramienta_nuestra(servidor, monkeypatch):
    """Si se cargara, gana la última en el ToolNode y el agente creería estar
    llamando a la que tiene las guardas puestas."""
    puerto = servidor.server_address[1]
    monkeypatch.setenv("MCP_EXTERNOS", f"erpnext=http://127.0.0.1:{puerto}/mcp")

    class Propia:
        name = "erpnext_customer_list"

    nombres = [h.name for h in mcp_cliente.cargar([Propia()])]

    assert nombres == ["erpnext_doc_submit"]
    assert any("ya existe" in m for m in mcp_cliente._motivos)


def test_el_resumen_NOMBRA_las_irreversibles_que_quedaron_cargadas(servidor, monkeypatch):
    """Que estén es una decisión; que no se vean sería un accidente.

    MUTACIÓN: sacar el bloque `IRREVERSIBLES cargadas`. Cae éste y sólo éste.
    """
    puerto = servidor.server_address[1]
    monkeypatch.setenv("MCP_EXTERNOS", f"erpnext=http://127.0.0.1:{puerto}/mcp")

    texto = mcp_cliente.resumen([])

    assert "IRREVERSIBLES" in texto
    assert "erpnext_doc_submit" in texto


def test_una_herramienta_que_falla_vuelve_como_resultado_y_no_como_excepcion(
    cliente, servidor, monkeypatch
):
    """Una excepción deja un AIMessage sin su ToolMessage y rompe el hilo entero.

    MUTACIÓN: dejar propagar `MCPExternoError` en `_envolver`. Cae éste y sólo
    éste.
    """
    puerto = servidor.server_address[1]
    monkeypatch.setenv("MCP_EXTERNOS", f"erpnext=http://127.0.0.1:{puerto}/mcp")
    herramientas = {h.name: h for h in mcp_cliente.cargar([])}
    # Se le corta el destino en vez de bajar el servidor: `shutdown()` frena el
    # bucle y NO cierra el socket —el mismo detalle que una revisión encontró en
    # `sim_banco.py`—, así que una conexión reutilizada por httpx seguía siendo
    # atendida y el test pasaba sin probar nada.
    mcp_cliente._clientes["erpnext"].destino = "http://127.0.0.1:1/mcp"

    salida = herramientas["erpnext_customer_list"].invoke({})

    assert "no contestó" in salida
    assert "No cambié nada" in salida


def test_un_servidor_que_no_levanta_no_tumba_el_agente(monkeypatch):
    """Un ERP de terceros caído es un martes; un agente mudo es el negocio parado."""
    monkeypatch.setenv("MCP_EXTERNOS", "erpnext=http://127.0.0.1:1/mcp")

    assert mcp_cliente.cargar([]) == []
    assert any("no pude conectarme" in m for m in mcp_cliente._motivos)


# ------------------------------------------------------------ configuración

def test_sin_la_variable_no_hay_superficie_externa(monkeypatch):
    monkeypatch.delenv("MCP_EXTERNOS", raising=False)
    assert mcp_cliente.servidores() == {}
    assert mcp_cliente.cargar([]) == []


def test_una_url_es_http_y_un_comando_es_stdio(monkeypatch):
    monkeypatch.setenv("MCP_EXTERNOS", "a=https://x.example/mcp,b=npx -y algo")
    configuracion = mcp_cliente.servidores()
    assert configuracion["a"]["transporte"] == "http"
    assert configuracion["b"]["transporte"] == "stdio"
    assert configuracion["b"]["destino"] == "npx -y algo"


def test_una_entrada_rota_se_rechaza_fuerte(monkeypatch):
    """Una entrada que se descarta callada es un servidor que no está."""
    monkeypatch.setenv("MCP_EXTERNOS", "solo_nombre")
    with pytest.raises(MCPExternoError):
        mcp_cliente.servidores()


def test_nuestro_endpoint_MCP_no_republica_una_herramienta_ajena():
    """`mcp_server._catalogo()` lee `TOOLS_GERENCIA`, que no se toca.

    Si el cableado sumara las externas a ESA constante en vez de a
    `TOOLS_AGENTE_GERENCIA`, nuestro endpoint autenticado pasaría a servir el
    `erpnext_doc_submit` de un tercero con nuestro token y bajo nuestro nombre.
    """
    from app import graph, mcp_server

    publicadas = {h["name"] for h in mcp_server.listar()}
    assert publicadas == {h.name for h in graph.TOOLS_GERENCIA}
    assert not any(n.startswith("erpnext_") for n in publicadas)


def test_la_lista_del_agente_no_es_el_mismo_objeto_que_la_publicada():
    from app import graph

    assert graph.TOOLS_AGENTE_GERENCIA is not graph.TOOLS_GERENCIA
