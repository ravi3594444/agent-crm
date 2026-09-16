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

import os

from app import clientes, erpnext, router
from app import telefono as _telefono

# Lo que viaja al contexto de las herramientas. Es el mismo diccionario que
# arma `app/graph.py` para WhatsApp; las herramientas no distinguen el canal, y
# es bueno que no lo hagan: la autorización tiene que estar en un solo lugar.
_ALCANCE = "customer"


class LlamadaDeEquipo(RuntimeError):
    """Un número del equipo llamó al canal de clientes."""


class IdentidadDeclaradaApagada(RuntimeError):
    """Llegó un número por parámetro y `VOZ_NUMERO_POR_PARAMETRO` está apagado."""


def numero_por_parametro_habilitado() -> bool:
    """Si un número que llega por parámetro de conexión vale como identidad.

    **Apagado por default, y así tiene que quedar en producción.** Un número
    que llega por la URL lo eligió el que abre la página: no lo firmó Meta ni
    lo puso la red telefónica. Sirve para una demo del dueño —es la única forma
    de mostrar un pedido de punta a punta desde el navegador, donde no hay
    `caller_id` de ninguna clase— y para nada más.

    Lo que NO puede hacer, ni con esto encendido, está en `de_parametro`: no
    abre la cuenta de un cliente que ya existe. Un número declarado da de alta
    y pide para SÍ MISMO; nunca lee lo de otro.
    """
    return _encendido("VOZ_NUMERO_POR_PARAMETRO")


def _encendido(variable: str) -> bool:
    return os.getenv(variable, "").strip().lower() in {"1", "true", "si", "sí", "yes"}


def caller_id_confiable() -> bool:
    """Si el número que trae la telefonía alcanza para SER un cliente.

    **Apagado por default.** Un `caller_id` se falsifica sin equipo especial, y
    acá no alcanza con no darle el `customer_code`: `_cuenta_del_remitente`
    (tools/pedidos.py) resuelve la cuenta POR TELÉFONO cuando no hay código, así
    que entregar el número es entregar la cuenta. Las dos cosas o ninguna.

    Con esto apagado, una llamada telefónica se atiende como un desconocido:
    catálogo, precios y stock, y lo suyo lo ve una persona. Encenderlo es
    decidir que el `caller_id` de tu operador alcanza —una decisión del dueño,
    como el tope de auto-confirmación—, y lo que se gana es que el cliente
    conocido pida sin repetir quién es.

    Lo cazó una review sobre este PR. El módulo decía que el número se falsifica
    y después lo usaba igual para resolver la cuenta, que es la contradicción
    más cara posible: un desconocido que marca el número de la panadería le
    leía los pedidos y le cargaba otros.
    """
    return _encendido("VOZ_CONFIA_EN_CALLER_ID")


def de_telefono(numero: str, *, id_llamada: str) -> dict[str, str]:
    """Contexto de autorización para una llamada con número de origen.

    Da identidad **sólo** con `VOZ_CONFIA_EN_CALLER_ID` encendido, que está
    apagado por default. Sin eso devuelve el contexto de un desconocido, que es
    lo que un número falsificable vale mientras nadie decida lo contrario.

    Levanta `LlamadaDeEquipo` si el número está en `TELEFONOS_EQUIPO`. No es
    para proteger al dueño de sí mismo: es para que una llamada del equipo no
    caiga en el agente de clientes y termine cargándole un pedido al dueño como
    si fuera un almacén. El que atiende al equipo es WhatsApp, con el router
    determinista y los códigos — que por teléfono no pueden existir.
    """
    canonico = _telefono.normalizar(numero)
    if canonico and router.es_equipo(canonico):
        raise LlamadaDeEquipo(canonico)
    if not caller_id_confiable():
        # Sin el permiso explícito del dueño, un caller_id no es nadie. Ver
        # `caller_id_confiable`: entregar el teléfono ya entrega la cuenta.
        return de_navegador(id_llamada=id_llamada)
    customer_code = ""
    if canonico:
        # Mismo lookup que `main._contexto`, y por eso tolera los formatos
        # tipeados a mano que hay en las fichas (+54 9 351 …, 0351 15 …).
        ficha = _ficha_o_none(canonico)
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


def _ficha_y_si_se_pudo(canonico: str) -> tuple[dict | None, bool]:
    """(ficha, se_pudo_leer). Un ERPNext caído NO es «no tiene cuenta».

    Los dos son `None` para el que pregunta «¿tiene cuenta?», y confundirlos
    tiene costos opuestos según quién llama, así que la respuesta trae las dos
    cosas y decide cada llamador. Esto salió de correr el servidor de verdad:
    con ERPNext apagado, la excepción subía hasta el relay, que la trata como
    «este factory no sirve» y sirve SU agente de fábrica — el de restaurante.
    Un cliente de una distribuidora de lácteos escuchando a una recepcionista
    de restaurante es peor que cualquier degradación.
    """
    try:
        return clientes.buscar_por_telefono(canonico, get_list=erpnext.get_list), True
    except erpnext.ERPNextError as exc:
        print(f"[voz] no se pudo leer la ficha del que llama: {exc}")
        return None, False


def _ficha_o_none(canonico: str) -> dict | None:
    """Para `de_telefono`, donde el número lo puso la red y sigue valiendo.

    Sin ficha el que llama queda sin `customer_code` —no puede consultar ni
    pedir sobre una cuenta— pero conserva su teléfono verificado, que es lo
    que le permite darse de alta. Degradar eso también sería castigar al
    cliente por una caída que no es suya.
    """
    return _ficha_y_si_se_pudo(canonico)[0]


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


def de_parametro(numero: str, *, id_llamada: str) -> dict[str, str]:
    """Contexto para un número que llegó por parámetro de conexión. DEMO.

    El número NO sale de la conversación y el modelo no lo ve: viaja como
    parámetro de la URL del websocket, se fija antes de que el que llama diga
    una palabra y ninguna herramienta lo acepta como argumento. Por eso un
    cliente no puede hablar para cambiarlo — que es la propiedad que hace que
    esto sea una demo floja y no un agujero.

    Tres cosas que no hace:

    * no corre si `VOZ_NUMERO_POR_PARAMETRO` está apagado, que es el default;
    * no atiende un número del equipo, igual que `de_telefono`;
    * **no abre la cuenta de un cliente que ya existe**. Ésa es la diferencia
      con `de_telefono`, donde el número lo puso la red. Acá lo eligió quien
      abrió la página, así que resolver una cuenta existente sería dejar que
      cualquiera escriba el número de la panadería y le lea los pedidos. Un
      número declarado que ya tiene cuenta se atiende como desconocido: puede
      preguntar precios, y para lo suyo lo ve una persona.
    """
    if not numero_por_parametro_habilitado():
        raise IdentidadDeclaradaApagada(
            "VOZ_NUMERO_POR_PARAMETRO no está encendido"
        )
    canonico = _telefono.normalizar(numero)
    if not canonico:
        return de_navegador(id_llamada=id_llamada)
    if router.es_equipo(canonico):
        raise LlamadaDeEquipo(canonico)
    ficha, se_pudo_leer = _ficha_y_si_se_pudo(canonico)
    if ficha or not se_pudo_leer:
        # Ya tiene cuenta: anónimo, porque un número declarado nunca abre una
        # cuenta que ya existe. Y si ERPNext no contestó, TAMBIÉN anónimo: no
        # se pudo descartar que exista, y la dirección segura es la de no
        # entregarle un teléfono que a lo mejor es de otro.
        print("[voz] número por parámetro sin alta posible: se atiende anónimo")
        return de_navegador(id_llamada=id_llamada)
    return _configurable(
        customer_code="", actor_phone=canonico, id_llamada=id_llamada
    )
