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

import itertools
import json
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import mcp_cliente
from app.mcp_cliente import ClienteMCP, MCPExternoError, _bloqueada

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
            # Lo que tarda se lo pone el TEST al servidor: un doble que decide
            # solo cuánto demora no puede estar en desacuerdo con el cliente
            # sobre qué timeout le tocaba a esta llamada.
            time.sleep(self.server.demora)  # type: ignore[attr-defined]
            return self._responder(200, {"jsonrpc": "2.0", "id": ident, "result": {
                "content": [{"type": "text", "text": f"ok:{params.get('name')}"}],
                "isError": False,
            }})
        return self._responder(200, {"jsonrpc": "2.0", "id": ident, "error": {
            "code": -32601, "message": "method not found"}})


class _Mudo(BaseHTTPRequestHandler):
    """Acepta la conexión, recibe el pedido y no contesta nunca.

    No es un puerto cerrado: eso falla al conectar, en milisegundos, y no prueba
    ningún timeout. Un ERP de terceros caído de verdad suele ser esto — el
    proceso vivo, el socket abierto y nadie del otro lado.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silencio en la salida de pytest
        pass

    def do_POST(self):  # el nombre lo fija la stdlib
        largo = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(largo)
        time.sleep(self.server.mudez)  # type: ignore[attr-defined]


@pytest.fixture
def servidor():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.vistas = []  # type: ignore[attr-defined]
    srv.demora = 0.0  # type: ignore[attr-defined]
    hilo = threading.Thread(target=srv.serve_forever, daemon=True)
    hilo.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def servidor_mudo():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Mudo)
    # Más que cualquier timeout de este archivo: lo que se mide es cuánto espera
    # el cliente, no cuándo se cansa el servidor.
    srv.mudez = 10.0  # type: ignore[attr-defined]
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
    monkeypatch.delenv("MCP_EXTERNOS_HTTP_INTERNOS", raising=False)
    yield
    # Un servidor stdio que descubrió `cargar()` es un proceso hijo VIVO, y el
    # cliente que lo tiene no lo devuelve nadie: sin esto queda corriendo.
    for sobrante in mcp_cliente._clientes.values():
        sobrante._matar()
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


# ------------------------------------------- el default de .env.example

def test_el_bloqueo_por_defecto_saca_modulos_enteros_y_ningun_poder():
    """`.env.example` trae una lista, no un vacío, y esto dice qué promete.

    La línea existe porque un tercio de las 125 de Casys son de RRHH, nómina,
    activos, manufactura y proyectos —cosas que una distribuidora no llama
    nunca— y cada una viaja en el prompt de CADA turno. Lo que este test
    protege es que ese recorte no se coma un poder: el dueño pidió
    explícitamente submit, cancel y poder tocar precios.

    Los dos lados se escriben acá y NO se importan del código: contra una
    constante compartida, las dos mitades del assert se mueven juntas y alguien
    puede agregar `*_submit` al default sin que nada se entere.

    MUTACIÓN: agregarle `erpnext_doc_submit` (o `*_submit`) a la línea de
    `.env.example`. Cae éste y sólo éste.
    """
    import pathlib

    linea = ""
    for cruda in (pathlib.Path(__file__).resolve().parents[1] / ".env.example").read_text().splitlines():
        if cruda.startswith("MCP_EXTERNOS_BLOQUEAR="):
            linea = cruda.split("=", 1)[1].strip()
    assert linea, "el default quedó vacío: .env.example ya no recorta nada"
    patrones = [p.strip() for p in linea.split(",") if p.strip()]

    # Lo que SÍ tiene que sacar: los módulos que este negocio no usa.
    for ajeno in ("erpnext_payroll_entry_list", "erpnext_salary_slip_get",
                  "erpnext_leave_application_create", "erpnext_timesheet_list",
                  "erpnext_asset_create", "erpnext_work_order_create",
                  "erpnext_bom_list", "erpnext_project_create"):
        assert _bloqueada(ajeno, patrones), f"{ajeno} debería estar bloqueada"

    # Lo que NO puede sacar: los poderes que el dueño pidió.
    for poder in ("erpnext_doc_submit", "erpnext_doc_cancel",
                  "erpnext_sales_order_submit", "erpnext_sales_order_cancel",
                  "erpnext_item_update", "erpnext_sales_order_create",
                  "erpnext_payment_entry_list", "erpnext_stock_balance",
                  "erpnext_ar_aging", "erpnext_customer_get"):
        assert not _bloqueada(poder, patrones), (
            f"{poder} es un poder que el dueño pidió y el default se lo comió"
        )


# ------------------------------------------- el entorno de un proceso stdio

def test_un_servidor_stdio_no_hereda_nuestras_credenciales(monkeypatch):
    """Era `env=dict(os.environ)`, o sea todo lo que tenemos.

    Un servidor MCP por stdio es código de otro corriendo como hijo nuestro. Con
    el entorno entero adentro veía las tres claves de ERPNext, el token de
    WhatsApp, la del modelo, la URL de Redis y los tokens del panel y de MCP —
    ninguna de las cuales necesita para hablar el protocolo.

    MUTACIÓN: volver `entorno_para` a `dict(os.environ)`. Cae ésta y sólo ésta.
    """
    from app.mcp_cliente import entorno_para

    monkeypatch.setenv("ERPNEXT_API_SECRET", "no-se-comparte")
    monkeypatch.setenv("WHATSAPP_TOKEN", "tampoco")
    monkeypatch.setenv("PATH", "/usr/bin")

    entorno = entorno_para("erpnext")

    assert "ERPNEXT_API_SECRET" not in entorno
    assert "WHATSAPP_TOKEN" not in entorno
    # Y lo mínimo para que un proceso arranque sí está.
    assert entorno["PATH"] == "/usr/bin"


def test_el_dueno_puede_nombrar_lo_que_ese_servidor_sí_necesita(monkeypatch):
    """Nombres de variable, no valores: el secreto no se escribe dos veces.

    Sin esto el arreglo rompería cualquier servidor stdio que se configure por
    entorno, que es como se configuran casi todos.

    MUTACIÓN: ignorar `MCP_EXTERNO_ENV_<NOMBRE>`. Cae ésta y sólo ésta.
    """
    from app.mcp_cliente import entorno_para

    monkeypatch.setenv("TOKEN_DEL_OTRO", "abc123")
    monkeypatch.setenv("ERPNEXT_API_SECRET", "no-se-comparte")
    monkeypatch.setenv("MCP_EXTERNO_ENV_OTRO", "TOKEN_DEL_OTRO")

    entorno = entorno_para("otro")

    assert entorno["TOKEN_DEL_OTRO"] == "abc123"
    # Nombrar una no abre las demás.
    assert "ERPNEXT_API_SECRET" not in entorno
    # Y el permiso es POR SERVIDOR: el de al lado no lo hereda.
    assert "TOKEN_DEL_OTRO" not in entorno_para("erpnext")


# --------------------------------------- un servidor stdio que es un proceso
#
# Acá no alcanza un doble en este mismo proceso: de lo único que hablan estos
# tests es de CUÁNDO llegan los bytes —media línea, dos escrituras, un goteo—, y
# eso lo decide un pipe de verdad con un proceso de verdad del otro lado.
#
# Cada guión DERIVA lo que contesta del pedido que recibió —el id y la marca que
# le mandamos—: un doble que escribe siempre lo mismo no puede estar en
# desacuerdo con el cliente sobre cuál era nuestro mensaje.

_HIJO_MEDIA_LINEA = r"""
import json, os, sys
marca = sys.argv[1]
pedido = json.loads(sys.stdin.buffer.readline())
respuesta = json.dumps({"jsonrpc": "2.0", "id": pedido["id"],
                        "result": {"eco": pedido["params"]["marca"]}}).encode()
if os.path.exists(marca):
    sys.stdout.buffer.write(respuesta + b"\n")
else:
    open(marca, "w").close()
    sys.stdout.buffer.write(respuesta[:len(respuesta) // 2])  # y se calla, sin \n
sys.stdout.buffer.flush()
sys.stdin.buffer.read()   # vivo y mudo hasta que lo maten
"""

_HIJO_PARTIDO = r"""
import json, sys, time
pedido = json.loads(sys.stdin.buffer.readline())
respuesta = json.dumps({"jsonrpc": "2.0", "id": pedido["id"],
                        "result": {"eco": pedido["params"]["marca"]}}).encode()
corte = len(respuesta) // 2
sys.stdout.buffer.write(respuesta[:corte])
sys.stdout.buffer.flush()
time.sleep(float(sys.argv[1]))
sys.stdout.buffer.write(respuesta[corte:] + b"\n")
sys.stdout.buffer.flush()
sys.stdin.buffer.read()
"""

_HIJO_GOTEO = r"""
import json, sys, time
pedido = json.loads(sys.stdin.buffer.readline())
respuesta = json.dumps({"jsonrpc": "2.0", "id": pedido["id"],
                        "result": {"eco": pedido["params"]["marca"]}}).encode()
for byte in respuesta:
    sys.stdout.buffer.write(bytes([byte]))
    sys.stdout.buffer.flush()
    time.sleep(float(sys.argv[1]))
sys.stdin.buffer.read()   # el \n no llega nunca
"""

_HIJO_PEGADO = r"""
import json, sys
pedido = json.loads(sys.stdin.buffer.readline())
aviso = json.dumps({"jsonrpc": "2.0", "method": "notifications/message",
                    "params": {"data": "trabajando"}}).encode()
respuesta = json.dumps({"jsonrpc": "2.0", "id": pedido["id"],
                        "result": {"eco": pedido["params"]["marca"]}}).encode()
sys.stdout.buffer.write(aviso + b"\n" + respuesta + b"\n")   # UNA sola escritura
sys.stdout.buffer.flush()
sys.stdin.buffer.read()
"""

_HIJO_SE_VA = r"""
import sys
sys.stdin.buffer.readline()   # lee el pedido y se va sin contestar
"""

_HIJO_SERVIDOR = r"""
import json, sys, time
demora = float(sys.argv[1])
while True:
    linea = sys.stdin.buffer.readline()
    if not linea:
        break
    pedido = json.loads(linea)
    metodo = pedido["method"]
    if metodo == "initialize":
        resultado = {"protocolVersion": pedido["params"]["protocolVersion"],
                     "capabilities": {}, "serverInfo": {"name": "hijo"}}
    elif metodo == "tools/list":
        resultado = {"tools": [{"name": "hijo_lento", "description": "Tarda.",
                                "inputSchema": {"type": "object",
                                                "properties": {}}}]}
    else:
        time.sleep(demora)   # el informe pesado de ERPNext
        resultado = {"content": [{"type": "text",
                                  "text": "ok:" + pedido["params"]["name"]}]}
    sys.stdout.buffer.write(json.dumps(
        {"jsonrpc": "2.0", "id": pedido["id"], "result": resultado}).encode() + b"\n")
    sys.stdout.buffer.flush()
"""


@pytest.fixture
def hijo(tmp_path):
    """Levanta un guión como servidor stdio y lo deja muerto al terminar."""
    hechos: list[ClienteMCP] = []
    numero = itertools.count()

    def _armar(guion: str, *argumentos: object) -> ClienteMCP:
        ruta = tmp_path / f"hijo{next(numero)}.py"
        ruta.write_text(textwrap.dedent(guion))
        destino = " ".join([sys.executable, str(ruta), *(str(a) for a in argumentos)])
        cliente = ClienteMCP("hijo", {"transporte": "stdio", "destino": destino})
        hechos.append(cliente)
        return cliente

    yield _armar
    for cliente in hechos:
        cliente._matar()


def test_media_linea_y_silencio_no_cuelga_la_llamada_ni_deja_el_cliente_trabado(
    hijo, tmp_path
):
    """`select()` prueba que hay UN byte, no una línea entera.

    Un hijo que escribe `{"jsonrpc"` y se calla dejaba a `readline()` esperando
    el `\\n` para siempre, y esperando adentro de `_candado`: con él quedaban
    colgadas también todas las llamadas que vinieran después a ese servidor.

    MUTACIÓN: sacar `self._matar()` de la rama del timeout de `_por_stdio`. El
    hijo trabado queda cacheado, la segunda llamada le vuelve a escribir al
    mismo mudo y también se cae. Cae éste y sólo éste.
    """
    cliente = hijo(_HIJO_MEDIA_LINEA, tmp_path / "ya-conteste-a-medias")
    cliente.timeout = 1.0

    arranque = time.monotonic()
    with pytest.raises(MCPExternoError) as caida:
        cliente.pedir("prueba/eco", {"marca": "uno"})
    tardanza = time.monotonic() - arranque

    assert "no contestó" in str(caida.value)
    assert tardanza < 4.0, "esperó mucho más que su fecha límite"
    assert not cliente._candado.locked()
    # La otra mitad, que el timeout solo no prueba: el hijo trabado se tiró, así
    # que el pedido siguiente arranca uno nuevo y el servidor vuelve.
    assert cliente.pedir("prueba/eco", {"marca": "dos"}) == {"eco": "dos"}


def test_un_mensaje_partido_en_dos_escrituras_se_arma_entero(hijo):
    """La mitad que rompe cualquier arreglo apurado: un pedazo no es un mensaje.

    El corte cae entre el id y el resultado, así que las dos escrituras son
    necesarias: sin la primera no hay id que coincida, sin la segunda no hay
    eco.

    MUTACIÓN: en el bucle de lectura, `self._pendiente += trozo` ->
    `self._pendiente = trozo`. Lo que ya había llegado se pierde, el mensaje no
    se arma nunca y la llamada muere por timeout. Cae éste y sólo éste.
    """
    cliente = hijo(_HIJO_PARTIDO, 0.2)
    cliente.timeout = 5.0

    assert cliente.pedir("prueba/eco", {"marca": "mitad-y-mitad"}) == {
        "eco": "mitad-y-mitad"}


def test_la_fecha_limite_no_se_reinicia_con_cada_pedazo(hijo):
    """Un hijo que gotea sin cerrar la línea es el mismo cuelgue con más pasos.

    MUTACIÓN: adentro del bucle, `queda = limite - time.monotonic()` ->
    `queda = self.timeout`. Cada byte renueva la espera y la llamada tarda todo
    el goteo en vez de su timeout. Cae éste y sólo éste.
    """
    cliente = hijo(_HIJO_GOTEO, 0.1)   # un byte cada 100 ms, y el \n nunca
    cliente.timeout = 0.5

    arranque = time.monotonic()
    with pytest.raises(MCPExternoError):
        cliente.pedir("prueba/eco", {"marca": "goteo"})
    tardanza = time.monotonic() - arranque

    assert tardanza < 2.0, "la fecha límite se corrió con cada byte que llegó"


def test_un_aviso_pegado_a_la_respuesta_no_se_lleva_puesta_la_respuesta(hijo):
    """Un servidor MCP avisa mientras trabaja, y puede hacerlo en la MISMA
    escritura que la respuesta.

    MUTACIÓN: en `_mensaje_entero`, `self._pendiente[corte + 1:]` -> `b""`. El
    aviso se lee, la respuesta que venía pegada detrás se tira y la llamada
    muere por timeout. Cae éste y sólo éste.
    """
    cliente = hijo(_HIJO_PEGADO)
    cliente.timeout = 5.0

    assert cliente.pedir("prueba/eco", {"marca": "pegado"}) == {"eco": "pegado"}


def test_un_hijo_que_se_va_sin_contestar_se_reporta_por_lo_que_pasó(hijo):
    """Un descriptor cerrado queda legible para siempre y devuelve `b""`.

    MUTACIÓN: sacar la rama `if not trozo` del bucle. El bucle gira en falso
    quemando CPU hasta la fecha límite y lo que se reporta es un timeout, que no
    es lo que pasó: el hijo se murió. Cae éste y sólo éste.
    """
    cliente = hijo(_HIJO_SE_VA)
    cliente.timeout = 1.0

    with pytest.raises(MCPExternoError) as caida:
        cliente.pedir("prueba/eco", {"marca": "chau"})

    assert "cerró la salida" in str(caida.value)


# ------------------------------- el timeout corto del descubrimiento

def test_un_servidor_configurado_que_no_contesta_no_se_lleva_el_arranque(
    servidor_mudo, monkeypatch
):
    """`app/graph.py` arma `TOOLS_AGENTE_GERENCIA` en el import: lo que se espere
    acá es el agente sin contestar un solo WhatsApp.

    MUTACIÓN: sacar `cliente.timeout = TIMEOUT_DESCUBRIMIENTO_SEGUNDOS` de
    `cargar()`. El descubrimiento pasa a esperar lo que espera una llamada con
    alguien del otro lado, por cada servidor configurado. Cae éste y sólo éste.
    """
    monkeypatch.setattr(mcp_cliente, "TIMEOUT_SEGUNDOS", 3.0)
    monkeypatch.setattr(mcp_cliente, "TIMEOUT_DESCUBRIMIENTO_SEGUNDOS", 0.3)
    puerto = servidor_mudo.server_address[1]
    monkeypatch.setenv("MCP_EXTERNOS", f"erpnext=http://127.0.0.1:{puerto}/mcp")

    arranque = time.monotonic()
    assert mcp_cliente.cargar([]) == []
    tardanza = time.monotonic() - arranque

    assert any("no pude conectarme" in m for m in mcp_cliente._motivos)
    assert tardanza < 1.5, "el arranque esperó como si fuera una llamada normal"


def test_despues_de_descubrir_una_herramienta_recupera_el_timeout_largo(
    hijo, monkeypatch
):
    """El corto es para el arranque. En `tools/call` hay una persona esperando
    UNA respuesta y un informe pesado de ERPNext tarda.

    MUTACIÓN: sacar el `finally` que devuelve `cliente.timeout` a
    `TIMEOUT_SEGUNDOS` en `cargar()`. La herramienta se queda con el timeout del
    descubrimiento y una respuesta lenta vuelve como «no contestó», que es la
    mitad que el test del arranque no mira. Cae éste y sólo éste.
    """
    monkeypatch.setattr(mcp_cliente, "TIMEOUT_SEGUNDOS", 10.0)
    monkeypatch.setattr(mcp_cliente, "TIMEOUT_DESCUBRIMIENTO_SEGUNDOS", 1.0)
    # Descubre rápido y contesta lento: la diferencia entre las dos fases.
    servidor = hijo(_HIJO_SERVIDOR, 2.0)
    monkeypatch.setenv("MCP_EXTERNOS", f"hijo={servidor.destino}")

    herramientas = {h.name: h for h in mcp_cliente.cargar([])}

    assert herramientas["hijo_lento"].invoke({}) == "ok:hijo_lento"


def test_el_http_cacheado_no_se_queda_con_el_timeout_de_la_fase_que_lo_creo(
    cliente, servidor
):
    """El `httpx.Client` se crea una vez y vive todo el proceso.

    Un timeout puesto al construirlo sería el de esa fase para siempre, y la
    fase que lo crea es siempre el descubrimiento: `initialize` es el primer
    pedido que pasa por acá.

    MUTACIÓN: `httpx.Client(timeout=self.timeout)` y sacar el
    `timeout=self.timeout` del `post`. Cae éste y sólo éste.
    """
    cliente.timeout = 0.3
    cliente.conectar()        # acá nace el cliente http, con el corto puesto
    servidor.demora = 1.0     # y acá tarda lo que tarda un informe de verdad
    cliente.timeout = 5.0

    assert cliente.llamar("erpnext_customer_list", {}) == "ok:erpnext_customer_list"


# ------------------------------- el token de OTRO sistema, en el cable

def test_un_token_no_sale_en_claro_hacia_un_destino_que_esta_en_la_red(monkeypatch):
    """`MCP_EXTERNO_TOKEN_<NOMBRE>` es la credencial del OTRO sistema y
    `http://` la manda en texto plano.

    Nada impedía escribir un host público en `MCP_EXTERNOS` con un token al
    lado: el bearer salía por internet y no avisaba nadie. Se corta al leer la
    configuración, que es antes de que salga la primera vez.

    MUTACIÓN: dejarle al `raise` de `servidores()` un mensaje que no nombre
    `MCP_EXTERNOS_HTTP_INTERNOS` —el corte sigue estando, la salida no—. Cae
    éste y sólo éste, porque es el único que LEE el mensaje: un corte que no
    dice cómo seguir manda a leer el código a quien sólo quería levantar el
    contenedor de al lado.

    Las dos mutaciones más gruesas del mismo `raise` voltean dos tests cada una,
    y está medido: sacarlo entero también voltea al de la declaración explícita,
    y sacarle `token_de(nombre, fuente) and` también voltea al del token que
    sale del mapa. Son el mismo `raise` mirado desde otro ángulo, no un test de
    más.
    """
    monkeypatch.setenv("MCP_EXTERNOS", "erpnext=http://mcp.ejemplo.com/mcp")
    monkeypatch.setenv("MCP_EXTERNO_TOKEN_ERPNEXT", "el-secreto-del-otro")

    with pytest.raises(MCPExternoError) as caida:
        mcp_cliente.servidores()

    # Y el error dice cómo seguir, que es la diferencia entre un corte y un muro.
    assert "MCP_EXTERNOS_HTTP_INTERNOS" in str(caida.value)
    # Sin token no hay nada que filtrar y no se rechaza: ese caso lo avisa
    # `readiness`, y cortarlo acá apagaría un servidor que no expone nada.
    monkeypatch.delenv("MCP_EXTERNO_TOKEN_ERPNEXT")
    assert mcp_cliente.servidores()["erpnext"]["transporte"] == "http"


def test_el_bearer_no_se_le_pega_a_un_pedido_http_que_sale_a_la_red(monkeypatch):
    """La mitad que no depende de cómo se armó el cliente.

    `servidores()` es la puerta, pero el header se pone acá y un `ClienteMCP`
    se puede construir sin pasar por esa puerta.

    MUTACIÓN: en `_cabeceras`, volver a `if token:`. Cae éste y sólo éste.
    """
    monkeypatch.setenv("MCP_EXTERNO_TOKEN_ERPNEXT", "el-secreto-del-otro")
    afuera = ClienteMCP("erpnext", {"transporte": "http",
                                    "destino": "http://mcp.ejemplo.com/mcp"})
    cifrado = ClienteMCP("erpnext", {"transporte": "http",
                                     "destino": "https://mcp.ejemplo.com/mcp"})

    assert "Authorization" not in afuera._cabeceras("tools/list")
    # Y no es que el header se cayó para todos: por TLS sigue viajando.
    assert cifrado._cabeceras("tools/list")["Authorization"] == (
        "Bearer el-secreto-del-otro")


def test_la_compose_documentada_sigue_andando_sin_configurar_nada_nuevo(monkeypatch):
    """`erpnext=http://mcp-erpnext:3012/mcp` es la línea que documenta el repo:
    el contenedor de al lado en la red de Docker, sin publicar al host.

    Un arreglo que le pida una variable nueva a ESE caso es el arreglo
    equivocado, así que el nombre de servicio de Docker —una sola etiqueta, que
    el DNS público no resuelve— alcanza por sí solo.

    MUTACIÓN: en `_destino_sin_red`, `return "." not in anfitrion` ->
    `return False`. Cae éste y sólo éste.
    """
    monkeypatch.delenv("MCP_EXTERNOS_HTTP_INTERNOS", raising=False)
    monkeypatch.setenv("MCP_EXTERNOS", "erpnext=http://mcp-erpnext:3012/mcp")
    monkeypatch.setenv("MCP_EXTERNO_TOKEN_ERPNEXT", "el-de-ese-contenedor")

    configuracion = mcp_cliente.servidores()
    cliente = ClienteMCP("erpnext", configuracion["erpnext"])

    assert cliente._cabeceras("tools/list")["Authorization"] == (
        "Bearer el-de-ese-contenedor")


def test_el_dueno_puede_declarar_interna_una_red_suya_con_nombre_y_punto(monkeypatch):
    """Una red propia con nombres con punto —una VPN, un Kubernetes con
    dominio— no entra por la heurística y tiene que poder entrar a mano, o el
    arreglo se convierte en «poné TLS o no funciona».

    MUTACIÓN: en `bearer_en_claro`, `nombre.strip().lower() not in
    http_internos(env)` -> `not http_internos(env)`, o sea declarar uno declara
    a todos. Cae éste y sólo éste — y es la mitad que importa, porque un permiso
    que se derrama al servidor de al lado es el mismo token en claro con otro
    nombre.
    """
    monkeypatch.setenv("MCP_EXTERNOS", "erpnext=http://mcp.interno.lacteos.ar/mcp")
    monkeypatch.setenv("MCP_EXTERNO_TOKEN_ERPNEXT", "el-secreto-del-otro")
    monkeypatch.setenv("MCP_EXTERNOS_HTTP_INTERNOS", "erpnext")

    configuracion = mcp_cliente.servidores()      # ya no se rechaza
    cliente = ClienteMCP("erpnext", configuracion["erpnext"])

    assert cliente._cabeceras("tools/list")["Authorization"] == (
        "Bearer el-secreto-del-otro")
    # Y el permiso es POR SERVIDOR: el de al lado, en el mismo host, no lo hereda.
    monkeypatch.setenv("MCP_EXTERNOS", "otro=http://mcp.interno.lacteos.ar/mcp")
    monkeypatch.setenv("MCP_EXTERNO_TOKEN_OTRO", "el-secreto-del-otro")
    with pytest.raises(MCPExternoError):
        mcp_cliente.servidores()


# ------------------- el mapa que se lee es el que se pasa, no el del proceso

def test_la_lista_de_servidores_sale_del_mapa_que_se_pasa_y_no_del_proceso(
    monkeypatch
):
    """`readiness.ejecutar(env)` revisa un `.env` CANDIDATO: el que todavía no
    está puesto.

    Leyendo `os.environ` acá, readiness aprobaba los servidores del proceso que
    lo corre en lugar de los del archivo que le pidieron revisar — y decía que
    sí sobre una configuración que nadie miró.

    MUTACIÓN: en `servidores()`, `crudo = str(fuente.get("MCP_EXTERNOS", "") or
    "").strip()` -> `crudo = os.getenv("MCP_EXTERNOS", "").strip()`. Cae éste y
    sólo éste. (Mutar el `fuente = ...` entero voltea también a los otros dos
    del mapa, que leen del mismo parámetro una variable cada uno.)
    """
    # Los DOS puestos y distintos: con uno solo, las dos versiones dan igual y
    # el test no puede estar en desacuerdo con el código.
    monkeypatch.setenv("MCP_EXTERNOS", "delproceso=http://127.0.0.1:1/mcp")

    configuracion = mcp_cliente.servidores(
        {"MCP_EXTERNOS": "delmapa=http://127.0.0.1:2/mcp"})

    assert list(configuracion) == ["delmapa"]
    # Y sin mapa sigue valiendo el proceso, que es de lo que vive `cargar()`.
    assert list(mcp_cliente.servidores()) == ["delproceso"]


def test_el_token_que_decide_si_el_bearer_sale_en_claro_sale_del_mismo_mapa(
    monkeypatch
):
    """El chequeo de arriba sólo sirve si el token se lee del MISMO archivo: un
    parámetro que se consulta en la primera línea y se abandona en la tercera es
    el mismo error una línea más abajo.

    MUTACIÓN: `fuente = os.environ if env is None else env` -> `fuente =
    os.environ` en `token_de()`. Cae éste y sólo éste.
    """
    # La lista, IGUAL en los dos lados: acá se prueba de dónde sale el token y
    # nada más, así que lo demás no puede ser lo que hace fallar al test.
    monkeypatch.setenv("MCP_EXTERNOS", "erpnext=http://mcp.ejemplo.com/mcp")
    monkeypatch.delenv("MCP_EXTERNO_TOKEN_ERPNEXT", raising=False)
    candidato = {"MCP_EXTERNOS": "erpnext=http://mcp.ejemplo.com/mcp",
                 "MCP_EXTERNO_TOKEN_ERPNEXT": "el-del-archivo"}

    # El token está en el archivo que se revisa aunque no esté en el proceso.
    with pytest.raises(MCPExternoError):
        mcp_cliente.servidores(candidato)

    # Y al revés: el del proceso no le inventa un token al archivo que no lo
    # tiene, que es la mitad que convierte esto en un aviso falso.
    monkeypatch.setenv("MCP_EXTERNO_TOKEN_ERPNEXT", "el-del-proceso")
    assert list(mcp_cliente.servidores(
        {"MCP_EXTERNOS": "erpnext=http://mcp.ejemplo.com/mcp"})) == ["erpnext"]


def test_las_redes_declaradas_internas_salen_del_mismo_mapa(monkeypatch):
    """Tercer consumidor del mismo mapa, y el que decide si se rechaza o no.

    MUTACIÓN: `fuente = os.environ if env is None else env` -> `fuente =
    os.environ` en `http_internos()`. Cae éste y sólo éste.
    """
    # La lista y el token, IGUALES en los dos lados, por lo mismo que arriba: lo
    # único que cambia entre el proceso y el archivo es la declaración.
    monkeypatch.setenv("MCP_EXTERNOS", "erpnext=http://mcp.interno.lacteos.ar/mcp")
    monkeypatch.setenv("MCP_EXTERNO_TOKEN_ERPNEXT", "el-del-archivo")
    monkeypatch.delenv("MCP_EXTERNOS_HTTP_INTERNOS", raising=False)
    candidato = {"MCP_EXTERNOS": "erpnext=http://mcp.interno.lacteos.ar/mcp",
                 "MCP_EXTERNO_TOKEN_ERPNEXT": "el-del-archivo",
                 "MCP_EXTERNOS_HTTP_INTERNOS": "erpnext"}

    assert list(mcp_cliente.servidores(candidato)) == ["erpnext"]

    # Y lo declarado en el proceso no le abre la puerta al archivo que no lo
    # declara: si no, readiness aprobaría un token saliendo en claro.
    monkeypatch.setenv("MCP_EXTERNOS_HTTP_INTERNOS", "erpnext")
    with pytest.raises(MCPExternoError):
        mcp_cliente.servidores({k: v for k, v in candidato.items()
                                if k != "MCP_EXTERNOS_HTTP_INTERNOS"})
