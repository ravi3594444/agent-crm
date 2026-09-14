"""Manual decisions taken by the HUMAN MANAGER on an exception order.

WHERE THIS SITS AMONG THE THREE ROLES
  Customer Sales Agent  — talks to customers, creates DRAFT orders, never submits.
  AI Management Agent   — explains, reports and notifies. It may NOT decide.
  Human Manager         — the only role that can confirm or reject by hand.

This module is the manual path ONLY. It is deliberately NOT registered in any
tool list (see app/graph.py): an LLM has no route to call it, so no prompt can
talk the system into confirming or rejecting an order.

The AUTOMATIC path is somewhere else and stays there: app/policy.py evaluates
the deterministic rules and app/tools/pedidos.py::_after_create re-validates
under a distributed lock and submits with the policy credential. A routine
order that passes every rule is confirmed with no human involved. Nothing in
this file participates in that.

CREDENTIAL BOUNDARIES ARE UNCHANGED. Submitting still happens only through
erpnext.submit_doc (policy identity); reads after a privileged transition use
erpnext.policy_get_doc. The customer-facing agent identity cannot submit.

HONESTY RULES
  - Never invent an order number, a stock figure or a confirmation.
  - Tell the customer only what ERPNext actually says.
  - When a customer notice cannot be delivered, say so to the manager instead
    of pretending it was sent.
"""
from __future__ import annotations

import os
import time

from app import confirmacion, erpnext, idioma, marcas, solicitudes, telefono
from app.locks import CoordinationError, distributed_lock
from app.outbound_status import (
    has_accepted,
    record_outbound,
    registrar_aviso_fallido,
    window_open,
)
from app.router import es_equipo

_PURPOSE_RECHAZO = "customer_order_rejected"
_PURPOSE_CANCELACION = "customer_order_cancelled"

# ERPNext has no "rejected" docstatus, and a draft cannot be cancelled. The
# closest durable state it does understand is status "Closed", which is one of
# the states its own get_reserved_qty does not count — see
# policy.ESTADOS_SIN_RESERVA.
_ESTADO_SIN_RESERVA = "Closed"

# Stamped into the remarks of every Delivery Note this system prepares, so
# "despreparar" can tell a draft the agent created from one a person made by
# hand in ERPNext. A draft without it is never touched.
#
# El único de los diez que NO vive en un comentario: va en un CAMPO del
# documento, así que ninguna consulta de Comment lo encuentra. El registro lo
# dice (`portador=CAMPO`) en vez de dejar que se descubra.
MARCA_REMITO_AGENTE = marcas.texto("remito_agente")


def _resultado(ok: bool, aviso_cliente: bool, detalle: str) -> dict:
    return {"ok": ok, "aviso_cliente": aviso_cliente, "detalle": detalle}


def _leer_doc(doctype: str, name: str) -> dict:
    """Manual decisions run outside the LLM and may use the policy identity."""
    getter = getattr(erpnext, "policy_get_doc", erpnext.get_doc)
    return getter(doctype, name)


def telefono_del_cliente(nombre_so: str) -> str:
    """Normalized mobile number of the order's customer, or '' if unknown.

    Returns '' rather than raising: a missing phone must degrade into "tell the
    manager to call them", never into a crash on the decision path.
    """
    try:
        so = _leer_doc("Sales Order", nombre_so)
        cliente = _leer_doc("Customer", so["customer"])
    except Exception as exc:
        print(f"[decisiones] {nombre_so}: no pude leer el cliente ({type(exc).__name__})")
        return ""
    return telefono.normalizar(cliente.get("mobile_no")) or ""


# POR DÓNDE ENTRÓ LA PERSONA QUE CONFIRMÓ, que es lo que se firma en el
# historial del pedido en ERPNext. Eran la palabra «WhatsApp» escrita a mano
# adentro del rastro, cuando WhatsApp era el único camino posible; en cuanto hay
# un segundo, ese literal pasa a ser una firma falsa en una auditoría. No hay
# default en ninguna de las dos funciones que lo reciben: el que confirma lo
# dice, o no compila.
CANAL_WHATSAPP = "WhatsApp"
CANAL_PANEL = "el panel"

# Cuánto puede tardar la sección crítica de emitir, y de dónde sale el número.
# Adentro de `solicitud:{pedido}` quedan CUATRO llamadas a ERPNext y ninguna a
# una persona: leer la solicitud, leer el pedido, el Submit, y —sólo si el
# Submit expira— la relectura que dice si commiteó igual. El cliente de política
# tiene `timeout=30.0` (app/erpnext.py) y no reintenta, así que el techo son
# 120 s. Eran 60 para una sección que llegaba a 315: once llamadas a ERPNext más
# un POST a Meta de 15 s, con un lease que nadie renueva.
LEASE_EMISION = 180

# El de afuera se queda en 60 A PROPÓSITO, y no es un olvido. Lo único que
# serializa es confirmar contra confirmar —`confirmar:` no lo toma nadie más en
# todo el repo—, y lo que cubre más allá de `solicitud:` es el anuncio, que es
# idempotente por diseño (ver `aprobacion.Emision`). Si vence, dos
# confirmaciones se pisan y la segunda cae en la rama «ya estaba confirmado»,
# que es exactamente para lo que existe. Un lease más largo acá no compra nada
# y alarga a varios minutos el pedido bloqueado cuando un proceso se muere.
LEASE_PUERTA = 60


def _con_emitido(resultado: dict, emitido: bool) -> dict:
    """Marca si el pedido quedó EMITIDO, que no es lo mismo que si salió bien.

    `ok` dice que la acción que el encargado pidió se hizo. Con una solicitud
    abierta esa acción NO es emitir: es aprobar la contraoferta. Sale bien, `ok`
    es True, y el pedido sigue siendo un BORRADOR reservando stock — lo que pasó
    es que se le mandaron los términos nuevos al cliente y se espera su
    respuesta. El panel leía `ok` y marcaba la fila como confirmada, o sea que
    mostraba una venta cerrada que no existe, sobre un pedido que todavía puede
    rechazarse. Son dos hechos distintos y ahora viajan separados: `emitido` es
    True sólo si el Sales Order está en `docstatus=1`.
    """
    return {**resultado, "emitido": emitido}


def confirmar(nombre: str, por: str, *, canal: str) -> dict:
    """Confirmar un pedido a mano. SÓLO UNA PERSONA DEL EQUIPO.

    LA PUERTA, y hasta este cambio no lo era. Esta función era un alias pelado
    de `aprobacion.confirmar_pedido`, y las tres cosas que hacen que confirmar
    sea seguro vivían en `aprobacion.manejar_boton`, o sea en el camino de
    WhatsApp y en ninguna otra parte:

      * **la autorización** — `es_equipo`, que acá no se comprobaba;
      * **la bifurcación por solicitud abierta** — sin ella, un pedido con una
        contraoferta esperando respuesta se confirmaba AL PRECIO ORIGINAL
        mientras el cliente mira los términos nuevos que todavía no aceptó;
      * **el cierre de la revisión humana** — sin él el borrador sigue en el
        índice de vencimientos y el barrido lo cierra más tarde por su cuenta.

    Las tres se movieron acá adentro, que es la forma que ya tienen `cancelar`,
    `despreparar` y `_decidir` en este mismo archivo: comprueban quién llama,
    toman su lock, y recién ahí hacen algo. `confirmar` era la única decisión
    del módulo que no hacía ninguna de las dos.

    `aprobacion.confirmar_pedido` sigue siendo el MECANISMO —es lo que está
    probado contra el toque repetido y contra un submit que commitea después de
    que el cliente HTTP se dio por vencido— y esta función es la POLÍTICA. El
    Submit lo sigue haciendo `erpnext.submit_doc` con la credencial de
    política, acá no cambia nada de eso.

    `canal` es `CANAL_WHATSAPP` o `CANAL_PANEL` y va al rastro durable: sin
    él el historial del pedido firmaría un camino por el que nadie pasó.

    Devuelve {"ok", "aviso_cliente", "detalle"}.
    """
    if not es_equipo(por):
        return _con_emitido(
            _resultado(False, False, "No tenés permiso para confirmar pedidos."), False
        )

    # EL LOCK NO SE LLAMA `accion:` A PROPÓSITO, y no es cosmético.
    # `acciones.py` envuelve TODO `manejar_boton` en `accion:{pedido}` para el
    # camino autorizado por código, y `locks.distributed_lock` arma el lock de
    # redis con `thread_local=False`: no es reentrante. Un lock con el mismo
    # nombre acá adentro esperaría 10 s por sí mismo y levantaría
    # `CoordinationError`, o sea que confirmar con código dejaría de funcionar.
    # `cancelar:{...}` y `despreparar:{...}` ya anidan así dentro de `accion:`.
    try:
        with distributed_lock(f"confirmar:{nombre}", lease_seconds=LEASE_PUERTA, wait_seconds=10):
            return _confirmar_autorizado(nombre, por, canal)
    except CoordinationError:
        return _con_emitido(
            _resultado(
                False,
                False,
                f"No pude coordinar la confirmación de {nombre}; probá de nuevo en un momento.",
            ),
            False,
        )


def _confirmar_autorizado(nombre: str, por: str, canal: str) -> dict:
    """Lo que hace «confirmar» con quien llama ya comprobado y el lock tomado.

    LA COMPROBACIÓN Y EL SUBMIT VAN EN LA MISMA SECCIÓN CRÍTICA, y el lock que
    la arma es `solicitud:{pedido}` y no `confirmar:{pedido}`: el segundo no lo
    toma NADIE del lado del cliente, así que leer «no hay solicitud» bajo él no
    impedía que se abriera una un instante después. La ventana era chica y
    cuesta el pedido entero — el cliente pide una excepción, `solicitudes.crear`
    la registra, y para cuando el encargado ve la pregunta el borrador ya se
    emitió a los términos viejos. `solicitudes.crear` toma AHORA el mismo lock y
    relee el borrador adentro, que es la otra mitad: sin ella el lock serializa
    y el segundo igual hace lo que iba a hacer.

    ADENTRO DEL LOCK VA `emitir` Y NADA MÁS. `anunciar` —el comentario, la marca
    durable, el aviso al equipo y el aviso al cliente— corre afuera, y no es una
    optimización: el lease son 180 s y el anuncio solo llega a 200 largos, con un
    POST a Meta de 15 s en el medio. Con la función entera adentro el lease
    vencía en mitad de la sección, `solicitudes.crear` tomaba la llave libre y le
    abría una solicitud a un pedido que estaba por emitirse — la misma carrera,
    reabierta por el reloj. Soltar antes no abre ninguna ventana: cuando
    `anunciar` empieza, el Submit ya commiteó y `_sigue_en_borrador` lee
    `docstatus=1` y se niega solo. Es la regla que el repo ya escribió tres
    veces: **el teléfono de nadie va adentro de un lock.**

    El orden es `accion:` ⊃ `confirmar:` ⊃ `solicitud:`, siempre en ese sentido
    y sin ciclos. `aprobar_solicitud` y `cerrar_revision_si_hay` se llaman
    FUERA del `with` a propósito: las dos vuelven a tomar `solicitud:{pedido}` y
    `locks.distributed_lock` no es reentrante (`thread_local=False`), así que
    desde adentro esperarían 10 s por un lock que ya tiene este mismo hilo.
    """
    from app.aprobacion import Emision, anunciar, emitir, solicitud_abierta_estricta
    from app.solicitudes import LecturaIncierta

    emision: Emision | None = None
    # CoordinationError sube hasta `confirmar`, que ya la contesta.
    with distributed_lock(f"solicitud:{nombre}", lease_seconds=LEASE_EMISION, wait_seconds=10):
        try:
            abierta = solicitud_abierta_estricta(nombre)
        except LecturaIncierta as exc:
            # NO se emite nada. «No pude leer el estado» no es «no hay
            # contraoferta esperando», y confundirlos con ERPNext caído emite
            # el borrador sin poder probar que nadie está esperando otra cosa.
            print(f"[decisiones] {nombre}: no confirmo, estado incierto ({exc})")
            return _con_emitido(
                _resultado(
                    False,
                    False,
                    f"No pude verificar el estado de {nombre} y no lo confirmé. "
                    "Probá de nuevo en un momento.",
                ),
                False,
            )
        # «Aprobar» no quiere decir «confirmar» cuando hay una solicitud
        # abierta: quiere decir «aprobá lo que pidió el cliente», y los términos
        # todavía tienen que ir al cliente antes de que se emita nada
        # (app/solicitudes.py).
        if abierta is None:
            emision = emitir(nombre)

    if emision is None:
        # No se emitió nada: salió una contraoferta y el pedido sigue borrador.
        return _con_emitido(aprobar_solicitud(nombre, por), False)
    if emision.rechazo is not None:
        return _con_emitido(emision.rechazo, False)

    resultado = _con_emitido(anunciar(emision, por, canal=canal), True)
    # «Confirmar» es también la salida documentada de una revisión humana, y
    # por eso REVISION_HUMANA NO es uno de `solicitudes.ABIERTOS`: esta palabra
    # tiene que seguir queriendo decir «emití este borrador». Cerrar la
    # revisión acá lo saca del índice de vencimientos en el acto en vez de
    # esperar al barrido; es un no-op cuando no hay revisión, así que un
    # encargado que la escribe tres veces paga tres lecturas.
    if resultado.get("ok"):
        cerrar_revision_si_hay(nombre, por, "una persona confirmó el pedido")
    return resultado


def confirmar_conteo(nombre: str, por: str) -> dict:
    """Confirm a physical stock count by hand. HUMAN MANAGER ONLY.

    Until somebody confirms it, a count is a WhatsApp message with a number in
    it: app/inventario.py does not treat the product as trustworthy and the bot
    keeps refusing to promise stock. Confirming it is what makes today's count
    worth something — and it has to be a person, because a count is a claim
    about the physical world that only a person can make.

    Submitting goes through erpnext.submit_doc, the policy credential, exactly
    like an order. Neither agent has Submit permission and this function is in
    no tool list, so no prompt can reach it.

    Returns {"ok": bool, "detalle": str}; `detalle` is the text for the manager.
    """
    try:
        actual = _leer_doc("Stock Reconciliation", nombre)
    except Exception as exc:
        print(f"[decisiones] {nombre}: no pude leer el conteo ({type(exc).__name__})")
        return {
            "ok": False,
            "detalle": f"No pude abrir el conteo {nombre}. Revisalo en ERPNext.",
        }

    estado = int(actual.get("docstatus") or 0)
    if estado == 1:
        return {"ok": True, "detalle": f"El conteo {nombre} ya estaba confirmado."}
    if estado != 0:
        return {
            "ok": False,
            "detalle": f"El conteo {nombre} está cancelado; cargá uno nuevo.",
        }

    try:
        erpnext.submit_doc("Stock Reconciliation", nombre)
    except Exception as exc:
        print(f"[decisiones] {nombre}: submit del conteo falló ({type(exc).__name__})")
        # A timeout can commit anyway: ask ERPNext what actually happened
        # before telling the manager it failed.
        try:
            actual = _leer_doc("Stock Reconciliation", nombre)
        except Exception:
            actual = {}
        if int(actual.get("docstatus") or 0) != 1:
            return {
                "ok": False,
                "detalle": (
                    f"No pude confirmar el conteo {nombre}. Confirmalo en ERPNext "
                    "o volvé a intentar."
                ),
            }

    _comentar_conteo(
        nombre, f"Conteo confirmado por un integrante autorizado ({por})."
    )
    return {
        "ok": True,
        "detalle": (
            f"Conteo {nombre} confirmado. Desde ahora el bot puede hablar de "
            "stock de esos productos."
        ),
    }


def _comentar_conteo(nombre: str, texto: str) -> None:
    try:
        erpnext.add_comment("Stock Reconciliation", nombre, texto)
    except Exception as exc:
        print(f"[decisiones] {nombre}: comentario falló ({type(exc).__name__})")


def rechazar(nombre: str, por: str, motivo: str = "") -> dict:
    """Reject an exception order by hand. HUMAN MANAGER ONLY.

    THE SILENCE THIS FIXES
    Until now tapping [Rechazar] only told the MANAGER that nothing had
    changed. The customer — who had been told the order was received and would
    be confirmed shortly — was never told anything and waited indefinitely.
    That is the worst outcome this system can produce, so the customer notice
    comes first and its success is reported back to the manager.

    The draft is NOT deleted and NOT cancelled: deleting it would destroy the
    audit trail of something the customer was already told about, and ERPNext
    cannot cancel a document that was never submitted. It is marked "Closed"
    instead, so it stops holding stock a live order could use, and the manager
    is told it is still there to amend or remove in ERPNext.
    """
    razon = (motivo or "").strip()
    tel = telefono_del_cliente(nombre)
    sin_reserva = _marcar_sin_reserva(nombre)

    avisado = False
    if tel:
        avisado = _avisar_cliente_rechazo(nombre, tel, razon)
    else:
        _comentar(
            nombre,
            "Rechazo no avisado al cliente: no tiene teléfono cargado. "
            "Requiere contacto manual.",
        )

    _comentar(
        nombre,
        f"Rechazado manualmente por un integrante autorizado ({por}). "
        f"Motivo: {razon or 'sin detalle'}. "
        f"Aviso al cliente: {'enviado' if avisado else 'NO enviado'}. "
        "El borrador queda sin confirmar para revisión en ERPNext. "
        + (
            f"Marcado como {_ESTADO_SIN_RESERVA}: ya no compromete stock."
            if sin_reserva
            else "ATENCIÓN: no pude marcarlo como "
            f"{_ESTADO_SIN_RESERVA}, sigue comprometiendo stock hasta que lo "
            "cierres o lo borres a mano."
        ),
    )

    if avisado:
        detalle = "Rechazo registrado y cliente avisado."
    elif tel:
        detalle = "Rechazo registrado, pero NO pude avisarle al cliente."
    else:
        detalle = "Rechazo registrado; el cliente no tiene teléfono cargado."
    return _resultado(True, avisado, detalle)


def _marcar_sin_reserva(nombre: str) -> bool:
    """Stop a rejected draft from holding stock nobody is going to deliver.

    Without this the order still counts as a promise in
    policy._comprometido_en_borradores, and a product the dairy actually has
    stays unavailable to the next customer until somebody remembers the draft.

    Only ever touches a DRAFT. A [Rechazar] tap can arrive for an order
    somebody already confirmed — the same message carries both buttons — and
    stamping "Closed" on a submitted order would release the stock ERPNext had
    reserved for it and drop it out of the delivery queue.

    DELEGATES to solicitudes.soltar_reserva, and that is the whole fix. This
    used to re-read BEFORE writing (to refuse a non-draft) but never AFTER, so
    "the write did not raise" was reported as "the stock is free" — and the
    manager's audit trail then said `Marcado como Closed: ya no compromete
    stock` about an order ERPNext may not have closed at all. soltar_reserva
    re-reads afterwards and only claims the release when the re-read agrees,
    which is the same rule app/confirmacion.py applies to every other promise
    this system makes.

    The returned value now means "proven released", not "the call went
    through". Everything the manager reads is unchanged, deliberately: the
    sentence is durable audit vocabulary that already lives in ERPNext
    comments, so this commit changes what makes it TRUE, not how it is worded.
    """
    from app import solicitudes

    try:
        liberado, detalle = solicitudes.soltar_reserva(nombre)
    except Exception as exc:
        print(
            f"[decisiones] {nombre}: no pude marcarlo como "
            f"{_ESTADO_SIN_RESERVA} ({type(exc).__name__})"
        )
        return False
    if not liberado:
        print(f"[decisiones] {nombre}: no quedó sin reserva ({detalle})")
    return liberado


def _comentar(nombre: str, texto: str) -> None:
    try:
        erpnext.add_comment("Sales Order", nombre, texto)
    except Exception as exc:
        print(f"[decisiones] {nombre}: comentario falló ({type(exc).__name__})")


def _texto_rechazo(nombre: str, razon: str, lengua: str | None = None) -> str:
    """En el idioma del cliente. Antes salía en los dos porque no se sabía cuál."""
    from app import idioma

    return idioma.t(
        "pedido.rechazado", lengua, pedido=nombre, motivo=f" ({razon})" if razon else ""
    )


def _avisar_cliente_rechazo(nombre: str, tel: str, razon: str) -> bool:
    """Free text first; an approved template only if Meta closed the window.

    Returns True only when Meta acknowledged a concrete message id.
    """
    from app import whatsapp
    from app.outbound_status import has_accepted, record_outbound

    try:
        if has_accepted(nombre, _PURPOSE_RECHAZO):
            return True
    except Exception as exc:
        print(f"[decisiones] {nombre}: has_accepted falló ({type(exc).__name__})")

    wamid = ""
    try:
        result = whatsapp.enviar_mensaje(
            tel, _texto_rechazo(nombre, razon, idioma.para_destinatario(tel))
        )
        wamid = _wamid(result)
    except Exception as exc:
        print(f"[decisiones] {nombre}: texto de rechazo falló ({type(exc).__name__})")
        wamid = _plantilla_rechazo(nombre, tel, razon)

    if not wamid:
        return False
    try:
        record_outbound(wamid, _PURPOSE_RECHAZO, order_name=nombre)
    except Exception as exc:
        print(f"[decisiones] {nombre}: tracking del rechazo falló ({type(exc).__name__})")
    return True


def _plantilla_rechazo(nombre: str, tel: str, razon: str) -> str:
    """Fallback for a closed 24-hour window. No template configured -> no send."""
    from app import whatsapp

    plantilla = os.getenv("WHATSAPP_CUSTOMER_REJECTED_TEMPLATE", "").strip()
    if not plantilla:
        print(f"[decisiones] {nombre}: falta WHATSAPP_CUSTOMER_REJECTED_TEMPLATE")
        return ""
    try:
        result = whatsapp.enviar_plantilla(
            tel,
            plantilla,
            os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "es_AR").strip() or "es_AR",
            [nombre, razon or "sin detalle"],
        )
        return _wamid(result)
    except Exception as exc:
        print(f"[decisiones] {nombre}: plantilla de rechazo falló ({type(exc).__name__})")
        return ""


def _wamid(result: object) -> str:
    if not isinstance(result, dict):
        return ""
    messages = result.get("messages")
    if not isinstance(messages, list) or not messages:
        return ""
    first = messages[0]
    wamid = first.get("id") if isinstance(first, dict) else None
    return wamid.strip() if isinstance(wamid, str) else ""


# ---------------------------------------------------------------------------
# Stage 2e — dispatch, in two human steps.
#
#   preparar  -> a DRAFT Delivery Note for a CONFIRMED order (policy identity,
#                docstatus forced to 0 by erpnext.policy_create_doc).
#   despachar -> submits that draft. A separate command, so nobody dispatches
#                by accident from the same tap, and no LLM tool can reach it.
#
# Neither function is in any tool list (tests/test_frontera_decisiones.py).
# ---------------------------------------------------------------------------


def _hoy() -> str:
    from app import policy

    return policy._hoy_del_negocio().isoformat()


def _remitos_borrador(nombre_so: str) -> list[str]:
    """Draft Delivery Notes already prepared for this order (policy read)."""
    filas = erpnext.policy_get_list(
        "Delivery Note Item",
        filters=[["against_sales_order", "=", nombre_so], ["docstatus", "=", 0]],
        fields=["parent", "against_sales_order", "docstatus"],
        limit=20,
        parent="Delivery Note",
    )
    nombres: list[str] = []
    for fila in filas:
        if str(fila.get("against_sales_order") or nombre_so).strip() != nombre_so:
            continue
        if int(float(fila.get("docstatus") or 0)) != 0:
            continue
        remito = str(fila.get("parent") or "").strip()
        if remito and remito not in nombres:
            nombres.append(remito)
    return nombres


def preparar(nombre: str, por: str) -> dict:
    """Prepare the dispatch of a CONFIRMED order: one draft Delivery Note. HUMAN ONLY.

    Idempotent: a second "preparar" for the same order reuses the draft instead
    of creating another one. A draft or rejected order cannot be prepared.
    """
    try:
        so = _leer_doc("Sales Order", nombre)
    except Exception as exc:
        print(f"[decisiones] {nombre}: no pude leer el pedido ({type(exc).__name__})")
        return _resultado(False, False, f"No pude abrir {nombre}. Revisalo en ERPNext.")
    if int(so.get("docstatus") or 0) != 1:
        return _resultado(
            False,
            False,
            f"{nombre} no está confirmado (docstatus {so.get('docstatus')}); primero "
            f"escribí 'confirmar {nombre}' o rechazalo.",
        )
    if str(so.get("status") or "").strip() in ("Closed", "Cancelled", "On Hold"):
        return _resultado(
            False, False, f"{nombre} está {so.get('status')}: no se prepara un despacho."
        )

    try:
        existentes = _remitos_borrador(nombre)
    except Exception as exc:
        print(f"[decisiones] {nombre}: no pude buscar remitos ({type(exc).__name__})")
        return _resultado(False, False, f"No pude verificar los remitos de {nombre}. Reintentá.")
    if existentes:
        return _resultado(
            True,
            False,
            f"📦 {nombre} ya tiene el remito {existentes[0]} preparado en borrador. "
            f"Para despacharlo escribí 'despachar {nombre}'.",
        )

    try:
        company, deposito = erpnext.default_context()
        renglones = [
            {
                "item_code": i["item_code"],
                "qty": i["qty"],
                "warehouse": i.get("warehouse") or deposito,
                "against_sales_order": nombre,
                "so_detail": i["name"],
            }
            for i in so.get("items", [])
            if isinstance(i, dict) and i.get("item_code")
        ]
        if not renglones:
            return _resultado(False, False, f"{nombre} no tiene renglones para despachar.")
        remito = erpnext.policy_create_doc(
            "Delivery Note",
            {
                "company": so.get("company") or company,
                "customer": so["customer"],
                "posting_date": _hoy(),
                "set_posting_time": 1,
                "items": renglones,
                "remarks": (
                    f"{MARCA_REMITO_AGENTE} Preparado por WhatsApp por un "
                    f"integrante autorizado ({por})."
                ),
            },
        )
    except Exception as exc:
        print(f"[decisiones] {nombre}: crear remito falló ({type(exc).__name__})")
        return _resultado(False, False, f"No pude preparar el remito de {nombre}. Revisalo en ERPNext.")

    nombre_remito = str(remito.get("name") or "").strip()
    _comentar(
        nombre,
        f"Remito {nombre_remito or '(sin nombre)'} preparado en BORRADOR por un integrante "
        f"autorizado ({por}). Se despacha con una confirmación aparte.",
    )
    return _resultado(
        True,
        False,
        f"📦 Remito {nombre_remito} preparado en borrador para {nombre}. Nada salió todavía: "
        f"cuando el reparto cargue, escribí 'despachar {nombre}'.",
    )


def despachar(nombre: str, por: str) -> dict:
    """Submit the prepared Delivery Note of an order. HUMAN ONLY, second step.

    Requires exactly one draft prepared by ``preparar``; with none it tells the
    manager to prepare first, with several it refuses and points to ERPNext.
    Submitting uses erpnext.submit_doc (policy identity), as every submit does.
    """
    try:
        existentes = _remitos_borrador(nombre)
    except Exception as exc:
        print(f"[decisiones] {nombre}: no pude buscar remitos ({type(exc).__name__})")
        return _resultado(False, False, f"No pude verificar los remitos de {nombre}. Reintentá.")
    if not existentes:
        return _resultado(
            False,
            False,
            f"{nombre} no tiene un remito preparado. Escribí 'preparar {nombre}' primero.",
        )
    if len(existentes) > 1:
        return _resultado(
            False,
            False,
            f"{nombre} tiene {len(existentes)} remitos en borrador ({', '.join(existentes)}). "
            "Dejá uno solo en ERPNext y volvé a escribir 'despachar'.",
        )
    remito = existentes[0]
    try:
        erpnext.submit_doc("Delivery Note", remito)
    except Exception as exc:
        print(f"[decisiones] {remito}: submit del remito falló ({type(exc).__name__})")
        try:
            actual = _leer_doc("Delivery Note", remito)
        except Exception:
            actual = {}
        if int(actual.get("docstatus") or 0) != 1:
            return _resultado(
                False,
                False,
                f"No pude despachar el remito {remito}. Confirmalo en ERPNext o reintentá.",
            )
    _comentar(
        nombre,
        f"Remito {remito} despachado (confirmado) por un integrante autorizado ({por}).",
    )
    return _resultado(True, False, f"🚚 Remito {remito} despachado. {nombre} queda en reparto.")


# ---------------------------------------------------------------------------
# cancelar <pedido> <motivo> — a CONFIRMED order, by a human, within the window.
#
# Twelve rules, all here and in tests/test_cancelacion.py:
#   1. only TELEFONOS_EQUIPO (checked again here, not just in the router);
#   2. only a SUBMITTED Sales Order, within CANCELACION_HORAS (24) of the
#      confirmation THIS system recorded DURABLY in ERPNext
#      (app/confirmacion.py) — no durable record, no cancellation, and a Redis
#      flush or restart cannot take the window away;
#   3. refused when a Delivery Note or Sales Invoice is CONFIRMED for it; a
#      draft remito is undone by "despreparar" first and a draft invoice in
#      ERPNext — never deleted from here;
#   4. never cancels linked documents; 5. re-read and validated under the
#   distributed lock; 6. policy identity only; 7. reason required, audited;
#   8. idempotent; 9-11. the customer is told once — free text inside the
#   window, optional template outside, otherwise dead-letter + one ToDo;
#   12. not an LLM tool (tests/test_frontera_decisiones.py).
# ---------------------------------------------------------------------------


def horas_cancelacion() -> float:
    """The cancellation window, from app/confirmacion.py (CANCELACION_HORAS)."""
    return confirmacion.horas_ventana()


def _vinculados(nombre_so: str) -> tuple[list[str], list[str], list[str]]:
    """(confirmados, remitos en borrador, facturas en borrador) for the order.

    Raises on a read failure: not knowing is never "there are none".

    The three groups get three different answers, because only one of them is
    something this system may undo:
      * a SUBMITTED Delivery Note or Sales Invoice blocks the WhatsApp
        cancellation outright. Cancelling the order would mean cancelling them
        first, and cascade-cancelling a document that already moved stock or
        money is not a decision a chat command gets to make.
      * a DRAFT Delivery Note is undone by the separate ``despreparar``
        command, which checks that the agent created it and nobody edited it.
        ``cancelar`` never deletes it silently.
      * a DRAFT Sales Invoice belongs to ERPNext: this system never creates
        invoices, so it has no way to know what is safe to remove.
    """
    confirmados: list[str] = []
    remitos_borrador: list[str] = []
    facturas_borrador: list[str] = []
    for doctype, campo, etiqueta in (
        ("Delivery Note", "against_sales_order", "remito"),
        ("Sales Invoice", "sales_order", "factura"),
    ):
        filas = erpnext.policy_get_list(
            f"{doctype} Item",
            filters=[[campo, "=", nombre_so], ["docstatus", "in", [0, 1]]],
            fields=["parent", campo, "docstatus"],
            limit=20,
            parent=doctype,
        )
        vistos: set[str] = set()
        for fila in filas:
            if str(fila.get(campo) or nombre_so).strip() != nombre_so:
                continue
            padre = str(fila.get("parent") or "").strip()
            if not padre or padre in vistos:
                continue
            vistos.add(padre)
            estado = int(float(fila.get("docstatus") or 0))
            if estado == 1:
                confirmados.append(f"{etiqueta} {padre} (confirmado)")
            elif doctype == "Delivery Note":
                remitos_borrador.append(padre)
            else:
                facturas_borrador.append(padre)
    return confirmados, remitos_borrador, facturas_borrador


# ---------------------------------------------------------------------------
# despreparar <pedido> — undo a preparation this system made, and nothing else.
#
# It exists so that "cancelar" never has to choose between refusing for ever
# and deleting a document behind the manager's back. Both are wrong: the first
# leaves an order nobody can cancel by WhatsApp after one accidental
# "preparar", and the second destroys a document a person may have edited.
#
# So the destructive step is its own explicit human command, and it only ever
# touches a DRAFT Delivery Note that this system created and nobody changed:
# same customer, same company, exactly the order's own lines. Anything else —
# a hand-made draft, an edited one, several of them, any invoice, anything
# submitted — is left alone and the manager is sent to ERPNext.
# ---------------------------------------------------------------------------


def _renglones_esperados(so: dict) -> dict[tuple[str, str], float]:
    """What a Delivery Note prepared from this order must contain, line by line."""
    esperado: dict[tuple[str, str], float] = {}
    for item in so.get("items") or []:
        if not isinstance(item, dict) or not item.get("item_code"):
            continue
        clave = (str(item.get("item_code")).strip(), str(item.get("name") or "").strip())
        esperado[clave] = esperado.get(clave, 0.0) + float(item.get("qty") or 0)
    return esperado


def _remito_intacto(remito: dict, so: dict, nombre_so: str) -> tuple[bool, str]:
    """Whether this draft is one the agent prepared and nobody has touched."""
    if int(remito.get("docstatus") or 0) != 0:
        return False, "ya no es un borrador"
    if not marcas.en_campo("remito_agente", remito):
        return False, "no lo preparó el agente"
    if str(remito.get("customer") or "").strip() != str(so.get("customer") or "").strip():
        return False, "el cliente del remito no es el del pedido"
    empresa_so = str(so.get("company") or "").strip()
    empresa_dn = str(remito.get("company") or "").strip()
    if empresa_so and empresa_dn and empresa_so != empresa_dn:
        return False, "la compañía del remito no es la del pedido"

    esperado = _renglones_esperados(so)
    real: dict[tuple[str, str], float] = {}
    for item in remito.get("items") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("against_sales_order") or "").strip() != nombre_so:
            return False, "tiene renglones de otro pedido"
        clave = (
            str(item.get("item_code") or "").strip(),
            str(item.get("so_detail") or "").strip(),
        )
        real[clave] = real.get(clave, 0.0) + float(item.get("qty") or 0)
    if set(real) != set(esperado):
        return False, "los renglones del remito ya no son los del pedido"
    for clave, cantidad in esperado.items():
        if abs(real[clave] - cantidad) > 0.000001:
            return False, "alguien cambió las cantidades del remito"
    return True, ""


def despreparar(nombre: str, por: str) -> dict:
    """Undo the preparation of an order: delete the agent's DRAFT Delivery Note.

    HUMAN ONLY, deterministic, in no tool list. Idempotent: with no draft left
    it reports that there is nothing prepared and the order can be cancelled.
    """
    if not es_equipo(por):
        return _resultado(False, False, "No tenés permiso para despreparar pedidos.")

    try:
        with distributed_lock(f"despreparar:{nombre}", lease_seconds=60, wait_seconds=10):
            so = _leer_doc("Sales Order", nombre)
            confirmados, borradores, facturas = _vinculados(nombre)
            if confirmados:
                return _resultado(
                    False,
                    False,
                    f"No toco {nombre}: ya tiene {', '.join(confirmados)}. Un documento "
                    "confirmado se resuelve en ERPNext; no cancelo en cascada.",
                )
            if facturas:
                return _resultado(
                    False,
                    False,
                    f"{nombre} tiene la factura {', '.join(facturas)} en borrador. "
                    "Las facturas se resuelven en ERPNext.",
                )
            if not borradores:
                return _resultado(
                    True,
                    False,
                    f"{nombre} no tiene remito preparado. Si querés anularlo: "
                    f"cancelar {nombre} <motivo>.",
                )
            if len(borradores) > 1:
                return _resultado(
                    False,
                    False,
                    f"{nombre} tiene {len(borradores)} remitos en borrador "
                    f"({', '.join(borradores)}). Dejá uno solo en ERPNext y reintentá.",
                )

            remito_nombre = borradores[0]
            remito = _leer_doc("Delivery Note", remito_nombre)
            intacto, problema = _remito_intacto(remito, so, nombre)
            if not intacto:
                return _resultado(
                    False,
                    False,
                    f"No borro el remito {remito_nombre}: {problema}. Resolvelo en "
                    "ERPNext y después volvé a intentar.",
                )

            # The record goes in BEFORE the deletion: a deleted document cannot
            # be commented on, and a deletion with no trace is not auditable.
            renglones = "; ".join(
                f"{float(i.get('qty') or 0):g} x {i.get('item_code')}"
                for i in remito.get("items") or []
                if isinstance(i, dict)
            )
            _comentar(
                nombre,
                f"Despreparado por un integrante autorizado ({por}): se borra el remito "
                f"BORRADOR {remito_nombre} que había preparado el agente "
                f"({renglones or 'sin renglones'}). Nada se despachó ni se canceló en cascada.",
            )
            erpnext.policy_delete_doc("Delivery Note", remito_nombre)
    except CoordinationError:
        return _resultado(
            False, False, f"No pude coordinar el desprepare de {nombre}; reintentá en un momento."
        )
    except Exception as exc:
        print(f"[decisiones] {nombre}: desprepare falló ({type(exc).__name__})")
        return _resultado(False, False, f"No pude despreparar {nombre}. Revisalo en ERPNext.")

    return _resultado(
        True,
        False,
        f"↩️ Remito {remito_nombre} borrado; {nombre} vuelve a estar sólo confirmado. "
        f"Para anularlo: cancelar {nombre} <motivo>.",
    )


def cancelar(nombre: str, por: str, motivo: str) -> dict:
    """Cancel a confirmed order by hand. HUMAN MANAGER ONLY, within the window."""
    if not es_equipo(por):
        return _resultado(False, False, "No tenés permiso para cancelar pedidos.")
    razon = " ".join(str(motivo or "").split())
    if len(razon) < 3:
        return _resultado(
            False, False, f"Falta el motivo. Escribí: cancelar {nombre} <motivo>"
        )

    ya_cancelado = False
    try:
        with distributed_lock(f"cancelar:{nombre}", lease_seconds=60, wait_seconds=10):
            so = _leer_doc("Sales Order", nombre)
            estado = int(so.get("docstatus") or 0)
            if estado == 2:
                ya_cancelado = True
            else:
                if estado != 1:
                    return _resultado(
                        False,
                        False,
                        f"{nombre} no está confirmado; un borrador se rechaza con "
                        f"'rechazar {nombre}'.",
                    )
                momento = confirmacion.momento(nombre)
                if momento is None:
                    return _resultado(
                        False,
                        False,
                        f"No puedo establecer cuándo se confirmó {nombre}: no hay registro "
                        "durable de que lo haya confirmado este sistema. Cancelalo en ERPNext.",
                    )
                horas = (time.time() - momento) / 3600.0
                if horas > horas_cancelacion():
                    return _resultado(
                        False,
                        False,
                        f"{nombre} se confirmó hace {horas:.0f} h; el límite para cancelar por "
                        f"WhatsApp es {horas_cancelacion():g} h. Cancelalo en ERPNext.",
                    )
                confirmados, borradores, facturas = _vinculados(nombre)
                if confirmados:
                    return _resultado(
                        False,
                        False,
                        f"No cancelo {nombre}: ya tiene {', '.join(confirmados)}. No cancelo "
                        "documentos vinculados en cascada; resolvelo en ERPNext.",
                    )
                if facturas:
                    return _resultado(
                        False,
                        False,
                        f"No cancelo {nombre}: tiene la factura {', '.join(facturas)} en "
                        "borrador. Las facturas se resuelven en ERPNext.",
                    )
                if borradores:
                    # Never deleted from here, and never in cascade: undoing a
                    # preparation is its own audited human command.
                    return _resultado(
                        False,
                        False,
                        f"No cancelo {nombre}: tiene el remito {', '.join(borradores)} "
                        f"preparado en borrador y no lo borro solo. Escribí "
                        f"'despreparar {nombre}' y después 'cancelar {nombre} <motivo>'.",
                    )
                try:
                    erpnext.policy_cancel_doc("Sales Order", nombre)
                except erpnext.ERPNextError:
                    # A timeout can commit anyway: trust ERPNext, not the client.
                    actual = _leer_doc("Sales Order", nombre)
                    if int(actual.get("docstatus") or 0) != 2:
                        raise
                _comentar(
                    nombre,
                    f"Cancelado por un integrante autorizado ({por}). Motivo: {razon}.",
                )
    except CoordinationError:
        return _resultado(
            False, False, f"No pude coordinar la cancelación de {nombre}; reintentá en un momento."
        )
    except Exception as exc:
        print(f"[decisiones] {nombre}: cancelación falló ({type(exc).__name__})")
        return _resultado(False, False, f"No pude cancelar {nombre}. Revisalo en ERPNext.")

    if ya_cancelado:
        return _resultado(True, False, f"{nombre} ya estaba cancelado; no cambié nada.")

    # Outside the cancelar lock (never nested), and a no-op unless the order was
    # in review: otherwise the manager keeps reading a "Vence:" line for a
    # document that no longer exists to review.
    cerrar_revision_si_hay(nombre, por, "una persona canceló el pedido")

    tel = telefono_del_cliente(nombre)
    avisado = _avisar_cliente_cancelacion(nombre, tel, razon) if tel else False
    if not tel:
        _comentar(nombre, "Cancelación no avisada al cliente: no tiene teléfono cargado.")
    _comentar(
        nombre,
        f"Aviso de cancelación al cliente: {'enviado' if avisado else 'NO enviado'}.",
    )
    if avisado:
        return _resultado(True, True, f"🛑 {nombre} cancelado. Ya le avisé al cliente.")
    return _resultado(
        True, False, f"🛑 {nombre} cancelado. NO pude avisarle al cliente; quedó una tarea para hacerlo."
    )


def _texto_cancelacion(nombre: str, razon: str, lengua: str | None = None) -> str:
    from app import idioma

    return idioma.t("pedido.cancelado", lengua, pedido=nombre, motivo=razon)


def _avisar_cliente_cancelacion(nombre: str, tel: str, razon: str) -> bool:
    """Once per order. Free text inside the customer's window; a template only
    outside it and only if configured; otherwise dead-letter + one ToDo."""
    from app import whatsapp

    try:
        if has_accepted(nombre, _PURPOSE_CANCELACION):
            return True
    except Exception as exc:
        print(f"[decisiones] {nombre}: has_accepted falló ({type(exc).__name__})")

    texto = _texto_cancelacion(nombre, razon, idioma.para_destinatario(tel))
    wamid = ""
    try:
        if window_open(tel):
            wamid = _wamid(whatsapp.enviar_mensaje(tel, texto))
        else:
            plantilla = os.getenv("WHATSAPP_CUSTOMER_CANCELLED_TEMPLATE", "").strip()
            if plantilla:
                wamid = _wamid(
                    whatsapp.enviar_plantilla(
                        tel,
                        plantilla,
                        os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "es_AR").strip() or "es_AR",
                        [nombre, razon],
                    )
                )
            else:
                print(f"[decisiones] {nombre}: ventana cerrada y sin WHATSAPP_CUSTOMER_CANCELLED_TEMPLATE")
    except Exception as exc:
        print(f"[decisiones] {nombre}: aviso de cancelación falló ({type(exc).__name__})")

    if not wamid:
        registrar_aviso_fallido(_PURPOSE_CANCELACION, nombre, texto)
        return False
    try:
        record_outbound(wamid, _PURPOSE_CANCELACION, order_name=nombre)
    except Exception as exc:
        print(f"[decisiones] {nombre}: tracking de la cancelación falló ({type(exc).__name__})")
    return True


# ---------------------------------------------------------------------------
# The Sales -> Management decision workflow (app/solicitudes.py).
#
# The sales side opens a DecisionRequest and answers the customer immediately.
# These functions are the other end: a HUMAN decides, deterministically, and
# the order only moves after the customer has said yes to the terms and every
# rule has been re-checked under the lock.
#
# Authority, stated once: nothing here is an LLM tool, every entry point
# re-checks TELEFONOS_EQUIPO itself, and the only writes are the policy
# identity's (erpnext.submit_doc, policy_update_status, policy_aplicar_terminos).
# ---------------------------------------------------------------------------


def _abierta(nombre: str) -> tuple[object | None, str]:
    """(the order's open request, why there is none). Never raises."""
    try:
        solicitud = solicitudes.leer(nombre)
    except Exception as exc:
        print(f"[decisiones] {nombre}: solicitud no legible ({type(exc).__name__})")
        return None, f"No pude leer la solicitud de {nombre}. Revisalo en ERPNext."
    if solicitud is None:
        return None, f"{nombre} no tiene ninguna decisión pendiente."
    if solicitud.estado == solicitudes.ESPERANDO_CLIENTE:
        if solicitud.es_respaldo:
            # Nobody decided this one: it expired and the fallback answered for
            # us. Saying "already decided" would let the manager believe their
            # late command was the decision.
            return None, solicitudes.texto_superada_equipo(solicitud)
        return None, (
            f"{nombre} ya está decidido y esperando al cliente: le ofrecí "
            f"{solicitudes.terminos_texto(solicitud.ofrecido, solicitud.moneda)}. "
            "Sin su respuesta no cierro nada."
        )
    if not solicitud.abierta:
        return None, (
            f"La solicitud de {nombre} ya está {solicitud.estado}"
            f"{f' ({solicitud.motivo})' if solicitud.motivo else ''}."
        )
    return solicitud, ""


def _decidir(nombre: str, por: str, decision: str, ofrecido: dict, motivo: str = "") -> dict:
    """Record ONE human decision and put the offer to the customer.

    A decision never confirms the order by itself: the terms change the date,
    the method or the money, so the customer has to accept them explicitly
    first (solicitudes.aceptar_cliente re-checks everything after that).
    """
    if not es_equipo(por):
        return _resultado(False, False, "No tenés permiso para decidir solicitudes.")

    try:
        with distributed_lock(f"solicitud:{nombre}", lease_seconds=60, wait_seconds=10):
            solicitud, problema = _abierta(nombre)
            if solicitud is None:
                return _resultado(False, False, problema)
            if solicitud.vencida():
                return _resultado(False, False, solicitudes.texto_vencida_equipo(solicitud))

            decidida = solicitudes.registrar(
                solicitud,
                decision,
                estado=solicitudes.ESPERANDO_CLIENTE,
                decision=decision,
                decidida_por=por,
                decidida_en=time.time(),
                ofrecido=dict(ofrecido),
                motivo=motivo,
            )
            if decidida is None:
                return _resultado(
                    False,
                    False,
                    f"No pude registrar la decisión de {nombre} en ERPNext; reintentá.",
                )
    except CoordinationError:
        return _resultado(
            False, False, f"No pude coordinar la decisión de {nombre}; reintentá en un momento."
        )
    except Exception as exc:
        print(f"[decisiones] {nombre}: decisión falló ({type(exc).__name__})")
        return _resultado(False, False, f"No pude decidir {nombre}. Revisalo en ERPNext.")

    _comentar(
        nombre,
        f"Decisión de la solicitud {decidida.id}: {decision} por un integrante "
        f"autorizado ({por}). Términos ofrecidos: "
        f"{solicitudes.terminos_texto(decidida.ofrecido, decidida.moneda)}. "
        f"{motivo or ''}".strip(),
    )
    avisado = solicitudes.ofrecer_al_cliente(decidida)
    cola = (
        "Le mandé la oferta al cliente y espero su respuesta."
        if avisado
        else "NO pude ponerle la oferta en cola al cliente; quedó una tarea para contactarlo."
    )
    return _resultado(
        True,
        avisado,
        f"✅ {nombre}: registré «{decision}» "
        f"({solicitudes.terminos_texto(decidida.ofrecido, decidida.moneda)}). {cola}",
    )


def aprobar_solicitud(nombre: str, por: str) -> dict:
    """Approve exactly what the customer asked for. HUMAN ONLY.

    "Exactly what the customer asked for" only exists when the customer said
    it in a form the system parsed. A request phrased in prose carries no date
    and no time — app/tools/pedidos.py opens it with `{"metodo": "entrega"}`
    because nothing may read a delivery date out of a customer's words — and
    approving that used to create an offer solicitudes.revalidar then refused
    forever ("la oferta no dice qué día se entrega"), telling the customer
    something had changed when nothing had. So an "ok" with nothing to approve
    changes NO state and asks for the terms instead.
    """
    solicitud, problema = _abierta(nombre)
    if solicitud is None:
        return _resultado(False, False, problema)
    faltan = solicitudes.terminos_incompletos(solicitud.solicitado)
    if faltan:
        return _resultado(
            False,
            False,
            f"«aprobar {nombre}» aprueba lo que pidió el cliente, y de eso "
            # El marco de este mensaje todavía es español (es otra migración),
            # así que lo que falta se nombra en español para que la frase no
            # quede mitad y mitad. El día que se migre el marco, pasa `lengua`.
            f"falta {solicitudes.enumerar(solicitudes.nombres_de_terminos(faltan, idioma.ES), idioma.ES)}: no puedo ofrecer una entrega "
            "cuyos términos nadie fijó. No cambié nada.\n\n"
            "Decime los términos completos:\n"
            f"{solicitudes.como_pedir_los_terminos(nombre, solicitud.solicitado)}",
        )
    return _decidir(nombre, por, solicitudes.APROBADA, dict(solicitud.solicitado))


def contraofertar(nombre: str, por: str, terminos: dict) -> dict:
    """Offer different terms: another date, another time, another fee. HUMAN ONLY."""
    if not terminos:
        return _resultado(
            False,
            False,
            f"Falta qué ofrecer. Escribí: contraoferta {nombre} <fecha> <hora> <cargo>",
        )
    return _decidir(nombre, por, solicitudes.CONTRAOFERTA, terminos)


def ofrecer_retiro(nombre: str, por: str, terminos: dict) -> dict:
    """Offer pickup at the shop instead of a delivery. HUMAN ONLY."""
    propuesta = {**terminos, "metodo": "retiro", "cargo": 0.0}
    return _decidir(nombre, por, solicitudes.RETIRO, propuesta)


def rechazar_solicitud(nombre: str, por: str, motivo: str) -> dict:
    """Refuse the exception, tell the customer, and stop holding stock. HUMAN ONLY."""
    if not es_equipo(por):
        return _resultado(False, False, "No tenés permiso para decidir solicitudes.")
    razon = " ".join(str(motivo or "").split())
    if len(razon) < 3:
        return _resultado(
            False, False, f"Falta el motivo. Escribí: rechazar {nombre} <motivo>"
        )

    try:
        with distributed_lock(f"solicitud:{nombre}", lease_seconds=60, wait_seconds=10):
            solicitud, problema = _abierta(nombre)
            if solicitud is None:
                return _resultado(False, False, problema)
            _liberado, detalle = solicitudes.soltar_reserva(nombre)
            rechazada = solicitudes.registrar(
                solicitud,
                "rechazada",
                estado=solicitudes.RECHAZADA,
                decision="rechazada",
                decidida_por=por,
                decidida_en=time.time(),
                motivo=razon,
            )
            if rechazada is None:
                return _resultado(
                    False, False, f"No pude registrar el rechazo de {nombre}; reintentá."
                )
    except CoordinationError:
        return _resultado(
            False, False, f"No pude coordinar el rechazo de {nombre}; reintentá en un momento."
        )
    except Exception as exc:
        print(f"[decisiones] {nombre}: rechazo de solicitud falló ({type(exc).__name__})")
        return _resultado(False, False, f"No pude rechazar {nombre}. Revisalo en ERPNext.")

    _comentar(
        nombre,
        f"Solicitud {rechazada.id} rechazada por un integrante autorizado ({por}). "
        f"Motivo: {razon}. {detalle.capitalize()}.",
    )
    avisado = solicitudes.avisar_rechazo(rechazada)
    cola = (
        "Ya le avisé al cliente."
        if avisado
        else "NO pude avisarle al cliente; quedó una tarea para contactarlo."
    )
    return _resultado(True, avisado, f"❌ {nombre}: solicitud rechazada. {detalle.capitalize()}. {cola}")


def cerrar_revision_si_hay(nombre: str, por: str, motivo: str) -> bool:
    """Close a human review a manager's command has just settled. Never raises.

    Called after cancelar/confirmar/rechazar succeed. A no-op unless the order
    really is in review (app/solicitudes.py::resolver_revision), so it is safe
    on every order and idempotent on repeats.
    """
    try:
        return solicitudes.resolver_revision(nombre, por, motivo)
    except Exception as exc:
        print(f"[decisiones] {nombre}: cierre de revisión falló ({type(exc).__name__})")
        return False


def ver_solicitud(nombre: str) -> str:
    """The pending request as a person reads it, or '' when there is none."""
    try:
        solicitud = solicitudes.leer(nombre)
    except Exception as exc:
        print(f"[decisiones] {nombre}: solicitud no legible ({type(exc).__name__})")
        return ""
    if solicitud is None:
        return ""
    return solicitudes.texto_estado(solicitud)
