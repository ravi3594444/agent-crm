"""Write tools. Everything here creates DRAFTS only.

A human opens ERPNext and clicks Submit. Only then does stock move and
only then does the ARCA factura get its CAE. The agent has no route to
submit — its ERPNext Role does not include the permission.

EL CLIENTE NO ES UN PARÁMETRO
`crear_pedido` ya no recibe `cliente`. Lo resuelve el webhook desde el
número de teléfono y viaja por `config`, invisible para el modelo. Antes el
modelo elegía a nombre de quién cargar el pedido, y lo único que lo
contenía era una línea del prompt. Ver app/tools/alcance.py.
"""

from __future__ import annotations

from datetime import date, timedelta

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app import erpnext, lock, log, policy
from app.notificar import notificar_equipo
from app.tools import alcance

_log = log.get("pedidos")


class LineaPedido(BaseModel):
    item_code: str = Field(description="Código exacto del producto, de buscar_producto")
    cantidad: float = Field(gt=0, description="Cantidad solicitada")


@tool
def crear_lead(nombre: str, nota: str = "", config: RunnableConfig = None) -> str:
    """Registra un cliente potencial nuevo (que aún no está en el sistema).
    Usar cuando escribe alguien desconocido y muestra interés.

    El teléfono lo tomo yo del mensaje, no hace falta que lo pases.
    """
    tel = alcance.telefono(config)
    try:
        doc = erpnext.create_doc(
            "Lead",
            {
                "lead_name": nombre,
                "mobile_no": tel,
                "source": "WhatsApp",
                "notes": nota,
            },
        )
    except erpnext.ERPNextError:
        return "No pude registrar el contacto ahora. Derivá a una persona con escalar_a_humano."
    erpnext.add_comment("Lead", doc["name"], f"Creado por Agente IA vía WhatsApp ({tel}).")
    return f"Contacto registrado como {doc['name']}."


@tool
def crear_pedido(
    lineas: list[LineaPedido],
    fecha_entrega: str | None = None,
    config: RunnableConfig = None,
) -> str:
    """Crea un pedido en BORRADOR para que el equipo lo revise y confirme.

    IMPORTANTE: esto NO confirma el pedido. Siempre decirle al cliente que
    el pedido queda pendiente de confirmación por el equipo.
    Verificar stock con consultar_stock antes de usar esta herramienta.

    El cliente lo identifico yo por su teléfono: no lo pases como parámetro
    y no le pidas su código.
    """
    if not lineas:
        return "No puedo crear un pedido vacío."

    cliente = alcance.cliente_code(config)
    if not cliente:
        return (
            "Todavía no tengo la ficha de este cliente, así que no puedo cargarle "
            "un pedido. Registralo primero con crear_lead y derivá con "
            "escalar_a_humano para que el equipo lo dé de alta."
        )

    if fecha_entrega:
        try:
            entrega = date.fromisoformat(fecha_entrega[:10]).isoformat()
        except ValueError:
            return f"No entendí la fecha '{fecha_entrega}'. Pedila como AAAA-MM-DD."
    else:
        entrega = (date.today() + timedelta(days=1)).isoformat()

    hilo = alcance.thread_id(config)

    try:
        doc = erpnext.create_doc(
            "Sales Order",
            {
                "customer": cliente,
                "delivery_date": entrega,
                "order_type": "Sales",
                "items": [
                    {"item_code": linea.item_code, "qty": linea.cantidad, "delivery_date": entrega}
                    for linea in lineas
                ],
            },
        )
    except erpnext.ERPNextError as e:
        _log.error("no pude crear el pedido de %s: %s", cliente, e)
        return (
            "No pude cargar el pedido en el sistema. Pedile disculpas, decile que "
            "el equipo lo carga a mano, y usá escalar_a_humano."
        )

    erpnext.add_comment(
        "Sales Order",
        doc["name"],
        f"Borrador creado por Agente IA vía WhatsApp. Hilo: {hilo or 'n/d'}. "
        f"Requiere revisión y confirmación humana.",
    )
    detalle = ", ".join(f"{linea.cantidad:g} x {linea.item_code}" for linea in lineas)

    return _decidir(doc["name"], detalle, entrega)


def _decidir(nombre: str, detalle: str, entrega: str) -> str:
    """Evaluar la política y, si corresponde, confirmar — todo bajo lock.

    El lock cierra la ventana entre "verifiqué stock" y "confirmé": sin él,
    dos pedidos simultáneos por los últimos litros pasan los dos. Si no se
    consigue el lock, no auto-confirmamos: va a revisión humana, que es el
    lado seguro.
    """
    try:
        completo = erpnext.get_doc("Sales Order", nombre)
    except erpnext.ERPNextError:
        _log.error("creé %s pero no pude releerlo para evaluarlo", nombre)
        return _respuesta_pendiente(nombre, detalle, entrega)

    if not policy.activa():
        # Sin auto-confirmación no hace falta lock ni evaluación completa.
        erpnext.add_comment(
            "Sales Order", nombre, "Requiere revisión humana: auto-confirmación desactivada"
        )
        notificar_equipo(nombre, completo, auto=False, motivos="auto-confirmación desactivada")
        return _respuesta_pendiente(nombre, detalle, entrega)

    decision: policy.Decision | None = None
    with lock.tomar("auto-confirmar") as conseguido:
        if not conseguido:
            motivo = "no pude tomar el lock de auto-confirmación"
            _log.warning("%s: %s -> revisión humana", nombre, motivo)
            erpnext.add_comment("Sales Order", nombre, f"Requiere revisión humana: {motivo}")
            notificar_equipo(nombre, completo, auto=False, motivos=motivo)
            return _respuesta_pendiente(nombre, detalle, entrega)

        decision = policy.evaluar(completo)

        if decision.auto:
            try:
                erpnext.submit_doc("Sales Order", nombre)
            except erpnext.ERPNextError as e:
                _log.error("policy aprobó %s pero el submit falló: %s", nombre, e)
                erpnext.add_comment(
                    "Sales Order",
                    nombre,
                    f"La política aprobó pero el submit falló ({e}). Requiere revisión humana.",
                )
                notificar_equipo(nombre, completo, auto=False, motivos=f"el submit falló: {e}")
                return _respuesta_pendiente(nombre, detalle, entrega)

            erpnext.add_comment(
                "Sales Order",
                nombre,
                "Auto-confirmado: cliente conocido, precio de lista, stock verificado, sin deuda.",
            )
            notificar_equipo(nombre, completo, auto=True)
            return (
                f"CONFIRMADO al instante. Pedido {nombre} ({detalle}), "
                f"entrega {entrega}. Decile al cliente que ya está confirmado."
            )

    # Fuera del lock a propósito: notificar es lento (HTTP a Meta) y no
    # necesita exclusión. `decision` está asignada en todo camino que llega
    # hasta acá.
    motivos = str(decision) if decision else "no pude evaluar la política"
    erpnext.add_comment("Sales Order", nombre, f"Requiere revisión humana: {motivos}")
    notificar_equipo(nombre, completo, auto=False, motivos=motivos)
    return _respuesta_pendiente(nombre, detalle, entrega)


def _respuesta_pendiente(nombre: str, detalle: str, entrega: str) -> str:
    return (
        f"Pedido {nombre} tomado ({detalle}), entrega {entrega}. "
        f"Decile al cliente que quedó tomado con ese número y que le confirmás "
        f"en unos minutos. NO digas que está confirmado."
    )


@tool
def escalar_a_humano(motivo: str, config: RunnableConfig = None) -> str:
    """Deriva la conversación a una persona del equipo. Usar ante reclamos,
    pedidos de descuento, temas de pago, o cuando no estés seguro."""
    from app.notificar import avisar_escalamiento  # evita import circular

    hilo = alcance.thread_id(config)
    tel = alcance.telefono(config)
    cliente = alcance.cliente_code(config)

    tarea = ""
    try:
        doc = erpnext.create_doc(
            "ToDo",
            {
                "description": (
                    f"[WhatsApp] Escalado por Agente IA: {motivo} "
                    f"(tel {tel or 'n/d'}, cliente {cliente or 'no registrado'}, hilo {hilo or 'n/d'})"
                ),
                "priority": "High",
            },
        )
        tarea = doc["name"]
    except erpnext.ERPNextError as e:
        _log.error("no pude crear el ToDo de escalamiento: %s", e)

    # ESTO es lo que faltaba: la tarea en ERPNext no la ve nadie hasta que
    # alguien entra al sistema. El aviso por WhatsApp sí.
    avisar_escalamiento(motivo=motivo, telefono=tel, cliente=cliente, tarea=tarea)

    return "Derivado al equipo. Decile que una persona sigue la conversación en un rato."
