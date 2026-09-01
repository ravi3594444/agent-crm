"""One-tap approval. The other half of removing the wait.

Instead of "open ERPNext, find the order, review, submit", the owner gets a
WhatsApp with buttons. He taps Confirmar from his lock screen. Two seconds.

BUG QUE TENÍA: iteraba `STAFF` (un `set`) y cortaba con `break` después del
primero. Con NOTIFICAR_SOLO_PRIMERO=true (el default), el "primero" era uno
al azar en cada arranque del proceso: los avisos le llegaban a un empleado
distinto cada vez y el dueño no se enteraba. Ahora STAFF es una lista
ordenada (app/router.py) y el corte es explícito.
"""

from __future__ import annotations

import os

from app import formato, log, router
from app.whatsapp import enviar_botones, enviar_mensaje

_log = log.get("notificar")


def _destinatarios() -> list[str]:
    """A quién avisamos. El orden es el de TELEFONOS_EQUIPO."""
    if not router.STAFF:
        _log.error(
            "no hay a quién notificar: TELEFONOS_EQUIPO está vacío. Los pedidos "
            "van a quedar en borrador sin que nadie se entere."
        )
        return []
    if os.getenv("NOTIFICAR_SOLO_PRIMERO", "true").lower() == "true":
        return router.STAFF[:1]
    return list(router.STAFF)


def notificar_equipo(nombre: str, so: dict, auto: bool, motivos: str = "") -> None:
    detalle = "\n".join(
        f"  · {float(i.get('qty') or 0):g} x {i.get('item_name') or i['item_code']}"
        for i in so.get("items", [])
    )
    cabecera = (
        f"✅ Pedido auto-confirmado {nombre}" if auto else f"🔔 Pedido {nombre} — necesita tu OK"
    )
    cuerpo = (
        f"{cabecera}\n"
        f"{so.get('customer_name') or so.get('customer')}\n"
        f"{detalle}\n"
        f"Total: {formato.pesos(float(so.get('grand_total') or 0))} · entrega {so.get('delivery_date')}"
    )
    if motivos:
        cuerpo += f"\n\nPor qué: {motivos}"

    for numero in _destinatarios():
        try:
            if auto:
                enviar_mensaje(numero, cuerpo)
            else:
                enviar_botones(
                    numero,
                    cuerpo,
                    [
                        {"id": f"ok:{nombre}", "title": "Confirmar"},
                        {"id": f"no:{nombre}", "title": "Rechazar"},
                        {"id": f"ver:{nombre}", "title": "Ver detalle"},
                    ],
                )
        except Exception as e:
            _log.error("no pude notificar a %s sobre %s: %s", numero, nombre, e)


def avisar_escalamiento(motivo: str, telefono: str, cliente: str, tarea: str = "") -> None:
    """Un escalamiento tiene que sonar en un teléfono, no solo crear un ToDo.

    El ToDo en ERPNext no lo ve nadie hasta que alguien entra al sistema. Si
    un cliente reclama y la única señal es una tarea, el reclamo espera
    hasta mañana.
    """
    cuerpo = (
        f"🙋 Un cliente necesita una persona\n"
        f"Tel: {telefono or 'n/d'}\n"
        f"Cliente: {cliente or 'no registrado'}\n"
        f"Motivo: {motivo}"
    )
    if tarea:
        cuerpo += f"\nTarea: {tarea}"
    for numero in _destinatarios():
        try:
            enviar_mensaje(numero, cuerpo)
        except Exception as e:
            _log.error("no pude avisar el escalamiento a %s: %s", numero, e)


def avisar_falla_tecnica(telefono: str, texto: str, error: str) -> None:
    """El mensaje de error le decía al cliente "ya avisé al equipo" y no
    avisaba a nadie. Ahora sí."""
    cuerpo = (
        f"⚠️ Falló un mensaje de WhatsApp\n"
        f"Tel: {telefono}\n"
        f"Mensaje: {texto[:300]}\n"
        f"Error: {error[:400]}"
    )
    for numero in _destinatarios():
        try:
            enviar_mensaje(numero, cuerpo)
        except Exception as e:
            _log.error("tampoco pude avisar la falla técnica a %s: %s", numero, e)
