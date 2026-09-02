"""Server-authenticated context injected into tools via RunnableConfig."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.runnables import RunnableConfig


class RuntimeContextError(RuntimeError):
    """Required server-authenticated tool context is absent or invalid."""


@dataclass(frozen=True)
class ActorContext:
    scope: str
    customer_code: str
    actor_phone: str
    inbound_message_id: str
    thread_id: str

    @property
    def is_management(self) -> bool:
        return self.scope == "management"


def actor_context(config: RunnableConfig) -> ActorContext:
    configurable: dict[str, Any] = dict((config or {}).get("configurable") or {})
    scope = str(configurable.get("actor_scope") or "").strip()
    if scope not in {"customer", "management"}:
        raise RuntimeContextError("contexto de autorización ausente")
    return ActorContext(
        scope=scope,
        customer_code=str(configurable.get("customer_code") or "").strip(),
        actor_phone=str(configurable.get("actor_phone") or "").strip(),
        inbound_message_id=str(
            configurable.get("inbound_message_id") or ""
        ).strip(),
        thread_id=str(configurable.get("thread_id") or "").strip(),
    )


def require_customer(config: RunnableConfig) -> ActorContext:
    actor = actor_context(config)
    if actor.scope != "customer" or not actor.customer_code:
        raise RuntimeContextError("cliente autenticado ausente")
    return actor


def require_management(config: RunnableConfig) -> ActorContext:
    """Management scope AND a phone that is on the staff list.

    The router already sends only staff phones to the management agent, so this
    is a second, independent check. A tool that changes what confirms orders
    without a human present should not rely on one gate having held.
    """
    from app import router

    actor = actor_context(config)
    if not actor.is_management or not actor.actor_phone:
        raise RuntimeContextError("gerencia autenticada ausente")
    if not router.es_equipo(actor.actor_phone):
        raise RuntimeContextError("teléfono no autorizado")
    return actor
