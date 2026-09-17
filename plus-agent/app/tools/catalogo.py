"""Read-only customer/management tools with server-enforced authorization.

Los parámetros llevan `description`: es lo que el modelo lee para decidir QUÉ
mandarle a cada herramienta. Sin eso veía `item_code` pelado y le pasaba las
palabras del cliente («muzzarella») donde va el código del catálogo.
"""
import os
from datetime import date
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field

from app import erpnext, idioma, inventario, policy
from app.formato import pesos
from app.runtime_context import RuntimeContextError, actor_context, require_customer

# CUÁNTOS PRODUCTOS SE LE MUESTRAN AL MODELO CUANDO LA BÚSQUEDA NO ENCUENTRA
# NADA. Trece en este negocio; el tope está por el cliente que tenga doscientos.
MAX_CATALOGO_SUGERIDO = 25

# CUÁNTOS ENTRAN EN EL PROMPT DE CADA MENSAJE. Más alto que el de arriba porque
# acá el objetivo es que ESTÉ COMPLETO: lo que no entra es lo único sobre lo que
# el modelo puede volver a equivocarse.
MAX_CATALOGO_PROMPT = 60

# El bloque se arma UNA vez y vale un minuto. `prompt_clientes` se rearma en
# CADA vuelta del react loop —tres por mensaje— y sin esto serían tres consultas
# a ERPNext para contestar un «hola». Un minuto sigue siendo en vivo para un
# catálogo: un producto que se da de alta ahora aparece en el mensaje siguiente,
# no al otro día.
CACHE_CATALOGO_SEGUNDOS = 60
_cache_catalogo: tuple[float, str] | None = None


def _en_una_linea(texto: object) -> str:
    """Un nombre de producto que no puede salirse de su renglón.

    Esto entra en el MENSAJE DE SISTEMA, que es el canal de las instrucciones, y
    un `item_name` con un salto de línea adentro deja de ser un ítem de la lista
    y pasa a ser una línea más del prompt. Hoy no es una puerta de un cliente
    —los Items los crea el dueño o el seed, y ninguna herramienta del agente de
    clientes escribe uno—, y por eso esto es un cierre barato y no un rediseño:
    el día que exista un camino donde alguien de afuera proponga un nombre, el
    canal ya está cerrado en vez de haber que acordarse.
    """
    plano = " ".join(str(texto or "").split())
    return plano[:120]


def bloque_para_prompt() -> str:
    """El catálogo entero para el prompt del cliente. `""` si no se pudo leer.

    POR QUÉ ESTO EXISTE Y NO ALCANZABA CON LA HERRAMIENTA. Medido en el VM, tres
    turnos seguidos preguntando por queso: `herramientas=0x0.0s` en los tres. El
    modelo contestó «only muzzarella and reggianito» sin abrir el catálogo, y
    después se copió a sí mismo del historial. Arreglar lo que `buscar_producto`
    CONTESTA no sirve cuando no se lo llama, y la regla del prompt que se lo
    ordena tampoco alcanzó: un prompt inclina, no obliga. Con la lista acá
    adentro la pregunta deja de depender de que decida mirar.

    VACÍO SI FALLA, y esto es lo único que no se puede equivocar: un ERPNext
    caído tiene que hacer DESAPARECER la sección, nunca dejar un encabezado con
    cero productos debajo. «Esto es lo que vendemos: (nada)» es la misma mentira
    que todo esto vino a arreglar, escrita por nosotros en vez de por el modelo.
    """
    global _cache_catalogo
    import time

    ahora = time.monotonic()
    if _cache_catalogo and ahora - _cache_catalogo[0] < CACHE_CATALOGO_SEGUNDOS:
        return _cache_catalogo[1]
    try:
        items = erpnext.get_list(
            "Item",
            filters=[["disabled", "=", 0]],
            fields=["item_name", "stock_uom"],
            limit=MAX_CATALOGO_PROMPT + 1,
        )
    except erpnext.ERPNextError:
        return ""
    nombres = [i for i in items if i.get("item_name")]
    if not nombres:
        return ""
    hay_mas = len(nombres) > MAX_CATALOGO_PROMPT
    nombres = nombres[:MAX_CATALOGO_PROMPT]
    lineas = "\n".join(
        f"- {_en_una_linea(i['item_name'])} (se vende por {_en_una_linea(i['stock_uom'])})"
        for i in nombres
    )
    cola = (
        "\nY HAY MÁS que no entran en esta lista: si te piden algo que no está "
        "acá, buscalo con buscar_producto ANTES de decir que no lo tenemos."
        if hay_mas
        else ""
    )
    bloque = (
        "LO QUE VENDEMOS\n"
        "Éstos son los productos que EXISTEN, al día de este mensaje. Si te "
        "piden algo que no figura con ese nombre, fijate primero si es alguno de "
        "éstos dicho de otra manera —en otro idioma, con el nombre de la "
        "categoría, con una marca—: «cheese» es queso. NO contestes que no lo "
        "tenemos sin haber mirado esta lista.\n"
        "Los PRECIOS y el STOCK no están acá y no se adivinan: eso sale de "
        "buscar_producto y consultar_stock, como siempre.\n"
        f"{lineas}{cola}"
    )
    _cache_catalogo = (ahora, bloque)
    return bloque


def _sin_coincidencia(consulta: str) -> str:
    """La búsqueda no encontró nada. Eso NO significa que no lo tengamos.

    `item_name` se busca con un LIKE, así que la coincidencia es por
    subcadena y en el idioma en que está cargado el catálogo. Medido en una
    conversación real: el cliente escribió «cheese», el catálogo dice «Queso
    cremoso», no hubo match — y el agente le contestó «we don't have cheese».
    Era falso, y era además el ÚNICO producto con stock cargado en el sistema.

    El texto viejo —«preguntale cómo lo llama él, u ofrecele lo más cercano»—
    dejaba la conclusión en manos del modelo, y el modelo concluyó una
    ausencia. Así que el catálogo viaja ACÁ ADENTRO: con la lista a la vista no
    tiene que recordar nada ni traducir nada, y la prohibición de decir que no
    lo tenemos es explícita y no una sugerencia.

    No se nombran precios ni stock: eso sigue saliendo de una búsqueda con
    coincidencia, que es la única que los mira.
    """
    try:
        catalogo = erpnext.get_list(
            "Item",
            filters=[["disabled", "=", 0]],
            fields=["item_name", "stock_uom"],
            limit=MAX_CATALOGO_SUGERIDO,
        )
    except erpnext.ERPNextError:
        # UNA CAÍDA NO ES UN CATÁLOGO VACÍO, y hasta acá las dos cosas caían en
        # el mismo texto: «preguntale cómo lo llama él». O sea que un ERPNext
        # que no contesta salía al cliente como una charla sobre el nombre del
        # producto, que es tapar una caída conversando. La primera búsqueda —la
        # de `buscar_producto`— sí anduvo y no matcheó, así que lo único cierto
        # es que ningún nombre coincide; si el sistema se cayó entre esa
        # consulta y ésta, tampoco hay con qué ofrecer alternativas, y eso hay
        # que decirlo en vez de completarlo.
        return (
            f"Ningún producto se llama '{consulta}', y al intentar leer el "
            "catálogo completo para ofrecerle algo parecido EL SISTEMA FALLÓ. "
            "NO le digas que no lo tenemos y NO le prometas nada: no pudiste "
            "verificar. Decile que en este momento no podés confirmar qué hay "
            "y que lo revisás enseguida; si insiste, usá escalar_a_humano."
        )
    if not catalogo:
        return (
            f"No encontré nada parecido a '{consulta}' en el catálogo. "
            "Preguntale cómo lo llama él. NO le digas que no lo tenemos: puede "
            "estar cargado con otro nombre."
        )
    lineas = "\n".join(
        f"- {item['item_name']} (se vende por {item['stock_uom']})"
        for item in catalogo
        if item.get("item_name")
    )
    return (
        f"Ninguno de los productos se llama '{consulta}'. ESO NO QUIERE DECIR QUE NO "
        "LO TENGAMOS: el catálogo está cargado en un idioma y el cliente puede "
        "haberlo pedido en otro, o con el nombre de la categoría en vez del "
        "producto. NO le contestes que no tenemos eso.\n"
        "Esto es PARTE del catálogo, no todo —son los primeros que trajo la "
        "consulta—: ofrecele lo que se parezca a lo que pidió, con su nombre tal "
        "cual figura acá, y si no está en esta lista NO concluyas que no "
        "existe.\n"
        f"{lineas}"
    )


@tool
def buscar_producto(
    consulta: Annotated[
        str,
        Field(description="Lo que nombró el cliente, con SUS palabras "
                          "(«muzzarella», «leche entera»). Nunca un código."),
    ],
) -> str:
    """Busca productos del catálogo por nombre y muestra su unidad exacta."""
    items = erpnext.get_list(
        "Item",
        filters=[["item_name", "like", f"%{consulta}%"], ["disabled", "=", 0]],
        fields=["item_code", "item_name", "stock_uom", "description"],
        limit=8,
    )
    if not items:
        return _sin_coincidencia(consulta)

    price_list = os.getenv("AUTO_CONFIRM_PRICE_LIST", "").strip()
    currency = os.getenv("AUTO_CONFIRM_CURRENCY", "").strip()
    try:
        today = policy._hoy_del_negocio()
    except erpnext.ERPNextError:
        today = None
    out = []
    for item in items:
        filters = [["item_code", "=", item["item_code"]], ["selling", "=", 1]]
        if price_list:
            filters.append(["price_list", "=", price_list])
        if currency:
            filters.append(["currency", "=", currency])
        prices = (
            erpnext.get_list(
                "Item Price",
                filters=filters,
                fields=[
                    "price_list_rate",
                    "price_list",
                    "currency",
                    "uom",
                    "valid_from",
                    "valid_upto",
                    "customer",
                    "batch_no",
                ],
                limit=20,
            )
            if price_list and currency and today is not None
            else []
        )
        matching = next(
            (
                price
                for price in prices
                if _catalog_price_is_valid(
                    price,
                    str(item.get("stock_uom") or ""),
                    price_list,
                    currency,
                    today,
                )
            ),
            None,
        )
        price_text = (
            f"{matching['currency']} {matching['price_list_rate']}"
            if matching
            else idioma.t("precio.a_confirmar")
        )
        # Sin viñeta: si el modelo copia la lista tal cual, app/formato.py
        # convierte el guión en «•» y el cliente recibe una lista de sistema.
        out.append(
            f"{item['item_name']} ({item['item_code']}) — {price_text} "
            f"por {item['stock_uom']}"
        )
    return "\n".join(out)


def _catalog_price_is_valid(
    price: dict,
    uom: str,
    price_list: str,
    currency: str,
    today: date,
) -> bool:
    if str(price.get("price_list") or "") != price_list:
        return False
    if str(price.get("currency") or "") != currency:
        return False
    if str(price.get("uom") or "") != uom:
        return False
    if price.get("customer") or price.get("batch_no"):
        return False
    try:
        valid_from = (
            date.fromisoformat(str(price["valid_from"]))
            if price.get("valid_from")
            else date.min
        )
        valid_upto = (
            date.fromisoformat(str(price["valid_upto"]))
            if price.get("valid_upto")
            else date.max
        )
    except ValueError:
        return False
    return valid_from <= today <= valid_upto


@tool
def consultar_stock(
    item_code: Annotated[
        str,
        Field(description="Código EXACTO del catálogo, tal como lo devolvió "
                          "buscar_producto. No las palabras del cliente."),
    ],
) -> str:
    """Consulta un nivel orientativo en el depósito de preparación."""
    try:
        warehouse = erpnext.default_warehouse()
    except erpnext.ERPNextError:
        return (
            f"No pude verificar el depósito de preparación para {item_code}. "
            "No confirmes disponibilidad."
        )
    # Un producto que no se inventaría no tiene conteo que vencer, y contestar
    # «nadie lo contó» sobre los tornillos es no contestar. Se dice que hay, sin
    # número: el número sería inventado, y la regla 1 de `prompts.py` no
    # distingue entre inventar un precio e inventar una existencia.
    if inventario.sin_seguimiento(item_code):
        return (
            f"{item_code}: es un producto que no llevamos contado, siempre "
            "tenemos. Podés tomarle el pedido. NO le des un número de "
            "existencias: no hay ninguno que sea cierto."
        )
    # Trust is earned per product by a confirmed count, and it expires.
    fresco, sin_confianza = inventario.confiable(item_code, warehouse)
    if not fresco:
        return (
            f"{item_code}: {sin_confianza}. No confirmes disponibilidad. Si lo "
            "pide igual, tomale el pedido y decile que el equipo se lo confirma "
            "en un rato; no le hables de revisiones ni de estados."
        )
    try:
        bins = erpnext.get_list(
            "Bin",
            filters=[
                ["item_code", "=", item_code],
                ["warehouse", "=", warehouse],
            ],
            fields=["warehouse", "actual_qty", "reserved_qty"],
            limit=10,
        )
    except erpnext.ERPNextError:
        return (
            f"No pude verificar el depósito de preparación para {item_code}. "
            "No confirmes disponibilidad."
        )
    if not bins:
        return (
            f"Sin registro de stock para {item_code} en el depósito de preparación. "
            "No confirmes disponibilidad."
        )

    available = sum(
        float(row.get("actual_qty") or 0) - float(row.get("reserved_qty") or 0)
        for row in bins
    )
    try:
        # The same deduction the auto-confirmation rule makes. A draft holds
        # units ERPNext has not reserved yet, so on Bin alone this tool
        # answered "hay stock" for milk another customer is already waiting
        # for — and the customer heard that as a promise.
        available -= policy.comprometido_en_borradores(item_code, warehouse)
    except erpnext.ERPNextError:
        return (
            f"No pude verificar cuánto de {item_code} ya está comprometido. "
            "No confirmes disponibilidad."
        )
    buffer = float(os.getenv("STOCK_BUFFER_PCT", "20")) / 100.0
    if buffer < 0 or buffer >= 1:
        return f"{item_code}: configuración de stock inválida. No confirmes disponibilidad."
    safe = available * (1 - buffer)
    if safe <= 0:
        return (
            f"{item_code}: SIN STOCK. Decile que de eso no tenés ahora y "
            "ofrecele una alternativa concreta del catálogo."
        )
    if safe < float(os.getenv("STOCK_POCO", "20")):
        return (
            f"{item_code}: POCO STOCK. Tomale el pedido sin prometer la cantidad: "
            "decile que te confirmás cuánto hay y que el equipo se lo cierra en "
            "un rato."
        )
    return (
        f"{item_code}: stock registrado. La cantidad exacta se vuelve a validar "
        "al crear y antes de confirmar el pedido, así que no se la prometas: "
        "seguí con el pedido sin anunciar cuánto hay."
    )


@tool
def estado_pedido(
    numero_pedido: Annotated[
        str,
        Field(description="El número real del pedido, como se lo dio el "
                          "sistema (SAL-ORD-2026-00042). Si no lo tenés, "
                          "pedíselo: no lo inventes."),
    ],
    config: RunnableConfig,
) -> str:
    """Consulta un pedido; clientes solo pueden ver pedidos de su propia cuenta."""
    try:
        actor = actor_context(config)
    except RuntimeContextError:
        return "No pude autorizar la consulta del pedido."
    try:
        order = erpnext.get_doc("Sales Order", numero_pedido)
    except erpnext.ERPNextError:
        return f"No encontré el pedido {numero_pedido}."
    # `gerencia_verificada`, no `is_management`: el alcance lo pone el webhook,
    # pero leer el pedido de CUALQUIER cliente lo habilita únicamente un
    # teléfono que sigue estando en la lista del equipo.
    if not actor.gerencia_verificada and (
        not actor.customer_code or order.get("customer") != actor.customer_code
    ):
        # Deliberately indistinguishable from a missing order to prevent ID
        # enumeration across customer accounts.
        return f"No encontré el pedido {numero_pedido}."
    states = {
        0: "anotado, todavía sin confirmar: lo confirma el equipo (al cliente "
           "decíselo así, sin la palabra borrador)",
        1: "confirmado",
        2: "cancelado",
    }
    return (
        f"Pedido {order['name']}: "
        f"{states.get(order.get('docstatus'), 'desconocido')}. "
        f"Total: {order.get('currency', '')} {order.get('grand_total', 0)}. "
        f"Fecha pedida: {order.get('delivery_date', 'a confirmar')}"
        " (es la que pidió, no una promesa: no la des por cierta si el pedido "
        "todavía no está confirmado)."
    )


@tool
def pedido_habitual(config: RunnableConfig) -> str:
    """Devuelve el último pedido confirmado del cliente autenticado."""
    try:
        actor = require_customer(config)
    except RuntimeContextError:
        return "No pude identificar una cuenta de cliente registrada."
    orders = erpnext.get_list(
        "Sales Order",
        filters=[["customer", "=", actor.customer_code], ["docstatus", "=", 1]],
        fields=["name"],
        limit=1,
        # SIN esto, «el último pedido» era el último MODIFICADO: Frappe ordena
        # por `modified desc` cuando nadie le dice otra cosa, y a un pedido
        # viejo lo toca cualquier cosa —un comentario, un cambio de estado, una
        # cancelación— mucho después de hecho. Así que a un cliente que pedía
        # «lo de siempre» se le podía ofrecer un pedido de hace meses, CON SU
        # FECHA, que es lo que esta misma respuesta imprime dos líneas abajo.
        # `creation` desempata dos pedidos del mismo día.
        order_by="transaction_date desc, creation desc",
    )
    if not orders:
        return "Esta cuenta no tiene pedidos anteriores confirmados."
    order = erpnext.get_doc("Sales Order", orders[0]["name"])
    lines = "\n".join(
        f"  · {float(item['qty']):g} {item.get('uom') or item.get('stock_uom')} "
        f"de {item.get('item_name') or item['item_code']}"
        for item in order.get("items", [])
    )
    return (
        f"Último pedido ({order['name']}, {order.get('transaction_date')}):\n{lines}\n"
        f"Total {pesos(order.get('grand_total', 0))}. Preguntale en UNA sola frase "
        "si quiere lo mismo y para qué día; no le repitas la lista salvo que la "
        "pida. Con esa respuesta ya tenés los cuatro datos para crear_pedido."
    )
