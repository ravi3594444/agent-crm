"""Both agents run on Qwen through one DashScope key, and CI never calls it.

The provider is mocked at the ChatOpenAI boundary: these tests prove WHAT the
app would construct (model names, endpoint, timeouts, thinking flags) and that
a missing key stops the process instead of quietly picking another provider.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import ClassVar

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import modelos


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
        "DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL", "LLM_MODEL_CLIENTES", "LLM_MODEL_GERENCIA",
        "LLM_TIMEOUT_SECONDS", "LLM_MAX_RETRIES", "LLM_TEMPERATURA_CLIENTES",
        "LLM_TEMPERATURA_GERENCIA", "QWEN_THINKING_CLIENTES", "QWEN_THINKING_GERENCIA",
        "QWEN_THINKING_BUDGET",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-not-real")
    _ChatOpenAIGrabado.instancias = []
    monkeypatch.setattr(modelos, "ChatOpenAI", _ChatOpenAIGrabado)


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


def test_management_agent_is_qwen_max_and_reasons_only_when_asked(monkeypatch) -> None:
    apagado = modelos.configuracion("gerencia")
    assert apagado["model"] == "qwen3.8-max-0902"
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


def test_everything_is_environment_configuration(monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("LLM_MODEL_CLIENTES", "qwen3.7-plus-2026-05-26")
    monkeypatch.setenv("LLM_MODEL_GERENCIA", "qwen3.8-max-0902")
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
    monkeypatch.setenv("LLM_MODEL_CLIENTES", "google_genai:gemini-3.5-flash")
    with pytest.raises(modelos.ConfiguracionModeloError, match="prefijo de proveedor"):
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
    reached for by accident; the only fallback is a hard failure."""
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
    assert modelos_construidos == ["qwen3.7-plus-2026-05-26", "qwen3.8-max-0902"]
    assert graph.agente_clientes is not None and graph.agente_gerencia is not None
    del _sys.modules["app.graph"]
