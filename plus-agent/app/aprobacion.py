"""Button taps -> real ERPNext actions.

Only phones on the staff list can approve. A stranger who somehow guesses a
button payload gets nothing.
"""
from app import avisos, confirmacion, erpnext, notificar, policy, solicitudes
from app.formato import pesos
from app.router import es_equipo


def solicitud_abierta_estricta(nombre: str):
    """La solicitud ABIERTA del pedido, o None — y `None` SÓLO quiere decir que no hay.

    Levanta `solicitudes.LecturaIncierta` si el estado no se pudo leer. Es el
    mismo predicado que `solicitud_abierta` y la misma única definición de
    «abierta»; lo único distinto es qué hace cuando ERPNext no contesta.

    Lo usa `decisiones.confirmar`, que es la única decisión irreversible del
    sistema: ahí «no pude leer» leído como «no hay solicitud» emite el pedido al
    precio viejo mientras el cliente tiene la contraoferta abierta en el
    teléfono, y el submit no se deshace. Ver `solicitudes.LecturaIncierta`.
    """
    solicitud = solicitudes.leer_estricto(nombre)
    return solicitud if solicitud is not None and solicitud.abierta else None


def solicitud_abierta(nombre: str):
    """The order's open decision request, or None. Never raises.

    Sin guión bajo porque tiene un segundo llamador: `decisiones.confirmar`,
    que es donde vive ahora la bifurcación que este predicado decide. Una copia
    más de esto —`acciones.py` ya tiene la suya— sería un concepto escrito tres
    veces y ningún test podría notar que discrepan.

    Colapsa la lectura fallida en `None`, que es lo que un llamador que sólo
    ENRUTA puede permitirse (el «no» de `manejar_boton` deriva a rechazar la
    solicitud o a rechazar el pedido: ninguna de las dos emite nada). El que
    decide algo irreversible usa `solicitud_abierta_estricta`.
    """
    try:
        return solicitud_abierta_estricta(nombre)
    except Exception as exc:
        print(f"[approval] {nombre}: solicitud no legible ({type(exc).__name__})")
        return None


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
    from app import decisiones

    decisiones.cerrar_revision_si_hay(nombre, telefono, motivo)


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
        # UNA LÍNEA, y es el punto. La bifurcación por solicitud abierta, el
        # cierre de la revisión humana y el lock por pedido vivían acá, en el
        # camino de WhatsApp, mientras `decisiones.confirmar` —el nombre que
        # cualquier otro llamador iba a usar— era un alias pelado de
        # `confirmar_pedido`. Un segundo llamador que no copiara esta función
        # entera confirmaba al precio original mientras el cliente mira una
        # contraoferta que no aceptó. Ahora todo eso está adentro de
        # `decisiones.confirmar` y este camino no es especial.
        from app import decisiones

        return decisiones.confirmar(
            nombre, telefono, canal=decisiones.CANAL_WHATSAPP
        )["detalle"]

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
        pendiente = solicitud_abierta(pedido.strip())
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


def confirmar_pedido(nombre: str, por: str, *, canal: str) -> dict:
    """El MECANISMO de confirmar, con quien llama YA autenticado y autorizado.

    Acá está lo que está probado contra el toque repetido y contra un submit que
    commitea DESPUÉS de que el cliente HTTP se dio por vencido. Quién puede
    hacerlo, si hay una solicitud abierta y el lock son de `decisiones.confirmar`,
    que es la puerta; esta función no comprueba nada de eso y no debe. El Submit
    sigue siendo `erpnext.submit_doc`, la credencial de política, y nada de acá
    es alcanzable desde una herramienta del modelo.

    `canal` NO TIENE DEFAULT, y ésa es la mitad que importa. El rastro que se
    escribe en ERPNext decía «mediante WhatsApp» como literal, así que el primer
    llamador que no fuera WhatsApp —el panel— iba a firmar en el historial del
    pedido un canal por el que nadie pasó, y un rastro de auditoría que miente
    es peor que no tenerlo. Un default habría hecho exactamente eso en silencio:
    acá el que confirma tiene que DECIR por dónde entró.

    Devuelve {"ok", "aviso_cliente", "detalle"} — `detalle` es lo que se le
    muestra al encargado.
    """
    # La ventana de anulación por WhatsApp la abre la marca durable, y el
    # default NO puede ser «sí»: por la rama de `ya_confirmado` no se pasa por
    # `registrar`, así que un segundo toque después de que la marca falló —o un
    # pedido confirmado a mano en ERPNext— informaba una anulación disponible
    # que el camino de cancelación después rechaza. Se comprueba, y `momento`
    # devuelve None para «no se puede probar»: eso es fallar cerrado.
    ventana = False
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
                f"Confirmado por un integrante autorizado mediante {canal} ({por}).",
            )
            # Durable record of WHEN, in ERPNext: it opens the manual
            # cancellation window and survives any Redis restart.
            #
            # EL BOOLEANO SE MIRA. `registrar` atrapa su propia excepción,
            # imprime y devuelve False (app/confirmacion.py), así que un fallo
            # NO llega al `except` de abajo: el pedido quedaba confirmado, la
            # ventana de cancelación no existía, y al encargado se le mandaba
            # igual «para anularlo dentro de las 24 h: cancelar …». Se le
            # prometía algo que el sistema iba a rechazar.
            ventana = confirmacion.registrar(
                nombre, f"manual (confirmación humana por {canal}, {por})"
            )
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
    if ya_confirmado:
        # No lo escribió esta llamada, así que hay que ir a mirarlo.
        ventana = confirmacion.momento(nombre) is not None
    _notificar_confirmada(nombre, actual, ventana=ventana)

    prefix = "ℹ️ Ya estaba confirmado." if ya_confirmado else f"✅ {nombre} confirmado."
    estado_aviso = _encolar_confirmacion(nombre, actual)
    detalle = f"{prefix} {estado_aviso[1]}"
    if not ventana:
        # Lo que el encargado tiene delante en el acto, no sólo el aviso
        # durable: si toca «cancelar» creyendo que puede, pierde el tiempo.
        detalle += (
            " No pude dejar el registro de la confirmación: la anulación por "
            "WhatsApp no está disponible, hacelo en ERPNext si hace falta."
        )
    return {"ok": True, "aviso_cliente": estado_aviso[0], "detalle": detalle}


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


def _notificar_confirmada(nombre: str, conocido: dict, *, ventana: bool = True) -> None:
    """Never raises: a notice problem must not change what the manager is told."""
    try:
        try:
            completo = _leer_doc("Sales Order", nombre)
        except erpnext.ERPNextError:
            completo = conocido
        # La CLAVE, no el texto: el mensaje se arma en el idioma del dueño. El
        # registro durable de más arriba sigue guardando su español, que es lo
        # que ya está escrito en los ERPNext de los despliegues.
        notificar.notificar_confirmacion(
            completo, "gerencia.fuente_manual", ventana=ventana
        )
    except Exception as exc:
        print(f"[approval] {nombre}: aviso de confirmación falló ({type(exc).__name__})")
