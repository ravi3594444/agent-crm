"""The system prompt is rebuilt every turn and never stored; history is bounded."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import ToolNode, create_react_agent
from pydantic import Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import conversacion  # noqa: E402


class EcoModel(BaseChatModel):
    """Records exactly what the model receives and always answers 'ok'."""

    seen: list = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    @property
    def _llm_type(self) -> str:
        return "eco"

    def bind_tools(self, tools, **kwargs):
        return self


@tool
def herramienta_inerte() -> str:
    """Nunca se llama; existe para que el agente tenga tools como en producción."""
    return ""


def _agente(prompt):
    model = EcoModel()
    agent = create_react_agent(
        model=model,
        tools=ToolNode([herramienta_inerte]),
        prompt=prompt,
        pre_model_hook=conversacion.recortar_historial,
        checkpointer=MemorySaver(),
    )
    return model, agent


def _config(thread: str, customer_code: str = "CUST-1") -> dict:
    return {
        "configurable": {
            "thread_id": thread,
            "actor_scope": "customer",
            "customer_code": customer_code,
            "actor_phone": "5493512222222",
            "inbound_message_id": "wamid.x",
        }
    }


def test_exactly_one_fresh_system_prompt_per_call_and_none_persisted():
    model, agent = _agente(conversacion.prompt_clientes)
    config = _config("t1")
    for turn in ("hola", "quiero manteca", "para mañana"):
        agent.invoke({"messages": [("user", turn)]}, config=config)

    assert len(model.seen) == 3
    for call in model.seen:
        systems = [m for m in call if isinstance(m, SystemMessage)]
        assert len(systems) == 1
        assert call[0] is systems[0]
        assert conversacion.business_today() in systems[0].content
        assert "Cliente con cuenta registrada" in systems[0].content
    # third call sees the whole short history: h a h a h
    body = [type(m).__name__ for m in model.seen[2] if not isinstance(m, SystemMessage)]
    assert body == ["HumanMessage", "AIMessage", "HumanMessage", "AIMessage", "HumanMessage"]

    stored = agent.get_state(config).values["messages"]
    assert stored and not any(isinstance(m, SystemMessage) for m in stored)


def test_unregistered_sender_gets_the_no_account_context():
    model, agent = _agente(conversacion.prompt_clientes)
    agent.invoke({"messages": [("user", "hola")]}, config=_config("t2", customer_code=""))
    system = model.seen[0][0]
    assert isinstance(system, SystemMessage)
    assert "sin cuenta de cliente registrada" in system.content


def test_history_is_bounded_and_starts_on_a_human_message(monkeypatch):
    monkeypatch.setenv("CONVERSATION_MAX_MESSAGES", "6")
    model, agent = _agente(conversacion.prompt_clientes)
    config = _config("t3")
    for i in range(12):
        agent.invoke({"messages": [("user", f"mensaje {i}")]}, config=config)

    last = [m for m in model.seen[-1] if not isinstance(m, SystemMessage)]
    assert len(last) <= 6
    assert isinstance(last[0], HumanMessage)
    assert last[-1].content == "mensaje 11"
    # the checkpoint itself still holds the full thread
    assert len(agent.get_state(config).values["messages"]) == 24


def test_legacy_checkpoints_with_stored_system_prompts_are_cleaned_on_read():
    model, agent = _agente(conversacion.prompt_clientes)
    config = _config("t4")
    agent.update_state(
        config,
        {
            "messages": [
                SystemMessage(content="prompt viejo. Fecha de hoy: 2020-01-01"),
                HumanMessage(content="hola"),
                AIMessage(content="¡hola!"),
                SystemMessage(content="otro prompt viejo. Fecha de hoy: 2020-01-02"),
            ]
        },
    )
    agent.invoke({"messages": [("user", "quiero leche")]}, config=config)
    call = model.seen[0]
    systems = [m for m in call if isinstance(m, SystemMessage)]
    assert len(systems) == 1
    assert "2020-01-0" not in systems[0].content
    assert conversacion.business_today() in systems[0].content


def test_management_prompt_uses_business_date_and_role():
    model, agent = _agente(conversacion.prompt_gerencia)
    agent.invoke(
        {"messages": [("user", "pendientes?")]},
        config={"configurable": {"thread_id": "g1", "actor_scope": "management"}},
    )
    system = model.seen[0][0]
    assert isinstance(system, SystemMessage)
    assert conversacion.business_today() in system.content
    assert "miembro autorizado del equipo" in system.content


def test_texto_plano_handles_gemini_content_blocks():
    assert conversacion.texto_plano(AIMessage(content="hola")) == "hola"
    blocks = AIMessage(content=[{"type": "text", "text": "a"}, {"type": "thinking", "thinking": "x"}, {"type": "text", "text": "b"}])
    assert conversacion.texto_plano(blocks) == "a\nb"


def test_max_history_has_a_sane_floor(monkeypatch):
    monkeypatch.setenv("CONVERSATION_MAX_MESSAGES", "1")
    assert conversacion.max_history() == 4
    monkeypatch.setenv("CONVERSATION_MAX_MESSAGES", "abc")
    with pytest.raises(RuntimeError):
        conversacion.max_history()
