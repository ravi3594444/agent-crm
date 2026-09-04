"""Los escenarios de validación, y el guión que los hace pasar sin modelo.

Cada escenario es una lista de PASOS que el piloto manda como mensajes de
WhatsApp firmados, más lo que hay que ver después. El guión de abajo es lo que
un modelo haría: qué herramienta llamar, con qué argumentos y en qué orden.

En modo Gemini de verdad el guión NO se usa: decide el modelo. Los escenarios
son los mismos, así que la comparación entre los dos modos es directa —lo que
cambia es quién eligió las herramientas.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from demo import datos
from demo.falso_modelo import (
    ULTIMO_PEDIDO,
    ULTIMO_RESULTADO,
    Llamada,
    Regla,
    Texto,
    contiene,
)

# --------------------------------------------------------------- los pasos


@dataclass
class Paso:
    """Un mensaje de WhatsApp y qué tiene que pasar después."""

    quien: str            # teléfono del remitente
    texto: str
    espera_respuesta: bool = True
    # Fragmentos que TIENEN que aparecer en lo que recibe el remitente.
    espera: list[str] = field(default_factory=list)
    # Fragmentos que NO pueden aparecer (p. ej. "confirmado" cuando no lo está).
    prohibe: list[str] = field(default_factory=list)
    # Documentos que tienen que quedar así: {"Sales Order/*": {"docstatus": 0}}
    documentos: dict[str, dict] = field(default_factory=dict)
    nota: str = ""


@dataclass
class Escenario:
    clave: str
    titulo: str
    pasos: list[Paso]
    # Reiniciar el contenedor del agente ANTES de este paso (índice).
    reiniciar_antes_de: int | None = None
    # Entorno distinto para este escenario (el agente se recrea con él).
    entorno: dict[str, str] = field(default_factory=dict)
    porque: str = ""


CLIENTE = datos.TELEFONO_HABITUAL
NUEVO = datos.TELEFONO_NUEVO
DUENO = datos.TELEFONO_DUENO


def escenarios() -> list[Escenario]:
    return [
        Escenario(
            "pedido_cliente_existente",
            "Un cliente que ya existe hace un pedido",
            porque="El camino más común. El pedido tiene que quedar en "
                   "BORRADOR: con AUTO_CONFIRM_MAX en 0 nada se confirma solo.",
            pasos=[
                Paso(CLIENTE, "hola, tenés leche entera?",
                     espera=["LECHE-ENT-1L"]),
                Paso(CLIENTE, "dame 10 unidades de leche entera para mañana",
                     espera=["SAL-ORD"], prohibe=["confirmado"],
                     documentos={"Sales Order/*": {"docstatus": 0}}),
            ],
        ),
        Escenario(
            "alta_y_pedido",
            "Un cliente nuevo se registra y pide",
            porque="El alta usa el teléfono del webhook firmado, nunca uno que "
                   "diga el modelo, y el pedido va a la dirección que dio.",
            pasos=[
                Paso(NUEVO, "hola, quiero hacer un pedido"),
                Paso(NUEVO,
                     "soy Panaderia La Nueva, estamos en San Martin 450, "
                     "Villa Allende, CP 5105",
                     espera=["Panaderia La Nueva"]),
                Paso(NUEVO, "mandame 20 unidades de yogur de frutilla para mañana",
                     espera=["SAL-ORD"],
                     documentos={"Sales Order/*": {"docstatus": 0}}),
            ],
        ),
        Escenario(
            "confirmacion_automatica",
            "Un pedido que se confirma solo, sin que nadie lo toque",
            porque="El único escenario donde la política dice sí. Exige TODO: "
                   "tope del dueño > 0, inventario contado hoy, precio de "
                   "lista, sin descuento, historial suficiente y sin deuda "
                   "vencida. Lo decide Python, nunca el modelo.",
            # Los cuatro topes que hacen falta. Ojo con el tercero: su default
            # es 0 y 0 significa "nadie lo configuró", que la política lee como
            # "no hay permiso". Sin ponerlo, NADA se auto-confirma nunca —y
            # `make check-env` lo informa como OK. Está en el informe.
            entorno={"AUTO_CONFIRM_MAX": "50000",
                     "AUTO_CONFIRM_MAX_DEBT": "1000",
                     "AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO": "100",
                     "AUTO_CONFIRM_MAX_CLIENTE_NUEVO": "10000"},
            pasos=[
                Paso(CLIENTE, "dame 10 unidades de leche entera para mañana",
                     espera=["SAL-ORD"],
                     documentos={"Sales Order/*": {"docstatus": 1}}),
            ],
        ),
        Escenario(
            "stock_insuficiente",
            "Un pedido más grande que el stock",
            porque="Nunca se promete lo que no hay. Del queso cremoso hay 3 kg "
                   "y se piden 20: el pedido no puede quedar confirmado.",
            pasos=[
                Paso(CLIENTE, f"necesito {datos.CANTIDAD_IMPOSIBLE} kilos de queso cremoso",
                     prohibe=["confirmado"]),
            ],
        ),
        Escenario(
            "pedido_descuento",
            "Un cliente pide descuento",
            porque="El agente de ventas no tiene ninguna herramienta que "
                   "otorgue un descuento. Tiene que derivar, no prometer.",
            pasos=[
                Paso(CLIENTE, "me hacés un descuento si te llevo 100 unidades?",
                     prohibe=["%", "descuento aprobado", "confirmado"]),
            ],
        ),
        Escenario(
            "entrega_excepcional",
            "Un cliente pide una entrega fuera de las reglas",
            porque="Ni el modelo ni el código deciden esto: queda una solicitud "
                   "para que la resuelva una persona.",
            pasos=[
                Paso(CLIENTE, "dame 10 unidades de manteca para mañana",
                     espera=["SAL-ORD"]),
                Paso(CLIENTE, "necesito que me lo lleven el domingo a la mañana"),
            ],
        ),
        Escenario(
            "gerente_aprueba",
            "El dueño aprueba un pedido por su número",
            porque="El camino determinístico: sin modelo, sin herramientas. "
                   "Lo confirma la credencial de política y nadie más.",
            pasos=[
                Paso(CLIENTE, "dame 10 unidades de leche entera para mañana",
                     espera=["SAL-ORD"]),
                Paso(DUENO, "confirmar " + ULTIMO_PEDIDO,
                     documentos={"Sales Order/*": {"docstatus": 1}}),
            ],
        ),
        Escenario(
            "gerente_rechaza",
            "El dueño rechaza un pedido por su número, con motivo",
            porque="Un rechazo deja de reservar stock y el cliente se tiene "
                   "que enterar. El pedido NO se confirma.",
            pasos=[
                Paso(CLIENTE, "dame 5 unidades de dulce de leche para mañana",
                     espera=["SAL-ORD"]),
                Paso(DUENO, "rechazar " + ULTIMO_PEDIDO + " no llegamos con el reparto",
                     documentos={"Sales Order/*": {"docstatus": 0}}),
            ],
        ),
        Escenario(
            "cliente_acepta",
            "El cliente acepta la oferta que le hicieron",
            porque="'acepto' se resuelve ANTES de que lo vea un modelo, y sólo "
                   "vale para una oferta abierta del que escribe.",
            pasos=[
                Paso(CLIENTE, "dame 10 unidades de leche descremada para mañana",
                     espera=["SAL-ORD"]),
                Paso(CLIENTE, "necesito que me lo lleven el domingo"),
                # Con fecha concreta: 'ok' aprueba lo que el cliente pidió
                # EN SUS PALABRAS, y esas palabras no traen una fecha que el
                # sistema pueda escribir (ver escenario aceptacion_sin_fecha).
                Paso(DUENO, "contraoferta " + ULTIMO_PEDIDO + " 2026-09-07 10:00 0"),
                Paso(CLIENTE, "acepto",
                     documentos={"Sales Order/*": {"docstatus": 1}}),
            ],
        ),
        Escenario(
            "aceptacion_sin_fecha",
            "El dueño aprueba con 'ok' una excepción que el cliente pidió en prosa",
            porque="HALLAZGO. 'ok' ofrece de vuelta exactamente lo que pidió el "
                   "cliente, y sus palabras ('el domingo') no traen una fecha "
                   "que el sistema pueda escribir. La oferta queda sin fecha y "
                   "la aceptación NUNCA puede cerrar: siempre cae en revisión "
                   "humana, con un mensaje que dice que algo cambió cuando no "
                   "cambió nada.",
            pasos=[
                Paso(CLIENTE, "dame 20 unidades de yogur de frutilla para mañana",
                     espera=["SAL-ORD"]),
                Paso(CLIENTE, "necesito que me lo lleven el domingo"),
                Paso(DUENO, "ok " + ULTIMO_PEDIDO),
                # Lo que se ve hoy: cae en revisión humana. Si algún día se
                # arregla, este escenario falla y hay que actualizarlo.
                Paso(CLIENTE, "acepto",
                     espera=["revisarlo con una persona"],
                     documentos={"Sales Order/*": {"docstatus": 0}}),
            ],
        ),
        Escenario(
            "preparar_despachar_cancelar",
            "Preparar el remito, despacharlo y cancelar",
            porque="Dos pasos separados a propósito: preparar deja un remito "
                   "en borrador, despachar lo confirma. Cancelar es de una "
                   "persona y no hay herramienta que lo haga.",
            pasos=[
                Paso(CLIENTE, "dame 10 unidades de leche entera para mañana",
                     espera=["SAL-ORD"]),
                Paso(DUENO, "confirmar " + ULTIMO_PEDIDO,
                     documentos={"Sales Order/*": {"docstatus": 1}}),
                Paso(DUENO, "preparar " + ULTIMO_PEDIDO,
                     documentos={"Delivery Note/*": {"docstatus": 0}}),
                Paso(DUENO, "despachar " + ULTIMO_PEDIDO,
                     documentos={"Delivery Note/*": {"docstatus": 1}}),
                # Con el remito ya despachado NO se cancela, y el sistema tiene
                # que decir por qué en vez de cancelar en cascada.
                Paso(DUENO, "cancelar " + ULTIMO_PEDIDO + " el cliente se arrepintio",
                     espera=["No cancelo"],
                     documentos={"Sales Order/*": {"docstatus": 1}}),
            ],
        ),
        Escenario(
            "cancelacion_sin_remito",
            "Cancelar un pedido confirmado que todavía no tiene remito",
            porque="La cancelación que sí procede. Es de una persona: ninguna "
                   "herramienta del agente de gerencia puede llegar acá.",
            pasos=[
                Paso(CLIENTE, "dame 10 unidades de leche entera para mañana",
                     espera=["SAL-ORD"]),
                Paso(DUENO, "confirmar " + ULTIMO_PEDIDO,
                     documentos={"Sales Order/*": {"docstatus": 1}}),
                Paso(DUENO, "cancelar " + ULTIMO_PEDIDO + " el cliente se arrepintio",
                     documentos={"Sales Order/*": {"docstatus": 2}}),
            ],
        ),
        Escenario(
            "reinicio_con_pendiente",
            "Reinicio del proceso con una solicitud abierta",
            porque="El estado de una solicitud vive en ERPNext, no en Redis. "
                   "Después de un reinicio con Redis vacío tiene que seguir "
                   "siendo legible y su vencimiento seguir vigente.",
            reiniciar_antes_de=2,
            pasos=[
                Paso(CLIENTE, "dame 10 unidades de manteca para mañana",
                     espera=["SAL-ORD"]),
                Paso(CLIENTE, "necesito que me lo lleven el domingo"),
                # Después del reinicio: el dueño pregunta por ese pedido y el
                # sistema tiene que saber que hay una solicitud abierta.
                Paso(DUENO, "ver " + ULTIMO_PEDIDO),
            ],
        ),
        Escenario(
            "gerencia_estado_del_sistema",
            "El dueño le pregunta al agente de gerencia cómo está el sistema",
            porque="HALLAZGO BLOQUEANTE. Es el único escenario que pasa por "
                   "una herramienta de gerencia con require_management, y no "
                   "funciona: app/main.py le pasa a responder_gerencia el HASH "
                   "del teléfono como actor_phone, así que router.es_equipo() "
                   "lo rechaza SIEMPRE. Le pasa a las 10 herramientas de "
                   "gerencia que piden autorización, para el dueño incluido.",
            pasos=[
                # Lo que se ve HOY. Cuando se arregle, este paso falla y hay
                # que darlo vuelta: es el testigo del bug, no su aceptación.
                Paso(DUENO, "como esta el sistema?",
                     espera=["no está autorizado"]),
            ],
        ),
    ]


# ----------------------------------------------------------------- el guión


def reglas() -> list[Regla]:
    """Lo que haría un modelo, escrito a mano. El primer match gana."""
    return [
        # -- gerencia: una sola herramienta, la de estado
        (contiene("como esta el sistema"), [
            Llamada("estado_del_sistema", {}),
            # Devuelve el resultado TAL CUAL. Un texto propio taparía que la
            # herramienta falló y el escenario pasaría sin probar nada.
            Texto(f"Estado del sistema:\n{ULTIMO_RESULTADO}"),
        ]),

        # -- catálogo
        (contiene("tenes leche"), [
            Llamada("buscar_producto", {"consulta": "leche"}),
            Texto("Tengo leche entera (LECHE-ENT-1L) y descremada "
                  "(LECHE-DESC-1L). ¿Cuántas unidades querés?"),
        ]),

        # -- pedidos, uno por producto
        (contiene("10 unidades de leche entera"), [
            Llamada("crear_pedido", {
                "lineas": [{"item_code": "LECHE-ENT-1L", "cantidad": 10,
                            "unidad": "Unidad"}],
                "fecha_entrega": "mañana"}),
            Texto(f"Listo, te dejé el pedido {ULTIMO_PEDIDO} anotado. Te confirmo en breve."),
        ]),
        (contiene("10 unidades de leche descremada"), [
            Llamada("crear_pedido", {
                "lineas": [{"item_code": "LECHE-DESC-1L", "cantidad": 10,
                            "unidad": "Unidad"}],
                "fecha_entrega": "mañana"}),
            Texto(f"Listo, te dejé el pedido {ULTIMO_PEDIDO} anotado. Te confirmo en breve."),
        ]),
        (contiene("10 unidades de manteca"), [
            Llamada("crear_pedido", {
                "lineas": [{"item_code": "MANTECA-200", "cantidad": 10,
                            "unidad": "Unidad"}],
                "fecha_entrega": "mañana"}),
            Texto(f"Listo, te dejé el pedido {ULTIMO_PEDIDO} anotado. Te confirmo en breve."),
        ]),
        (contiene("5 unidades de dulce de leche"), [
            Llamada("crear_pedido", {
                "lineas": [{"item_code": "DDL-400", "cantidad": 5,
                            "unidad": "Unidad"}],
                "fecha_entrega": "mañana"}),
            Texto(f"Listo, te dejé el pedido {ULTIMO_PEDIDO} anotado. Te confirmo en breve."),
        ]),
        (contiene("20 unidades de yogur"), [
            Llamada("crear_pedido", {
                "lineas": [{"item_code": "YOG-FRUT-190", "cantidad": 20,
                            "unidad": "Unidad"}],
                "fecha_entrega": "mañana"}),
            Texto(f"Listo, te dejé el pedido {ULTIMO_PEDIDO} anotado. Te confirmo en breve."),
        ]),

        # -- stock insuficiente: primero se consulta, y con eso se contesta
        (contiene("kilos de queso cremoso"), [
            Llamada("consultar_stock", {"item_code": datos.ITEM_SIN_STOCK}),
            Texto("De queso cremoso no tengo esa cantidad. No te la prometo: "
                  "decime si querés que te reserve lo que hay."),
        ]),

        # -- descuento: no hay herramienta que lo dé, se deriva
        (contiene("descuento"), [
            Llamada("escalar_a_humano",
                    {"motivo": "el cliente pide descuento por volumen"}),
            Texto("El descuento lo tiene que autorizar el dueño. Le paso tu "
                  "pedido y te contestamos."),
        ]),

        # -- alta de cliente nuevo
        (contiene("quiero hacer un pedido"), [
            Texto("¡Hola! Para tomarte el pedido necesito el nombre del "
                  "negocio y la dirección de entrega."),
        ]),
        (contiene("panaderia la nueva"), [
            Llamada("crear_cliente", {
                "nombre": "Panaderia La Nueva",
                "direccion": {"calle": "San Martin 450",
                              "localidad": "Villa Allende",
                              "codigo_postal": "5105", "referencia": ""}}),
            Texto("Te registré como Panaderia La Nueva. ¿Qué necesitás?"),
        ]),

        # -- entrega excepcional: se pide, no se decide
        (contiene("me lo lleven el domingo"), [
            Llamada("pedir_excepcion_de_entrega", {
                "numero_de_pedido": ULTIMO_PEDIDO,
                "lo_que_pidio_el_cliente": "necesito que me lo lleven el domingo"}),
            Texto("Lo pasé para que lo autorice una persona. Te aviso en cuanto "
                  "me contesten."),
        ]),
    ]
