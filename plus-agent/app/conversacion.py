"""Per-turn prompt construction and bounded history for both agents.

Why this exists: the agents are checkpointed per WhatsApp thread for
``CONVERSATION_TTL_DAYS``. If every turn appends a fresh system message to
that thread, the persisted history accumulates one system prompt per turn
and the Gemini adapter concatenates ALL of them into the system instruction:
several contradictory "Fecha de hoy" lines, unbounded token growth, and a
model that may compute "mañana" from yesterday's date. The system prompt is
therefore built at call time and never stored, and the model only ever sees a
bounded tail of the conversation.
"""
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.messages import BaseMessage, SystemMessage, trim_messages
from langchain_core.runnables import RunnableConfig

from app import idioma
from app.prompts import SYSTEM_ES_AR
from app.prompts_gerencia import SYSTEM_GERENCIA

_DEFAULT_MAX_MESSAGES = 40


def max_history() -> int:
    """Upper bound of non-system messages the model sees per turn."""
    try:
        value = int(os.getenv("CONVERSATION_MAX_MESSAGES", str(_DEFAULT_MAX_MESSAGES)))
    except ValueError as exc:
        raise RuntimeError("CONVERSATION_MAX_MESSAGES debe ser un entero") from exc
    return max(4, value)


def business_today() -> str:
    zone_name = os.getenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires").strip()
    try:
        return datetime.now(ZoneInfo(zone_name)).date().isoformat()
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RuntimeError("BUSINESS_TIMEZONE inválida") from exc


def _mensajes(state) -> list[BaseMessage]:
    messages = state.messages if hasattr(state, "messages") else state["messages"]
    return list(messages or [])


def recortar_historial(state) -> dict:
    """pre_model_hook: drop stored system messages and keep a bounded tail.

    ``start_on="human"`` guarantees the tail never opens with an AI tool call
    whose ToolMessage was cut off, which Gemini rejects.
    """
    body = [m for m in _mensajes(state) if not isinstance(m, SystemMessage)]
    trimmed = trim_messages(
        body,
        strategy="last",
        token_counter=len,
        max_tokens=max_history(),
        start_on="human",
        include_system=False,
    )
    return {"llm_input_messages": trimmed or body[-1:]}


def _configurable(config: RunnableConfig | None) -> dict:
    return dict((config or {}).get("configurable") or {})


def prompt_clientes(state, config: RunnableConfig) -> list[BaseMessage]:
    """Fresh customer system prompt; identity comes only from server config."""
    customer_code = str(_configurable(config).get("customer_code") or "").strip()
    contexto = (
        "Cliente con cuenta registrada en ERPNext."
        if customer_code
        else (
            "Remitente sin cuenta de cliente registrada. Si quiere comprar, "
            "registrá primero un contacto y derivá el alta comercial."
        )
    )
    # Sólo lo que el cliente PIDIÓ explícitamente fija su idioma. Sin nada
    # guardado, la regla es la de espejo de siempre: el modelo sigue el idioma
    # del último mensaje, que es el comportamiento anterior palabra por palabra.
    guardado = idioma.cliente_guardado(_configurable(config).get("actor_phone"))
    system = SYSTEM_ES_AR.format(
        NEGOCIO=os.getenv("NOMBRE_NEGOCIO", "la empresa"),
        CONTEXTO_CLIENTE=contexto,
        HORARIO=os.getenv("HORARIO_ATENCION", "lunes a viernes de 8 a 17"),
        HOY=business_today(),
        IDIOMA_REGLA=idioma.regla_prompt(guardado),
    )
    return [SystemMessage(content=system), *_mensajes(state)]


def prompt_gerencia(state, config: RunnableConfig) -> list[BaseMessage]:
    del config
    # El equipo NO espeja: contesta en el idioma que fijó el dueño, y mientras
    # nadie lo fije, en el de por defecto.
    system = SYSTEM_GERENCIA.format(
        NEGOCIO=os.getenv("NOMBRE_NEGOCIO", "la empresa"),
        USUARIO="miembro autorizado del equipo",
        HOY=business_today(),
        IDIOMA_REGLA=idioma.regla_prompt(idioma.gerencia()),
    )
    return [SystemMessage(content=system), *_mensajes(state)]


def texto_plano(msg) -> str:
    """Providers differ: some return a string, Gemini returns a list of
    content blocks. Always hand WhatsApp plain text."""
    c = getattr(msg, "content", msg)
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        partes = [
            b.get("text", "")
            for b in c
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in partes if p).strip() or str(c)
    return str(c)
