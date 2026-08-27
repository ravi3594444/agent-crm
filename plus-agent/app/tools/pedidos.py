"""Write tools. Everything here creates DRAFTS only.

A human opens ERPNext and clicks Submit. Only then does stock move and
only then does the ARCA factura get its CAE. The agent has no route to
submit — its ERPNext Role does not include the permission.
"""
from datetime import date, timedelta

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app import erpnext, policy
from app.notificar import notificar_equipo


class LineaPedido(BaseModel):
    item_code: str = Field(description="Código exacto del producto, de buscar_producto")
    cantidad: float = Field(gt=0, description="Cantidad solicitada")


@tool
def crear_lead(nombre: str, telefono: str, nota: str = "") -> str:
    """Registra un cliente potencial nuevo (que aún no está en el sistema).
    Usar cuando escribe alguien desconocido y muestra interés."""
    doc = erpnext.create_doc(
        "Lead",
        {
            "lead_name": nombre,
            "mobile_no": telefono,
            "source": "WhatsApp",
            "notes": nota,
        },
    )
    erpnext.add_comment("Lead", doc["name"], "Creado por Agente IA vía WhatsApp.")
    return f"Contacto registrado como {doc['name']}."


@tool
def crear_pedido(
    cliente: str,
    lineas: list[LineaPedido],
    fecha_entrega: str | None = None,
    thread_id: str = "",
) -> str:
    """Crea un pedido en BORRADOR para que el equipo lo revise y confirme.

    IMPORTANTE: esto NO confirma el pedido. Siempre decirle al cliente que
    el pedido queda pendiente de confirmación por el equipo.
    Verificar stock con consultar_stock antes de usar esta herramienta.
    """
    if not lineas:
        return "No puedo crear un pedido vacío."

    entrega = fecha_entrega or (date.today() + timedelta(days=1)).isoformat()

    doc = erpnext.create_doc(
        "Sales Order",
        {
            "customer": cliente,
            "delivery_date": entrega,
            "order_type": "Sales",
            "items": [
                {"item_code": l.item_code, "qty": l.cantidad, "delivery_date": entrega}
                for l in lineas
            ],
        },
    )
    erpnext.add_comment(
        "Sales Order",
        doc["name"],
        f"Borrador creado por Agente IA vía WhatsApp. Hilo: {thread_id or 'n/d'}. "
        f"Requiere revisión y confirmación humana.",
    )
    detalle = ", ".join(f"{l.cantidad:g} x {l.item_code}" for l in lineas)

    # --- the wait-killer ---------------------------------------------------
    # Deterministic policy decides. The agent does not, and cannot.
    completo = erpnext.get_doc("Sales Order", doc["name"])
    decision = policy.evaluar(completo)

    if decision.auto:
        erpnext.submit_doc("Sales Order", doc["name"])
        erpnext.add_comment(
            "Sales Order", doc["name"],
            "Auto-confirmado: cliente conocido, precio de lista, stock verificado, sin deuda.",
        )
        notificar_equipo(doc["name"], completo, auto=True)
        return (
            f"CONFIRMADO al instante. Pedido {doc['name']} ({detalle}), "
            f"entrega {entrega}. Decile al cliente que ya está confirmado."
        )

    erpnext.add_comment(
        "Sales Order", doc["name"],
        f"Requiere revisión humana: {decision}",
    )
    notificar_equipo(doc["name"], completo, auto=False, motivos=str(decision))
    return (
        f"Pedido {doc['name']} tomado ({detalle}), entrega {entrega}. "
        f"Decile al cliente que quedó tomado con ese número y que le confirmás "
        f"en unos minutos. NO digas que está confirmado."
    )


@tool
def escalar_a_humano(motivo: str, thread_id: str = "") -> str:
    """Deriva la conversación a una persona del equipo. Usar ante reclamos,
    pedidos de descuento, temas de pago, o cuando no estés seguro."""
    doc = erpnext.create_doc(
        "ToDo",
        {
            "description": f"[WhatsApp] Escalado por Agente IA: {motivo} (hilo {thread_id})",
            "priority": "High",
        },
    )
    return f"Derivado al equipo (tarea {doc['name']}). Alguien continúa la conversación."
