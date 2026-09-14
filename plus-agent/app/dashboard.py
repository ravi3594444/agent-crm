"""Read-only dashboard API. No agent tools or business write operations.

An independent ASGI surface keeps its bearer auth and CORS away from the signed
WhatsApp webhook. The UI is public static content; business data never is.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from html import unescape
from urllib.parse import quote, unquote, urlsplit

LIMIT = 250
READ_TIMEOUT = 3.0

# EL PANEL TIENE SUS PROPIOS HILOS, y no es una optimización.
#
# `asyncio.to_thread` usa el executor por DEFECTO, que es el mismo que usa
# `main._run_sync` para meter el webhook de WhatsApp en la cola. Ese pool son
# `min(32, cpu+4)` hilos —seis en la VM de 2 vCPU— y `erpnext._client` espera
# hasta 20 s. Seis lecturas lentas del panel ocupaban los seis hilos y el
# webhook se quedaba esperando: Meta recibe 503 y reintenta el mensaje del
# cliente. O sea, alguien mirando el panel podía frenar las ventas.
#
# Con un pool propio y acotado, la peor lectura del panel sólo hace esperar a
# otra lectura del panel.
_HILOS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dashboard")
ORDER_FIELDS = [
    "name", "customer", "customer_name", "transaction_date", "delivery_date",
    "grand_total", "currency", "status", "docstatus",
]


class RecordNotFound(Exception):
    """A document outside the configured company is not a dashboard record."""


def public_erp_origin() -> str:
    """Only an explicitly configured browser origin, never the internal API URL."""
    value = os.getenv("ERPNEXT_PUBLIC_URL", "").strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not parsed.netloc or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment
            or not (parsed.scheme == "https" or (parsed.scheme == "http" and local))
        ):
            return ""
    except ValueError:
        return ""
    return value


def plain_text(value: object) -> str:
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", str(value or ""))).split())[:2000]


def model_status() -> list[dict]:
    """Reuse the repo's provider selection, aliases, and defaults without a model call."""
    from app import modelos

    try:
        provider = modelos.proveedor()
        configured = bool(modelos.clave_api(provider)[1])
        names = [modelos.nombre_modelo(role, prov=provider)[1] for role in ("clientes", "gerencia")]
        status = "Configured" if configured else "Missing credentials"
    except Exception:
        names, status = ["Unavailable", "Unavailable"], "Configuration error"
    return [
        {"id": "sales", "name": "Sales agent", "role": "Customer conversations & order drafts",
         "model": names[0], "status": status},
        {"id": "manager", "name": "Management agent", "role": "Business reports & manager assistance",
         "model": names[1], "status": status},
    ]


def controls() -> dict:
    """Canonical settings reader preserves the repo's lost-state guards.

    This existing reader may use policy-scoped READS to verify durable Company
    audit markers. It does not propose, apply, or bypass an owner setting.
    """
    from app import inventario, limites

    labels = {
        "AUTO_CONFIRM_MAX": "Order ceiling",
        "AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO": "Quantity per product",
        "STOCK_BUFFER_PCT": "Stock buffer",
        "AUTO_CONFIRM_MAX_CLIENTE_NUEVO": "New customer ceiling",
    }
    rows = limites.resumen()
    policies = [{
        "id": row["nombre"], "name": labels.get(row["nombre"], row["alias"]),
        "value": limites.mostrar(row["nombre"], row["valor"], en_idioma="en")
        if not row["problema"] else "Unavailable",
        "note": row["problema"] or row["significado"],
        "source": row["origen"], "unit": row["unidad"], "valid": not bool(row["problema"]),
    } for row in rows]
    policies.append({
        "id": "STOCK_CONFIABLE_HORAS", "name": "Stock trust window",
        "value": f"{inventario.horas_de_validez():g} hours",
        "note": "A submitted stock count must be recent enough before auto-confirmation.",
        "source": "Environment", "valid": True,
    })
    return {"policies": policies}


def operations() -> dict:
    from app import avisos, outbound_status

    client = outbound_status.cliente()
    client.ping()
    notice_count = avisos.pendientes()
    return {
        "redis": "Connected",
        # The worker takes a short lease while handling a turn; idle is not failure.
        "worker": "Active lease" if client.exists("wa:{inbound}:worker-lock") else "No active lease",
        "queuedMessages": int(client.llen("wa:{inbound}:queue")),
        "failedReplies": int(client.llen("wa:{inbound}:dead")),
        "failedNotices": int(client.llen(outbound_status.DEAD_NOTIFY_KEY)),
        "queuedNotices": notice_count if notice_count >= 0 else None,
        "providerHealth": "Not probed",
    }


def order_detail(order_id: str) -> dict:
    from app import erpnext

    with erpnext.manager_scope():
        try:
            doc = erpnext.get_doc("Sales Order", order_id, timeout=READ_TIMEOUT)
        except erpnext.ERPNextError as exc:
            if exc.status_code in {403, 404}:
                raise RecordNotFound from exc
            raise
        if doc.get("company") != erpnext.default_company():
            raise RecordNotFound
        result = order_row(doc)
        result["items"] = [{
            "code": str(item.get("item_code") or ""),
            "name": str(item.get("item_name") or item.get("item_code") or "Item"),
            "qty": number(item.get("qty")), "rate": number(item.get("rate")),
            "amount": number(item.get("amount")), "unit": str(item.get("uom") or ""),
        } for item in doc.get("items", [])]
        result["address"] = plain_text(doc.get("shipping_address") or doc.get("address_display"))
        result["erpUrl"] = (
            public_erp_origin() + "/app/sales-order/" + quote(order_id, safe="")
            if public_erp_origin() else ""
        )
        return result


# ---------------------------------------------------------------- conversaciones

# Cuántos mensajes devuelve una conversación: los más NUEVOS, que es lo que
# alguien abre a mirar. Un hilo de un año son miles y ninguno de los de arriba
# se lee.
CONVERSACION_MAX = 60


def _hilo_del_telefono(telefono: str) -> str:
    """El id de hilo del webhook, importado y NO reescrito.

    `app/main.py` arma `wa:{sha256(teléfono)}` y este módulo tiene que buscar
    exactamente ese. Copiar el sha acá serían dos definiciones de la misma
    cosa que nadie obliga a coincidir: el día que una cambie, las
    conversaciones dejan de encontrarse y no falla nada —devuelve vacío—, que
    es la peor forma de romperse.
    """
    from app.main import _thread_tag

    return f"cli:{_thread_tag(telefono)}"


def _visible(rol: str, texto: str, cuando: str | None) -> dict:
    fila = {"role": rol, "text": texto}
    if cuando is not None:
        fila["at"] = cuando
    return fila


def _mensajes_visibles(mensajes: list, sellos: list[str] | None) -> list[dict]:
    """Lo que una PERSONA puede ver de un hilo del agente.

    Tres reglas, y cada una tapa algo que se vería sin ella:

    * un `ToolMessage` lleva la salida cruda de una herramienta —`stock de
      leche: 12`—, así que se convierte en una línea neutra y su CONTENIDO NO
      SALE. El dueño tiene que saber que el agente hizo algo entre dos
      mensajes; no necesita ver con qué.
    * un `AIMessage` sin texto es el turno en que el modelo llamó a la
      herramienta. Mostrarlo son globos vacíos en la pantalla.
    * cualquier otro tipo (un `SystemMessage` que se colara) no se muestra. La
      lista blanca es la que decide, no una lista negra: un tipo nuevo de
      LangChain aparece como invisible, no como una fuga.
    """
    visibles: list[dict] = []
    for indice, m in enumerate(mensajes):
        tipo = type(m).__name__
        texto = str(getattr(m, "content", "") or "").strip()
        if sellos is None:
            cuando = None  # el llamador no pidió horas y no se inventa ninguna
        else:
            cuando = sellos[indice] if indice < len(sellos) else ""
            if not cuando:
                continue  # sin hora propia no se muestra: ver `_leer_hilo`
        if tipo == "HumanMessage" and texto:
            visibles.append(_visible("customer", texto, cuando))
        elif tipo == "AIMessage" and texto:
            visibles.append(_visible("agent", texto, cuando))
        elif tipo == "ToolMessage":
            if visibles and visibles[-1]["role"] == "note":
                continue  # dos consultas seguidas son UNA línea, no dos
            visibles.append(
                _visible("note", "The agent looked something up.", cuando)
            )
    return visibles


def conversation(customer_id: str) -> dict:
    """El hilo de WhatsApp de UN cliente de esta empresa.

    LA DIRECCIÓN IMPORTA Y ES LA ÚNICA QUE HAY. El hilo se guarda bajo
    `sha256(teléfono)`, que no se puede invertir, así que no existe «listar las
    conversaciones»: se llega cliente -> `mobile_no` -> hash -> hilo. Eso hace
    que toda transcripción se alcance a través de un cliente que este token ya
    tiene derecho a ver, y el derecho lo decide lo mismo que en el resto del
    panel: que el cliente le haya comprado a ESTA empresa.

    `Customer` es un maestro GLOBAL en ERPNext —no tiene campo `company`—, así
    que la pertenencia se comprueba contra sus pedidos, igual que en
    `snapshot`. Sin eso, un token de este panel leería las conversaciones de
    los clientes de otra empresa del mismo ERPNext.

    El teléfono NO sale en la respuesta. Se usa para encontrar el hilo y se
    descarta.
    """
    from app import erpnext
    from app import telefono as telefonos

    with erpnext.manager_scope():
        try:
            doc = erpnext.get_doc("Customer", customer_id, timeout=READ_TIMEOUT)
        except erpnext.ERPNextError as exc:
            if exc.status_code in {403, 404}:
                raise RecordNotFound from exc
            raise
        suyos = erpnext.get_list(
            "Sales Order",
            filters=[["company", "=", erpnext.default_company()],
                     ["customer", "=", customer_id]],
            fields=["name"], limit=1, timeout=READ_TIMEOUT,
        )
        if not suyos:
            raise RecordNotFound
        nombre = str(doc.get("customer_name") or customer_id)
        numero = telefonos.normalizar(doc.get("mobile_no"))

    base = {
        "customerId": customer_id,
        "customerName": nombre,
        "reachable": bool(numero),
        "messages": [],
        "truncated": False,
        "retentionDays": _dias_de_retencion(),
        "lastAt": "",
    }
    if not numero:
        # NO es un error: un cliente cargado a mano puede no tener WhatsApp.
        return base

    hilo = _leer_hilo(_hilo_del_telefono(numero))
    if hilo is None:
        return base
    mensajes, sellos, cortado = hilo
    visibles = _mensajes_visibles(mensajes, sellos)
    base["lastAt"] = visibles[-1]["at"] if visibles else ""
    base["truncated"] = cortado or len(visibles) > CONVERSACION_MAX
    base["messages"] = visibles[-CONVERSACION_MAX:]
    return base


def _dia_local(sello: str) -> str:
    """El día DEL NEGOCIO de un sello ISO, o "" si no se entiende.

    El checkpoint se sella en UTC y el día del panel sale de `reloj.ahora()`,
    que es la zona del negocio (UTC-3). Comparar los textos hacía desaparecer
    de «Hoy» toda conversación de después de las 21:00 —ya es mañana en UTC—,
    que en un distribuidor de lácteos es la franja en que el almacén cierra la
    caja y encarga para el día siguiente. O sea, justo las que importan.
    """
    from app import reloj

    try:
        return (
            datetime.fromisoformat(sello).astimezone(reloj.zona()).date().isoformat()
        )
    except (TypeError, ValueError):
        return ""


def _dias_de_retencion() -> int:
    """Lo que dice el .env, para no prometer un archivo que no existe."""
    try:
        return max(1, int(os.getenv("CONVERSATION_TTL_DAYS", "30")))
    except ValueError:
        return 30


# Cuántos checkpoints se recorren para fechar los mensajes. Cada turno escribe
# unos pocos, así que esto cubre holgadamente los `CONVERSACION_MAX` últimos
# mensajes; lo que quede antes se reporta como truncado en vez de inventarle
# una hora.
HISTORIA_MAX = 400


def _ultimo_del_hilo(thread_id: str) -> tuple[list, str] | None:
    """Los mensajes y CUÁNDO se movió por última vez, en UNA lectura.

    `_leer_hilo` recorre el historial entero para poder fechar cada mensaje, y
    eso cuesta ~17x más: medido contra el Redis de desarrollo, 2,3 ms contra
    37 ms por hilo. `today` mira hasta 120 clientes, así que ahí la diferencia
    es entre un cuarto de segundo y cuatro segundos y medio — en la pantalla
    que el dueño abre primero todas las mañanas, y sobre un Redis casi vacío.

    Esta lectura NO sabe cuándo llegó cada mensaje y por eso no lo dice: los
    mensajes salen sin `at`. Lo único que fecha es el HILO, con el sello del
    último checkpoint, que es exactamente «cuándo se movió esta conversación».
    """
    try:
        from app.graph import checkpointer

        tupla = checkpointer().get_tuple({"configurable": {"thread_id": thread_id}})
    except Exception as exc:
        print(f"[dashboard] no pude leer un hilo ({type(exc).__name__})")
        return None
    if tupla is None:
        return None
    valores = tupla.checkpoint.get("channel_values") or {}
    return list(valores.get("messages") or []), str(tupla.checkpoint.get("ts") or "")


def _leer_hilo(thread_id: str) -> tuple[list, list[str], bool] | None:
    """Los mensajes de un hilo, CUÁNDO llegó cada uno, y si se cortó atrás.

    DE DÓNDE SALE LA HORA. Un mensaje de LangChain no trae timestamp, así que
    la primera versión de esto no devolvía ninguno. Pero el historial sí: cada
    checkpoint tiene su `ts` y su lista de mensajes, y la lista crece. El
    mensaje que aparece por primera vez en el checkpoint N pasó en el `ts` de
    N —el momento en que el turno se persistió, que está a milisegundos de
    cuando se manejó—. Es un dato real, no una estimación, y por eso se puede
    mostrar.

    Un mensaje que no se pueda fechar NO se devuelve: se corta la punta vieja y
    se dice `truncado`. Rellenar con la hora del vecino sería inventar la única
    cosa que esta función aporta.

    None es «no hay hilo» y también «no lo pude leer», y acá las dos cosas se
    pueden aplastar a propósito: la pantalla dice lo mismo en los dos casos
    —no hay nada que mostrar— y distinguirlas le contaría al que mira algo
    sobre la infraestructura que no le sirve.
    """
    try:
        from app.graph import checkpointer

        # UNO DE MÁS, y es lo único que distingue «éste es el principio del
        # hilo» de «acá se cortó la página». Sin esa distinción, el checkpoint
        # más viejo que vuelve parece el primero, y los mensajes que ya traía
        # acumulados se fechan con SU sello — que es justo la hora inventada
        # que esta función existe para no dar.
        historia = list(
            checkpointer().list(
                {"configurable": {"thread_id": thread_id}}, limit=HISTORIA_MAX + 1
            )
        )
    except Exception as exc:
        print(f"[dashboard] no pude leer un hilo ({type(exc).__name__})")
        return None
    if not historia:
        return None
    cortado = len(historia) > HISTORIA_MAX
    historia = historia[:HISTORIA_MAX]

    # `list` devuelve del más NUEVO al más viejo; fechar necesita el orden
    # inverso, porque lo que fecha un mensaje es la primera vez que aparece.
    historia.reverse()
    sellos: list[str] = []
    mensajes: list = []
    for indice, tupla in enumerate(historia):
        actuales = list((tupla.checkpoint.get("channel_values") or {}).get("messages") or [])
        if len(actuales) < len(mensajes):
            continue
        sello = str(tupla.checkpoint.get("ts") or "")
        # Los que YA estaban en el checkpoint más viejo retenido sólo se pueden
        # fechar si ese checkpoint es de verdad el principio del hilo.
        nuevos = len(actuales) - len(mensajes)
        desconocido = indice == 0 and cortado
        sellos.extend(["" if desconocido else sello] * nuevos)
        mensajes = actuales
    # Un sello vacío no se muestra: `_mensajes_visibles` lo saltea. La punta
    # vieja se pierde y `truncado` lo dice, que es la mitad que faltaba.
    return mensajes, sellos, cortado


# ----------------------------------------------------------------------- hoy

# Cuántos clientes se miran para armar «hoy». Acotado porque cada uno es una
# lectura de Redis: sin tope, un catálogo de clientes grande vuelve la pantalla
# de apertura la más cara del panel.
HOY_MAX_CLIENTES = 120


def today() -> dict:
    """Con quién habló el agente hoy, y quién de esos NO compró.

    La fila que vale es la de `orderId: null`: alguien que escribió y no
    terminó comprando. Es lo único de esta pantalla que el dueño no puede
    sacar de ninguna otra —los pedidos ya los ve— y es una venta que se
    estaba perdiendo sin que quedara registro en ninguna parte.

    De dónde sale cada mitad, porque no es simétrico: los pedidos salen de
    ERPNext y las conversaciones de Redis. Un cliente sólo entra si le compró
    algo a esta empresa alguna vez —es la misma comprobación de pertenencia
    que `conversation`, y por el mismo motivo—, así que «habló hoy» acá
    significa «un cliente conocido habló hoy». Alguien que escribió por primera
    vez y todavía no tiene pedido no aparece; aparece en cuanto lo tenga.
    """
    from app import erpnext, reloj
    from app import telefono as telefonos

    ahora = reloj.ahora()
    hoy = ahora.date().isoformat()
    truncated: list[str] = []
    errors: list[str] = []

    with erpnext.manager_scope():
        empresa = erpnext.default_company()
        # SIN LOS PEDIDOS NO HAY PANTALLA, y devolver la mitad es peor que no
        # devolver nada: cada fila lleva un `orderId`, y con la lista vacía
        # TODAS dirían «habló y no compró». Esa fila es justo la que el dueño
        # va a mirar —una venta perdida— así que inventarla por una lectura
        # que falló es la peor forma de equivocarse acá. Se levanta y la ruta
        # contesta 502; la UI ya tiene reintento para eso.
        pedidos = erpnext.get_list(
            "Sales Order",
            filters=[["company", "=", empresa], ["transaction_date", "=", hoy]],
            # `creation` además de los de siempre: es la hora que lleva la fila
            # de un cliente nuevo que todavía no tiene conversación.
            fields=[*ORDER_FIELDS, "creation"], limit=LIMIT,
            order_by="creation desc, name desc", timeout=READ_TIMEOUT,
        )
        # Los candidatos a «habló hoy» son los clientes de esta empresa, y se
        # los busca por pedidos recientes: es el mismo recorte que `snapshot`.
        try:
            recientes = erpnext.get_list(
                "Sales Order",
                filters=[["company", "=", empresa]],
                fields=["customer"], limit=LIMIT,
                order_by="transaction_date desc, name desc", timeout=READ_TIMEOUT,
            )
        except Exception:
            errors.append("customers")
            recientes = []
        # SE DEDUPLICA CONSERVANDO EL ORDEN, que viene de ERPNext por fecha
        # descendente. Un `sorted(set(...))` lo cambiaba por orden alfabético
        # justo antes de cortar en 120: con más clientes que el tope, el que
        # compró hoy y se llama «Zunino» quedaba afuera y entraba uno que no
        # compra desde marzo y se llama «Almacén».
        cuentas: list[str] = []
        vistos: set[str] = set()
        for fila_r in recientes:
            cuenta_r = str(fila_r.get("customer") or "")
            if cuenta_r and cuenta_r not in vistos:
                vistos.add(cuenta_r)
                cuentas.append(cuenta_r)
        if len(cuentas) > HOY_MAX_CLIENTES:
            truncated.append("conversations")
            cuentas = cuentas[:HOY_MAX_CLIENTES]
        fichas = []
        if cuentas:
            try:
                fichas = erpnext.get_list(
                    "Customer", filters=[["name", "in", cuentas]],
                    fields=["name", "customer_name", "mobile_no"],
                    limit=len(cuentas), timeout=READ_TIMEOUT,
                )
            except Exception:
                errors.append("customers")

    pedido_de = {}
    for fila in pedidos:
        pedido_de.setdefault(str(fila.get("customer") or ""), str(fila.get("name") or ""))

    conversaciones = []
    for ficha in fichas:
        numero = telefonos.normalizar(ficha.get("mobile_no"))
        if not numero:
            continue
        hilo = _ultimo_del_hilo(_hilo_del_telefono(numero))
        if hilo is None:
            continue
        mensajes, sello = hilo
        if _dia_local(sello) != hoy:
            continue  # el hilo existe, pero no se movió hoy
        del_cliente = [
            m for m in _mensajes_visibles(mensajes, None) if m["role"] == "customer"
        ]
        if not del_cliente:
            continue
        cuenta = str(ficha.get("name") or "")
        conversaciones.append({
            "customerId": cuenta,
            "customerName": str(ficha.get("customer_name") or cuenta),
            "turns": len(del_cliente),
            # El sello del HILO, no el del último mensaje del cliente: esta
            # lectura no fecha mensajes. Es «cuándo se movió la conversación»,
            # que es lo que la pantalla ordena y muestra.
            "lastAt": sello,
            "lastLine": del_cliente[-1]["text"][:280],
            "orderId": pedido_de.get(cuenta) or None,
        })
    conversaciones.sort(key=lambda c: c["lastAt"], reverse=True)

    # QUIÉN ES NUEVO: el que compró hoy y no había comprado NUNCA antes. Se
    # pregunta al revés —quiénes SÍ tienen un pedido anterior— porque eso es
    # una sola consulta acotada, y el complemento es la respuesta. Preguntar
    # «¿es nuevo?» por cliente serían N llamadas para lo mismo.
    de_hoy = sorted({str(x.get("customer") or "") for x in pedidos} - {""})
    con_historia: set[str] = set()
    if de_hoy:
        with erpnext.manager_scope():
            try:
                anteriores = erpnext.get_list(
                    "Sales Order",
                    filters=[["company", "=", empresa], ["customer", "in", de_hoy],
                             ["transaction_date", "<", hoy]],
                    fields=["customer"], limit=LIMIT, timeout=READ_TIMEOUT,
                )
                con_historia = {str(x.get("customer") or "") for x in anteriores}
            except Exception:
                # Sin poder comprobarlo, NADIE es nuevo. Marcar de nuevo a un
                # cliente viejo es decirle al dueño que ganó una cuenta que ya
                # tenía; el error al revés sólo omite una fila.
                errors.append("new customers")
                con_historia = set(de_hoy)
    # SALE DE LOS PEDIDOS, no de las conversaciones. Filtrar `conversaciones`
    # exigía además teléfono, hilo legible y un mensaje del cliente hoy — así
    # que un cliente nuevo cuyo primer pedido se cargó a mano, o cuyo chat ya
    # expiró, no aparecía nunca. La pantalla dice «primer pedido hoy», y eso es
    # un hecho de ERPNext: la conversación, si está, es un adorno.
    por_cliente = {c["customerId"]: c for c in conversaciones}
    nuevos = []
    for fila_p in pedidos:
        cuenta = str(fila_p.get("customer") or "")
        if not cuenta or cuenta in con_historia or cuenta in {n["customerId"] for n in nuevos}:
            continue
        charla = por_cliente.get(cuenta)
        if charla is not None:
            nuevos.append(charla)
            continue
        # Sin conversación no se inventa ninguna: cero turnos, sin última
        # línea, y la hora es la del PEDIDO, que es el hecho que lo pone acá.
        cuando = _momento_iso(fila_p.get("creation"))
        if not cuando:
            continue  # sin hora legible no sale: no se inventa una
        nuevos.append({
            "customerId": cuenta,
            "customerName": str(fila_p.get("customer_name") or cuenta),
            "turns": 0,
            "lastAt": cuando,
            "lastLine": "",
            "orderId": str(fila_p.get("name") or ""),
        })

    return {
        "date": hoy,
        "generatedAt": ahora.isoformat(),
        "conversations": conversaciones,
        "newCustomers": nuevos,
        "orders": [order_row(x) for x in pedidos],
        "errors": errors,
        "truncated": truncated,
    }


# --------------------------------------------------------------------- la cola

COLA_MAX = 50


def queue() -> dict:
    """Lo que el agente VA a hacer, que hoy no se ve en ninguna parte.

    Después de que la agenda pasó a ser filas durables, el agente tiene trabajo
    futuro —avisarle a un cliente antes de la entrega, recordarle un plazo al
    dueño, cerrar un borrador que nadie decidió— y el dueño no tiene forma de
    saber qué le va a decir a sus clientes esta noche. Ésa es la pregunta que
    alguien hace antes de confiarle el teléfono a un agente.

    SALE DEL ÍNDICE DE REDIS, que es un caché y no la verdad. Está bien para
    una pantalla —lo peor que pasa después de un flush es que muestre de menos
    hasta que el barrido reconstruya— y está dicho acá para que nadie lo use
    como fuente para decidir nada.

    `que` se arma ACÁ y no en el navegador: los tipos de fila se agregan, y un
    `switch` del lado del cliente se queda viejo sin que nadie se entere.
    """
    from app import agenda, avisos, outbound_status

    cliente = outbound_status.cliente()
    ahora = agenda._ahora()
    proximas: list[dict] = []
    try:
        crudos = cliente.zrangebyscore(
            agenda.CLAVE_INDICE, f"{ahora:.3f}", "+inf"
        )[:COLA_MAX]
    except Exception:
        crudos = []
    for miembro in crudos:
        sobre, identificador = agenda._partir(miembro)
        fila = agenda.leer(sobre, identificador)
        if fila is None or not fila.con_plazo:
            continue
        proximas.append({
            "id": fila.id,
            "orderId": sobre,
            "type": fila.tipo,
            "dueAt": agenda._momento(fila.vence).isoformat(),
            "what": _que_va_a_hacer(fila),
        })

    # DE QUÉ CLIENTE ES CADA UNA. La fila de agenda sólo guarda el pedido, y
    # «SAL-ORD-2026-00042 a las 17:00» no le dice nada a una persona. Los
    # nombres se traen en UNA consulta para todos los pedidos nombrados, no uno
    # por fila.
    esperando: list[dict] = []
    from app import erpnext

    with erpnext.manager_scope():
        empresa = erpnext.default_company()
        # ESTA CONSULTA AUTORIZA, NO DECORA — y la diferencia es una fuga.
        #
        # El índice de agenda es UNO SOLO para todo el sitio y no sabe de
        # empresas. Usar esta lectura sólo para ponerle el nombre al cliente
        # dejaba pasar las filas de pedidos de OTRA empresa: se iban con su id
        # de pedido, su tipo de acción, su hora y su descripción, y lo único
        # que les faltaba era el nombre. Un pedido de otra empresa no es un
        # registro de este panel, así que si no está acá, no sale.
        #
        # Y si la consulta falla, no sale NINGUNA: sin poder comprobar la
        # pertenencia, mostrar es adivinar.
        nombres: dict[str, str] = {}
        pedidos_citados = sorted({p["orderId"] for p in proximas})
        if pedidos_citados:
            try:
                for fila_so in erpnext.get_list(
                    "Sales Order",
                    filters=[["company", "=", empresa], ["name", "in", pedidos_citados]],
                    fields=["name", "customer", "customer_name"],
                    limit=len(pedidos_citados), timeout=READ_TIMEOUT,
                ):
                    nombres[str(fila_so.get("name"))] = str(
                        fila_so.get("customer_name") or fila_so.get("customer") or ""
                    )
            except Exception:
                nombres = {}
        proximas = [p for p in proximas if p["orderId"] in nombres]
        # LO QUE ESPERA A UNA PERSONA: el dueño es el cuello de botella de esto
        # y hoy no lo ve en ninguna parte. Son los borradores todavía abiertos,
        # que es la misma consulta que `snapshot` ya hace para `pendingOrders`.
        try:
            abiertos = erpnext.get_list(
                "Sales Order",
                filters=[["company", "=", empresa], ["docstatus", "=", 0],
                         ["status", "not in", ["Closed", "Cancelled", "On Hold"]]],
                fields=["name", "customer", "customer_name", "creation"],
                limit=COLA_MAX, order_by="creation asc, name asc", timeout=READ_TIMEOUT,
            )
        except Exception:
            abiertos = []
        for fila_so in abiertos:
            desde = _momento_iso(fila_so.get("creation"))
            if not desde:
                continue  # sin fecha legible no se inventa una
            esperando.append({
                "orderId": str(fila_so.get("name") or ""),
                "customer": str(fila_so.get("customer_name") or fila_so.get("customer") or ""),
                "since": desde,
                "what": "Waiting for someone to confirm or reject it",
            })

    for p in proximas:
        p["customer"] = nombres[p["orderId"]]

    pendientes_avisos = avisos.pendientes()
    return {
        "generatedAt": agenda._momento(ahora).isoformat(),
        "upcoming": proximas,
        "waitingOnAPerson": esperando,
        "undelivered": {
            "replies": int(cliente.llen("wa:{inbound}:dead")),
            "notices": int(cliente.llen(outbound_status.DEAD_NOTIFY_KEY)),
        },
        "queuedNotices": pendientes_avisos if pendientes_avisos >= 0 else None,
        "source": "working index",
    }


def _momento_iso(valor: object) -> str:
    """El `creation` de ERPNext como ISO con zona, o "" si no se entiende.

    Frappe devuelve sellos SIN zona. `reloj.de_erpnext` es la única lectura de
    eso en el repo y por eso se usa ésta y no un `fromisoformat` suelto: dos
    formas de leer el mismo sello se desincronizan y la pantalla muestra horas
    corridas.
    """
    from app import reloj

    try:
        momento = reloj.de_erpnext(str(valor or ""))
    except Exception:
        return ""
    return momento.isoformat() if momento else ""


def _que_va_a_hacer(fila) -> str:
    """Una oración terminada por tipo de fila, en inglés, armada en el server."""
    from app import agenda

    return {
        agenda.CIERRE_BORRADOR: "Release the stock this draft is holding",
        agenda.AVISO_CIERRE: "Tell the customer their draft was closed",
        agenda.AVISO_ANTES_DE_ENTREGA: "Remind the customer before their delivery",
        agenda.RECORDATORIO_PLAZO_DUENO: "Remind you that this order's deadline is close",
        agenda.SEGUIMIENTO: "Follow up with the team about this order",
        agenda.BAJA_DE_PEDIDO: "Release the stock of an order the customer took back",
        agenda.AVISO_BAJA_AL_DUENO: "Tell you a customer took their order back",
    }.get(fila.tipo, "Scheduled work on this order")


TOKEN_MINIMO = 32

# Un token anónimo está autenticado pero no es NADIE: mira y no toca.
ANONIMO = ""


def entradas_de_tokens(crudo: str) -> tuple[dict[str, str], list[str]]:
    """`DASHBOARD_TOKENS` -> ({token: teléfono}, motivos de lo que NO entró).

    Formato: ``<token>:<teléfono>,<token>:<teléfono>``. El teléfono se
    normaliza acá una vez, con el mismo `telefono.normalizar` que usa el
    webhook, así que `+54 9 11 …` y `5491…` son la misma persona en los dos
    lados. Un token más corto que TOKEN_MINIMO no entra: adivinable no es
    autenticación.

    DEVUELVE LAS DOS MITADES porque las dos hacen falta, y en lugares distintos:
    el panel usa las válidas y `readiness` necesita saber qué se descartó. El
    descarte era MUDO —una entrada rota simplemente no existía—, así que un
    token que el dueño pegó en el `.env` y no funciona no tenía dónde
    explicarse: ni un log, ni un error, ni una línea en el preflight. Con un
    solo parser, además, el chequeo no puede discrepar con el panel sobre qué
    entrada es válida.

    EL TOKEN NUNCA SALE EN UN MOTIVO: los motivos se leen en la salida de
    `readiness`, que es lo que la gente pega en un chat cuando algo no anda.
    """
    from app import telefono as telefonos

    pares: dict[str, str] = {}
    problemas: list[str] = []
    for posicion, entrada in enumerate(crudo.split(","), start=1):
        entrada = entrada.strip()
        if not entrada:
            continue
        if ":" not in entrada:
            problemas.append(f"la entrada {posicion} no tiene «token:teléfono»")
            continue
        token, _, numero = entrada.partition(":")
        token, numero = token.strip(), telefonos.normalizar(numero)
        if len(token) < TOKEN_MINIMO:
            problemas.append(
                f"el token de la entrada {posicion} tiene {len(token)} caracteres "
                f"y el mínimo es {TOKEN_MINIMO}"
            )
        elif not numero:
            problemas.append(
                f"el teléfono de la entrada {posicion} no se puede interpretar"
            )
        elif token in pares:
            problemas.append(f"la entrada {posicion} repite un token que ya estaba")
        else:
            pares[token] = numero
    return pares, problemas


def _tokens_por_persona() -> dict[str, str]:
    """Las entradas válidas de `DASHBOARD_TOKENS`. Ver `entradas_de_tokens`."""
    return entradas_de_tokens(os.getenv("DASHBOARD_TOKENS", ""))[0]


def quien(header: str) -> str | None:
    """Quién está mirando: TRES respuestas, y hay que distinguir las tres.

    - ``None``  -> el token no sirve. No pasa.
    - ``ANONIMO`` (``""``) -> el token compartido `DASHBOARD_API_TOKEN`.
      Autenticado, pero no es nadie en particular: sólo lectura.
    - un teléfono -> un token de `DASHBOARD_TOKENS`, que dice de quién es.

    La idea entera del dashboard cabe en una línea: **un token es una forma
    de probar «soy este teléfono» sin WhatsApp.** No inventa un sistema de
    permisos nuevo — lo que ese teléfono puede hacer lo siguen decidiendo
    `router.es_equipo` y las mismas guardas que valen sobre el webhook
    firmado, que es lo único que las herramientas de decisión verifican.

    Compará con ``is None`` y no por verdad: ``ANONIMO`` es falsy y es un
    token VÁLIDO. La misma trampa que `pendientes._tiene_marca`.
    """
    if not header.startswith("Bearer "):
        return None
    supplied = header.removeprefix("Bearer ").encode()

    # Sin `break`: recorrer todas las entradas siempre cuesta lo mismo, así
    # que el tiempo de respuesta no dice en qué posición estaba el token.
    encontrado: str | None = None
    for token, numero in _tokens_por_persona().items():
        if hmac.compare_digest(supplied, token.encode()):
            encontrado = numero
    if encontrado is not None:
        return encontrado

    compartido = os.getenv("DASHBOARD_API_TOKEN", "")
    if len(compartido) >= TOKEN_MINIMO and hmac.compare_digest(
        supplied, compartido.encode()
    ):
        return ANONIMO
    return None


def puede_decidir(mirando: str | None) -> bool:
    """¿Este token puede tocar un pedido, o sólo mirarlo?

    `decisiones.confirmar`, `rechazar`, `preparar`, `despachar` y
    `aprobar_solicitud` **no verifican quién llama**: confían en que el
    llamador ya lo hizo, y hoy ese llamador es `aprobacion.manejar_boton`,
    que exige `es_equipo` sobre el webhook FIRMADO de Meta. Un dashboard que
    llame a cualquiera de ellas es el único control entre un token y un
    Submit, así que la verificación vive acá y no se saltea en ninguna ruta.

    El token compartido nunca alcanza: no nombra a una persona, y una
    decisión sin nombre no se puede auditar.
    """
    from app import router

    return bool(mirando) and router.es_equipo(mirando)


def hay_acceso_configurado() -> bool:
    """¿Hay ALGUNA forma de entrar? Sin esto, 503 y ningún dato de negocio.

    Cualquiera de las dos alcanza: el token compartido o al menos un token
    por persona. Mirar sólo `DASHBOARD_API_TOKEN` dejaba el dashboard en 503
    para una instalación configurada entera con `DASHBOARD_TOKENS`.
    """
    return (
        len(os.getenv("DASHBOARD_API_TOKEN", "")) >= TOKEN_MINIMO
        or bool(_tokens_por_persona())
    )


def authorized(header: str) -> bool:
    """Compatibilidad: ¿el token sirve para LEER? Las escrituras usan `quien`."""
    return quien(header) is not None


def allowed_origin(origin: str) -> bool:
    # Exact origins only. A wildcard must never expose manager data.
    configured = os.getenv("DASHBOARD_ALLOWED_ORIGINS", "").split(",")
    return bool(origin) and origin in {x.strip().rstrip("/") for x in configured if x.strip() != "*"}


def number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def order_row(row: dict) -> dict:
    status = str(row.get("status") or "Unknown")
    if row.get("docstatus") == 2:
        state = "cancelled"
    elif status in {"Closed", "On Hold"}:
        state = "closed" if status == "Closed" else "on-hold"
    elif row.get("docstatus") == 0:
        state = "pending"
    elif row.get("docstatus") == 1:
        state = "completed" if status == "Completed" else "confirmed"
    else:
        state = "unknown"
    return {
        "id": str(row.get("name", "")),
        "customerId": str(row.get("customer", "")),
        "customer": str(row.get("customer_name") or row.get("customer") or "Unknown customer"),
        "date": str(row.get("transaction_date") or ""),
        "deliveryDate": str(row.get("delivery_date") or ""),
        "total": number(row.get("grand_total")),
        "currency": str(row.get("currency") or ""),
        "status": state,
        "erpStatus": status,
        "channel": "ERPNext",
    }


def snapshot() -> dict:
    from app import erpnext, reloj

    now = reloj.ahora()
    since = (now.date() - timedelta(days=29)).isoformat()
    company = erpnext.default_company()
    errors: list[str] = []
    truncated: list[str] = []

    def read(label: str, doctype: str, **kwargs) -> list[dict] | None:
        try:
            rows = erpnext.get_list(doctype, limit=LIMIT + 1, timeout=READ_TIMEOUT, **kwargs)
        except Exception:
            # Never put upstream response bodies or credential-bearing exceptions in JSON.
            errors.append(label)
            return None
        if len(rows) > LIMIT:
            truncated.append(label)
        return rows[:LIMIT]

    with erpnext.manager_scope():
        orders = read(
            "orders", "Sales Order",
            filters=[["company", "=", company], ["transaction_date", ">=", since],
                     ["transaction_date", "<=", now.date().isoformat()]],
            fields=ORDER_FIELDS,
            order_by="transaction_date desc, name desc",
        )
        pending = read(
            "pending orders", "Sales Order",
            filters=[["company", "=", company], ["docstatus", "=", 0],
                     ["status", "not in", ["Closed", "Cancelled", "On Hold"]]],
            fields=ORDER_FIELDS, order_by="creation asc, name asc",
        )
        # LOS CLIENTES SALEN DE LOS PEDIDOS, y los pedidos ya están acotados a
        # `company`. Preguntar por `Customer` a secas devolvía TODOS los
        # habilitados del sitio: en un ERPNext con más de una empresa, un token
        # de este dashboard veía nombres, grupos y territorios de clientes de
        # otra. `Customer` es un maestro GLOBAL en ERPNext —no tiene campo
        # `company`—, así que no hay un filtro directo: la única pertenencia
        # real es «le vendimos algo», y eso son los Sales Orders.
        #
        # Es además lo que la pantalla ya dice ser: «las personas y negocios
        # detrás de tus pedidos». Un cliente sin un solo pedido en la ventana no
        # aparece, y está bien: no hay nada que mostrar de él.
        #
        # Si los pedidos no se pudieron leer, los clientes tampoco. Fallar
        # cerrado: la alternativa es ensanchar la lectura justo cuando no se
        # puede acotar, que es el bug de arriba otra vez.
        cuentas = sorted(
            {str(x.get("customer") or "") for x in (orders or []) + (pending or [])}
            - {""}
        )
        customers: list[dict] | None = None
        if orders is None or pending is None:
            errors.append("customers")
        elif cuentas:
            customers = read(
                "customers", "Customer",
                filters=[["name", "in", cuentas], ["disabled", "=", 0]],
                fields=["name", "customer_name", "customer_group", "territory"],
                order_by="modified desc, name desc",
            )
        else:
            customers = []
        warehouse = erpnext.default_warehouse()
        bins = read(
            "inventory", "Bin", filters=[["warehouse", "=", warehouse]],
            fields=["name", "item_code", "warehouse", "actual_qty", "reserved_qty", "stock_uom"],
            order_by="item_code asc, name asc",
        )
        items = []
        if bins:
            items = read(
                "product names", "Item",
                filters=[["name", "in", list({b["item_code"] for b in bins})]],
                fields=["name", "item_name", "stock_uom"], order_by="name asc",
            ) or []
        labels = {x["name"]: x for x in items}
        try:
            currency = erpnext.get_doc("Company", company, timeout=READ_TIMEOUT).get("default_currency") or ""
        except Exception:
            currency = ""
            errors.append("currency")

    products = None
    if bins is not None:
        products = []
        for b in bins:
            actual, reserved = number(b.get("actual_qty")), number(b.get("reserved_qty"))
            item = labels.get(b["item_code"], {})
            products.append({
                "id": b["item_code"], "name": item.get("item_name") or b["item_code"],
                "unit": item.get("stock_uom") or b.get("stock_uom") or "units",
                "stock": actual, "reserved": reserved,
                "available": None if actual is None or reserved is None else actual - reserved,
                "warehouse": b.get("warehouse", ""),
            })

    return {
        "mode": "live", "generatedAt": now.isoformat(), "today": now.date().isoformat(),
        "since": since, "company": company, "currency": currency,
        "orders": None if orders is None else [order_row(x) for x in orders],
        "pendingOrders": None if pending is None else [order_row(x) for x in pending],
        "customers": None if customers is None else [{
            "id": x["name"], "name": x.get("customer_name") or x["name"],
            "group": x.get("customer_group") or "Customer", "territory": x.get("territory") or "",
        } for x in customers],
        "products": products, "policies": None,
        "agents": model_status(), "erpOrigin": public_erp_origin(),
        "errors": errors, "truncated": truncated, "limit": LIMIT,
    }


async def _en_hilo(funcion, *args):
    """Corre una lectura en el pool DEL PANEL, no en el del proceso."""
    return await asyncio.get_running_loop().run_in_executor(
        _HILOS, funcion, *args
    )


class DashboardAPI:
    """Authenticated read endpoints, mounted only at /api/dashboard."""

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        origin = headers.get("origin", "")
        cors = allowed_origin(origin)
        response_headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
            (b"vary", b"Origin"),
        ]
        if cors:
            response_headers.extend([
                (b"access-control-allow-origin", origin.encode()),
                (b"access-control-allow-methods", b"GET, OPTIONS"),
                (b"access-control-allow-headers", b"Authorization"),
            ])

        async def reply(code, payload):
            await send({"type": "http.response.start", "status": code, "headers": response_headers})
            await send({"type": "http.response.body", "body": json.dumps(payload, allow_nan=False).encode()})

        path = scope.get("path", "").removeprefix(scope.get("root_path", ""))
        detail_match = re.fullmatch(r"/orders/([^/]{1,140})", path)
        conversation_match = re.fullmatch(r"/customers/([^/]{1,140})/conversation", path)
        readers = {
            "/snapshot": snapshot, "/controls": controls, "/operations": operations,
            "/today": today, "/queue": queue,
        }
        if path == "/config" and scope["method"] == "GET":
            # No company, model, origin, or business data is returned before auth.
            await reply(200, {"service": "plus-agent", "apiVersion": 1,
                              "configured": hay_acceso_configurado()})
            return
        if path not in readers and not detail_match and not conversation_match:
            await reply(404, {"error": "Not found"})
            return
        if scope["method"] == "OPTIONS":
            await reply(200 if cors else 403, {"ok": cors})
            return
        if scope["method"] != "GET":
            await reply(405, {"error": "This dashboard is read-only"})
            return
        if not hay_acceso_configurado():
            await reply(503, {"error": "Live dashboard access is not configured"})
            return
        # `quien` tiene TRES respuestas y ANONIMO es falsy: `is None` y no verdad.
        mirando = quien(headers.get("authorization", ""))
        if mirando is None:
            response_headers.append((b"www-authenticate", b"Bearer"))
            await reply(401, {"error": "A valid dashboard access token is required"})
            return
        # Same-origin requests need no CORS. Cross-origin access is explicit.
        host = headers.get("host", "")
        same_origin = origin == f"{scope.get('scheme', 'http')}://{host}"
        if origin and not same_origin and not cors:
            await reply(403, {"error": "This dashboard origin is not allowed"})
            return
        try:
            if detail_match:
                order_id = unquote(detail_match[1])
                if "/" in order_id or "\\" in order_id or order_id in {".", ".."}:
                    raise RecordNotFound
                data = await _en_hilo(order_detail, order_id)
            elif conversation_match:
                customer_id = unquote(conversation_match[1])
                if "/" in customer_id or "\\" in customer_id or customer_id in {".", ".."}:
                    raise RecordNotFound
                data = await _en_hilo(conversation, customer_id)
            else:
                data = await _en_hilo(readers[path])
        except RecordNotFound:
            await reply(404, {"error": "Order not found in this workspace"})
            return
        except Exception:
            await reply(502, {"error": "Could not read CRM data. Check the agent service."})
            return
        await reply(200, data)


def install_dashboard(application) -> None:
    """Mount exactly the same application in production and HTTP integration tests."""
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    application.mount("/api/dashboard", DashboardAPI())
    application.mount(
        "/dashboard", StaticFiles(directory=Path(__file__).parent / "dashboard_ui", html=True),
        name="dashboard",
    )
