"""Dashboard API: reads, and TWO named exceptions that are not reads.

An independent ASGI surface keeps its bearer auth and CORS away from the signed
WhatsApp webhook. The UI is public static content; business data never is.

The rule is still «este panel lee», and the exceptions are named here instead of
softening it — the same shape the router uses at the branch that allows them:

  * `POST /orders/{id}/confirm` confirms ONE order, through
    `decisiones.confirmar`, which is the same door the WhatsApp button uses.
  * `POST /settings/propose` leaves ONE settings change WAITING for the
    four-digit code. It changes nothing by itself, and there is deliberately no
    route that applies the code: the owner confirms on WhatsApp, so the second
    step stays on another device and another channel. That is what hard rule 3
    asks for, and it is also why `limites.aplicar` — which has no attempt
    counter, because its only door is a signed webhook — is not published here.

Both are gated by `puede_decidir`, not by `authorized`: reading is enough to
look, never to act.
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

# Y LA ESCRITURA TIENE EL SUYO, por la misma razón un nivel más abajo.
#
# Una lectura son unos segundos; confirmar es otra cosa entera: hasta 10 s
# esperando `confirmar:`, otros 10 esperando `solicitud:`, y después la cadena
# del submit contra timeouts de 20 y 30 s. Medido, un confirm contra un ERPNext
# colgado ocupa su hilo varios MINUTOS. Con los cuatro hilos compartidos eso se
# come el pool y `/snapshot`, `/today` y `/queue` se quedan en la cola:
# `run_in_executor` encola sin límite y el cliente que se cansa no cancela nada,
# así que el panel se veía muerto para todo el mundo mientras el trabajo seguía
# ahí adentro. Con un pool aparte, el peor confirm sólo hace esperar a otro
# confirm.
#
# NO LLEVA DEADLINE, y es deliberado. Cortar la espera no cancela el hilo ni el
# submit —`run_in_executor` no se interrumpe—, así que un tope sólo cambiaría
# «la respuesta tarda» por «se le informa un fallo a alguien cuyo pedido SÍ se
# emitió». Un encargado que lee eso vuelve a tocar el botón. La confirmación es
# idempotente y aguanta el segundo toque, pero el informe sería mentira.
_HILOS_ESCRITURA = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dashboard-write")
ORDER_FIELDS = [
    "name", "customer", "customer_name", "transaction_date", "delivery_date",
    "grand_total", "currency", "status", "docstatus",
]


# Un cuerpo de pedido acotado. 8 KiB es holgadísimo para `{"setting": ...,
# "value": ...}` y sigue siendo chico para que a nadie le sirva mandarlo.
CUERPO_MAXIMO = 8 * 1024


class CuerpoInvalido(Exception):
    """El cuerpo del POST no se puede usar. Se le cuenta al cliente, sin detalles."""


async def cuerpo_json(receive) -> dict:
    """El cuerpo de un POST como dict, o CuerpoInvalido.

    EL TECHO SE MIDE ADENTRO DEL BUCLE, y no contra `content-length`: un pedido
    `chunked` no manda ese header, así que comprobarlo después de juntar todo es
    comprobarlo cuando ya se buffereó todo — que es justo lo que el techo existe
    para no hacer.

    El llamador tiene que haber pasado la autenticación ANTES de llamar acá, o
    un desconocido puede hacer que el proceso junte megabytes.
    """
    crudo = b""
    while True:
        evento = await receive()
        if evento.get("type") != "http.request":
            break
        crudo += evento.get("body", b"") or b""
        if len(crudo) > CUERPO_MAXIMO:
            raise CuerpoInvalido("The request body is too large")
        if not evento.get("more_body"):
            break
    if not crudo.strip():
        raise CuerpoInvalido("This request needs a JSON body")
    try:
        datos = json.loads(crudo.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CuerpoInvalido("The request body is not valid JSON") from exc
    if not isinstance(datos, dict):
        raise CuerpoInvalido("The request body has to be a JSON object")
    return datos


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


def _pedido_de_la_empresa(order_id: str) -> dict:
    """El pedido, o `RecordNotFound` si no es de ESTA empresa. Bajo `manager_scope`.

    `Customer` y `Sales Order` son maestros globales en ERPNext, así que la
    pertenencia no es un campo que se pueda filtrar en la ruta: hay que leer el
    documento y comparar la empresa. Está acá y no copiado en cada ruta porque
    el segundo llamador escribe —confirma un pedido— y dos comprobaciones de
    pertenencia que nadie obliga a coincidir es como se cuela la que falta.
    """
    from app import erpnext

    try:
        doc = erpnext.get_doc("Sales Order", order_id, timeout=READ_TIMEOUT)
    except erpnext.ERPNextError as exc:
        if exc.status_code in {403, 404}:
            raise RecordNotFound from exc
        raise
    if doc.get("company") != erpnext.default_company():
        raise RecordNotFound
    return doc


def order_detail(order_id: str) -> dict:
    from app import erpnext

    with erpnext.manager_scope():
        doc = _pedido_de_la_empresa(order_id)
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


def confirmar_desde_el_panel(order_id: str, quien_decide: str) -> dict:
    """NO se llama `confirmar_pedido`, y el nombre viejo era una trampa: hay una
    `aprobacion.confirmar_pedido` que es el MECANISMO del submit, ésta no la
    llama, y las dos no son intercambiables — ésta entra por la puerta
    (`decisiones.confirmar`) y aquélla está del otro lado de la puerta.

    Confirmar UN pedido desde el panel, a nombre de una persona con nombre.

    LO QUE ESTA FUNCIÓN NO HACE, y es la mitad del diseño: no decide nada. No
    mira si hay una solicitud abierta, no cierra la revisión, no toma el lock y
    no comprueba `es_equipo` por su cuenta — todo eso vive adentro de
    `decisiones.confirmar`, que es la puerta, y si viviera también acá serían
    dos copias de la misma política que nadie obliga a estar de acuerdo. Lo
    único propio del panel es lo de arriba: que el pedido sea de ESTA empresa.

    LA REGLA DURA 2 NO SE TOCA. El Submit lo sigue haciendo `erpnext.submit_doc`
    con la credencial de política, que no es alcanzable desde ninguna
    herramienta del modelo. Un endpoint no es una herramienta en ese sentido: lo
    invoca una PERSONA autenticada, y ejecuta el mismo Python determinista que
    corre cuando esa misma persona toca el botón de WhatsApp.

    `detail` es el texto del agente tal cual, en el idioma del negocio: es
    exactamente lo que se le habría dicho por WhatsApp a quien confirma. Se
    devuelve sin traducir a propósito — dos canales que explican el mismo
    resultado con palabras distintas terminan discrepando, y acá el motivo de un
    rechazo («se rechazó antes y ya no reserva stock») es lo único que dice qué
    hacer después.
    """
    from app import decisiones, erpnext

    with erpnext.manager_scope():
        # LA PERTENENCIA PRIMERO, y con la misma lectura que `order_detail`:
        # levanta `RecordNotFound` para un pedido de otra empresa, así que un
        # token válido no puede confirmar un pedido que ni siquiera puede ver.
        pedido = _pedido_de_la_empresa(order_id)

    # EL NOMBRE CANÓNICO, EL DEL DOCUMENTO, no el texto que vino en la URL — y
    # la diferencia entre los dos es un lock. Esta lectura ya se hacía y su
    # resultado se tiraba. ERPNext resuelve el nombre sin mirar mayúsculas
    # (MariaDB colaciona así por defecto), así que un
    # `POST /orders/so-ord-0001/confirm` encuentra el pedido igual — y después
    # `decisiones.confirmar` arma con ese texto `solicitud:so-ord-0001`, que en
    # Redis NO es la misma llave que el `solicitud:SO-ORD-0001` que toma
    # `solicitudes.crear` (de `so["name"]`, siempre canónico) y que toma el
    # botón de WhatsApp (`acciones.pedido_valido` termina en `.upper()`).
    # Dos llaves distintas para el mismo pedido es no tener exclusión mutua
    # entre los dos canales: exactamente la carrera que ese lock cierra.
    # El panel era el único camino que no canonizaba.
    nombre = str(pedido.get("name") or order_id)

    resultado = decisiones.confirmar(
        nombre, quien_decide, canal=decisiones.CANAL_PANEL
    )
    return {
        "orderId": nombre,
        "ok": bool(resultado.get("ok")),
        # `ok` NO ES «se confirmó», y confundirlos mostraba una venta que no
        # existe. Con una contraoferta abierta la puerta aprueba la solicitud y
        # le manda los términos nuevos al cliente: sale bien, `ok` es True, y no
        # hubo ningún Submit — el pedido sigue siendo un borrador reservando
        # stock, y el cliente todavía puede rechazarlo. `submitted` es el hecho
        # que el panel necesita para pintar la fila, y es el único que dice
        # `docstatus=1`.
        #
        # VIAJA SIN `bool()`, y eso es lo que lo hace honesto: `null` es «no se
        # pudo comprobar» y no es lo mismo que `false`. Un Submit que expira
        # puede haber commiteado, y si la relectura tampoco contesta nadie sabe
        # cómo quedó el pedido; `bool(None)` lo convertía en un «no se emitió»
        # que nadie leyó, y el encargado deja como borrador un pedido
        # confirmado. Con `null` el panel refresca en vez de afirmar.
        "submitted": resultado.get("emitido"),
        "customerNotified": bool(resultado.get("aviso_cliente")),
        "detail": str(resultado.get("detalle") or ""),
    }


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


def normalizar_token(crudo: object) -> str:
    """Cómo se compara un token. UNA regla, y todos los lados la llaman a ella.

    Había tres, y no coincidían: `entradas_de_tokens` recortaba los espacios de
    cada token por persona, `readiness` recortaba el compartido antes de
    validarlo, y `quien()` comparaba el compartido CRUDO contra lo que llega en
    el header. Con `DASHBOARD_API_TOKEN=" abc… "` en el `.env` —entre comillas,
    que es como se pega un secreto— el preflight decía LISTO sobre un valor
    recortado que nadie usaba, el panel recorta lo que la persona tipea
    (`dashboard_ui/app.js`), y todas las peticiones daban 401 contra un
    preflight en verde. Una sola función es lo que hace que eso no pueda volver.
    """
    return str(crudo or "").strip()


def token_compartido() -> str:
    """`DASHBOARD_API_TOKEN` tal como se compara. Ver `normalizar_token`."""
    return normalizar_token(os.getenv("DASHBOARD_API_TOKEN", ""))


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

    NINGUNA AMBIGÜEDAD SE ACEPTA A MEDIAS: si dos entradas comparten el token,
    o dos entradas apuntan al mismo teléfono, se caen LAS DOS. Antes ganaba la
    primera y las otras se descartaban calladas, y eso rompía lo único para lo
    que sirve tener un token por persona: revocar. Revocar es sacar una línea
    del `.env`, y con dos líneas para la misma persona el dueño saca una, cree
    que le cortó el acceso, y la otra sigue entrando y sigue pudiendo confirmar
    pedidos; si saca la otra, ASCIENDE un token que hasta ese momento no hacía
    nada. En los dos casos el `.env` no dice lo que pasa.

    El token repetido es peor todavía y es el que se veía menos: dos entradas
    con el mismo token y teléfonos DISTINTOS resolvían al primero, así que la
    persona a la que se le dio ese token entraba con la identidad de la otra —y
    con sus permisos, si la otra está en TELEFONOS_EQUIPO—. Rechazar las dos es
    la forma que falla cerrada, y cuesta poco: el panel no es la única puerta
    para confirmar, el botón de WhatsApp no depende de esto, y
    `deploy/configurar_dashboard.py` ya se niega a emitir un segundo token, así
    que para llegar acá hay que haber editado el archivo a mano.

    EL TOKEN NUNCA SALE EN UN MOTIVO: los motivos se leen en la salida de
    `readiness`, que es lo que la gente pega en un chat cuando algo no anda.
    """
    from app import telefono as telefonos

    candidatas: list[tuple[int, str, str]] = []
    problemas: list[str] = []
    for posicion, entrada in enumerate(crudo.split(","), start=1):
        entrada = entrada.strip()
        if not entrada:
            continue
        if ":" not in entrada:
            problemas.append(f"la entrada {posicion} no tiene «token:teléfono»")
            continue
        token, _, numero = entrada.partition(":")
        token, numero = normalizar_token(token), telefonos.normalizar(numero)
        if len(token) < TOKEN_MINIMO:
            problemas.append(
                f"el token de la entrada {posicion} tiene {len(token)} caracteres "
                f"y el mínimo es {TOKEN_MINIMO}"
            )
        elif not numero:
            problemas.append(
                f"el teléfono de la entrada {posicion} no se puede interpretar"
            )
        else:
            candidatas.append((posicion, token, numero))

    # DOS PASADAS, y hace falta que sean dos: en una sola, «repetida» sólo se
    # puede decir de la segunda, y es la primera la que se queda con el acceso.
    # El teléfono se compara NORMALIZADO —`+54 9 11 …` y `5491…` son la misma
    # persona y dos strings—, igual que en `deploy/configurar_dashboard.py`.
    veces_token: dict[str, int] = {}
    veces_numero: dict[str, int] = {}
    for _, token, numero in candidatas:
        veces_token[token] = veces_token.get(token, 0) + 1
        veces_numero[numero] = veces_numero.get(numero, 0) + 1

    pares: dict[str, str] = {}
    for posicion, token, numero in candidatas:
        if veces_token[token] > 1:
            problemas.append(
                f"la entrada {posicion} repite un token que está en otra entrada: "
                "no entra ninguna de las dos"
            )
        elif veces_numero[numero] > 1:
            problemas.append(
                f"la entrada {posicion} es uno de varios tokens para el mismo "
                "teléfono: no entra ninguno, porque sacar una sola línea no le "
                "cortaría el acceso"
            )
        else:
            pares[token] = numero
    return pares, problemas


# Lo último que se avisó, para no repetir el aviso en cada petición.
_TOKENS_AVISADOS: str | None = None


def _tokens_por_persona() -> dict[str, str]:
    """Las entradas válidas de `DASHBOARD_TOKENS`. Ver `entradas_de_tokens`.

    Y LOS MOTIVOS SE LOGUEAN. Tirarlos acá era lo que hacía invisible todo lo
    de arriba: una entrada rota no existía y punto, sin una línea en ningún
    lado. `readiness` los informa, pero es un comando que hay que acordarse de
    correr; el proceso que está sirviendo el panel es el que sabe qué entradas
    está usando de verdad. Se avisa una vez por valor de `DASHBOARD_TOKENS`,
    porque esto corre en cada petición. Ningún token se imprime.
    """
    global _TOKENS_AVISADOS

    crudo = os.getenv("DASHBOARD_TOKENS", "")
    pares, problemas = entradas_de_tokens(crudo)
    if problemas and crudo != _TOKENS_AVISADOS:
        _TOKENS_AVISADOS = crudo
        for motivo in problemas:
            print(f"[dashboard] DASHBOARD_TOKENS: {motivo}")
    return pares


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

    compartido = token_compartido()
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
        len(token_compartido()) >= TOKEN_MINIMO
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


async def _en_hilo(funcion, *args, pool=None):
    """Corre una lectura en el pool DEL PANEL, no en el del proceso.

    `pool` es para la escritura, que va a `_HILOS_ESCRITURA`: si comparte los
    hilos de lectura, un confirm lento deja al resto del panel en la cola.
    """
    return await asyncio.get_running_loop().run_in_executor(
        pool or _HILOS, funcion, *args
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
                (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
                # `Content-Type` ESTÁ PERMITIDO aunque el endpoint no lea el
                # cuerpo. Era `Authorization` sola, y el POST natural de un
                # `fetch` manda `Content-Type: application/json`, que no está en
                # la lista segura de CORS: el navegador rechaza el preflight y
                # la petición no sale nunca. No hay log, no hay error, no llega
                # nada al servidor — se ve como «el botón Confirmar no hace
                # nada», que es lo más caro de diagnosticar que hay.
                (b"access-control-allow-headers", b"Authorization, Content-Type"),
                # Sin esto CADA lectura del panel paga dos viajes: `Authorization`
                # no está en la lista segura, así que todo pedido cross-origin va
                # con preflight, y sin `max-age` Chrome lo cachea 5 s contra un
                # refresco de 60.
                (b"access-control-max-age", b"600"),
            ])

        async def reply(code, payload):
            await send({"type": "http.response.start", "status": code, "headers": response_headers})
            await send({"type": "http.response.body", "body": json.dumps(payload, allow_nan=False).encode()})

        path = scope.get("path", "").removeprefix(scope.get("root_path", ""))
        detail_match = re.fullmatch(r"/orders/([^/]{1,140})", path)
        conversation_match = re.fullmatch(r"/customers/([^/]{1,140})/conversation", path)
        # La ÚNICA ruta que escribe. `[^/]` la mantiene fuera de `detail_match`,
        # que es `fullmatch` sobre un segmento sin barras.
        confirm_match = re.fullmatch(r"/orders/([^/]{1,140})/confirm", path)
        # LA SEGUNDA RUTA QUE ESCRIBE, y no escribe un ajuste: deja UNO
        # esperando el código de cuatro dígitos que sale por WhatsApp. No hay
        # ninguna ruta que lo confirme, a propósito — ver `proponer_ajuste`.
        propose_match = path == "/settings/propose"
        settings_match = path == "/settings"
        # La TERCERA que escribe, y la única que mueve plata sin código: el
        # dueño lo pidió así. Lo que la acota es `PRECIO_CAMBIO_MAX_PCT`, que
        # arranca en 0 —ningún precio se escribe— y vive en la pantalla de
        # ajustes como cualquier otro tope.
        price_match = re.fullmatch(r"/products/([^/]{1,140})/price", path)
        readers = {
            "/snapshot": snapshot, "/controls": controls, "/operations": operations,
            "/today": today, "/queue": queue, "/sales": sales, "/advice": advice,
            "/prices": prices,
        }
        if path == "/config" and scope["method"] == "GET":
            # No company, model, origin, or business data is returned before auth.
            await reply(200, {"service": "plus-agent", "apiVersion": 1,
                              "configured": hay_acceso_configurado()})
            return
        if (path not in readers and not detail_match and not conversation_match
                and not confirm_match and not propose_match and not settings_match
                and not price_match):
            await reply(404, {"error": "Not found"})
            return
        if scope["method"] == "OPTIONS":
            await reply(200 if cors else 403, {"ok": cors})
            return
        # El panel sigue siendo de sólo lectura salvo en UNA ruta, y se dice así:
        # la excepción se nombra donde está la regla, en vez de aflojar la regla.
        if confirm_match or propose_match or price_match:
            # ESTA RAMA HAY QUE EXTENDERLA CON CADA RUTA QUE ESCRIBA. El `elif`
            # de abajo contesta 405 «read-only» a todo lo que no caiga acá, así
            # que una ruta de escritura nueva que se olvide de nombrarse acá no
            # funciona nunca — y la que se olvide en la guarda de abajo funciona
            # DE MÁS, que es peor.
            if scope["method"] != "POST":
                await reply(405, {"error": "This is a POST"})
                return
        elif scope["method"] != "GET":
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
        # ESCRIBIR PIDE UN NOMBRE. `quien` devuelve tres cosas y el token
        # COMPARTIDO (`ANONIMO`, `""`) es una de ellas: está autenticado y no es
        # nadie en particular, así que puede mirar y nunca decidir — una
        # confirmación sin nombre no se puede auditar. `puede_decidir` además
        # exige `router.es_equipo`, o sea las MISMAS guardas que valen sobre el
        # webhook firmado de Meta; el token no inventa permisos nuevos, sólo es
        # otra forma de probar «soy este teléfono».
        if (confirm_match or propose_match or price_match) and not puede_decidir(mirando):
            # UNA guarda, dos frases. La guarda es una sola a propósito —dos
            # predicados de permiso se separan con el tiempo y entonces el panel
            # y el WhatsApp dejan de estar de acuerdo sobre quién puede actuar—,
            # pero al que la choca hay que decirle qué es lo que no puede.
            que = ("confirm orders" if confirm_match
                   else "change prices" if price_match else "change settings")
            await reply(403, {
                "error": f"This token can read the dashboard but cannot {que}"
            })
            return
        # Same-origin requests need no CORS. Cross-origin access is explicit.
        #
        # EL ESQUEMA SE COMPARA, PERO EN UNA SOLA DIRECCIÓN, y las dos mitades
        # arreglan cosas distintas.
        #
        # Por qué se afloja: el navegador omite `Origin` en un GET del mismo
        # origen pero SIEMPRE lo manda en un POST, así que el botón Confirmar
        # del propio panel pasa por acá — y pasaba sólo si `scope["scheme"]`
        # decía `https`, que depende de que el proxy de adelante mande
        # `X-Forwarded-Proto` (el Dockerfile arranca uvicorn con
        # `--proxy-headers` justamente por eso). Donde no lo mande, el proceso
        # se ve a sí mismo en http, el navegador dice https, y el confirm da 403
        # con TODAS las lecturas andando: «el botón no hace nada» otra vez, sin
        # necesidad de DASHBOARD_ALLOWED_ORIGINS.
        #
        # Por qué NO más que eso: `http://` y `https://` son dos orígenes
        # distintos para el navegador, y aceptar los dos sin mirar deja que una
        # página servida en http sobre este mismo host le hable al servicio
        # https salteándose la lista exacta de `allowed_origin`. Se acepta sólo
        # el ASCENSO —el proceso en http y el navegador en https, que es el
        # proxy terminando TLS—; la bajada no tiene ningún caso legítimo.
        host = headers.get("host", "")
        esquema = scope.get("scheme", "http")
        same_origin = origin == f"{esquema}://{host}" or (
            esquema == "http" and origin == f"https://{host}"
        )
        if origin and not same_origin and not cors:
            await reply(403, {"error": "This dashboard origin is not allowed"})
            return
        # EL CUERPO SE LEE ACÁ: después de las guardas y antes del pool. Antes
        # de autenticar, un desconocido hace que el proceso junte megabytes;
        # adentro del pool ya no hay loop sobre el que esperar `receive`.
        pedido: dict = {}
        setting = valor = ""
        if propose_match or price_match:
            try:
                pedido = await cuerpo_json(receive)
            except CuerpoInvalido as exc:
                await reply(400, {"error": str(exc)})
                return
            valor = plain_text(pedido.get("value"))[:600]
        if propose_match:
            setting = plain_text(pedido.get("setting"))[:140]
            if not setting:
                await reply(400, {"error": "Tell me which setting to change"})
                return
        elif price_match and not valor:
            await reply(400, {"error": "Tell me the new price"})
            return

        try:
            if propose_match:
                data = await _en_hilo(
                    proponer_ajuste, setting, valor, mirando, pool=_HILOS_ESCRITURA
                )
            elif price_match:
                producto = unquote(price_match[1])
                if "/" in producto or "\\" in producto or producto in {".", ".."}:
                    raise RecordNotFound
                data = await _en_hilo(
                    cambiar_precio_desde_el_panel, producto, valor, mirando,
                    pool=_HILOS_ESCRITURA,
                )
            elif settings_match:
                data = await _en_hilo(settings, mirando)
            elif confirm_match:
                order_id = unquote(confirm_match[1])
                if "/" in order_id or "\\" in order_id or order_id in {".", ".."}:
                    raise RecordNotFound
                data = await _en_hilo(
                    confirmar_desde_el_panel, order_id, mirando, pool=_HILOS_ESCRITURA
                )
            elif detail_match:
                order_id = unquote(detail_match[1])
                if "/" in order_id or "\\" in order_id or order_id in {".", ".."}:
                    raise RecordNotFound
                data = await _en_hilo(order_detail, order_id)
            elif conversation_match:
                customer_id = unquote(conversation_match[1])
                if "/" in customer_id or "\\" in customer_id or customer_id in {".", ".."}:
                    raise RecordNotFound
                data = await _en_hilo(conversation, customer_id)
            elif path == "/sales":
                # La única lectura con parámetro. `scope["path"]` no trae la
                # query —ASGI la deja en `query_string`—, así que el ruteo de
                # arriba la encuentra igual y el período se lee acá.
                data = await _en_hilo(sales, dias_pedidos(scope.get("query_string", b"")))
            else:
                data = await _en_hilo(readers[path])
        except RecordNotFound:
            await reply(404, {"error": "Order not found in this workspace"})
            return
        except Exception:
            # «No pude leer» es una frase de LECTURA, y para una escritura que
            # falló es mentira: el operador lee que hay un problema de conexión
            # y el problema era su valor. Las escrituras contestan lo suyo.
            if propose_match or price_match:
                await reply(502, {
                    "error": "The change could not be completed. Check the agent service."
                })
                return
            await reply(502, {"error": "Could not read CRM data. Check the agent service."})
            return
        await reply(200, data)



# ------------------------------------------------------- ventas y consejos


# El período lo elige el PANEL y lo acota el servidor. `days` viene del
# selector 7/30 y se recorta a 1..90: un entero sin techo del lado del cliente
# es la forma barata de pedirle a ERPNext que recorra todo.
DIAS_VENTAS_DEFECTO = 7
DIAS_VENTAS_MAX = 90


def dias_pedidos(query: bytes) -> int:
    """`?days=N` acotado. Cualquier cosa rara cae en el default, no en un error:
    un período ilegible no es motivo para dejar al dueño sin pantalla."""
    from urllib.parse import parse_qs

    crudo = (parse_qs(query.decode("latin-1")).get("days") or [""])[0]
    try:
        pedido = int(crudo)
    except (TypeError, ValueError):
        return DIAS_VENTAS_DEFECTO
    return max(1, min(DIAS_VENTAS_MAX, pedido))


def _monto(valor: object) -> float:
    try:
        return float(valor or 0)
    except (TypeError, ValueError):
        return 0.0


def _dia_de(pedido: dict):
    from datetime import date

    crudo = pedido.get("transaction_date")
    if isinstance(crudo, date):
        return crudo
    try:
        return date.fromisoformat(str(crudo)[:10])
    except ValueError:
        return None


def sales(dias: int = DIAS_VENTAS_DEFECTO) -> dict:
    """Ventas CONFIRMADAS (`docstatus=1`) de esta empresa en los últimos `dias`.

    Consulta propia y no `gerencia._ventas_del_periodo`: esa herramienta
    devuelve PROSA para el modelo —una frase con el total ya pasado por
    `pesos()`— y de una frase no sale un gráfico. Los filtros son los mismos.

    `company` va en el filtro por lo mismo que en `snapshot`: un ERPNext puede
    tener varias empresas y este panel es de una.
    """
    from datetime import timedelta

    from app import erpnext, policy

    errors: list[str] = []
    truncated: list[str] = []
    empresa = erpnext.default_company()
    hoy = policy._hoy_del_negocio()
    desde = hoy - timedelta(days=max(0, dias - 1))
    vacio = {"currency": None, "since": desde.isoformat(), "until": hoy.isoformat(),
             "total": None, "orders": None, "averageOrder": None, "daily": None,
             "topProducts": None, "topCustomers": None,
             "errors": errors, "truncated": truncated}

    with erpnext.manager_scope():
        try:
            moneda = str(
                erpnext.get_doc("Company", empresa, timeout=READ_TIMEOUT).get("default_currency") or ""
            )
        except erpnext.ERPNextError:
            # None y no "": el panel distingue «no disponible» de un valor.
            moneda = None
            errors.append("currency")
        try:
            pedidos = erpnext.get_list(
                "Sales Order",
                filters=[["docstatus", "=", 1], ["company", "=", empresa],
                         ["transaction_date", ">=", desde.isoformat()],
                         ["transaction_date", "<=", hoy.isoformat()]],
                fields=["name", "customer", "customer_name", "grand_total", "transaction_date"],
                order_by="transaction_date desc", limit=LIMIT, timeout=READ_TIMEOUT,
            )
        except erpnext.ERPNextError:
            errors.append("sales")
            return {**vacio, "currency": moneda}
        if len(pedidos) >= LIMIT:
            truncated.append("sales")
        renglones: list[dict] | None = []
        if pedidos:
            try:
                renglones = erpnext.get_list(
                    "Sales Order Item",
                    # `parent` es el DOCTYPE padre y NO es opcional en una tabla
                    # hija: sin él Frappe la rechaza, y ése fue el bug por el que
                    # `informe(que="stock_bajo")` nunca contestó contra un
                    # ERPNext real.
                    parent="Sales Order",
                    filters=[["parent", "in", [str(x.get("name") or "") for x in pedidos]]],
                    fields=["parent", "item_code", "item_name", "qty", "amount"],
                    # +1 es el centinela: con él se sabe si el tope se
                    # ALCANZÓ de verdad, en vez de suponerlo.
                    limit=LIMIT * 8 + 1, timeout=READ_TIMEOUT,
                )
            except erpnext.ERPNextError:
                # No es un ranking vacío: es un ranking que no se pudo leer. Es
                # la misma distinción que `averageOrder`, un campo más allá.
                errors.append("products")
                renglones = None

    dentro = [x for x in pedidos if (_dia_de(x) or hoy) >= desde]
    total = sum(_monto(x.get("grand_total")) for x in dentro)
    por_dia: dict[str, dict] = {}
    por_cliente: dict[str, dict] = {}
    for x in dentro:
        dia = _dia_de(x)
        if dia:
            fila = por_dia.setdefault(
                dia.isoformat(), {"date": dia.isoformat(), "total": 0.0, "orders": 0})
            fila["total"] += _monto(x.get("grand_total"))
            fila["orders"] += 1
        cuenta = str(x.get("customer") or "")
        if cuenta:
            fila = por_cliente.setdefault(cuenta, {
                "id": cuenta, "name": str(x.get("customer_name") or cuenta),
                "orders": 0, "total": 0.0})
            fila["orders"] += 1
            fila["total"] += _monto(x.get("grand_total"))
    if renglones is not None and len(renglones) > LIMIT * 8:
        truncated.append("topProducts")
        renglones = renglones[:LIMIT * 8]
    por_producto: dict[str, dict] | None = None if renglones is None else {}
    for r in renglones or ():
        code = str(r.get("item_code") or "")
        if not code:
            continue
        fila = por_producto.setdefault(code, {
            "id": code, "name": str(r.get("item_name") or code),
            "quantity": 0.0, "total": 0.0})
        fila["quantity"] += _monto(r.get("qty"))
        fila["total"] += _monto(r.get("amount"))
    for fila in list(por_dia.values()) + list(por_cliente.values()) + list((por_producto or {}).values()):
        fila["total"] = round(fila["total"], 2)

    return {
        "currency": moneda,
        "since": desde.isoformat(),
        "until": hoy.isoformat(),
        "total": round(total, 2),
        "orders": len(dentro),
        # Sin pedidos el promedio NO es 0: es «no hay promedio». Un 0 se lee
        # «vendí y el ticket fue cero», que es una afirmación que nadie midió.
        "averageOrder": round(total / len(dentro), 2) if dentro else None,
        "daily": sorted(por_dia.values(), key=lambda f: f["date"]),
        "topProducts": None if por_producto is None
        else sorted(por_producto.values(), key=lambda f: -f["total"])[:10],
        "topCustomers": sorted(por_cliente.values(), key=lambda f: -f["total"])[:10],
        "errors": errors,
        "truncated": truncated,
    }


def advice() -> dict:
    """Lo que el dueño tendría que saber sin haber preguntado.

    `enabled` SE INFORMA Y NO FILTRA. `consejos.activo()` arranca apagado a
    propósito, pero lo que apaga es el ENVÍO: desde el 1/10/2026 Meta cobra los
    mensajes de servicio por unidad, y el tope de 3 por día está para eso. Leer
    los hallazgos en un panel que el dueño abrió él mismo no manda nada ni
    cuesta nada. Filtrar acá dejaría la pantalla vacía en la configuración de
    fábrica, o sea justo cuando más falta hace que muestre algo.

    `peso` NO SALE. Para `perdida` es plata y para `quiebre` son unidades: dos
    números que no se comparan entre sí, y ordenarlos juntos arma un ranking sin
    sentido. Se ordena acá DENTRO de cada clase —donde sí es comparable— y lo
    que viaja es el ORDEN de la lista.

    `sobre` es polimórfico, así que sale por duplicado a propósito: `about` es
    el texto que se muestra y `customerId`/`orderId`/`productId` es el enlace,
    exactamente uno de los tres, para que el panel no tenga que re-deducir el
    tipo del `kind`.
    """
    from datetime import UTC, datetime

    from app import consejos, erpnext, policy

    dia = policy._hoy_del_negocio()
    detectores = (
        (consejos.PERDIDA, "orderId", consejos.perdidas),
        (consejos.DORMIDO, "customerId", consejos.dormidos),
        (consejos.DEUDA, "customerId", consejos.deudas),
        (consejos.QUIEBRE, "productId", consejos.quiebres),
    )
    items: list[dict] = []
    errors: list[str] = []
    try:
        with erpnext.manager_scope():
            moneda = str(erpnext.get_doc(
                "Company", erpnext.default_company(), timeout=READ_TIMEOUT
            ).get("default_currency") or "")
    except erpnext.ERPNextError:
        moneda = None
        errors.append("currency")
    for clase, campo, detectar in detectores:
        try:
            hallados = detectar(dia)
        # Ningún detector levanta —lo prometen sus docstrings—, pero si un día
        # uno deja de cumplirlo el panel pierde UNA clase y no las cuatro, y el
        # nombre de la que se cayó sale en `errors` en vez de desaparecer.
        except Exception:
            errors.append(clase)
            continue
        for consejo in sorted(hallados, key=lambda c: -_monto(c.peso)):
            enlaces = {"customerId": None, "orderId": None, "productId": None}
            enlaces[campo] = str(consejo.sobre)
            items.append({
                "id": str(consejo.clave),
                "kind": str(consejo.clase),
                "title": str(consejo.titulo),
                "body": str(consejo.cuerpo),
                "about": str(consejo.sobre),
                "assumption": str(consejo.supuesto or ""),
                # `datos` tiene los números de cada detector, pero sus claves no
                # están verificadas por clase: prometer un `amount` sin haberlas
                # mirado sería inventar. El cuerpo ya trae la cifra en prosa.
                "amount": None,
                **enlaces,
            })
    return {
        "generatedAt": datetime.now(UTC).isoformat(),
        "enabled": consejos.activo(),
        "currency": moneda,
        "items": items,
        "errors": errors,
        "truncated": [],
    }


# ----------------------------------------------------------------- precios


def prices() -> dict:
    """Los precios de LISTA de lo que vendemos, y si se pueden cambiar hoy.

    Endpoint aparte y no un campo más del snapshot, por dos razones: el
    snapshot arma los productos desde `Bin` —o sea desde el depósito, que es lo
    que los acota a esta empresa— y un precio no es una existencia; y la
    pantalla de inventario ya tiene los productos, así que lo único que falta
    es el precio para cruzarlo por `id`.

    `canChange` es lo que le deja decir a la pantalla POR QUÉ no hay botón, en
    vez de no tenerlo y parecer roto. Arranca en false: `PRECIO_CAMBIO_MAX_PCT`
    viene en 0 —ningún precio se escribe— y ésa es la postura de fábrica, no un
    error de configuración.
    """
    from app import erpnext, limites, precios

    lista, moneda = precios.lista_y_moneda()
    try:
        banda = float(limites.vigente("PRECIO_CAMBIO_MAX_PCT") or 0)
    except Exception:
        # `vigente()` falla CERRADO —levanta si no puede leer el almacén— y acá
        # eso se traduce a la banda más angosta que hay. Un tope que no se pudo
        # leer no puede convertirse en permiso.
        banda = 0.0

    errors: list[str] = []
    truncated: list[str] = []
    filas: list[dict] | None = None
    if not (lista and moneda):
        # No es un fallo de lectura: es que esta instalación no tiene lista o
        # moneda de auto-confirmación, y sin las dos ningún precio que se
        # escriba lo mira nadie.
        errors.append("priceList")
    else:
        with erpnext.manager_scope():
            try:
                crudas = erpnext.get_list(
                    "Item Price",
                    filters=[["price_list", "=", lista], ["currency", "=", moneda],
                             ["selling", "=", 1]],
                    fields=["item_code", "price_list_rate", "uom"],
                    order_by="item_code asc", limit=LIMIT + 1,
                )
            except Exception:
                crudas = None
                errors.append("prices")
        if crudas is not None:
            if len(crudas) > LIMIT:
                truncated.append("prices")
            filas = [{
                "id": plain_text(fila.get("item_code")),
                "price": number(fila.get("price_list_rate")),
                "unit": plain_text(fila.get("uom")),
            } for fila in crudas[:LIMIT] if fila.get("item_code")]

    return {
        "priceList": lista or None, "currency": moneda or None,
        "bandPct": banda,
        "canChange": bool(lista and moneda and banda > 0),
        "items": filas, "errors": errors, "truncated": truncated,
    }


def cambiar_precio_desde_el_panel(item_code: str, valor: str, quien: str) -> dict:
    """Cambiar UN precio de lista desde el panel, por la misma puerta única.

    NO REIMPLEMENTA NADA. `precios.cambiar` es la definición de qué es cambiar
    un precio —la banda, el único cambio por producto por día reservado con NX,
    la unidad leída del producto, la relectura después de escribir y ahora el
    rastro durable— y acá sólo se le dice por qué canal entró.

    EL `manager_scope` VA ADENTRO DE ESTA FUNCIÓN, y eso no es estilo. El pool
    corre por `run_in_executor`, que —a diferencia de `asyncio.to_thread`— NO
    copia el contexto: el `ContextVar` de la credencial vuelve a su default, que
    es `customer`. Abrir el scope alrededor del `await`, en el router, no llega
    hasta acá, y el precio se escribiría con la credencial del agente de
    CLIENTES. Eso es fusionar dos de las tres identidades de la regla dura 2, en
    silencio y sin fallar: el rol de cliente tiene Create sobre Item Price.

    `ok` NO SE DEDUCE DEL TEXTO. `precios.cambiar` devuelve prosa para sus diez
    salidas, y parsearla ataría el panel al catálogo de idiomas. Se relee el
    precio y se compara con el pedido, que es la misma pregunta que el panel
    necesita contestar y la única que no puede mentir.
    """
    from app import erpnext, precios

    codigo = str(item_code or "").strip()
    try:
        pedido = float(str(valor).replace(",", "."))
    except (TypeError, ValueError):
        pedido = 0.0

    with erpnext.manager_scope():
        detalle = precios.cambiar(codigo, valor, quien, canal=precios.CANAL_PANEL)
        quedo = precios.precio_actual(codigo)

    return {
        "productId": codigo,
        "ok": bool(pedido > 0 and quedo is not None and abs(quedo - pedido) < 0.01),
        # `null` es «no se pudo leer», y no es 0: un precio que no se pudo
        # releer no es un precio de cero.
        "price": quedo,
        # Sin traducir, igual que el `detail` de confirmar y de proponer: es la
        # MISMA frase que sale por WhatsApp, y dos canales que explican el mismo
        # resultado con palabras distintas terminan discrepando.
        "detail": plain_text(detalle),
    }


# ------------------------------------------------- los ajustes del negocio


# CÓMO SE LLAMA CADA GRUPO PARA UNA PERSONA, y en qué orden se muestran. El
# orden no es cosmético: arriba va lo que el dueño toca durante la instalación
# —cómo se llama su negocio, cómo se llaman sus plantillas de Meta— y abajo los
# topes que deciden si se mueve plata. Mezclarlos en una sola lista hace que
# subir un techo parezca del mismo tamaño que corregir un horario.
GRUPOS_DEL_PANEL: tuple[tuple[str, str], ...] = (
    ("negocio", "Your business"),
    ("plantillas", "WhatsApp templates"),
    ("entrega", "Delivery and pickup"),
    ("limites", "Automatic confirmation"),
    ("idioma", "Language"),
)

# De dónde salió el valor, dicho para quien lee el panel. `origen` es del
# dominio y viaja en castellano desde app/limites.py; esto es la etiqueta.
_ORIGEN = {
    "dueño": "You set this",
    "arranque": "From the server file",
    "default": "Shipped default",
    "perdido": "Lost from the store",
}


def settings(mirando: str) -> dict:
    """Todo lo que el dueño puede cambiar, agrupado, y de dónde sale cada valor.

    `mirando` es el teléfono de quien mira, y hace falta para UNA cosa: decir si
    esa persona ya tiene un cambio esperando su código. La propuesta es por
    teléfono (`limites._clave_propuesta`), así que es suya y de nadie más — y
    sin mostrarla, pedir un segundo cambio pisaría el primero sin que se vea.

    NUNCA VIAJA EL CÓDIGO. `limites.pendiente()` lo saca a propósito; acá se
    usa esa función y no `_propuesta_viva` justamente por eso.

    POR QUÉ SE MUESTRA `source`. Un tope en 0 no es un error de configuración:
    es la postura de arranque que decidió el dueño (regla dura 4). Diciendo que
    el 0 viene del default y no de él, el panel puede mostrarlo sin pintarlo de
    rojo y sin ofrecer un número «recomendado» — que sería subirle un techo de
    seguridad por su cuenta.
    """
    from app import limites

    try:
        filas = limites.resumen(lengua="en")
    except Exception:
        # El almacén ilegible o el fusible armado. Es lo que el dueño MÁS
        # necesita ver, así que no puede salir como el 502 genérico de lectura.
        return {"groups": [], "pending": None,
                "problem": "The saved settings could not be read."}

    por_grupo: dict[str, list] = {clave: [] for clave, _ in GRUPOS_DEL_PANEL}
    for fila in filas:
        nombre = fila["nombre"]
        grupo = limites.grupo(nombre)
        if grupo not in por_grupo:
            continue
        defi = limites.TODOS[nombre]
        por_grupo[grupo].append({
            "id": nombre,
            "name": fila["alias"],
            "meaning": fila["significado"],
            "unit": fila["unidad"],
            "kind": defi.tipo,
            "optional": bool(defi.opcional),
            # El valor CRUDO y el mostrado son dos: el primero es lo que hay que
            # volver a mandar para no cambiar nada, el segundo es prosa.
            "value": plain_text(fila["valor"]),
            "display": plain_text(
                limites.mostrar(nombre, fila["valor"], en_idioma="en")
            ),
            "source": _ORIGEN.get(fila["origen"], fila["origen"]),
            "configured": fila["origen"] == "dueño",
            "problem": plain_text(fila["problema"]),
        })

    pendiente = None
    try:
        crudo = limites.pendiente(mirando) if mirando else None
    except Exception:
        crudo = None
    if crudo:
        pendiente = {
            "id": plain_text(crudo.get("limite")),
            "name": plain_text(crudo.get("alias")),
            "from": plain_text(crudo.get("anterior")),
            "to": plain_text(crudo.get("nuevo")),
        }

    return {
        "groups": [
            {"id": clave, "name": titulo, "settings": por_grupo[clave]}
            for clave, titulo in GRUPOS_DEL_PANEL
            if por_grupo[clave]
        ],
        "pending": pendiente,
        "problem": "",
    }


def proponer_ajuste(setting: str, valor: str, quien_pide: str) -> dict:
    """Deja UN cambio esperando el código de cuatro dígitos. No cambia nada.

    ACÁ NO HAY CONFIRMACIÓN, Y ES EL DISEÑO. No existe ningún endpoint que
    aplique el código, a propósito: el dueño lo confirma por WhatsApp, o sea en
    otro aparato y por otro canal. Eso es lo que la regla dura 3 pide de verdad
    —que el segundo paso sea FUERA DE BANDA—, y un panel que propusiera y
    confirmara en la misma pantalla lo colapsaría a uno solo: quien tiene el
    token tendría las dos mitades.

    Y de yapa saca un problema que no habría tenido arreglo barato:
    `limites.aplicar` no tiene contador de intentos ni bloqueo, y no los
    necesita mientras la única puerta sea un WhatsApp firmado por Meta desde un
    número del equipo. Publicarla por HTTP convertía 9000 valores con diez
    minutos de vida en algo que se rompe a fuerza bruta en segundos.

    NO SE DEVUELVE `limites.proponer()`. Ese dict TRAE el código adentro, y
    devolverlo lo publica en el navegador, en cualquier proxy y en cualquier
    log. Lo que se devuelve es la prosa de `ajustes.preparar`, que es la tercera
    llamadora de la ÚNICA implementación —las otras dos son la herramienta de
    gerencia y el ruteo determinista— y no re-deriva ni la propuesta, ni la
    huella, ni el vencimiento, ni el mensaje.
    """
    from app import ajustes, idioma, limites

    lengua = idioma.gerencia()
    try:
        defi = limites.definicion(setting)
    except limites.LimiteError as exc:
        return {"ok": False, "pending": False,
                "detail": limites.motivo(exc, lengua)}

    texto = ajustes.preparar(defi.nombre, valor, quien_pide)

    # SI QUEDÓ ESPERANDO, SE PREGUNTA; no se deduce del texto. `preparar`
    # devuelve prosa para las cuatro salidas —preparado, repetido, valor
    # inválido y «no te pude mandar el código»— y leer cuál es parseando esa
    # frase sería atar el panel al catálogo de idiomas. El estado real es si hay
    # una propuesta viva para este teléfono, y eso se pregunta.
    try:
        espera = limites.pendiente(quien_pide) or {}
    except Exception:
        espera = {}
    quedo = str(espera.get("limite") or "") == defi.nombre
    return {
        "ok": quedo,
        "pending": quedo,
        "setting": defi.nombre,
        "name": defi.alias[0],
        # Sin traducir, igual que el `detail` de `confirmar_desde_el_panel` y
        # por el mismo motivo: es LA MISMA frase que le llega por WhatsApp, y
        # dos canales que explican el mismo resultado con palabras distintas
        # terminan discrepando justo cuando hay que decidir qué hacer.
        "detail": plain_text(texto),
    }


def install_dashboard(application) -> None:
    """Mount exactly the same application in production and HTTP integration tests."""
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    application.mount("/api/dashboard", DashboardAPI())
    application.mount(
        "/dashboard", StaticFiles(directory=Path(__file__).parent / "dashboard_ui", html=True),
        name="dashboard",
    )
