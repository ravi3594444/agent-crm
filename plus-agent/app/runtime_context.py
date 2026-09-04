"""Server-authenticated context injected into tools via RunnableConfig.

EL TELÉFONO ES LA IDENTIDAD; EL HASH NO ES NINGUNA IDENTIDAD.
``actor_phone`` es el número que firmó Meta, en la forma canónica de
app/telefono.py, y es lo único con lo que se autoriza. Para un log o una
métrica se usa ``tag``, que es un hash y no vuelve a ser un número nunca.
Pasar un hash donde va el teléfono no "degrada" la autorización: la rompe en
silencio —``router.es_equipo`` contesta False para todo hash— y el dueño
recibe "no estás autorizado" de su propio sistema. Ya pasó; ver el README.

Nada de acá puede venir de un mensaje, de un prompt ni de un argumento de
herramienta: ``configurable`` lo llena app/graph.py con lo que resolvió el
webhook, y ninguna herramienta de gerencia acepta un teléfono como parámetro
(lo verifica tests/test_autorizacion.py).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from langchain_core.runnables import RunnableConfig

from app import telefono as _telefono

# Una sola negativa para todas las herramientas de gerencia: la respuesta no
# dice qué número llamó ni qué habría contestado la herramienta.
SIN_PERMISO = "Ese número no está autorizado para esto. No consulté ni cambié nada."


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
        """El alcance que puso el webhook. NO alcanza para elevar privilegios."""
        return self.scope == "management"

    @property
    def gerencia_verificada(self) -> bool:
        """Alcance de gerencia Y un teléfono que está en la lista del equipo.

        Es lo único que habilita una lectura o una escritura de gerencia. El
        alcance por sí solo no: lo fija app/graph.py y una sola puerta que se
        abre mal entrega el negocio entero, así que la lista se vuelve a
        consultar acá adentro.
        """
        from app import router

        return (
            self.is_management
            and bool(self.actor_phone)
            and router.es_equipo(self.actor_phone)
        )

    @property
    def tag(self) -> str:
        """Hash corto del teléfono. SÓLO para logs y métricas."""
        return hashlib.sha256(self.actor_phone.encode()).hexdigest()[:10]


def actor_context(config: RunnableConfig) -> ActorContext:
    configurable: dict[str, Any] = dict((config or {}).get("configurable") or {})
    scope = str(configurable.get("actor_scope") or "").strip()
    if scope not in {"customer", "management"}:
        raise RuntimeContextError("contexto de autorización ausente")
    return ActorContext(
        scope=scope,
        customer_code=str(configurable.get("customer_code") or "").strip(),
        # Canónico o vacío. Un hash de teléfono no sobrevive a esto como un
        # número que `es_equipo` pueda reconocer, así que falla cerrado.
        actor_phone=_telefono.normalizar(configurable.get("actor_phone")),
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
    actor = actor_context(config)
    if not actor.is_management or not actor.actor_phone:
        raise RuntimeContextError("gerencia autenticada ausente")
    if not actor.gerencia_verificada:
        raise RuntimeContextError("teléfono no autorizado")
    return actor
