"""Both agents run on ONE provider, chosen explicitly, and CI never calls it.

The provider is mocked at the ChatOpenAI boundary: these tests prove WHAT the
app would construct (model names, endpoint, timeouts, thinking flags) and that
a missing key stops the process instead of quietly picking another provider —
including the case that matters most, where the OTHER provider's key is sitting
right there in the environment.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import ClassVar

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import modelos

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Captured before the autouse fixture stands it in: the tests about the
# signature carry-through need the REAL client, not a recorder.
_ChatGeminiReal = modelos.ChatGemini


class _ChatOpenAIGrabado(RunnableLambda):
    """Stands in for langchain_openai.ChatOpenAI: records kwargs, no network.

    A Runnable, because create_react_agent composes ``prompt | model``.
    """

    instancias: ClassVar[list[dict]] = []

    def __init__(self, **kwargs) -> None:
        super().__init__(lambda _entrada: AIMessage(content="ok"))
        self.kwargs = kwargs
        type(self).instancias.append(kwargs)

    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture(autouse=True)
def _entorno(monkeypatch: pytest.MonkeyPatch):
    for variable in (
        # LLM_PROVIDER included: conftest pins it, and the tests below are the
        # ones that choose it. Deleted here so "no variable" exercises the
        # default rather than whatever the suite or a real .env pinned.
        "LLM_PROVIDER",
        "DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL", "LLM_MODEL_CLIENTES", "LLM_MODEL_GERENCIA",
        "QWEN_SALES_MODEL", "QWEN_MANAGER_MODEL",
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_BASE_URL",
        "GEMINI_SALES_MODEL", "GEMINI_MANAGER_MODEL",
        "LLM_TIMEOUT_SECONDS", "LLM_MAX_RETRIES", "LLM_TEMPERATURA_CLIENTES",
        "LLM_TEMPERATURA_GERENCIA", "QWEN_THINKING_CLIENTES", "QWEN_THINKING_GERENCIA",
        "QWEN_THINKING_BUDGET",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-not-real")
    _ChatOpenAIGrabado.instancias = []
    monkeypatch.setattr(modelos, "ChatOpenAI", _ChatOpenAIGrabado)
    # construir() resolves the client class by name from the provider, so the
    # Gemini one has to be stood in too or the real class would be built.
    monkeypatch.setattr(modelos, "ChatGemini", _ChatOpenAIGrabado)


def test_sales_agent_is_qwen_plus_with_thinking_off_by_default() -> None:
    modelo = modelos.construir("clientes")
    cfg = modelo.kwargs
    assert cfg["model"] == "qwen3.7-plus-2026-05-26"
    assert cfg["base_url"] == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    assert cfg["api_key"] == "sk-test-not-real"
    assert cfg["extra_body"] == {"enable_thinking": False}
    assert cfg["streaming"] is False
    assert cfg["temperature"] == 0.3
    assert cfg["timeout"] == 60.0
    assert cfg["max_retries"] == 2


def test_management_agent_is_the_documented_qwen_max_and_reasons_only_when_asked(monkeypatch) -> None:
    apagado = modelos.configuracion("gerencia")
    assert apagado["model"] == "qwen3.8-max"
    assert apagado["extra_body"] == {"enable_thinking": False}
    assert apagado["temperature"] == 0.1

    monkeypatch.setenv("QWEN_THINKING_GERENCIA", "true")
    monkeypatch.setenv("QWEN_THINKING_BUDGET", "512")
    encendido = modelos.configuracion("gerencia")
    assert encendido["extra_body"] == {"enable_thinking": True, "thinking_budget": 512}
    # DashScope refuses enable_thinking without streaming.
    assert encendido["streaming"] is True
    # Turning the manager's reasoning on never touches the sales agent.
    assert modelos.configuracion("clientes")["extra_body"] == {"enable_thinking": False}


def test_the_dated_manager_snapshot_is_selectable_once_verified(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_MANAGER_MODEL", "qwen3.8-max-0902")
    assert modelos.configuracion("gerencia")["model"] == "qwen3.8-max-0902"
    assert modelos.configuracion("clientes")["model"] == "qwen3.7-plus-2026-05-26"
    assert modelos.nombre_modelo("gerencia") == ("QWEN_MANAGER_MODEL", "qwen3.8-max-0902")


def test_the_previous_variable_names_still_work_but_the_qwen_names_win(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL_GERENCIA", "qwen3.8-max-0902")
    assert modelos.configuracion("gerencia")["model"] == "qwen3.8-max-0902"
    monkeypatch.setenv("QWEN_MANAGER_MODEL", "qwen3.8-max")
    assert modelos.configuracion("gerencia")["model"] == "qwen3.8-max"


def test_region_is_derived_from_the_endpoint_host() -> None:
    assert modelos.region("https://dashscope-intl.aliyuncs.com/compatible-mode/v1") == "internacional (Singapur)"
    assert modelos.region("https://dashscope.aliyuncs.com/compatible-mode/v1") == "China (Beijing)"
    assert modelos.region("https://example.com/v1") == "desconocida"


def test_everything_is_environment_configuration(monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("QWEN_SALES_MODEL", "qwen3.7-plus-2026-05-26")
    monkeypatch.setenv("QWEN_MANAGER_MODEL", "qwen3.8-max-0902")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "25")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    cfg = modelos.configuracion("gerencia")
    assert cfg["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert cfg["timeout"] == 25.0
    assert cfg["max_retries"] == 0
    assert cfg["model"] == "qwen3.8-max-0902"


def test_missing_key_stops_the_process_instead_of_falling_back(monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    with pytest.raises(modelos.ConfiguracionModeloError, match="DASHSCOPE_API_KEY"):
        modelos.construir("clientes")
    assert _ChatOpenAIGrabado.instancias == []


def test_a_gemini_style_model_name_is_refused_not_translated(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_SALES_MODEL", "google_genai:gemini-3.5-flash")
    with pytest.raises(modelos.ConfiguracionModeloError, match=r"QWEN_SALES_MODEL.*prefijo de proveedor"):
        modelos.configuracion("clientes")


@pytest.mark.parametrize(
    ("variable", "valor"),
    [
        ("QWEN_THINKING_GERENCIA", "maybe"),
        ("LLM_TIMEOUT_SECONDS", "0"),
        ("LLM_TIMEOUT_SECONDS", "rápido"),
        ("LLM_MAX_RETRIES", "-1"),
        ("DASHSCOPE_BASE_URL", "http://insecure.example"),
    ],
)
def test_nonsense_configuration_is_an_error_not_a_guess(monkeypatch, variable, valor) -> None:
    monkeypatch.setenv(variable, valor)
    with pytest.raises(modelos.ConfiguracionModeloError):
        modelos.configuracion("gerencia")


def test_no_provider_package_other_than_openai_compatible_is_required() -> None:
    """The requirements must not carry a second chat provider that could be
    reached for by accident; the only fallback is a hard failure.

    Gemini support does NOT change this: it goes through Google's own
    OpenAI-compatible endpoint with the same ChatOpenAI client, so there is
    still exactly one chat client in the image and nothing to reach for.
    """
    requisitos = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    assert "langchain-openai" in requisitos
    assert "langchain-google-genai" not in requisitos
    assert "langchain-anthropic" not in requisitos


def test_graph_builds_both_agents_from_the_factory_without_network(monkeypatch) -> None:
    """Importing the graph constructs the models; with the boundary mocked it
    must produce exactly one sales and one management configuration."""
    import importlib
    import sys as _sys

    if "app.graph" in _sys.modules:
        del _sys.modules["app.graph"]
    # The checkpointer needs Redis Stack; stand it in too so the import is pure.
    from langgraph.checkpoint.memory import MemorySaver

    class _Saver(MemorySaver):
        def __init__(self, *args, **kwargs):
            super().__init__()

        def setup(self):
            return None

    import langgraph.checkpoint.redis as redis_ckpt

    monkeypatch.setattr(redis_ckpt, "RedisSaver", _Saver)
    graph = importlib.import_module("app.graph")
    modelos_construidos = [c["model"] for c in _ChatOpenAIGrabado.instancias]
    assert modelos_construidos == ["qwen3.7-plus-2026-05-26", "qwen3.8-max"]
    assert graph.agente_clientes is not None and graph.agente_gerencia is not None
    del _sys.modules["app.graph"]


# ---------------------------------------------------------------------------
# WHICH provider. Chosen explicitly; never inferred from which key happens to
# be lying around, because that is how a business ends up talking to a model
# nobody picked, on somebody else's quota and invoice.
# ---------------------------------------------------------------------------


def test_with_no_variable_the_provider_is_qwen_as_it_always_was() -> None:
    """An existing .env that has never heard of LLM_PROVIDER keeps working."""
    prov = modelos.proveedor()
    assert prov.nombre == "qwen"
    assert modelos.configuracion("clientes")["base_url"] == modelos.BASE_URL_DEFAULT


def test_gemini_is_selected_explicitly_and_uses_one_key_for_both_agents(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-not-real-0000")
    monkeypatch.setenv("GEMINI_SALES_MODEL", "gemini-3.5-flash")
    monkeypatch.setenv("GEMINI_MANAGER_MODEL", "gemini-3.5-flash")

    ventas = modelos.configuracion("clientes")
    gerencia = modelos.configuracion("gerencia")

    assert modelos.proveedor().nombre == "gemini"
    assert ventas["model"] == "gemini-3.5-flash"
    assert gerencia["model"] == "gemini-3.5-flash"
    assert ventas["base_url"] == gerencia["base_url"] == GEMINI_BASE_URL
    # ONE key, both agents, and it is the Gemini one — not renamed, not reused.
    assert ventas["api_key"] == gerencia["api_key"] == "AIza-test-not-real-0000"
    # The temperatures stay per role: the provider changed, the roles did not.
    assert (ventas["temperature"], gerencia["temperature"]) == (0.3, 0.1)


def test_the_gemini_defaults_are_the_documented_flash_model_and_endpoint(monkeypatch) -> None:
    """With only the provider and the key set, both roles are configured."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-not-real-0000")

    for rol in modelos.ROLES:
        variable, nombre = modelos.nombre_modelo(rol)
        assert nombre == "gemini-3.5-flash"
        assert variable.startswith("GEMINI_")
        assert modelos.configuracion(rol)["base_url"] == GEMINI_BASE_URL


def test_the_standard_google_key_name_is_accepted_as_a_synonym(monkeypatch) -> None:
    """GOOGLE_API_KEY is what Google's own tooling sets, so a .env that already
    has it does not need the key written down twice. GEMINI_API_KEY wins."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-from-google-var-000")
    assert modelos.configuracion("clientes")["api_key"] == "AIza-from-google-var-000"
    assert modelos.clave_api(modelos.proveedor())[0] == "GOOGLE_API_KEY"

    monkeypatch.setenv("GEMINI_API_KEY", "AIza-from-gemini-var-000")
    assert modelos.configuracion("clientes")["api_key"] == "AIza-from-gemini-var-000"
    assert modelos.clave_api(modelos.proveedor())[0] == "GEMINI_API_KEY"


def test_an_unknown_provider_is_refused_and_nothing_is_built(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(modelos.ConfiguracionModeloError, match=r"LLM_PROVIDER.*no es un proveedor"):
        modelos.construir("clientes")
    assert _ChatOpenAIGrabado.instancias == []


def test_gemini_does_not_require_a_dashscope_key(monkeypatch) -> None:
    """The whole point of the switch: one Gemini key is enough to run."""
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-not-real-0000")

    modelo = modelos.construir("gerencia")

    assert modelo.kwargs["model"] == "gemini-3.5-flash"
    assert modelo.kwargs["base_url"] == GEMINI_BASE_URL


def test_the_gemini_key_is_never_read_from_the_dashscope_variable(monkeypatch) -> None:
    """Not disguised, not aliased: a Gemini run reads Gemini variables only."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-a-qwen-key-that-is-not-gemini")

    with pytest.raises(modelos.ConfiguracionModeloError, match="GEMINI_API_KEY"):
        modelos.configuracion("clientes")
    assert _ChatOpenAIGrabado.instancias == []


def test_a_missing_gemini_key_never_falls_back_to_qwen(monkeypatch) -> None:
    """A DashScope key sitting in the environment is exactly what a silent
    fallback would grab. Choosing gemini with no Gemini key is a stop."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")  # DASHSCOPE_API_KEY is set by the fixture

    with pytest.raises(modelos.ConfiguracionModeloError) as fallo:
        modelos.construir("clientes")

    mensaje = str(fallo.value)
    assert "GEMINI_API_KEY" in mensaje and "no hay proveedor de respaldo" in mensaje
    assert "DASHSCOPE" not in mensaje  # never point him at the wrong credential
    assert _ChatOpenAIGrabado.instancias == []


def test_a_missing_qwen_key_never_falls_back_to_gemini(monkeypatch) -> None:
    """And the same in the other direction, so neither key covers for the other."""
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-not-real-0000")
    monkeypatch.setenv("LLM_PROVIDER", "qwen")

    with pytest.raises(modelos.ConfiguracionModeloError, match="DASHSCOPE_API_KEY"):
        modelos.construir("clientes")
    assert _ChatOpenAIGrabado.instancias == []


def test_the_thinking_knobs_are_qwen_only_and_are_not_sent_to_gemini(monkeypatch) -> None:
    """Gemini's OpenAI-compatible endpoint does not take DashScope's
    enable_thinking, so it is not sent at all — and streaming, which existed
    only because DashScope demands it with thinking, stays off."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-not-real-0000")
    monkeypatch.setenv("QWEN_THINKING_GERENCIA", "true")
    monkeypatch.setenv("QWEN_THINKING_BUDGET", "512")

    cfg = modelos.configuracion("gerencia")

    assert cfg["extra_body"] == {}
    assert cfg["streaming"] is False


def test_a_gemini_model_name_with_a_provider_prefix_is_refused(monkeypatch) -> None:
    """`google_genai:gemini-…` was init_chat_model's format and is what an old
    .env still carries. Refused with the variable named, never translated."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-not-real-0000")
    monkeypatch.setenv("GEMINI_SALES_MODEL", "google_genai:gemini-3.5-flash")

    with pytest.raises(modelos.ConfiguracionModeloError, match="GEMINI_SALES_MODEL"):
        modelos.configuracion("clientes")


def test_the_legacy_model_names_still_work_under_gemini_but_lose(monkeypatch) -> None:
    """LLM_MODEL_* is what an existing .env has. It is honoured, and the
    provider-specific variable overrides it."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-not-real-0000")
    monkeypatch.setenv("LLM_MODEL_GERENCIA", "gemini-3.5-pro")
    assert modelos.configuracion("gerencia")["model"] == "gemini-3.5-pro"

    monkeypatch.setenv("GEMINI_MANAGER_MODEL", "gemini-3.5-flash")
    assert modelos.configuracion("gerencia")["model"] == "gemini-3.5-flash"


def test_an_insecure_gemini_endpoint_is_refused(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-not-real-0000")
    monkeypatch.setenv("GEMINI_BASE_URL", "http://generativelanguage.googleapis.com/v1beta/openai/")

    with pytest.raises(modelos.ConfiguracionModeloError, match="GEMINI_BASE_URL"):
        modelos.configuracion("clientes")


def test_the_google_endpoint_is_a_known_region() -> None:
    assert modelos.region(GEMINI_BASE_URL) == "global (Google)"


def test_graph_builds_both_gemini_agents_from_the_factory_without_network(monkeypatch) -> None:
    """The switch has to reach the actual agents, and reach them at import."""
    import importlib
    import sys as _sys

    monkeypatch.delenv("DASHSCOPE_API_KEY")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-not-real-0000")
    if "app.graph" in _sys.modules:
        del _sys.modules["app.graph"]
    from langgraph.checkpoint.memory import MemorySaver

    class _Saver(MemorySaver):
        def __init__(self, *args, **kwargs):
            super().__init__()

        def setup(self):
            return None

    import langgraph.checkpoint.redis as redis_ckpt

    monkeypatch.setattr(redis_ckpt, "RedisSaver", _Saver)
    graph = importlib.import_module("app.graph")

    assert [c["model"] for c in _ChatOpenAIGrabado.instancias] == [
        "gemini-3.5-flash",
        "gemini-3.5-flash",
    ]
    assert {c["base_url"] for c in _ChatOpenAIGrabado.instancias} == {GEMINI_BASE_URL}
    assert graph.agente_clientes is not None and graph.agente_gerencia is not None
    del _sys.modules["app.graph"]


# ---------------------------------------------------------------------------
# Secret masking. Everything the verification script prints comes from the
# provider, and a provider that echoes the key back in an error message would
# otherwise put it in a terminal and a scrollback.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("variable", "clave"),
    [
        ("DASHSCOPE_API_KEY", "sk-qwen-key-not-real-0001"),
        ("GEMINI_API_KEY", "AIzaSyNotARealGeminiKey00"),
        ("GOOGLE_API_KEY", "AIzaSyNotARealGoogleKey0"),
    ],
)
def test_the_configured_key_is_masked_wherever_it_appears(monkeypatch, variable, clave) -> None:
    monkeypatch.setenv(variable, clave)
    texto = f"401 desde el proveedor: api_key={clave} no autorizada"

    oculto = modelos.enmascarar(texto)

    assert clave not in oculto
    assert modelos.OCULTO in oculto
    assert "no autorizada" in oculto  # still readable as a diagnosis


def test_a_key_shape_is_masked_even_if_it_is_not_the_configured_one() -> None:
    """The provider's own error text can carry a credential this process never
    had — a key from another project, a rotated one. Masked by shape."""
    for clave in ("sk-abcdef0123456789", "AIzaSyAAAAAAAAAAAAAAAAAAAAAAAA"):
        oculto = modelos.enmascarar(f"error con {clave}")
        assert clave not in oculto and modelos.OCULTO in oculto


def test_the_other_providers_key_is_masked_too(monkeypatch) -> None:
    """Masking follows the variables, not the active provider: a stale key in
    the .env must not leak just because it is not the one in use."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyNotARealGeminiKey00")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "un-valor-viejo-cualquiera")

    oculto = modelos.enmascarar("dump: un-valor-viejo-cualquiera / AIzaSyNotARealGeminiKey00")

    assert "un-valor-viejo-cualquiera" not in oculto
    assert "AIzaSyNotARealGeminiKey00" not in oculto


def test_masking_never_mangles_text_over_a_short_value(monkeypatch) -> None:
    """CI runs the container with DASHSCOPE_API_KEY=noop. Replacing a
    four-letter "secret" everywhere would corrupt every message that contains
    it, so only a value long enough to be a real key is substituted."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "noop")

    assert modelos.enmascarar("noop: sin novedad") == "noop: sin novedad"


def test_a_masked_message_is_bounded() -> None:
    assert len(modelos.enmascarar("x" * 5000)) == 400


# ---------------------------------------------------------------------------
# Function calling, which is the only thing the agents ask of a model. The
# check runs against a fake provider here; against the real one it is
# `make verificar-modelos`, by hand, never in CI.
# ---------------------------------------------------------------------------


def _script_de_verificacion():
    """deploy/verificar_modelos.py, imported by path (deploy/ is not a package)."""
    ruta = Path(__file__).resolve().parents[1] / "deploy" / "verificar_modelos.py"
    spec = importlib.util.spec_from_file_location("verificar_modelos", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class _ModeloQueLlamaLaHerramienta:
    """A provider that answers with a tool call, then with a final message."""

    def __init__(self, *, llama: bool = True) -> None:
        self.llama = llama
        self.invocaciones: list[list] = []
        self.herramientas: list = []

    def bind_tools(self, tools, **kwargs):
        self.herramientas = list(tools)
        return self

    def invoke(self, mensajes):
        self.invocaciones.append(list(mensajes))
        if not self.llama:
            return AIMessage(content="claro que sí")
        if len(self.invocaciones) == 1:
            return AIMessage(
                content="",
                tool_calls=[{"name": "ping", "args": {"texto": "ok"}, "id": "call-1"}],
            )
        return AIMessage(content="pong:ok", usage_metadata={
            "input_tokens": 11, "output_tokens": 3, "total_tokens": 14,
        })


def test_the_check_proves_the_model_can_call_a_tool(monkeypatch, capsys) -> None:
    script = _script_de_verificacion()
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyNotARealGeminiKey00")
    falso = _ModeloQueLlamaLaHerramienta()
    monkeypatch.setattr(modelos, "construir", lambda rol: falso)

    assert script.probar("clientes") is True

    salida = capsys.readouterr().out
    assert "OK    clientes" in salida
    assert "gemini-3.5-flash" in salida and "generativelanguage.googleapis.com" in salida
    assert "tokens=11+3" in salida
    # The tool was actually bound, called, and its result fed back.
    assert [h.name for h in falso.herramientas] == ["ping"]
    assert len(falso.invocaciones) == 2
    assert any(type(m).__name__ == "ToolMessage" for m in falso.invocaciones[1])
    assert "pong:ok" in str(falso.invocaciones[1][-1].content)


def test_a_model_that_answers_without_calling_the_tool_is_a_failure(monkeypatch, capsys) -> None:
    """An agent whose model chats instead of calling tools is useless here, so
    a friendly paragraph is a FAILED check, not a pass."""
    script = _script_de_verificacion()
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyNotARealGeminiKey00")
    monkeypatch.setattr(modelos, "construir", lambda rol: _ModeloQueLlamaLaHerramienta(llama=False))

    assert script.probar("gerencia") is False
    assert "FALLA gerencia" in capsys.readouterr().out


def test_the_check_imports_no_business_module(monkeypatch) -> None:
    """No ERPNext, no WhatsApp, no Redis, no orders: the script can only reach
    the model factory. Asserted on what it IMPORTS, because that is what
    decides what it is able to touch — its docstring names those systems only
    to say it stays away from them."""
    import ast

    ruta = Path(__file__).resolve().parents[1] / "deploy" / "verificar_modelos.py"
    arbol = ast.parse(ruta.read_text())
    importados: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            importados.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom):
            raiz = nodo.module or ""
            importados.update(f"{raiz}.{alias.name}" for alias in nodo.names)

    de_la_app = {n for n in importados if n == "app" or n.startswith("app.")}
    # `app` itself is the .env load; `app.modelos` is the factory. Nothing else.
    assert de_la_app == {"app", "app.modelos"}, de_la_app
    for prohibido in ("erpnext", "whatsapp", "avisos", "solicitudes", "policy", "redis", "locks"):
        assert not any(prohibido in n for n in importados), prohibido


def test_the_check_sends_only_its_own_synthetic_text(monkeypatch) -> None:
    """The only "data" in the request is the constant written in the script."""
    script = _script_de_verificacion()
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyNotARealGeminiKey00")
    falso = _ModeloQueLlamaLaHerramienta()
    monkeypatch.setattr(modelos, "construir", lambda rol: falso)

    script.probar("clientes")

    enviado = " ".join(str(m.content) for m in falso.invocaciones[0])
    assert script.TEXTO_DE_PRUEBA == "ok"
    assert "ping" in enviado
    assert "SAL-ORD" not in enviado and "54935" not in enviado
    assert "AIzaSyNotARealGeminiKey00" not in enviado


def test_the_check_refuses_to_run_in_ci(monkeypatch, capsys) -> None:
    script = _script_de_verificacion()
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(
        modelos, "construir", lambda rol: pytest.fail("no debe construirse en CI")
    )

    assert script.main() == 2
    assert "no corre en CI" in capsys.readouterr().out


def test_the_check_stops_when_the_selected_provider_has_no_key(monkeypatch, capsys) -> None:
    script = _script_de_verificacion()
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")  # DASHSCOPE_API_KEY is set by the fixture
    monkeypatch.setattr(
        modelos, "construir", lambda rol: pytest.fail("no hay clave: no se construye nada")
    )

    assert script.main() == 1

    salida = capsys.readouterr().out
    assert "GEMINI_API_KEY" in salida and "nada que verificar" in salida


def test_the_check_runs_both_roles_and_masks_the_key(monkeypatch, capsys) -> None:
    script = _script_de_verificacion()
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyNotARealGeminiKey00")
    roles: list[str] = []

    def construir(rol):
        roles.append(rol)
        return _ModeloQueLlamaLaHerramienta()

    monkeypatch.setattr(modelos, "construir", construir)

    assert script.main() == 0

    salida = capsys.readouterr().out
    assert roles == ["clientes", "gerencia"]
    assert "OK    clientes" in salida and "OK    gerencia" in salida
    assert "AIzaSyNotARealGeminiKey00" not in salida
    assert "clave en GEMINI_API_KEY" in salida  # the variable, never the value


# ---------------------------------------------------------------------------
# Gemini's thought signature. Verified against the real endpoint: replaying a
# tool call WITHOUT it answers 400, with it both steps answer 200, and turning
# thinking off does not lift the requirement. Both agents live on calling tools
# and reading the result, so without this the first customer who asks a price
# gets an error.
# ---------------------------------------------------------------------------

_FIRMA = "EooDCocDARFNMg-firma-de-prueba"


def _respuesta_con_firma(firma: str = _FIRMA, *, id_llamada: str = "call_1") -> dict:
    """Lo que contesta Gemini cuando decide llamar una herramienta."""
    return {
        "id": "chatcmpl-1",
        "created": 0,
        "model": "gemini-3.5-flash",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": id_llamada,
                            "type": "function",
                            "function": {"name": "ping", "arguments": '{"texto":"ok"}'},
                            "extra_content": {"google": {"thought_signature": firma}},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def test_the_signature_is_extracted_from_a_raw_gemini_answer() -> None:
    firmas = modelos.firmas_de_respuesta(_respuesta_con_firma())

    assert firmas == {"call_1": {"google": {"thought_signature": _FIRMA}}}


@pytest.mark.parametrize(
    "respuesta",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {"role": "assistant", "content": "hola"}}]},
        {"choices": [{"message": {"tool_calls": [{"id": "x", "function": {}}]}}]},
        {"choices": ["no es un dict"]},
        "ni siquiera es un dict",
        None,
    ],
)
def test_an_answer_without_signatures_yields_nothing_and_never_raises(respuesta) -> None:
    assert modelos.firmas_de_respuesta(respuesta) == {}


def _cliente_gemini():
    """A real ChatGemini. Constructing it makes no request."""
    return _ChatGeminiReal(
        model="gemini-3.5-flash", api_key="AIza-test-not-real-0000", base_url=GEMINI_BASE_URL
    )


def test_the_signature_is_kept_on_the_message_so_it_survives_the_turn(monkeypatch) -> None:
    """It goes in additional_kwargs because that is what LangGraph's
    checkpointer serializes with the message: the next turn — and the next
    process — still has it."""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    resultado = _cliente_gemini()._create_chat_result(_respuesta_con_firma())

    mensaje = resultado.generations[0].message
    assert mensaje.additional_kwargs[modelos.CLAVE_FIRMAS] == {
        "call_1": {"google": {"thought_signature": _FIRMA}}
    }
    # The tool call itself is parsed as usual.
    assert [c["name"] for c in mensaje.tool_calls] == ["ping"]


def test_the_signature_is_sent_back_on_the_tool_call(monkeypatch) -> None:
    """THE fix. The replay carries extra_content again, which is what Gemini
    demands and what langchain-openai drops on its own."""
    from langchain_core.messages import HumanMessage, ToolMessage

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    cliente = _cliente_gemini()
    respondido = cliente._create_chat_result(_respuesta_con_firma()).generations[0].message

    payload = cliente._get_request_payload(
        [
            HumanMessage(content="llamá a ping"),
            respondido,
            ToolMessage(content="pong:ok", tool_call_id="call_1"),
        ]
    )

    asistentes = [m for m in payload["messages"] if m.get("tool_calls")]
    assert len(asistentes) == 1
    (llamada,) = asistentes[0]["tool_calls"]
    assert llamada["extra_content"] == {"google": {"thought_signature": _FIRMA}}
    assert llamada["id"] == "call_1"


def test_a_tool_call_with_no_stored_signature_is_left_alone(monkeypatch) -> None:
    """Nothing is invented: a message that never carried a signature is sent
    exactly as before."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    sin_firma = AIMessage(
        content="",
        tool_calls=[{"name": "ping", "args": {"texto": "ok"}, "id": "call_9"}],
    )

    payload = _cliente_gemini()._get_request_payload(
        [
            HumanMessage(content="hola"),
            sin_firma,
            ToolMessage(content="pong:ok", tool_call_id="call_9"),
        ]
    )

    (asistente,) = [m for m in payload["messages"] if m.get("tool_calls")]
    assert "extra_content" not in asistente["tool_calls"][0]


def test_signatures_are_matched_by_tool_call_id_not_by_position(monkeypatch) -> None:
    """Two calls in one conversation must not swap signatures."""
    from langchain_core.messages import HumanMessage, ToolMessage

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    cliente = _cliente_gemini()
    primero = cliente._create_chat_result(
        _respuesta_con_firma("firma-A", id_llamada="call_A")
    ).generations[0].message
    segundo = cliente._create_chat_result(
        _respuesta_con_firma("firma-B", id_llamada="call_B")
    ).generations[0].message

    payload = cliente._get_request_payload(
        [
            HumanMessage(content="uno"),
            primero,
            ToolMessage(content="pong:A", tool_call_id="call_A"),
            segundo,
            ToolMessage(content="pong:B", tool_call_id="call_B"),
        ]
    )

    firmas = {
        m["tool_calls"][0]["id"]: m["tool_calls"][0]["extra_content"]["google"][
            "thought_signature"
        ]
        for m in payload["messages"]
        if m.get("tool_calls")
    }
    assert firmas == {"call_A": "firma-A", "call_B": "firma-B"}


def test_the_gemini_provider_builds_the_signature_carrying_client(monkeypatch) -> None:
    """And the plain client stays plain for Qwen: the fix reaches production
    only through the provider that needs it."""
    assert modelos.PROVEEDORES["gemini"].clase == "ChatGemini"
    assert modelos.PROVEEDORES["qwen"].clase == "ChatOpenAI"

    construidas: list[str] = []
    monkeypatch.setattr(
        modelos, "ChatGemini", lambda **kw: construidas.append("ChatGemini") or object()
    )
    monkeypatch.setattr(
        modelos, "ChatOpenAI", lambda **kw: construidas.append("ChatOpenAI") or object()
    )

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-not-real-0000")
    modelos.construir("clientes")
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    modelos.construir("clientes")

    assert construidas == ["ChatGemini", "ChatOpenAI"]


def test_chat_gemini_is_a_chat_openai_so_nothing_else_changes() -> None:
    """One client, one protocol: the subclass only re-attaches a dropped field."""
    from langchain_openai import ChatOpenAI as Real

    assert issubclass(_ChatGeminiReal, Real)
