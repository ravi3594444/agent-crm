"""Quién llama, y con qué autoridad.

EL TELÉFONO SIGUE SIENDO LA IDENTIDAD, PERO NO EL MISMO TELÉFONO
En WhatsApp el número lo firma Meta: llega en el webhook y no hay forma de que
el que escribe lo elija. Acá no. El número de una llamada lo pone la red del que
llama, y en telefonía se falsifica sin equipo especial. Es una identidad MÁS
DÉBIL que la de WhatsApp, y el sistema tiene que saberlo.

De ahí sale la única regla de este módulo:

    un número de voz nunca es un número del equipo.

Aunque el que llama marque desde el teléfono del dueño, este canal lo atiende
como cliente. `scope` es siempre "customer" y no hay parámetro para cambiarlo —
`ActorContext.gerencia_verificada` exige alcance de gerencia, así que toda
herramienta de gerencia queda cerrada por construcción y no por prompt. Un
`caller_id` falsificado consigue, como máximo, lo que consigue un cliente: ver
su propio catálogo y dejar un pedido que alguien revisa.

`ORIGEN_VERIFICADO` es lo que separa una demo de un cliente en producción. En el
navegador no hay número de ninguna clase: el que llama es anónimo y nada lo
identifica, así que `de_navegador()` no le da cuenta a nadie y el agente atiende
como a un desconocido —catálogo y alta, que es exactamente lo que puede hacer un
desconocido por WhatsApp—.
"""
from __future__ import annotations

from app import clientes, erpnext, router, telefono as _telefono

# Lo que viaja al contexto de las herramientas. Es el mismo diccionario que
# arma `app/graph.py` para WhatsApp; las herramientas no distinguen el canal, y
# es bueno que no lo hagan: la autorización tiene que estar en un solo lugar.
_ALCANCE = "customer"


class LlamadaDeEquipo(RuntimeError):
    """Un número del equipo llamó al canal de clientes."""


def de_telefono(numero: str, *, id_llamada: str) -> dict[str, str]:
    """Contexto de autorización para una llamada con número de origen.

    Levanta `LlamadaDeEquipo` si el número está en `TELEFONOS_EQUIPO`. No es
    para proteger al dueño de sí mismo: es para que una llamada del equipo no
    caiga en el agente de clientes y termine cargándole un pedido al dueño como
    si fuera un almacén. El que atiende al equipo es WhatsApp, con el router
    determinista y los códigos — que por teléfono no pueden existir.
    """
    canonico = _telefono.normalizar(numero)
    if canonico and router.es_equipo(canonico):
        raise LlamadaDeEquipo(canonico)
    customer_code = ""
    if canonico:
        # Mismo lookup que `main._contexto`, y por eso tolera los formatos
        # tipeados a mano que hay en las fichas (+54 9 351 …, 0351 15 …).
        ficha = clientes.buscar_por_telefono(canonico, get_list=erpnext.get_list)
        if ficha:
            customer_code = str(ficha["name"])
    return _configurable(customer_code=customer_code, actor_phone=canonico, id_llamada=id_llamada)


def de_navegador(*, id_llamada: str) -> dict[str, str]:
    """Contexto para una llamada del navegador: nadie, sin cuenta y sin teléfono.

    No hay `caller_id` en una pestaña. Dar cuenta acá —por un número tipeado,
    por un parámetro de la URL— sería identidad declarada por el que llama, que
    es el único tipo de identidad que este sistema nunca aceptó: cualquiera
    escribiría el número de otro y leería sus pedidos.

    Sin `customer_code`, `require_customer` rechaza todo lo que escribe sobre
    una cuenta ajena y el agente queda con catálogo, stock y alta — la demo
    completa salvo pedir en nombre de otro.
    """
    return _configurable(customer_code="", actor_phone="", id_llamada=id_llamada)


def _configurable(*, customer_code: str, actor_phone: str, id_llamada: str) -> dict[str, str]:
    return {
        "actor_scope": _ALCANCE,
        "customer_code": customer_code,
        "actor_phone": actor_phone,
        # El id de la llamada ocupa el lugar del id del mensaje de Meta: es lo
        # que hace idempotente a `crear_pedido` (`tools/pedidos.py::_message_key`),
        # así que un reintento del relay sobre la misma llamada no crea dos
        # pedidos. Con prefijo, porque un id de AssemblyAI y uno de Meta no
        # comparten forma y nada debería poder confundirlos.
        "inbound_message_id": f"voz:{id_llamada}" if id_llamada else "",
        "thread_id": f"voz:{id_llamada}" if id_llamada else "",
    }
