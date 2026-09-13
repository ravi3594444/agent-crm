"""El agente de clientes, armado para el relay de voz. Uno por llamada.

POR QUÉ SE ARMA POR LLAMADA Y NO UNA VEZ
`run_tool` del relay recibe `(nombre, argumentos)` y nada más: no hay lugar para
pasarle quién llamó. Así que la identidad se ata acá, en una clausura, y cada
llamada tiene la suya. Un agente construido una vez al importar sería un agente
con el contexto de autorización de la primera llamada del día, sirviéndoselo a
todas las demás — el peor error posible en este sistema y el más fácil de no ver,
porque en una demo de una sola llamada funciona perfecto.

El prompt también se arma acá, y por eso la fecha y el idioma guardado son los
del momento en que sonó el teléfono.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

from app import erpnext
from app.voz import herramientas, identidad, prompt as prompt_voz


def _con_credencial_de_cliente(configurable: dict[str, str]):
    """La clausura que corre una herramienta con la identidad de ESTA llamada.

    `customer_scope()` es lo mismo que hace `graph.responder_cliente`: fija la
    credencial de ERPNext del agente de clientes —la que lee y crea borradores,
    y no puede hacer Submit— para el hilo que atiende la llamada. Sin esto, un
    hilo sin scope cae en la clave de cliente igual, pero por defecto y no por
    decisión; que sea explícito es lo que hace que se lea en el código.
    """

    def correr(nombre: str, argumentos: dict[str, Any]) -> tuple[str, bool]:
        with erpnext.customer_scope():
            return herramientas.ejecutar(nombre, argumentos, configurable=configurable)

    return correr


def para_llamada(configurable: dict[str, str]):
    """Un `AgentDefinition` atado al contexto de autorización que se le pasa."""
    # Import adentro: `calling_agent` es una dependencia sólo del canal de voz,
    # y el resto de app/voz/ —las herramientas, el prompt, la identidad— se
    # importa y se testea sin ella.
    from calling_agent.agent_spec import AgentDefinition

    telefono = configurable.get("actor_phone", "")
    return AgentDefinition(
        name="plus-clientes",
        build_prompt=lambda: prompt_voz.construir(
            customer_code=configurable.get("customer_code", ""),
            telefono=telefono,
        ),
        greeting=prompt_voz.saludo(),
        tools=herramientas.especificaciones(),
        run_tool=_con_credencial_de_cliente(configurable),
        # Una voz multilingüe: el cliente habla español rioplatense y la regla
        # de idioma del prompt lo hace espejar al que llama. Una voz sólo en
        # inglés contestaría en inglés con acento y rompería esa regla desde
        # afuera del prompt, que es donde no se puede arreglar.
        voice=os.getenv("VOZ_AGENTE", "diego").strip() or "diego",
    )


def desde_navegador():
    """El agente de una llamada del navegador: anónimo, sin cuenta.

    Es lo que usa la demo. `AGENT_FACTORY` apunta acá, y el relay la llama una
    vez por conexión — de ahí sale el id, y de ahí que dos pestañas abiertas a
    la vez no compartan ni identidad ni idempotencia.
    """
    return para_llamada(identidad.de_navegador(id_llamada=uuid.uuid4().hex))


def desde_telefono(numero: str, *, id_llamada: str = ""):
    """El agente de una llamada telefónica real, identificada por `caller_id`.

    No lo usa todavía ningún transporte: el de telefonía es el que sigue
    (`transport/base.py` en el repo del relay ya está hecho para eso, y el µ-law
    de Telnyx/Twilio no necesita transcodificar). Está acá porque es el que
    decide qué significa el número, y esa decisión no es del transporte.

    Levanta `identidad.LlamadaDeEquipo` si el número es del equipo: por voz no
    hay agente de gerencia, y el motivo está en `app/voz/__init__.py`.
    """
    return para_llamada(
        identidad.de_telefono(numero, id_llamada=id_llamada or uuid.uuid4().hex)
    )
