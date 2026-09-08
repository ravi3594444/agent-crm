"""Los borradores que esperan a una persona: quiénes son, y qué se les anota.

QUÉ PROBLEMA RESUELVE
Un pedido que no se auto-confirma queda como borrador con un comentario
«Requiere revisión humana: …» y nada más. No hay una lista de esos pedidos en
ninguna parte: no son una solicitud de decisión (`app/solicitudes.py`, que sí
tiene plazo y oferta de respaldo), no son un aviso fallido, y `make decisiones`
los busca con grep en los logs de un contenedor que se borran al recrearlo.

Este módulo es la ÚNICA definición de «este borrador está esperando a alguien»,
para que el registro de sombra (W1) y —cuando llegue— el plazo con su aviso
honesto (W2) cuenten exactamente el mismo conjunto. Dos definiciones distintas
de la misma cola es cómo un resumen dice 14 y otro 11.

CÓMO CORRE
Desde el barrido que ya existe (`main._solicitudes_scheduler`, cada 60 s), en
su propio try/except al lado de `solicitudes.tick()`: un fallo acá no puede
saltear el vencimiento de una solicitud, y al revés.

Nada de este módulo escribe en el pedido ni le habla a un cliente. Enumera y
delega.
"""
from __future__ import annotations

from app import erpnext

# Los pedidos que crea el agente llevan la clave del mensaje de WhatsApp en
# po_no (`tools/pedidos.py::_message_key`), con este prefijo. Un borrador que
# alguien cargó a mano en ERPNext no es nuestro y no se toca.
PREFIJO_AGENTE = "WA-"
# Cuántos borradores se miran por ronda. ERPNext pagina y el barrido vuelve en
# 60 s; pedir "todos" es cómo una cola vieja hace un request de 414.
MAX_CANDIDATOS = 100


def borradores_esperando(limite: int = MAX_CANDIDATOS) -> list[dict]:
    """Borradores del agente que todavía espera decidir una persona.

    Devuelve [] cuando ERPNext no contesta, y eso NO es «no hay ninguno»: es
    «no sé». El que llama no puede convertirlo en un número para el dueño, sólo
    en «esta ronda no anoté nada».

    Deliberadamente NO filtra por `[confirmado-por-agente]` ni por `[solicitud]`
    en ERPNext: no se puede pedir "un comentario que NO existe" en un filtro de
    Frappe. `docstatus=0` ya descarta lo confirmado (confirmar es someter), y
    quien necesite excluir las que tienen una solicitud abierta lo pregunta con
    `solicitudes.vencimientos`, que lo hace en UNA lectura.
    """
    try:
        filas = erpnext.policy_get_list(
            "Sales Order",
            filters=[
                ["docstatus", "=", 0],
                ["po_no", "like", f"{PREFIJO_AGENTE}%"],
            ],
            fields=["name", "customer", "customer_name", "grand_total", "creation"],
            limit=max(1, int(limite or MAX_CANDIDATOS)),
            order_by="creation asc",
        )
    except Exception as exc:  # el barrido nunca muere por una ronda
        print(f"[pendientes] no pude listar los borradores: {type(exc).__name__}: {exc}")
        return []
    return [f for f in filas if str(f.get("name") or "").strip()]


def _sin_solicitud_abierta(pedidos: list[str]) -> list[str]:
    """Los que NO tienen una solicitud de decisión con plazo vivo.

    Los que la tienen ya están cubiertos: `app/solicitudes.py` les puso plazo,
    les avisa al cliente y les ofrece un respaldo cuando vencen. Anotarlos o
    empujarlos de nuevo desde acá sería el segundo aviso por el mismo pedido.

    Si la lectura falla se devuelven TODOS: la sombra es sólo un registro, y de
    los dos errores posibles —anotar uno que ya tenía plazo, o no anotar
    ninguno— el primero no le hace nada a nadie.
    """
    if not pedidos:
        return []
    from app import solicitudes

    try:
        con_plazo = solicitudes.vencimientos(pedidos)
    except Exception as exc:
        print(f"[pendientes] no pude ver qué borradores ya tienen plazo: {type(exc).__name__}")
        return list(pedidos)
    return [p for p in pedidos if p not in con_plazo]


def tick() -> int:
    """Una ronda del barrido. Devuelve cuántos registros de sombra se anotaron.

    Nunca levanta: corre en un hilo de fondo y un hipo de Redis o de ERPNext no
    puede parar el bucle que va a volver a intentar en un minuto.
    """
    from app import sombra

    if not sombra.encendido():
        return 0

    candidatos = borradores_esperando()
    if not candidatos:
        return 0

    por_nombre = {str(f["name"]): f for f in candidatos}
    elegibles = _sin_solicitud_abierta(list(por_nombre))

    anotados = 0
    for nombre in elegibles:
        if anotados >= sombra.POR_RONDA:
            break
        try:
            if sombra.ya_anotado(nombre) is not False:
                # True = ya tiene registro. None = ERPNext no contestó, y
                # anotar a ciegas duplicaría el registro del pedido.
                continue
            completo = erpnext.policy_get_doc("Sales Order", nombre)
            if int(completo.get("docstatus") or 0) != 0:
                continue  # alguien lo decidió entre el listado y ahora
            if sombra.anotar(nombre, completo):
                anotados += 1
        except Exception as exc:  # un pedido no arruina la ronda
            print(f"[pendientes] {nombre}: la sombra falló: {type(exc).__name__}: {exc}")
    return anotados
