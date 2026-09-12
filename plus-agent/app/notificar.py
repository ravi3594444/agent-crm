"""Notify staff about an order.

Preferred channel: an approved WhatsApp template. A business-initiated message
to the owner's phone needs one, because the customer's inbound message does
not open a 24-hour service window for a different number.

Fallback: while a staff phone has ITS OWN window open (it wrote to the bot in
the last 24 hours) Meta allows free-form messages to it, so the alert goes out
as free text with reply buttons. Otherwise the alert fails closed and ERPNext
records that manual follow-up is required. The bot never claims an alert that
Meta did not accept.
"""
import hashlib
import os

from app import entrega, erpnext, reloj
from app.formato import cantidad, pesos
from app.outbound_status import (
    claim_once,
    has_accepted,
    record_outbound,
    registrar_aviso_fallido,
    release_claim,
    window_open,
)
from app.router import STAFF
from app.whatsapp import enviar_botones, enviar_mensaje, enviar_plantilla


def _lengua_equipo() -> str:
    """El idioma en que lee el equipo. Nunca levanta."""
    from app import idioma as idioma_mod

    return idioma_mod.gerencia()


def _boton(clave: str, lengua: str | None = None) -> str:
    """La etiqueta de un botón, en el idioma del equipo y ya recortada.

    Meta corta el título en 20 caracteres y `whatsapp.enviar_botones` lo hace
    también, las dos veces en silencio. Recortar acá no arregla eso: lo que lo
    arregla es que las etiquetas entren, y `tests/test_idioma_salida.py` lo
    exige para las dos versiones de cada una.
    """
    from app import idioma as idioma_mod

    return idioma_mod.t(clave, lengua if lengua is not None else _lengua_equipo())


def _texto_libre(
    nombre: str, so: dict, auto: bool, motivos: str, detalle: str,
    lengua: str | None = None,
) -> str:
    from app import idioma as idioma_mod

    encabezado = idioma_mod.t(
        "gerencia.encabezado_confirmado" if auto else "gerencia.encabezado_pendiente",
        lengua,
    )
    lineas = [
        encabezado,
        idioma_mod.t(
            "gerencia.cuerpo_pedido",
            lengua,
            pedido=nombre,
            cliente=so.get("customer_name") or so.get("customer") or "Cliente",
            detalle=detalle,
            # `pesos`, como TODA la plata que lee una persona. Era la única que
            # quedaba escrita a mano: `f"{...:,.2f}"` da "1,200.50", que un
            # argentino lee como un peso veinte, y sin símbolo — justo en el
            # aviso con el que autoriza el pedido.
            total=pesos(so.get("grand_total"), 2),
            entrega=so.get("delivery_date")
            or idioma_mod.t("gerencia.sin_fecha", lengua),
        ),
    ]
    if not auto:
        sin_obs = idioma_mod.t("gerencia.sin_observaciones", lengua)
        lineas.append(
            idioma_mod.t(
                "gerencia.motivo", lengua, motivo=(motivos or sin_obs)[:300]
            )
        )
        lineas.append(
            idioma_mod.t("gerencia.responder_para_decidir", lengua, pedido=nombre)
        )
    # Interactive bodies are capped at 1024 characters by Meta.
    return "\n".join(lineas)[:1024]


def notificar_equipo(
    nombre: str, so: dict, auto: bool, motivos: str = ""
) -> bool:
    """Return True only when Meta accepts at least one staff notification."""
    detalle = "; ".join(
        f"{i['qty']:g} x {i.get('item_name') or i['item_code']}"
        for i in so.get("items", [])
    )[:1000] or "Sin líneas"
    variable = (
        "WHATSAPP_STAFF_CONFIRMED_TEMPLATE"
        if auto
        else "WHATSAPP_STAFF_PENDING_TEMPLATE"
    )
    plantilla = os.getenv(variable, "").strip()
    locale_plantilla = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "es_AR").strip() or "es_AR"

    if not STAFF:
        print(f"[staff-notify] {nombre}: TELEFONOS_EQUIPO vacío")
        erpnext.add_comment(
            "Sales Order",
            nombre,
            "Alerta al equipo no enviada: TELEFONOS_EQUIPO no está configurado.",
        )
        return False

    parametros = [
        nombre,
        "Confirmado" if auto else "Pendiente de revisión",
        str(so.get("customer_name") or so.get("customer") or "Cliente"),
        detalle,
        f"{float(so.get('grand_total') or 0):.2f}",
        str(so.get("delivery_date") or "Sin fecha"),
        (motivos or "Sin observaciones")[:1000],
    ]
    # A generic ERPNext Sales Order has no durable "rejected draft" state.
    # Offer only actions whose state transition we can enforce truthfully.
    acciones = None if auto else [f"ok:{nombre}", f"ver:{nombre}"]
    lengua = _lengua_equipo()
    texto = _texto_libre(nombre, so, auto, motivos, detalle, lengua)
    # El `id` es el payload que parsea el router y NO se traduce nunca; el
    # `title` es lo único que lee el dueño, y es lo que estaba en español aunque
    # el cuerpo del aviso saliera en inglés. Éste es además el único camino del
    # producto en que él recibe algo sin haber escrito nada, así que su idioma no
    # puede salir de lo que tecleó: sale del ajuste.
    botones = (
        None
        if auto
        else [
            {"id": f"ok:{nombre}", "title": _boton("boton.confirmar", lengua)},
            {"id": f"ver:{nombre}", "title": _boton("boton.ver_detalle", lengua)},
        ]
    )

    enviados = 0
    sin_canal = 0
    telefonos = sorted(STAFF)
    if os.getenv("NOTIFICAR_SOLO_PRIMERO", "true").lower() == "true":
        telefonos = telefonos[:1]
    for telefono in telefonos:
        recipient_tag = hashlib.sha256(telefono.encode()).hexdigest()[:12]
        purpose = (
            "staff_order_confirmed" if auto else "staff_order_pending"
        ) + f":{recipient_tag}"
        try:
            if has_accepted(nombre, purpose):
                enviados += 1
                continue
            if plantilla:
                result = enviar_plantilla(
                    telefono,
                    plantilla,
                    locale_plantilla,
                    parametros,
                    acciones,
                )
            elif window_open(telefono):
                result = (
                    enviar_botones(telefono, texto, botones)
                    if botones
                    else enviar_mensaje(telefono, texto)
                )
            else:
                sin_canal += 1
                print(
                    f"[staff-notify] {nombre}: falta {variable} y la ventana de "
                    f"24 h de {recipient_tag} está cerrada"
                )
                continue
            enviados += 1
            wamid = result["messages"][0]["id"]
            try:
                record_outbound(wamid, purpose, order_name=nombre)
            except Exception as tracking_error:
                # The API acceptance is still real. Record the observability
                # gap without causing a duplicate send.
                print(
                    f"[staff-notify] {nombre}: tracking falló "
                    f"({type(tracking_error).__name__})"
                )
                erpnext.add_comment(
                    "Sales Order",
                    nombre,
                    "Meta aceptó la alerta al equipo, pero no se pudo guardar "
                    "su seguimiento de entrega.",
                )
        except Exception as e:
            print(
                f"[staff-notify] {nombre}: envío falló "
                f"({type(e).__name__})"
            )

    if enviados:
        erpnext.add_comment(
            "Sales Order",
            nombre,
            f"Alerta de WhatsApp aceptada por Meta para {enviados} integrante(s).",
        )
        return True

    if sin_canal:
        erpnext.add_comment(
            "Sales Order",
            nombre,
            f"Alerta al equipo no enviada: falta configurar {variable} y ningún "
            "teléfono del equipo escribió al bot en las últimas 24 h. "
            "Requiere seguimiento manual.",
        )
    else:
        erpnext.add_comment(
            "Sales Order",
            nombre,
            "Alerta al equipo no enviada; requiere seguimiento manual.",
        )
    # Nobody received it: park it and make sure a person sees a task.
    registrar_aviso_fallido(
        "staff_order_confirmed" if auto else "staff_order_pending", nombre, texto
    )
    return False


# ---------------------------------------------------------------------------
# Stage 2e — the confirmed-order notice: exactly once per order.
#
# Both confirmation paths end here: policy.evaluar + submit in
# app/tools/pedidos.py (automatic) and aprobacion.confirmar_pedido (a human on
# the signed webhook). The first one to claim the order sends; the other finds
# the claim and does nothing. A claim whose send reaches nobody is released, so
# the other path (or a retry) can still notify.
# ---------------------------------------------------------------------------

CONFIRMACION_TTL_SEGUNDOS = 30 * 24 * 60 * 60


def _momento_negocio() -> str:
    """El respaldo era `datetime.now()` SIN zona — el reloj del servidor, casi
    siempre UTC— así que una zona mal escrita ponía en el aviso una hora de
    otro reloj sin decirlo. Ahora respalda al default del negocio, con log."""
    return reloj.ahora_con_respaldo("notificar").strftime("%Y-%m-%d %H:%M")


def _direccion_de_entrega(so: dict) -> str:
    """The delivery address as a person reads it; the Address name if unreadable."""
    nombre = entrega.nombre_direccion(so)
    if not nombre:
        return ""
    try:
        doc = erpnext.policy_get_doc("Address", nombre)
    except Exception:
        return nombre
    return entrega.texto_direccion(doc) if isinstance(doc, dict) else nombre


def _renglones(so: dict) -> str:
    partes = []
    for item in so.get("items") or []:
        if not isinstance(item, dict):
            continue
        unidad = item.get("uom") or item.get("stock_uom") or "u"
        partes.append(
            f"{cantidad(item.get('qty'))} {unidad} × "
            f"{item.get('item_name') or item.get('item_code') or 'producto'}"
        )
    return "; ".join(partes) or "sin renglones"


def renglones(so: dict) -> str:
    """The order's lines as a person reads them. Same text for every channel."""
    return _renglones(so)


def direccion_de_entrega(so: dict) -> str:
    """The order's delivery address as a person reads it, or ''."""
    return _direccion_de_entrega(so)


def texto_confirmacion(
    so: dict, fuente: str, momento: str | None = None, lengua: str | None = None
) -> str:
    """What the manager reads: every field the client asked for, in order."""
    direccion = _direccion_de_entrega(so)
    entrega_txt = " — ".join(
        parte for parte in (direccion, str(so.get("delivery_date") or "")) if parte
    ) or __import__("app.idioma", fromlist=["t"]).t(
        "gerencia.a_coordinar", lengua if lengua is not None else _lengua_equipo()
    )
    total = f"{pesos(so.get('grand_total'), 2)} {so.get('currency') or ''}".strip()
    from app import idioma as idioma_mod

    return idioma_mod.t(
        "gerencia.confirmado_detalle",
        lengua if lengua is not None else _lengua_equipo(),
        pedido=so.get("name"),
        cliente=so.get("customer_name") or so.get("customer") or "Cliente",
        detalle=_renglones(so),
        total=total,
        entrega=entrega_txt,
        fuente=fuente,
        momento=momento or _momento_negocio(),
        horas=os.getenv("CANCELACION_HORAS", "24"),
    )[:3500]


def notificar_confirmacion(so: dict, fuente: str) -> bool:
    """Tell the human manager an order is confirmed — exactly once per order.

    ``fuente`` is "automática (política)" or "manual (confirmación humana)".
    Returns True when Meta accepted it for at least one staff phone, or when
    the order was already notified. Never raises.
    """
    nombre = str(so.get("name") or "").strip()
    if not nombre:
        return False
    try:
        if not claim_once(f"confirm-notice:{nombre}", CONFIRMACION_TTL_SEGUNDOS):
            return True
    except Exception as exc:
        # Cannot coordinate: a possible duplicate beats a certain silence.
        print(f"[staff-notify] {nombre}: claim no disponible ({type(exc).__name__})")

    momento = _momento_negocio()
    texto = texto_confirmacion(so, fuente, momento)
    plantilla = os.getenv("WHATSAPP_STAFF_CONFIRMED_TEMPLATE", "").strip()
    locale_plantilla = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "es_AR").strip() or "es_AR"
    parametros = [
        nombre,
        "Confirmado",
        str(so.get("customer_name") or so.get("customer") or "Cliente"),
        _renglones(so)[:1000],
        f"{float(so.get('grand_total') or 0):.2f} {so.get('currency') or ''}".strip(),
        " — ".join(
            p for p in (_direccion_de_entrega(so), str(so.get("delivery_date") or "")) if p
        )[:1000]
        or "a coordinar",
        f"Origen: {fuente}; confirmado {momento}"[:1000],
    ]

    if not STAFF:
        print(f"[staff-notify] {nombre}: TELEFONOS_EQUIPO vacío, sin aviso de confirmación")
        erpnext.add_comment(
            "Sales Order", nombre, "Aviso de confirmación al equipo no enviado: TELEFONOS_EQUIPO vacío."
        )
        release_claim(f"confirm-notice:{nombre}")
        registrar_aviso_fallido("manager_order_confirmed", nombre, texto)
        return False

    telefonos = sorted(STAFF)
    if os.getenv("NOTIFICAR_SOLO_PRIMERO", "true").lower() == "true":
        telefonos = telefonos[:1]
    enviados = 0
    for telefono in telefonos:
        tag = hashlib.sha256(telefono.encode()).hexdigest()[:12]
        purpose = f"manager_order_confirmed:{tag}"
        try:
            if has_accepted(nombre, purpose):
                enviados += 1
                continue
            if plantilla:
                result = enviar_plantilla(telefono, plantilla, locale_plantilla, parametros)
            elif window_open(telefono):
                result = enviar_mensaje(telefono, texto)
            else:
                print(
                    f"[staff-notify] {nombre}: sin plantilla de confirmación y ventana "
                    f"de 24 h de {tag} cerrada"
                )
                continue
            enviados += 1
            try:
                record_outbound(result["messages"][0]["id"], purpose, order_name=nombre)
            except Exception as tracking_error:
                print(f"[staff-notify] {nombre}: tracking falló ({type(tracking_error).__name__})")
        except Exception as exc:
            print(f"[staff-notify] {nombre}: aviso de confirmación falló ({type(exc).__name__})")

    if enviados:
        erpnext.add_comment(
            "Sales Order",
            nombre,
            f"Aviso de pedido confirmado ({fuente}) aceptado por Meta para {enviados} integrante(s).",
        )
        return True

    release_claim(f"confirm-notice:{nombre}")
    erpnext.add_comment(
        "Sales Order",
        nombre,
        "Aviso de pedido confirmado al equipo NO enviado (sin plantilla y sin ventana de "
        "24 h abierta, o Meta lo rechazó). Quedó en la lista de avisos pendientes.",
    )
    registrar_aviso_fallido("manager_order_confirmed", nombre, texto)
    return False


# ---------------------------------------------------------------------------
# Exception alerts to the HUMAN MANAGER.
#
# Everything that needs a human's attention outside the normal order flow goes
# through alertar_excepcion(). It is the single funnel on purpose: the client
# wants the AI Management Agent to be able to PHONE the manager for urgent
# situations later, and that channel plugs in here — one place to add, with
# every caller already routed through it. No call code is written yet.
#
# These alerts are informational. They never decide anything: confirming or
# rejecting an order stays in app/decisiones.py, reachable only from the signed
# webhook after router.es_equipo authenticates the manager.
# ---------------------------------------------------------------------------

# Urgency is carried explicitly so a future voice channel can pick a threshold
# without every caller having to be revisited.
URGENCIA_NORMAL = "normal"
URGENCIA_ALTA = "alta"


def alertar_excepcion(
    asunto: str,
    cuerpo: str,
    *,
    urgencia: str = URGENCIA_NORMAL,
    plantilla_env: str = "",
    parametros: list[str] | None = None,
) -> bool:
    """Tell the human manager something needs them. Never raises.

    Free text first, because staff usually have an open window; an approved
    template only if Meta refuses and one is configured. Returns True when Meta
    acknowledged at least one message, so callers can be honest about whether
    the manager was really reached.
    """
    from app import router

    destinatarios = _destinatarios(router)
    if not destinatarios:
        print(f"[alerta] {asunto}: TELEFONOS_EQUIPO vacío, nadie fue avisado")
        return False
    return _alertar(destinatarios, asunto, cuerpo, urgencia, plantilla_env, parametros)


def telefono_dueno() -> str:
    """El número del DUEÑO, explícito: TELEFONO_DUENO. "" si no se puede saber.

    El resumen del día es un mensaje para el dueño, no una alerta para «el
    equipo». La ruta genérica (_destinatarios) elige el primero de la lista
    ORDENADA ALFABÉTICAMENTE de TELEFONOS_EQUIPO, y con más de un número ese
    primero puede ser un empleado. Acá no se deriva nada de esa lista:

      * TELEFONO_DUENO cargado -> ese, y tiene que ser uno de TELEFONOS_EQUIPO
        (un error de tipeo no puede mandar el estado del negocio a un extraño);
      * vacío y el equipo tiene UN solo número -> ése, sin ambigüedad;
      * vacío y hay varios -> "" y se dice en el log. `make check-env` lo
        marca como error para que no quede así.
    """
    from app import router, telefono

    equipo = list(getattr(router, "STAFF", None) or [])
    crudo = os.getenv("TELEFONO_DUENO", "").strip()
    if crudo:
        numero = telefono.normalizar(crudo)
        if not numero:
            print("[dueño] TELEFONO_DUENO no se puede interpretar")
            return ""
        if numero not in equipo:
            print("[dueño] TELEFONO_DUENO no está en TELEFONOS_EQUIPO: no le mando nada")
            return ""
        return numero
    if len(equipo) == 1:
        return equipo[0]
    print(
        f"[dueño] TELEFONO_DUENO vacío y el equipo tiene {len(equipo)} números: "
        "no sé quién es el dueño"
    )
    return ""


def avisar_dueno(
    asunto: str,
    cuerpo: str,
    *,
    plantilla_env: str = "",
    parametros: list[str] | None = None,
) -> bool:
    """A message for the OWNER only (the daily digest). Never raises.

    Same delivery as alertar_excepcion — free text first, template if Meta
    refuses and one is configured — but the recipient is telefono_dueno(), not
    the first of the sorted staff list. With no owner resolvable nothing is
    sent and the failure is recorded, so it shows up as a ToDo and in the next
    digest instead of vanishing.
    """
    numero = telefono_dueno()
    if not numero:
        print(f"[dueño] {asunto}: sin destinatario, no salió")
        registrar_aviso_fallido(f"owner:{asunto[:40]}", "", f"{asunto}\n{cuerpo}".strip()[:3500])
        return False
    return _alertar([numero], asunto, cuerpo, URGENCIA_NORMAL, plantilla_env, parametros)


def _alertar(
    destinatarios: list[str],
    asunto: str,
    cuerpo: str,
    urgencia: str,
    plantilla_env: str,
    parametros: list[str] | None,
) -> bool:
    """Deliver one text to these numbers: free text, then the template. Never raises."""
    from app import whatsapp

    texto = f"{asunto}\n{cuerpo}".strip()[:3500]
    locale_plantilla = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "es_AR").strip() or "es_AR"
    plantilla = os.getenv(plantilla_env, "").strip() if plantilla_env else ""

    enviados = 0
    for numero in destinatarios:
        try:
            whatsapp.enviar_mensaje(numero, texto)
            enviados += 1
            continue
        except Exception as exc:
            print(f"[alerta] {asunto}: texto falló ({type(exc).__name__})")
        if not plantilla:
            continue
        try:
            whatsapp.enviar_plantilla(
                numero, plantilla, locale_plantilla, parametros or [asunto, cuerpo[:512]]
            )
            enviados += 1
        except Exception as exc:
            print(f"[alerta] {asunto}: plantilla falló ({type(exc).__name__})")

    if not enviados:
        print(f"[alerta] {asunto}: urgencia={urgencia} no llegó a nadie")
        registrar_aviso_fallido(f"exception:{asunto[:40]}", "", texto)
    return bool(enviados)


def _destinatarios(router_module) -> list[str]:
    """Staff phones, read at call time so a reload of TELEFONOS_EQUIPO applies."""
    staff = getattr(router_module, "STAFF", None) or []
    numeros = sorted(staff)
    if os.getenv("NOTIFICAR_SOLO_PRIMERO", "true").lower() == "true":
        return numeros[:1]
    return numeros


def pedir_confirmacion_conteo(telefono: str, nombre: str, texto: str) -> bool:
    """Ask the manager to confirm a count with one tap.

    He is by definition inside the 24-hour window — he just sent the count —
    so a free-form interactive message works and no template is needed. Never
    raises: if the button cannot be sent, the caller tells him to confirm it in
    ERPNext instead of pretending it is done.
    """
    from app import whatsapp

    try:
        whatsapp.enviar_botones(
            telefono,
            texto,
            [{"id": f"conteo:{nombre}", "title": _boton("boton.confirmar_conteo")}],
        )
        return True
    except Exception as exc:
        print(f"[staff-notify] botón de conteo {nombre} falló ({type(exc).__name__})")
        return False


def pedir_codigo_de_ajuste(telefono: str, texto: str) -> bool:
    """Send the owner the confirmation code for a setting he asked to change.

    DIRECT, and to HIS number — not through the model's reply. A code that came
    back inside a tool result is a code the model has read, and a model that has
    read it can confirm the change on its own: the second step stops being a
    person and becomes the same turn as the first. This path is the reason the
    two steps are two.

    He is by definition inside the 24-hour window — he just wrote to ask for the
    change — so free-form text works and no template is needed. Never raises;
    False means he was NOT told, and the caller must not leave a change pending
    on a code nobody can read.
    """
    from app import whatsapp

    if not telefono:
        return False
    try:
        whatsapp.enviar_mensaje(telefono, texto)
        return True
    except Exception as exc:
        print(f"[staff-notify] código de ajuste no enviado ({type(exc).__name__})")
        return False


def texto_falla_tecnica(
    telefono: str, texto: str, error: str, lengua: str | None = None
) -> tuple[str, str]:
    """(asunto, cuerpo) of the alert the team gets when a turn ended in an apology.

    In the team's language — the manager who switched to English reads it in
    English — and with the sender's words QUOTED, never pasted: what somebody
    wrote is data to read, not an instruction, not even after a person forwards
    it (app/solicitudes.py::citar, app/formato.py::sin_citas).
    """
    from app import idioma as idioma_mod
    from app.solicitudes import citar

    lengua = lengua or _lengua_equipo()
    cuerpo = idioma_mod.t(
        "gerencia.falla_cuerpo",
        lengua,
        telefono=telefono or idioma_mod.t("gerencia.sin_dato", lengua),
        mensaje=citar(texto, 300) or idioma_mod.t("gerencia.sin_dato", lengua),
        error=str(error)[:200],
    )
    return idioma_mod.t("gerencia.falla_asunto", lengua), cuerpo


def avisar_falla_tecnica(telefono: str, texto: str, error: str) -> bool:
    """Somebody got the technical-problem apology. That text says the team was
    told, so this makes it true."""
    asunto, cuerpo = texto_falla_tecnica(telefono, texto, error)
    return alertar_excepcion(
        asunto,
        cuerpo,
        urgencia=URGENCIA_ALTA,
        plantilla_env="WHATSAPP_STAFF_ALERT_TEMPLATE",
    )


def avisar_escalamiento(
    motivo: str, telefono: str, cliente: str, tarea: str = ""
) -> bool:
    """An ERPNext ToDo is invisible until someone opens the system; a complaint
    would wait until morning. This makes the phone ring instead."""
    from app import idioma as idioma_mod

    lengua = _lengua_equipo()
    cuerpo = idioma_mod.t(
        "gerencia.escalamiento_cuerpo",
        lengua,
        cliente=cliente or idioma_mod.t("gerencia.no_registrado", lengua),
        telefono=telefono or idioma_mod.t("gerencia.sin_dato", lengua),
        motivo=str(motivo)[:300],
    )
    if tarea:
        cuerpo += "\n" + idioma_mod.t("gerencia.escalamiento_tarea", lengua, tarea=tarea)
    return alertar_excepcion(
        idioma_mod.t("gerencia.escalamiento_asunto", lengua),
        cuerpo,
        urgencia=URGENCIA_ALTA,
        plantilla_env="WHATSAPP_STAFF_ALERT_TEMPLATE",
    )
