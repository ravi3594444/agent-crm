"""Cambiar un precio de lista por WhatsApp, automáticamente y sin confirmación.

La lógica vive acá y no en la herramienta porque hay UNA sola definición de qué
es cambiar un precio; `app/tools/crm.py::cambiar_precio` es la puerta del
modelo y no repite nada de esto.

LO QUE EL DUEÑO DECIDIÓ, Y LO QUE NO ES SUYO DECIDIR
----------------------------------------------------
Suyo: que lo haga el modelo y que no haya un código por cambio. Textual: «no
one can confirm everytime i need automated». Escribir el precio de LISTA no es
inocente —`policy._precio_estandar` auto-confirma cuando el renglón coincide
con la lista, así que quien escribe la lista influye en lo que se confirma
solo—, se lo dijimos, y es su negocio.

No suyo, porque no es una preferencia sino cómo se comporta un modelo:

1. El modelo aporta UN valor, el número. `price_list` y `currency` salen de
   `policy` —las mismas constantes por las que filtra `_precio_estandar`, así
   que el precio escrito y el precio buscado no pueden discrepar— y la unidad
   del `stock_uom` del producto, leído de ERPNext en el momento. Un precio al
   que le falte una de las tres queda escrito y no lo mira nadie: no da error,
   simplemente deja de auto-confirmarse.

2. UN cambio por producto por día. La banda acota UN salto, no una SERIE:
   quince por ciento cinco veces seguidas es el doble, y el modelo puede llamar
   cinco veces en el mismo turno sin que nadie lo note. El techo real es la
   banda POR DÍA, con un dueño que ve cada respuesta.

3. Se relee después de escribir. «Listo» por haber mandado el PUT contesta otra
   pregunta, y las dos se diferencian justo cuando importa.

Con `PRECIO_CAMBIO_MAX_PCT` en 0 —el default— no se escribe ningún precio.
"""
from __future__ import annotations

from app import erpnext, idioma


def cambiar(producto: object, precio: object, telefono: str) -> str:
    """Escribe el precio de lista de UN producto. Devuelve qué pasó, en prosa.

    El llamador —y hoy es sólo el router de app/main.py— ya comprobó que el
    número está en TELEFONOS_EQUIPO. No se vuelve a comprobar acá porque esta
    función no es alcanzable desde ninguna otra puerta; el día que lo sea, la
    comprobación va con ella.
    """
    from app import limites, policy

    lengua = idioma.gerencia()
    try:
        banda = float(limites.vigente("PRECIO_CAMBIO_MAX_PCT") or 0)
    except (TypeError, ValueError):
        banda = 0.0
    if banda <= 0:
        return idioma.t("crm.precio_banda_cerrada", lengua)

    # Lista y moneda salen de `policy`: son las MISMAS constantes por las que
    # filtra `_precio_estandar`, así que el precio escrito y el precio buscado
    # no pueden discrepar. Sin ellas no se escribe nada — un Item Price sin
    # lista o sin moneda queda puesto y no lo mira nadie.
    lista = str(getattr(policy, "PRICE_LIST", "") or "").strip()
    moneda = str(getattr(policy, "CURRENCY", "") or "").strip()
    if not lista or not moneda:
        return idioma.t("crm.precio_sin_lista", lengua)

    codigo = str(producto or "").strip()
    if not codigo:
        return idioma.t("crm.precio_sin_producto", lengua)
    try:
        nuevo = float(str(precio).replace(",", "."))
    except (TypeError, ValueError):
        return idioma.t("crm.precio_sin_producto", lengua)
    if nuevo <= 0:
        return idioma.t("crm.precio_sin_producto", lengua)

    try:
        ficha = erpnext.get_doc("Item", codigo)
    except erpnext.ERPNextError as exc:
        return idioma.t("crm.precio_error", lengua, exc=exc)
    # La unidad sale del producto, no del mensaje: `_precio_estandar` compara
    # `uom` contra `stock_uom` y descarta el renglón si difieren, así que un
    # precio escrito en otra unidad es un precio que no auto-confirma nada.
    unidad = str(ficha.get("stock_uom") or "").strip()
    if not unidad:
        return idioma.t("crm.precio_sin_unidad", lengua, item_code=codigo)

    filtro = [
        ["item_code", "=", codigo], ["price_list", "=", lista],
        ["currency", "=", moneda], ["uom", "=", unidad], ["selling", "=", 1],
    ]
    try:
        actuales = erpnext.get_list(
            "Item Price", filters=filtro, fields=["price_list_rate"], limit=2
        )
    except erpnext.ERPNextError as exc:
        return idioma.t("crm.precio_error", lengua, exc=exc)
    anterior = 0.0
    if actuales:
        try:
            anterior = float(actuales[0].get("price_list_rate") or 0)
        except (TypeError, ValueError):
            anterior = 0.0
    if anterior <= 0:
        # Sin precio previo no hay contra qué medir la banda. El primero lo
        # siembra `deploy/`, con las tres columnas puestas.
        return idioma.t("crm.precio_sin_anterior", lengua, item_code=codigo)

    # UN CAMBIO POR PRODUCTO POR DÍA. La banda acota UN salto; no acota una
    # SERIE. Quince por ciento cinco veces seguidas es el doble, y el modelo
    # puede llamar cinco veces en el mismo turno sin que nadie lo note. El tope
    # diario es lo que convierte la banda en un límite de verdad: el techo real
    # pasa a ser la banda POR DÍA, con un dueño que ve cada respuesta.
    #
    # Se reserva ANTES de escribir y con NX, así que dos workers a la vez no
    # pasan los dos. Si Redis no contesta NO se escribe: un tope que falla
    # abierto no es un tope, y lo que está del otro lado es plata.
    from app import locks
    reserva = f"plus-agent:precio:{codigo}"
    try:
        primero = locks.conexion().set(reserva, str(nuevo), nx=True, ex=86400)
    except Exception:
        return idioma.t("crm.precio_error", lengua, exc="no pude coordinar el cambio")
    if not primero:
        return idioma.t("crm.precio_ya_hoy", lengua, item_code=codigo)

    movimiento = abs(nuevo - anterior) / anterior * 100.0
    if movimiento > banda + 0.0001:
        # Se suelta: el cambio no se hizo, así que no gastó el día.
        try:
            locks.conexion().delete(reserva)
        except Exception:
            pass
        return idioma.t(
            "crm.precio_fuera_de_banda", lengua, item_code=codigo,
            antes=f"{anterior:g}", ahora=f"{nuevo:g}",
            movimiento=f"{movimiento:.1f}", banda=f"{banda:g}",
        )

    try:
        erpnext.escribir_precio_de_lista(
            item_code=codigo, price_list=lista, currency=moneda,
            uom=unidad, rate=nuevo,
        )
    except erpnext.ERPNextError as exc:
        return idioma.t("crm.precio_error", lengua, exc=exc)

    # Se relee con el MISMO filtro por el que va a buscar la auto-confirmación.
    # Contestar «listo» por haber mandado el PUT es contestar otra pregunta, y
    # las dos se diferencian justo cuando importa.
    leido = 0.0
    try:
        quedaron = erpnext.get_list(
            "Item Price", filters=filtro, fields=["price_list_rate"], limit=2
        )
        if quedaron:
            leido = float(quedaron[0].get("price_list_rate") or 0)
    except (erpnext.ERPNextError, TypeError, ValueError):
        leido = 0.0
    if abs(leido - nuevo) >= 0.01:
        return idioma.t("crm.precio_no_verificado", lengua, item_code=codigo)
    return idioma.t(
        "crm.precio_hecho", lengua, item_code=codigo,
        antes=f"{anterior:g}", ahora=f"{leido:g}", unidad=unidad,
    )
