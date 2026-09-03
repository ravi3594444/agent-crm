"""Button taps -> real ERPNext actions.

Only phones on the staff list can approve. A stranger who somehow guesses a
button payload gets nothing.
"""
from app import avisos, confirmacion, erpnext, notificar, policy, solicitudes
from app.formato import pesos
from app.router import es_equipo


def _solicitud_abierta(nombre: str):
    """The order's open decision request, or None. Never raises."""
    try:
        solicitud = solicitudes.leer(nombre)
    except Exception as exc:
        print(f"[approval] {nombre}: solicitud no legible ({type(exc).__name__})")
        return None
    return solicitud if solicitud is not None and solicitud.abierta else None


def _texto_solicitud(nombre: str) -> str:
    from app import decisiones

    try:
        return decisiones.ver_solicitud(nombre)
    except Exception as exc:
        print(f"[approval] {nombre}: estado de solicitud no legible ({type(exc).__name__})")
        return ""


def _cerrar_revision(nombre: str, telefono: str, motivo: str) -> None:
    """End a human review a manager has just dealt with. Never raises.

    The review's own deadline exists so a draft cannot reserve stock for ever
    (app/solicitudes.py). A manager acting on the order settles it, so the
    review is closed here rather than left for the sweep — and if this fails,
    the sweep still reaches it, which is why nothing is escalated.
    """
    try:
        solicitudes.resolver_revision(nombre, telefono, motivo)
    except Exception as exc:
        print(f"[approval] {nombre}: cierre de revisión falló ({type(exc).__name__})")


def _leer_doc(doctype: str, name: str) -> dict:
    """Approval runs outside the LLM and may use the policy credential."""
    getter = getattr(erpnext, "policy_get_doc", erpnext.get_doc)
    return getter(doctype, name)


def manejar_boton(reply_id: str, telefono: str) -> str:
    if not es_equipo(telefono):
        return "No tenés permiso para aprobar pedidos."
    if ":" not in reply_id:
        return "No entendí esa acción."

    accion, nombre = reply_id.split(":", 1)

    if accion == "ok":
        # An order with an OPEN decision request is not confirmed by "aprobar":
        # that word means "approve what the customer asked for", and the terms
        # still have to be put to the customer before anything is submitted.
        # See app/solicitudes.py.
        pendiente = _solicitud_abierta(nombre)
        if pendiente is not None:
            from app import decisiones

            return decisiones.aprobar_solicitud(nombre, telefono)["detalle"]
        resultado = confirmar_pedido(nombre, telefono)
        # "confirmar" is also the documented way out of a human review, and it
        # is why REVISION_HUMANA is deliberately NOT one of solicitudes.ABIERTOS
        # — this word has to keep meaning "submit this draft". Closing the
        # review here takes the draft out of the expiry index at once instead
        # of waiting for the sweep to notice; it is a no-op when there is no
        # review, so a manager typing it three times costs three reads.
        if resultado.get("ok"):
            _cerrar_revision(nombre, telefono, "una persona confirmó el pedido")
        return resultado["detalle"]

    if accion == "contraoferta":
        # "contraoferta:<pedido>:<fecha> <hora> <cargo>"
        from app import decisiones

        pedido, _, crudo = nombre.partition(":")
        terminos = solicitudes.parsear_terminos(crudo)
        if terminos is None:
            return (
                f"No entendí los términos. Escribí: contraoferta {pedido.strip()} "
                "<fecha> <hora> <cargo>, por ejemplo "
                f"'contraoferta {pedido.strip()} mañana 18:00 1500'."
            )
        return decisiones.contraofertar(pedido.strip(), telefono, terminos)["detalle"]

    if accion == "retiro":
        # "retiro:<pedido>:<fecha> <hora>" — pickup instead of a delivery.
        from app import decisiones

        pedido, _, crudo = nombre.partition(":")
        terminos = solicitudes.parsear_terminos(crudo, con_cargo=False)
        if terminos is None:
            return (
                f"No entendí los términos. Escribí: retiro {pedido.strip()} "
                "<fecha> <hora>, por ejemplo "
                f"'retiro {pedido.strip()} jueves 10:00'."
            )
        return decisiones.ofrecer_retiro(pedido.strip(), telefono, terminos)["detalle"]

    if accion == "no":
        # The customer was told the order was received and would be confirmed.
        # Rejecting must therefore tell them too — see app/decisiones.py.
        from app import decisiones

        # With an open request, "rechazar" refuses the EXCEPTION the customer
        # asked for and frees the stock its draft was holding; the draft itself
        # stays for the manager to amend.
        pedido, _, motivo_libre = nombre.partition(":")
        pendiente = _solicitud_abierta(pedido.strip())
        if pendiente is not None:
            return decisiones.rechazar_solicitud(
                pedido.strip(), telefono, motivo_libre or "sin detalle"
            )["detalle"]
        nombre = pedido.strip()
        resultado = decisiones.rechazar(nombre, telefono, motivo_libre)
        if resultado.get("ok"):
            _cerrar_revision(nombre, telefono, "una persona rechazó el pedido")
        cola = (
            "Ya le avisé al cliente."
            if resultado["aviso_cliente"]
            else "NO pude avisarle al cliente; contactalo vos."
        )
        return (
            f"❌ {nombre} rechazado. El borrador queda sin confirmar para que lo "
            f"revises o lo borres en ERPNext. {cola}"
        )

    if accion == "conteo":
        # A physical count is a claim about the real world; only a person can
        # make it. The submit uses the policy credential, never an LLM tool.
        from app import decisiones

        return decisiones.confirmar_conteo(nombre, telefono)["detalle"]

    if accion == "preparar":
        # Stage 2e: a DRAFT Delivery Note, policy identity. Dispatching it is a
        # second, separate human command — never the same tap, never an LLM.
        from app import decisiones

        return decisiones.preparar(nombre, telefono)["detalle"]

    if accion == "despachar":
        from app import decisiones

        return decisiones.despachar(nombre, telefono)["detalle"]

    if accion == "despreparar":
        # Undo a preparation: deletes ONLY a draft Delivery Note this system
        # created and nobody edited, after auditing it on the order. It is the
        # single place allowed to remove a linked draft, so "cancelar" never
        # has to.
        from app import decisiones

        return decisiones.despreparar(nombre, telefono)["detalle"]

    if accion == "cancelar":
        # "cancelar:<pedido>:<motivo>" — a confirmed order, within the window,
        # with a reason. Everything is re-checked in app/decisiones.py.
        from app import decisiones

        pedido, _, motivo = nombre.partition(":")
        return decisiones.cancelar(pedido.strip(), telefono, motivo)["detalle"]

    if accion == "ver":
        try:
            so = _leer_doc("Sales Order", nombre)
        except erpnext.ERPNextError:
            return f"No pude abrir {nombre}. Revisalo en ERPNext."
        estado_solicitud = _texto_solicitud(nombre)
        detalle = "\n".join(
            f"  · {i['qty']:g} x {i.get('item_name') or i['item_code']} "
            f"= {pesos(i.get('amount', 0))}"
            for i in so.get("items", [])
        )
        cuerpo = (
            f"{nombre} — {so.get('customer_name') or so['customer']}\n{detalle}\n"
            f"Total {pesos(so.get('grand_total', 0))} · entrega {so.get('delivery_date')}"
        )
        return f"{cuerpo}\n\n{estado_solicitud}" if estado_solicitud else cuerpo

    return "Acción desconocida."


def confirmar_pedido(nombre: str, por: str) -> dict:
    """Confirm one order on behalf of an ALREADY AUTHENTICATED human manager.

    Moved out of manejar_boton unchanged so app/decisiones.py can offer it as
    the manual-path entry point without duplicating logic that is already
    proven against duplicate taps and submit timeouts that commit after the
    HTTP client gives up. Submission still uses the policy credential via
    erpnext.submit_doc; nothing here is reachable from an LLM tool.

    Returns {"ok", "aviso_cliente", "detalle"} — `detalle` is the text shown to
    the manager.
    """
    try:
        actual = _leer_doc("Sales Order", nombre)
        ya_confirmado = actual.get("docstatus") == 1
        if not ya_confirmado and actual.get("docstatus") != 0:
            return {
                "ok": False,
                "aviso_cliente": False,
                "detalle": f"No se puede confirmar {nombre} en su estado actual.",
            }
        if not ya_confirmado and policy.sin_reserva(actual.get("status")):
            # A rejected draft is left Closed so it stops holding stock.
            # ERPNext does not count a Closed order in reserved_qty even after
            # a submit, so submitting this one would promise units that no
            # reservation system can see, and it would never reach the
            # delivery queue either. Reopening it is a deliberate act.
            return {
                "ok": False,
                "aviso_cliente": False,
                "detalle": (
                    f"{nombre} está {actual.get('status')} — se rechazó antes y ya "
                    "no reserva stock. Si lo querés confirmar, reabrilo en ERPNext "
                    "(estado Draft) y volvé a tocar Confirmar."
                ),
            }
        if not ya_confirmado:
            try:
                erpnext.submit_doc("Sales Order", nombre)
            except erpnext.ERPNextError:
                # A timeout can happen after ERPNext committed. Re-read the
                # source of truth before reporting a failed confirmation.
                actual = _leer_doc("Sales Order", nombre)
                if actual.get("docstatus") != 1:
                    raise
            erpnext.add_comment(
                "Sales Order",
                nombre,
                f"Confirmado por un integrante autorizado mediante WhatsApp ({por}).",
            )
            # Durable record of WHEN, in ERPNext: it opens the manual
            # cancellation window and survives any Redis restart.
            confirmacion.registrar(nombre, f"manual (confirmación humana, {por})")
    except erpnext.ERPNextError as error:
        print(f"[approval] {nombre}: {type(error).__name__}")
        return {
            "ok": False,
            "aviso_cliente": False,
            "detalle": (
                f"No pude comprobar la confirmación de {nombre}. Revisalo en ERPNext."
            ),
        }

    # Stage 2e: the manager team gets ONE confirmed-order notice per order, no
    # matter which path confirmed it or how many times the button is tapped.
    _notificar_confirmada(nombre, actual)

    prefix = "ℹ️ Ya estaba confirmado." if ya_confirmado else f"✅ {nombre} confirmado."
    estado_aviso = _encolar_confirmacion(nombre, actual)
    return {"ok": True, "aviso_cliente": estado_aviso[0], "detalle": f"{prefix} {estado_aviso[1]}"}


def _encolar_confirmacion(nombre: str, conocido: dict) -> tuple[bool, str]:
    """Queue the customer's authoritative confirmation. Never raises.

    Returns (the customer is covered, what to tell the manager). "Covered"
    includes a notice queued by the automatic path minutes earlier: the queue
    is keyed on (event, order), so the customer is told exactly once no matter
    how many paths reach this point or how many times a button is tapped.
    """
    try:
        completo = _leer_doc("Sales Order", nombre)
    except Exception:
        completo = conocido
    try:
        nuevo = avisos.confirmacion_cliente(completo)
    except Exception as exc:
        print(f"[approval] {nombre}: no pude encolar el aviso ({type(exc).__name__})")
        return False, (
            "NO pude poner en cola el aviso al cliente; contactalo vos."
        )
    if nuevo:
        return True, "El aviso al cliente quedó en cola y sale enseguida."
    return True, "El cliente ya tenía su confirmación; no le mando otra."


def _notificar_confirmada(nombre: str, conocido: dict) -> None:
    """Never raises: a notice problem must not change what the manager is told."""
    try:
        try:
            completo = _leer_doc("Sales Order", nombre)
        except erpnext.ERPNextError:
            completo = conocido
        notificar.notificar_confirmacion(completo, "manual (confirmación humana)")
    except Exception as exc:
        print(f"[approval] {nombre}: aviso de confirmación falló ({type(exc).__name__})")
