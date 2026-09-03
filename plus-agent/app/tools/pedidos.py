"""Customer write tools with deterministic identity and order guardrails."""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app import clientes, entrega, erpnext, outbound_status, policy
from app.locks import CoordinationError, distributed_lock
from app.notificar import notificar_confirmacion, notificar_equipo
from app.runtime_context import RuntimeContextError, actor_context

_MESES = {
    "ene": 1, "enero": 1, "feb": 2, "febrero": 2, "mar": 3, "marzo": 3,
    "abr": 4, "abril": 4, "may": 5, "mayo": 5, "jun": 6, "junio": 6,
    "jul": 7, "julio": 7, "ago": 8, "agosto": 8, "sep": 9, "sept": 9,
    "septiembre": 9, "set": 9, "setiembre": 9, "oct": 10, "octubre": 10,
    "nov": 11, "noviembre": 11, "dic": 12, "diciembre": 12,
}

_DIAS = {
    "lunes": 0,
    "martes": 1,
    "miercoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sabado": 5,
    "domingo": 6,
}
_ZONA_HORARIA_DEFAULT = "America/Argentina/Buenos_Aires"


class FechaEntregaInvalida(ValueError):
    """A supplied delivery date needs clarification from the customer."""


def _sin_tildes(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", text.lower())
        if unicodedata.category(char) != "Mn"
    ).strip()


def _hoy_del_negocio() -> date:
    zone_name = os.getenv("BUSINESS_TIMEZONE", _ZONA_HORARIA_DEFAULT).strip()
    try:
        return datetime.now(ZoneInfo(zone_name)).date()
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise erpnext.ERPNextError("BUSINESS_TIMEZONE inválida") from exc


def _validar_fecha(candidate: date, today: date) -> str:
    if candidate < today:
        raise FechaEntregaInvalida("la fecha de entrega ya pasó")
    return candidate.isoformat()


def _parse_fecha(text: str, *, hoy: date | None = None) -> str:
    """Parse supported customer date forms; absence/ambiguity never defaults."""
    today = hoy or _hoy_del_negocio()
    if text is None:
        raise FechaEntregaInvalida("falta la fecha de entrega")
    normalized = _sin_tildes(str(text))
    if not normalized:
        raise FechaEntregaInvalida("falta la fecha de entrega")

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        try:
            candidate = date.fromisoformat(normalized)
        except ValueError as exc:
            raise FechaEntregaInvalida("la fecha de entrega no existe") from exc
        return _validar_fecha(candidate, today)

    if normalized in ("hoy", "ahora"):
        return today.isoformat()
    if normalized in ("manana", "para manana"):
        return (today + timedelta(days=1)).isoformat()
    if normalized in ("pasado manana", "para pasado manana"):
        return (today + timedelta(days=2)).isoformat()

    for weekday, index in _DIAS.items():
        if weekday in normalized:
            delta = (index - today.weekday()) % 7
            return (today + timedelta(days=delta or 7)).isoformat()

    # "2 de septiembre", "el 2 de sep", "2 septiembre 2026" — como escribe la
    # gente por WhatsApp. Sin esto el parser solo aceptaba ISO y DD/MM.
    match_mes = re.search(
        r"(\d{1,2})\s*(?:de\s+)?("
        + "|".join(_MESES)
        + r")[a-z]*\.?(?:\s+(?:de\s+)?(\d{4}))?",
        normalized,
    )
    if match_mes:
        day = int(match_mes.group(1))
        month = _MESES[match_mes.group(2)]
        year_text = match_mes.group(3)
        if year_text:
            try:
                candidate = date(int(year_text), month, day)
            except ValueError as exc:
                raise FechaEntregaInvalida("la fecha de entrega no existe") from exc
            return _validar_fecha(candidate, today)
        for year in range(today.year, today.year + 9):
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if candidate >= today:
                return candidate.isoformat()
        raise FechaEntregaInvalida("la fecha de entrega no existe")

    match = re.fullmatch(
        r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", normalized
    )
    if match:
        day, month, year_text = (
            int(match.group(1)),
            int(match.group(2)),
            match.group(3),
        )
        if year_text:
            year = 2000 + int(year_text) if len(year_text) == 2 else int(year_text)
            try:
                candidate = date(year, month, day)
            except ValueError as exc:
                raise FechaEntregaInvalida("la fecha de entrega no existe") from exc
            return _validar_fecha(candidate, today)
        for year in range(today.year, today.year + 9):
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if candidate >= today:
                return candidate.isoformat()
        raise FechaEntregaInvalida("la fecha de entrega no existe")

    raise FechaEntregaInvalida("no pude interpretar la fecha de entrega")


def _unidad_clave(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "", _sin_tildes(value))
    aliases = {
        "u": "unidad",
        "un": "unidad",
        "unidad": "unidad",
        "unidades": "unidad",
        "kg": "kg",
        "kgs": "kg",
        "kilo": "kg",
        "kilos": "kg",
        "kilogramo": "kg",
        "kilogramos": "kg",
        "l": "litro",
        "lt": "litro",
        "lts": "litro",
        "litro": "litro",
        "litros": "litro",
    }
    return aliases.get(normalized, normalized)


class DireccionEntrega(BaseModel):
    """La dirección donde hay que entregar, como la dijo el cliente.

    El teléfono NO está acá y no está en ninguna herramienta: viene del
    webhook firmado de Meta. Si el modelo pudiera pasarlo, un mensaje
    alcanzaría para dar de alta a otra persona o para pedir en su nombre.
    """

    calle: str = Field(
        min_length=1, description="Calle y número, tal como lo dijo el cliente"
    )
    localidad: str = Field(min_length=1, description="Ciudad, pueblo o localidad")
    codigo_postal: str = Field(
        default="", description="Código postal si lo dijo; vacío si no lo dijo"
    )
    referencia: str = Field(
        default="", description="Piso, departamento, entre qué calles; opcional"
    )

    def como_erpnext(self) -> dict:
        return {
            "address_line1": self.calle.strip(),
            "address_line2": self.referencia.strip(),
            "city": self.localidad.strip(),
            "pincode": self.codigo_postal.strip(),
        }


def _cuenta_del_remitente(config: RunnableConfig) -> tuple[object, str]:
    """(actor, cliente) de quien escribió, siempre por su teléfono verificado.

    El webhook resuelve la cuenta al empezar el turno. Un cliente que se acaba
    de dar de alta —en este mismo turno, con crear_cliente— todavía no la tiene
    ahí, así que se vuelve a resolver por teléfono. El número es siempre el del
    mensaje que firmó Meta: nunca uno que dijo el modelo.
    """
    actor = actor_context(config)
    if actor.scope != "customer" or not actor.actor_phone:
        raise RuntimeContextError("cliente autenticado ausente")
    if actor.customer_code:
        return actor, actor.customer_code
    try:
        ficha = clientes.buscar_por_telefono(
            actor.actor_phone, get_list=erpnext.get_list
        )
    except erpnext.ERPNextError as exc:
        # Un ERPNext caído no es "no tiene cuenta". Falla cerrada, y como
        # RuntimeContextError para que la herramienta devuelva texto en vez de
        # levantar: una excepción rompe el hilo de conversación del cliente.
        raise RuntimeContextError("no pude resolver la cuenta del remitente") from exc
    if not ficha:
        raise RuntimeContextError("el remitente todavía no tiene cuenta")
    return actor, str(ficha["name"])


class LineaPedido(BaseModel):
    item_code: str = Field(
        min_length=1, description="Código exacto devuelto por buscar_producto"
    )
    cantidad: float = Field(gt=0, description="Cantidad solicitada por el cliente")
    unidad: str = Field(
        min_length=1,
        description="Unidad que dijo el cliente; debe coincidir con la del catálogo",
    )


def _message_key(message_id: str) -> str:
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:40]
    return f"WA-{digest}"


def _log_ref(value: str) -> str:
    """Non-reversible correlation tag for logs; never log ERP/customer IDs."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _find_existing(customer: str, message_key: str) -> dict | None:
    rows = erpnext.get_list(
        "Sales Order",
        filters=[["po_no", "=", message_key], ["customer", "=", customer]],
        fields=["name", "customer", "docstatus", "delivery_date"],
        limit=2,
    )
    if not rows:
        return None
    try:
        return erpnext.get_doc("Sales Order", rows[0]["name"])
    except erpnext.ERPNextError:
        return rows[0]


def _validated_lines(lines: list[LineaPedido]) -> tuple[list[dict], str | None]:
    combined: dict[tuple[str, str], float] = {}
    canonical_uom: dict[tuple[str, str], str] = {}
    for line in lines:
        code = line.item_code.strip()
        try:
            item = erpnext.get_doc("Item", code)
        except erpnext.ERPNextError:
            return [], f"No pude validar el producto {code}."
        if int(item.get("disabled") or 0):
            return [], f"El producto {code} no está habilitado."
        stock_uom = str(item.get("stock_uom") or "").strip()
        if not stock_uom:
            return [], f"El producto {code} no tiene una unidad válida en el catálogo."
        if _unidad_clave(line.unidad) != _unidad_clave(stock_uom):
            return [], (
                f"No creé el pedido: {code} se vende por {stock_uom}, no por "
                f"{line.unidad}. Confirmá la cantidad en {stock_uom}; no conviertas "
                "la unidad automáticamente."
            )
        key = (code, _unidad_clave(stock_uom))
        combined[key] = combined.get(key, 0) + float(line.cantidad)
        canonical_uom[key] = stock_uom
    validated = [
        {"item_code": code, "qty": qty, "uom": canonical_uom[(code, uom_key)]}
        for (code, uom_key), qty in combined.items()
    ]
    return validated, None


def _summary(order: dict, fallback: list[dict]) -> str:
    items = order.get("items") if isinstance(order.get("items"), list) else fallback
    parts = []
    for item in items or []:
        try:
            qty = float(item.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        parts.append(
            f"{qty:g} {item.get('uom') or item.get('stock_uom') or 'unidad'} "
            f"de {item.get('item_code') or 'producto'}"
        )
    return ", ".join(parts) or "detalle disponible en ERPNext"


def _order_result(order: dict, fallback: list[dict], fallback_date: str) -> str:
    name = str(order.get("name") or "").strip()
    if not name:
        return (
            "PEDIDO_NO_CREADO. No hay un número de pedido verificable; "
            "derivá el caso al equipo."
        )
    status = int(order.get("docstatus") or 0)
    delivery = order.get("delivery_date") or fallback_date
    detail = _summary(order, fallback)
    if status == 1:
        return (
            f"PEDIDO_CONFIRMADO. Número real: {name}. Resumen: {detail}. "
            f"Entrega: {delivery}. Estado: confirmado."
        )
    if status == 2:
        return (
            f"PEDIDO_CANCELADO. Número real: {name}. Resumen: {detail}. "
            f"Entrega: {delivery}. Estado: cancelado; no crees otro pedido "
            "sin una nueva solicitud del cliente."
        )
    return (
        f"PEDIDO_PENDIENTE. Número real: {name}. Resumen: {detail}. "
        f"Entrega: {delivery}. Estado: borrador pendiente de revisión."
    )


def _safe_notify(name: str, order: dict, *, auto: bool, reasons: str = "") -> bool:
    try:
        return bool(notificar_equipo(name, order, auto=auto, motivos=reasons))
    except Exception as exc:
        print(f"[orders] notificación al equipo falló ({type(exc).__name__})")
        return False


def _notificar_confirmada(order: dict) -> None:
    """Stage 2e: the manager hears about every confirmed order exactly once.

    The customer reads PEDIDO_CONFIRMADO in this same turn, so the order is
    marked as already communicated: no later path sends a second confirmation.
    The marker also opens the manual cancellation window.
    """
    try:
        outbound_status.marcar_confirmacion(str(order.get("name") or ""), informado_en_chat=True)
    except Exception as exc:
        print(f"[orders] marca de confirmación falló ({type(exc).__name__})")
    try:
        notificar_confirmacion(order, "automática (política)")
    except Exception as exc:
        print(f"[orders] aviso de confirmación falló ({type(exc).__name__})")


def _after_create(order: dict, validated: list[dict], delivery: str) -> str:
    name = str(order.get("name") or "").strip()
    if not name:
        return _order_result({}, validated, delivery)

    try:
        complete = erpnext.get_doc("Sales Order", name)
    except erpnext.ERPNextError:
        complete = order

    try:
        decision = policy.evaluar(complete)
    except Exception as exc:
        print(
            f"[orders] política falló order={_log_ref(name)} "
            f"type={type(exc).__name__}"
        )
        decision = policy.Decision(False, ["no se pudo completar la política"])

    if decision.auto:
        try:
            with policy.auto_submit_lock():
                # Re-read and re-run every rule while holding the global lock.
                complete = erpnext.get_doc("Sales Order", name)
                final_decision = policy.evaluar(complete)
                if final_decision.auto:
                    submitted = erpnext.submit_doc("Sales Order", name)
                    complete = submitted or complete
                    erpnext.add_comment(
                        "Sales Order",
                        name,
                        "Auto-confirmado después de revalidación bajo lock distribuido.",
                    )
                    _notificar_confirmada(complete)
                    return _order_result(complete, validated, delivery)
                decision = final_decision
        except Exception as exc:
            print(
                f"[orders] auto-confirmación falló order={_log_ref(name)} "
                f"type={type(exc).__name__}"
            )
            # A timeout can be ambiguous: submission may have committed. Resolve
            # the actual ERP state before telling the customer anything.
            try:
                complete = erpnext.get_doc("Sales Order", name)
            except erpnext.ERPNextError:
                pass
            if int(complete.get("docstatus") or 0) == 1:
                _notificar_confirmada(complete)
                return _order_result(complete, validated, delivery)
            decision = policy.Decision(False, ["auto-confirmación no disponible"])

    erpnext.add_comment(
        "Sales Order", name, f"Requiere revisión humana: {decision}"
    )
    _safe_notify(name, complete, auto=False, reasons=str(decision))
    resultado = _order_result(complete, validated, delivery)
    if entrega.MOTIVO in str(decision):
        # El cliente tiene que escuchar "lo recibimos, estamos viendo la
        # entrega" — nunca "confirmado". Una paráfrasis alegre de un borrador
        # es exactamente cómo se pierde un cliente el día de la entrega, así
        # que la instrucción viaja con el resultado en vez de confiar en que el
        # modelo lo deduzca.
        resultado += (
            " ENTREGA EN REVISIÓN: decile al cliente que el pedido quedó "
            "RECIBIDO y que estamos revisando la entrega a esa dirección. "
            "NO le digas que está confirmado, y no prometas día ni hora."
        )
    return resultado


@tool
def crear_lead(
    nombre: str,
    config: RunnableConfig,
    nota: str = "",
) -> str:
    """Registra al remitente autenticado como contacto potencial."""
    try:
        actor = actor_context(config)
    except RuntimeContextError:
        return "No pude autenticar el remitente; no registré el contacto."
    if actor.scope != "customer" or not actor.actor_phone:
        return "No pude autenticar el remitente; no registré el contacto."
    if actor.customer_code:
        return "La cuenta ya está registrada; no creé otro contacto."
    try:
        existing = erpnext.get_list(
            "Lead",
            filters=[["mobile_no", "=", actor.actor_phone]],
            fields=["name"],
            limit=1,
        )
        if existing:
            return f"Contacto ya registrado como {existing[0]['name']}."
        message_ref = (
            _message_key(actor.inbound_message_id)
            if actor.inbound_message_id
            else "sin referencia"
        )
        # ERPNext v15+ stores Lead notes as a child table (CRM Note); a plain
        # string here makes the API return HTTP 500 and no Lead is created.
        detalle = " ".join(part for part in (nota.strip(), "") if part)
        doc = erpnext.create_doc(
            "Lead",
            {
                "lead_name": nombre,
                "mobile_no": actor.actor_phone,
                "notes": [
                    {
                        "note": (
                            f"Origen: WhatsApp. {detalle}".strip()
                            + f"<br>Referencia segura: {message_ref}"
                        )
                    }
                ],
            },
        )
    except erpnext.ERPNextError:
        return "No pude registrar el contacto. Derivá el caso al equipo."
    erpnext.add_comment("Lead", doc["name"], "Creado por Agente IA vía WhatsApp.")
    return f"Contacto registrado como {doc['name']}."


@tool
def crear_pedido(
    lineas: list[LineaPedido],
    fecha_entrega: str,
    config: RunnableConfig,
) -> str:
    """Crea un pedido para el cliente autenticado.

    Cada línea requiere código, cantidad y la unidad textual confirmada por el
    cliente. La fecha de entrega también es obligatoria. Nunca conviertas una
    unidad ni inventes una fecha.
    """
    try:
        actor, cuenta = _cuenta_del_remitente(config)
    except RuntimeContextError:
        return (
            "PEDIDO_NO_CREADO. No hay una cuenta de cliente autenticada; "
            "si es un cliente nuevo usá crear_cliente primero."
        )
    if not actor.inbound_message_id:
        return (
            "PEDIDO_NO_CREADO. Falta la referencia segura del mensaje; "
            "derivá el caso al equipo y no reintentes automáticamente."
        )
    if not lineas:
        return "PEDIDO_NO_CREADO. El pedido no puede estar vacío."

    message_key = _message_key(actor.inbound_message_id)
    try:
        existing = _find_existing(cuenta, message_key)
    except erpnext.ERPNextError:
        return (
            "PEDIDO_NO_CREADO. No pude verificar si este mensaje ya tenía un "
            "pedido; no reintentes automáticamente y derivá el caso al equipo."
        )
    if existing:
        return _order_result(existing, [], "")

    try:
        business_today = _hoy_del_negocio()
        delivery = _parse_fecha(fecha_entrega, hoy=business_today)
    except FechaEntregaInvalida as exc:
        return (
            f"PEDIDO_NO_CREADO. {exc}. Pedile al cliente una fecha válida y explícita."
        )
    except erpnext.ERPNextError:
        return (
            "PEDIDO_NO_CREADO. No pude validar la fecha del negocio; "
            "derivá el caso al equipo."
        )

    try:
        with distributed_lock(
            f"order-message:{message_key}", lease_seconds=300, wait_seconds=30
        ):
            existing = _find_existing(cuenta, message_key)
            if existing:
                return _order_result(existing, [], delivery)

            validated, validation_error = _validated_lines(lineas)
            if validation_error:
                return f"PEDIDO_NO_CREADO. {validation_error}"

            try:
                company, warehouse = erpnext.default_context()
                payload: dict = {
                    "customer": cuenta,
                    "company": company,
                    "po_no": message_key,
                    "transaction_date": business_today.isoformat(),
                    "delivery_date": delivery,
                    "order_type": "Sales",
                    "items": [
                        {
                            **line,
                            "delivery_date": delivery,
                            "warehouse": warehouse,
                            "conversion_factor": 1,
                        }
                        for line in validated
                    ],
                }
                # El pedido dice a dónde va. Si ERPNext lo dedujera solo,
                # dos pedidos del mismo cliente podrían salir con direcciones
                # distintas; y sin dirección la política no puede verificar la
                # entrega, así que el pedido queda en borrador (bien). Si el
                # cliente acaba de dar una dirección en esta conversación, va a
                # ESA: es la que la política tiene que mirar.
                envio = clientes.direccion_para_pedido(cuenta, actor.actor_phone)
                if envio:
                    payload["customer_address"] = envio
                    payload["shipping_address_name"] = envio
                if policy.PRICE_LIST:
                    payload["selling_price_list"] = policy.PRICE_LIST
                if policy.CURRENCY:
                    payload["currency"] = policy.CURRENCY
                order = erpnext.create_doc("Sales Order", payload)
            except erpnext.ERPNextError:
                # A transport timeout may have happened after commit. Resolve by
                # the persisted business key before claiming creation failed.
                try:
                    existing = _find_existing(cuenta, message_key)
                except erpnext.ERPNextError:
                    existing = None
                if existing:
                    return _order_result(existing, validated, delivery)
                return (
                    "PEDIDO_NO_CREADO. ERPNext no confirmó la creación y no hay "
                    "un número verificable; derivá el caso al equipo."
                )

            name = str(order.get("name") or "").strip()
            if not name:
                return (
                    "PEDIDO_NO_CREADO. ERPNext no devolvió un número verificable; "
                    "derivá el caso al equipo."
                )
            erpnext.add_comment(
                "Sales Order",
                name,
                f"Borrador creado por Agente IA. Referencia idempotente: {message_key}.",
            )
    except CoordinationError:
        # Never create without cross-worker idempotency. A concurrent worker may
        # already have completed, so make one final read-only resolution.
        try:
            existing = _find_existing(cuenta, message_key)
        except erpnext.ERPNextError:
            existing = None
        if existing:
            return _order_result(existing, [], delivery)
        return (
            "PEDIDO_NO_CREADO. No pude coordinar una creación segura; "
            "derivá el caso al equipo y no reintentes automáticamente."
        )
    # The idempotency critical section ends once the durable keyed draft exists.
    # Policy evaluation can be slow and has its own global submit lock.
    return _after_create(order, validated, delivery)


@tool
def crear_cliente(
    nombre: str,
    direccion: DireccionEntrega,
    config: RunnableConfig,
) -> str:
    """Registra al remitente como cliente, con su dirección de entrega.

    Usala cuando escribe alguien que no tiene cuenta y quiere pedir. Pedile
    ANTES el nombre (o el del negocio) y la dirección completa: calle y
    número, localidad y código postal si lo sabe. No inventes ninguno de esos
    datos y no preguntes el teléfono: ya lo tenemos verificado del mensaje.

    Después de esto podés usar crear_pedido en la misma conversación.
    """
    try:
        actor = actor_context(config)
    except RuntimeContextError:
        return "No pude autenticar el remitente; no registré la cuenta."
    if actor.scope != "customer" or not actor.actor_phone:
        return "No pude autenticar el remitente; no registré la cuenta."

    try:
        resultado = clientes.crear(nombre, actor.actor_phone, direccion.como_erpnext())
    except CoordinationError:
        return (
            "No pude coordinar el alta de forma segura; no reintentes ahora y "
            "derivá el caso al equipo."
        )
    except erpnext.ERPNextError as exc:
        print(f"[orders] alta de cliente falló: {exc}")
        return (
            "No pude registrar la cuenta. Derivá el caso al equipo y no "
            "prometas nada."
        )

    cuenta = resultado["cliente"]
    ya_estaba = "" if resultado["creado"] else " (ya tenía cuenta)"
    try:
        doc = erpnext.get_doc("Address", resultado["direccion"])
        en_zona, motivo_zona = entrega.en_zona(doc)
    except erpnext.ERPNextError:
        en_zona, motivo_zona = False, "no pude verificar la zona de reparto"

    if en_zona:
        return (
            f"Cuenta lista: {cuenta}{ya_estaba}. Entregamos en esa zona. "
            "Ya podés tomarle el pedido con crear_pedido."
        )
    return (
        f"Cuenta lista: {cuenta}{ya_estaba}. ATENCIÓN: {motivo_zona}. Podés "
        "tomarle el pedido igual, pero va a quedar RECIBIDO y pendiente de "
        "revisión de entrega: no le prometas la entrega ni le digas que está "
        "confirmado."
    )


@tool
def escalar_a_humano(motivo: str, config: RunnableConfig) -> str:
    """Deriva la conversación autenticada a una persona del equipo."""
    try:
        actor = actor_context(config)
    except RuntimeContextError:
        return "No pude autenticar la conversación para derivarla."
    reference = (
        _message_key(actor.inbound_message_id)
        if actor.inbound_message_id
        else "sin referencia de mensaje"
    )
    account = actor.customer_code or "cuenta no registrada"
    try:
        doc = erpnext.create_doc(
            "ToDo",
            {
                "description": (
                    f"[WhatsApp] Escalado por Agente IA: {motivo}. "
                    f"Cuenta: {account}. Referencia: {reference}."
                ),
                "priority": "High",
            },
        )
    except erpnext.ERPNextError:
        return "No pude crear la tarea de derivación; avisá que el equipo revisará el caso."
    return f"Derivado al equipo (tarea {doc['name']})."
