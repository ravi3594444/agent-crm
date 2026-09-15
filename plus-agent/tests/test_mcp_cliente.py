"""Herramientas de otro servidor MCP: el puente, y lo que el puente NO deja pasar.

Acá no se levanta ningún servidor de terceros. Lo que se prueba es el código de
esta casa: que el puente sincrónico funcione, que un nombre repetido no tape una
herramienta nuestra, que lo bloqueado no llegue al modelo, y —lo más importante—
que `graph.TOOLS_GERENCIA` NO se mueva, porque es la constante que
`app/mcp_server.py` publica por nuestra puerta autenticada.
"""
from __future__ import annotations

import pytest

from app import mcp_cliente


class HerramientaFalsa:
    """Lo que devuelve `MultiServerMCPClient.get_tools()`: asíncrona, args dict."""

    def __init__(self, name, description="hace algo", devuelve="ok"):
        self.name = name
        self.description = description
        # Estas herramientas traen el JSON Schema del otro servidor como DICT,
        # no como un modelo de pydantic. Es la diferencia que hay que sostener.
        self.args_schema = {"type": "object", "properties": {}}
        self._devuelve = devuelve
        self.recibio = None

    async def ainvoke(self, argumentos):
        self.recibio = argumentos
        return self._devuelve


@pytest.fixture(autouse=True)
def sin_cache():
    mcp_cliente._cargadas = None
    mcp_cliente._motivos.clear()
    yield
    mcp_cliente._cargadas = None


def _con_servidor(monkeypatch, herramientas):
    class ClienteFalso:
        def __init__(self, configuracion):
            self.configuracion = configuracion

        async def get_tools(self):
            return list(herramientas)

    import langchain_mcp_adapters.client as modulo

    monkeypatch.setattr(modulo, "MultiServerMCPClient", ClienteFalso)


# ------------------------------------------------------------ configuración

def test_without_the_variable_there_is_no_external_surface(monkeypatch):
    monkeypatch.delenv("MCP_EXTERNOS", raising=False)
    assert mcp_cliente.servidores() == {}
    assert mcp_cliente.cargar([]) == []


def test_an_http_destination_becomes_the_http_transport(monkeypatch):
    monkeypatch.setenv("MCP_EXTERNOS", "erp=https://mcp.example/mcp")
    assert mcp_cliente.servidores() == {
        "erp": {"url": "https://mcp.example/mcp", "transport": "streamable_http"}
    }


def test_a_command_becomes_the_stdio_transport(monkeypatch):
    monkeypatch.setenv("MCP_EXTERNOS", "erp=npx -y @casys/mcp-erpnext --categories=sales")
    configuracion = mcp_cliente.servidores()["erp"]
    assert configuracion["transport"] == "stdio"
    assert configuracion["command"] == "npx"
    assert configuracion["args"] == ["-y", "@casys/mcp-erpnext", "--categories=sales"]


def test_an_entry_without_a_destination_is_refused_loudly(monkeypatch):
    """Una entrada rota que se descarta callada es un servidor que no está."""
    monkeypatch.setenv("MCP_EXTERNOS", "solo_nombre")
    with pytest.raises(mcp_cliente.MCPExternoError):
        mcp_cliente.servidores()


# ------------------------------------------------------------------ el puente

def test_an_async_tool_can_be_called_from_synchronous_code(monkeypatch):
    """Todo el runtime es síncrono; estas herramientas no lo son.

    Mata a este test cambiar `_sincronizar` para que devuelva la herramienta tal
    cual: `agente_gerencia.invoke` no sabe esperar una corrutina.
    """
    falsa = HerramientaFalsa("erpnext_customer_list", devuelve="tres clientes")
    _con_servidor(monkeypatch, [falsa])
    monkeypatch.setenv("MCP_EXTERNOS", "erp=https://mcp.example/mcp")

    herramienta = mcp_cliente.cargar([])[0]
    assert herramienta.invoke({}) == "tres clientes"


def test_the_arguments_reach_the_far_side(monkeypatch):
    falsa = HerramientaFalsa("erpnext_item_get")
    _con_servidor(monkeypatch, [falsa])
    monkeypatch.setenv("MCP_EXTERNOS", "erp=https://mcp.example/mcp")

    mcp_cliente.cargar([])[0].invoke({"item_code": "LECHE-1L"})
    assert falsa.recibio == {"item_code": "LECHE-1L"}


def test_a_huge_answer_is_cut(monkeypatch):
    largo = "x" * (mcp_cliente.MAX_RESULTADO + 1_000)
    _con_servidor(monkeypatch, [HerramientaFalsa("erpnext_doc_list", devuelve=largo)])
    monkeypatch.setenv("MCP_EXTERNOS", "erp=https://mcp.example/mcp")

    assert len(mcp_cliente.cargar([])[0].invoke({})) == mcp_cliente.MAX_RESULTADO


# ------------------------------------------------------- lo que NO se carga

class Propia:
    def __init__(self, name):
        self.name = name


def test_a_name_that_collides_with_ours_is_not_loaded(monkeypatch):
    """Si se cargara, taparía a la nuestra en el ToolNode —gana la última— y el
    agente creería estar llamando a la que tiene las guardas puestas."""
    _con_servidor(monkeypatch, [HerramientaFalsa("informe", devuelve="ajena")])
    monkeypatch.setenv("MCP_EXTERNOS", "erp=https://mcp.example/mcp")

    cargadas = mcp_cliente.cargar([Propia("informe")])
    assert cargadas == []
    assert any("ya existe" in m for m in mcp_cliente._motivos)


def test_a_blocked_pattern_never_reaches_the_model(monkeypatch):
    _con_servidor(monkeypatch, [
        HerramientaFalsa("erpnext_customer_list"),
        HerramientaFalsa("erpnext_doc_submit"),
        HerramientaFalsa("erpnext_sales_order_cancel"),
    ])
    monkeypatch.setenv("MCP_EXTERNOS", "erp=https://mcp.example/mcp")
    monkeypatch.setenv("MCP_EXTERNOS_BLOQUEAR", "*_submit,*_cancel")

    nombres = [h.name for h in mcp_cliente.cargar([])]
    assert nombres == ["erpnext_customer_list"]


def test_nothing_is_blocked_by_default(monkeypatch):
    """El default no decide la política del negocio; la hace escribible."""
    _con_servidor(monkeypatch, [HerramientaFalsa("erpnext_doc_submit")])
    monkeypatch.setenv("MCP_EXTERNOS", "erp=https://mcp.example/mcp")
    monkeypatch.delenv("MCP_EXTERNOS_BLOQUEAR", raising=False)

    assert [h.name for h in mcp_cliente.cargar([])] == ["erpnext_doc_submit"]


# ------------------------------- lo nuestro no se contamina con lo de afuera

def test_our_own_mcp_endpoint_never_republishes_a_third_party_tool(monkeypatch):
    """LA PROPIEDAD QUE MÁS IMPORTA DE TODO ESTE ARCHIVO.

    `mcp_server._catalogo()` lee `graph.TOOLS_GERENCIA`. Si el cableado de
    `graph.py` le sumara las externas a ESA constante en vez de a
    `TOOLS_AGENTE_GERENCIA`, nuestro endpoint autenticado pasaría a servir el
    `erpnext_doc_submit` de un tercero como si fuera nuestro — con nuestro token
    y bajo nuestro nombre.

    Y el test que dice «el catálogo publicado es exactamente el del agente»
    seguiría en VERDE, porque los dos lados se moverían juntos. Por eso esto se
    afirma contra la lista escrita en el repo y no contra la del agente.
    """
    from app import graph, mcp_server

    publicadas = {h["name"] for h in mcp_server.listar()}
    escritas = {h.name for h in graph.TOOLS_GERENCIA}
    assert publicadas == escritas
    assert not any(n.startswith("erpnext_") for n in publicadas)


def test_the_agent_list_is_a_different_object_from_the_published_one():
    """Sumar en el lugar equivocado es un `+=` de distancia; esto lo nombra."""
    from app import graph

    assert graph.TOOLS_AGENTE_GERENCIA is not graph.TOOLS_GERENCIA
    # Sin servidores externos configurados son iguales en contenido, y está bien:
    # lo que no puede pasar es que sean el MISMO objeto, porque entonces
    # cualquier extensión del agente extiende también lo que publicamos.
    assert all(h in graph.TOOLS_AGENTE_GERENCIA for h in graph.TOOLS_GERENCIA)
