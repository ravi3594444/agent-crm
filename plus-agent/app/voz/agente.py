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
from app.voz import herramientas, identidad
from app.voz import prompt as prompt_voz


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


def desde_navegador(parametros: dict[str, str] | None = None):
    """`AGENT_FACTORY`: el agente de UNA conexión del navegador.

    El relay la llama una vez por conexión y le pasa los parámetros de la URL
    del websocket. De ahí sale el id de llamada, y de ahí que dos pestañas
    abiertas a la vez no compartan ni identidad ni idempotencia.

    `?telefono=` sólo significa algo con `VOZ_NUMERO_POR_PARAMETRO` encendido,
    que es demo y está apagado por default. Sin eso —y sin el parámetro— el que
    llama es un desconocido: catálogo, stock y alta, que es exactamente lo que
    puede hacer un desconocido por WhatsApp.

    NUNCA LEVANTA, y no es prolijidad. El relay trata un factory que falla como
    «servime el agente de fábrica», y el de fábrica es el de RESTAURANTE. Con
    ERPNext caído un segundo, el que llama a la distribuidora de lácteos
    escuchaba a una recepcionista ofreciéndole mesa para dos. Lo encontró un
    arranque de verdad del servidor, no un test — los tests tenían el lookup
    mockeado, así que ninguno podía ver la excepción que subía.

    La degradación correcta es atender SIN cuenta: el que llama pregunta
    precios y lo suyo lo ve una persona.
    """
    id_llamada = uuid.uuid4().hex
    declarado = str((parametros or {}).get("telefono") or "").strip()
    if declarado:
        try:
            return para_llamada(identidad.de_parametro(declarado, id_llamada=id_llamada))
        except identidad.IdentidadDeclaradaApagada:
            print("[voz] llegó ?telefono= y VOZ_NUMERO_POR_PARAMETRO está apagado")
        except identidad.LlamadaDeEquipo:
            print("[voz] llegó ?telefono= de un número del equipo: se atiende anónimo")
        except Exception as exc:
            print(f"[voz] no se pudo resolver ?telefono=: {type(exc).__name__}: {exc}")
    return para_llamada(identidad.de_navegador(id_llamada=id_llamada))


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
