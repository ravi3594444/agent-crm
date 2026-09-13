"""Seed an empty ERPNext with a plausible dairy business, in Spanish or English.

WHY YOU NEED THIS
The client is 100% on paper. There is no catalog to import, no customer list,
nothing. You cannot demo a WhatsApp sales agent against an empty database —
the bot will just say "no encontre ese producto" to everything.

    python deploy/seed_dairy.py                 # el de siempre, en español
    python deploy/seed_dairy.py --dataset en    # el mismo negocio, en inglés
    SEED_DATASET=en python deploy/seed_dairy.py # lo mismo, por entorno

TWO DATASETS, SAME SHAPE. The English one exists because a demo is the product:
an English-speaking prospect watching the bot answer "Leche entera sachet 1 L"
is watching somebody else's product. Same thirteen items, same seven customers,
same quantities and the same relative prices — only the words change, so a
scenario written against one walks the other.

The Spanish one is the DEFAULT and stays exactly as it was: an existing
deployment that runs this script again seeds what it seeded before.

PRICES ARE PLACEHOLDERS IN BOTH. Argentine prices move fast and the dollar ones
are round numbers, not a price list - do not show either to the client as if
they were real. Ask for his actual prices.
"""
import argparse
import os
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import erpnext  # noqa: E402

# La lista de precios que se siembra. Es la misma que `AUTO_CONFIRM_PRICE_LIST`
# apunta en un despliegue normal, y por eso importa en qué MONEDA está: ver
# `_revisar_moneda`.
LISTA_DE_PRECIOS = "Standard Selling"

GRUPO = "Lacteos"
GRUPOS_CLIENTE = ("Comercio", "Gastronomia")
UNIDADES = ("Unidad", "Kg")
MONEDA = "ARS"

PRODUCTOS = [
    ("LEC-ENT-1L",  "Leche entera sachet 1 L",     "Unidad",  1200),
    ("LEC-DES-1L",  "Leche descremada sachet 1 L", "Unidad",  1250),
    ("LEC-BOT-1L",  "Leche entera botella 1 L",    "Unidad",  1650),
    ("YOG-BEB-1L",  "Yogur bebible frutilla 1 L",  "Unidad",  1900),
    ("YOG-FIR-190", "Yogur firme vainilla 190 g",  "Unidad",   650),
    ("QUE-CRE",     "Queso cremoso",               "Kg",      9800),
    ("QUE-MUZ",     "Queso muzzarella",            "Kg",     11500),
    ("QUE-PSA",     "Queso port salut",            "Kg",     10900),
    ("RIC-FRE",     "Ricota fresca",               "Kg",      6200),
    ("MAN-200",     "Manteca 200 g",               "Unidad",  2400),
    ("DDL-400",     "Dulce de leche 400 g",        "Unidad",  2100),
    ("CRE-200",     "Crema de leche 200 ml",       "Unidad",  1400),
    ("QUE-RAL-100", "Queso rallado 100 g",         "Unidad",  1300),
]

CLIENTES = [
    ("Almacen Don Jose",      "+5493511111111", "Comercio"),
    ("Kiosco La Esquina",     "+5493512222222", "Comercio"),
    ("Panaderia San Martin",  "+5493513333333", "Comercio"),
    ("Rotiseria El Fogon",    "+5493514444444", "Gastronomia"),
    ("Pizzeria Napoli",       "+5493515555555", "Gastronomia"),
    ("Supermercado Yrigoyen", "+5493516666666", "Comercio"),
    ("Cafe Belgrano",         "+5493517777777", "Gastronomia"),
]

STOCK_INICIAL = {
    "LEC-ENT-1L": 400, "LEC-DES-1L": 250, "LEC-BOT-1L": 180,
    "YOG-BEB-1L": 120, "YOG-FIR-190": 300,
    "QUE-CRE": 45, "QUE-MUZ": 60, "QUE-PSA": 30, "RIC-FRE": 18,
    "MAN-200": 90, "DDL-400": 140, "CRE-200": 70, "QUE-RAL-100": 110,
}


# --------------------------------------------------------------------------
# EL MISMO NEGOCIO, EN INGLÉS. Trece productos, siete clientes y las mismas
# cantidades: lo único que cambia son las palabras y la escala del precio.
#
# LOS CÓDIGOS TAMBIÉN CAMBIAN, y no es cosmético: el código del producto SALE
# POR WHATSAPP. El aviso de un conteo de stock dice «Count of QUE-CRE», así que
# un catálogo en inglés con códigos en español le muestra al prospecto la
# palabra que el resto de la demo evita.
#
# Los precios están en dólares redondos —la misma relación entre productos,
# dividida por mil— porque un almacén que cobra $11.500 por un kilo de
# mozzarella en una demo en dólares no es una demo, es una distracción. Siguen
# siendo inventados, igual que los otros.
GRUPO_EN = "Dairy"
GRUPOS_CLIENTE_EN = ("Retail", "Food service")
UNIDADES_EN = ("Unit", "Kg")
MONEDA_EN = "USD"

PRODUCTOS_EN = [
    ("MILK-WHL-1L", "Whole milk pouch 1 L",          "Unit",  1.20),
    ("MILK-SKM-1L", "Skim milk pouch 1 L",           "Unit",  1.25),
    ("MILK-BTL-1L", "Whole milk bottle 1 L",         "Unit",  1.65),
    ("YOG-DRK-1L",  "Drinking yoghurt strawberry 1 L", "Unit", 1.90),
    ("YOG-SET-190", "Set yoghurt vanilla 190 g",     "Unit",  0.65),
    ("CHE-CRM",     "Cream cheese",                  "Kg",    9.80),
    ("CHE-MOZ",     "Mozzarella",                    "Kg",   11.50),
    ("CHE-PSL",     "Port salut cheese",             "Kg",   10.90),
    ("RIC-FRS",     "Fresh ricotta",                 "Kg",    6.20),
    ("BUT-200",     "Butter 200 g",                  "Unit",  2.40),
    ("CAR-400",     "Milk caramel spread 400 g",     "Unit",  2.10),
    ("CRM-200",     "Single cream 200 ml",           "Unit",  1.40),
    ("CHE-GRT-100", "Grated cheese 100 g",           "Unit",  1.30),
]

# Números del rango 555, que es el que existe para no ser el teléfono de
# nadie. Los argentinos de arriba cumplen lo mismo con un 11111111.
CLIENTES_EN = [
    ("Riverside Grocery",     "+15550101001", "Retail"),
    ("Corner Market",         "+15550202002", "Retail"),
    ("Main Street Bakery",    "+15550303003", "Retail"),
    ("Oak Street Diner",      "+15550404004", "Food service"),
    ("Hilltop Pizzeria",      "+15550505005", "Food service"),
    ("Lakeside Supermarket",  "+15550606006", "Retail"),
    ("Park Avenue Cafe",      "+15550707007", "Food service"),
]

STOCK_INICIAL_EN = {
    "MILK-WHL-1L": 400, "MILK-SKM-1L": 250, "MILK-BTL-1L": 180,
    "YOG-DRK-1L": 120, "YOG-SET-190": 300,
    "CHE-CRM": 45, "CHE-MOZ": 60, "CHE-PSL": 30, "RIC-FRS": 18,
    "BUT-200": 90, "CAR-400": 140, "CRM-200": 70, "CHE-GRT-100": 110,
}


@dataclass(frozen=True)
class Datos:
    """Un catálogo sembrable. Los dos tienen exactamente la misma forma."""

    nombre: str
    grupo: str
    grupos_cliente: tuple[str, ...]
    unidades: tuple[str, ...]
    productos: list
    clientes: list
    stock: dict
    # LA MONEDA DE ESTOS PRECIOS, y es un campo y no un detalle: 1.20 y 1200 son
    # el mismo producto en dos monedas, y un Item Price sin `currency` hereda la
    # de la lista de precios. Sembrar el catálogo en dólares contra una lista en
    # pesos escribía «1,20 ARS» por litro de leche: la pantalla muestra $1.20 con
    # LOCALE=en_US y parece bien, mientras los libros dicen un peso veinte. Y
    # AUTO_CONFIRM_MAX es un monto en pesos, así que un catálogo mil veces más
    # barato vuelve sin sentido la aritmética de TODOS los límites.
    moneda: str
    # La frase con la que el operador prueba el bot al final. Nombra un
    # producto del catálogo que se acaba de sembrar, así que es del dataset.
    pregunta: str


# Cómo se pide cada uno. "es" y "en" son los nombres; el resto son las formas
# en que alguien los escribe sin pensarlo.
_DICHOS = {
    "es": ("es", "es_ar", "espanol", "español", "spanish", ""),
    "en": ("en", "en_us", "english", "ingles", "inglés"),
}


def dataset(nombre: str | None = None) -> Datos:
    """El catálogo que se va a sembrar. Por defecto, el de siempre.

    El nombre sale del argumento, del entorno (`SEED_DATASET`) o del default,
    en ese orden. Uno desconocido NO es el inglés por error: es el español con
    un aviso, porque sembrar el catálogo equivocado en un ERPNext real es un
    catálogo que alguien tiene que borrar a mano.
    """
    crudo = str(nombre if nombre is not None else os.getenv("SEED_DATASET", ""))
    limpio = crudo.strip().lower()
    elegido = next(
        (clave for clave, dichos in _DICHOS.items() if limpio in dichos), ""
    )
    if not elegido:
        print(f"  ! dataset desconocido {crudo!r}: siembro el español")
        elegido = "es"
    if elegido == "en":
        return Datos(
            nombre="en",
            grupo=GRUPO_EN,
            grupos_cliente=GRUPOS_CLIENTE_EN,
            unidades=UNIDADES_EN,
            productos=PRODUCTOS_EN,
            clientes=CLIENTES_EN,
            stock=STOCK_INICIAL_EN,
            moneda=MONEDA_EN,
            pregunta="hi, do you have cream cheese?",
        )
    # Se leen los globals AHORA y no al importar: son los mismos nombres de
    # siempre, y lo que un test (o un fork) les ponga encima sigue valiendo.
    return Datos(
        nombre="es",
        grupo=GRUPO,
        grupos_cliente=GRUPOS_CLIENTE,
        unidades=UNIDADES,
        productos=PRODUCTOS,
        clientes=CLIENTES,
        stock=STOCK_INICIAL,
        moneda=MONEDA,
        pregunta="hola, tenes queso cremoso?",
    )


def _ensure(doctype: str, name: str, payload: dict) -> str:
    """Idempotent - safe to run twice."""
    try:
        erpnext.get_doc(doctype, name)
        print(f"  = {doctype} {name} ya existe")
        return name
    except erpnext.ERPNextError:
        pass
    doc = erpnext.create_doc(doctype, payload)
    print(f"  + {doctype} {doc['name']}")
    return doc["name"]


class MonedaEquivocada(RuntimeError):
    """La lista de precios no está en la moneda del catálogo que se pide."""


def _moneda_de_la_lista(lista: str) -> str:
    """La moneda que ERPNext tiene puesta en esa lista, o "" si no se puede leer."""
    try:
        doc = erpnext.get_doc("Price List", lista)
    except erpnext.ERPNextError:
        return ""
    return str(doc.get("currency") or "").strip()


def _revisar_moneda(datos: "Datos", lista: str = LISTA_DE_PRECIOS) -> None:
    """Falla ANTES de escribir un solo precio si las monedas no coinciden.

    Un `Item Price` sin `currency` hereda la de la lista, y las dos escalas son
    plausibles por separado: 1.20 es un litro de leche en dólares y 1200 lo es
    en pesos. Escritas en la lista equivocada no hay ningún error — hay un
    catálogo mil veces más barato, que la pantalla muestra como `$1.20` y los
    libros leen como un peso veinte. Y como `AUTO_CONFIRM_MAX` es un monto en
    la moneda de la lista, todo tope queda comparando contra números de otra
    escala: nada llega nunca al límite y todo se auto-confirma.

    Por eso ABORTA en vez de avisar: sembrar es escribir en el ERPNext del
    cliente, y trece precios mal cargados los borra una persona a mano.
    """
    de_la_lista = _moneda_de_la_lista(lista)
    if not de_la_lista:
        # Todavía no existe, o no se puede leer. No se inventa nada: el
        # `currency` explícito de cada Item Price hace que ERPNext rechace la
        # mezcla si la lista aparece después con otra moneda.
        print(f"  ! no pude leer la moneda de «{lista}»: sigo con {datos.moneda}")
        return
    if de_la_lista != datos.moneda:
        raise MonedaEquivocada(
            f"la lista de precios «{lista}» está en {de_la_lista} y el catálogo "
            f"«{datos.nombre}» tiene precios en {datos.moneda}. Un precio de "
            f"{datos.productos[0][3]} escrito en una lista en {de_la_lista} no da "
            f"error: da un catálogo en otra escala, y AUTO_CONFIRM_MAX es un monto "
            f"en {de_la_lista}. Creá una lista en {datos.moneda} y apuntá "
            f"AUTO_CONFIRM_PRICE_LIST a ella, o sembrá el catálogo en "
            f"{'español' if datos.nombre == 'en' else 'inglés'}."
        )


def _account_by_type(company: str, account_type: str) -> str:
    """Resolve an account structurally, independent of chart language/names."""
    rows = erpnext.get_list(
        "Account",
        filters=[
            ["company", "=", company],
            ["is_group", "=", 0],
            ["account_type", "=", account_type],
        ],
        fields=["name"],
        limit=1,
    )
    return str(rows[0].get("name") or "").strip() if rows else ""


def _stock_reconciliation_payload(company: str, items: list[dict]) -> dict:
    """Build an opening-stock payload using ERPNext account metadata."""
    temporary_opening = _account_by_type(company, "Temporary")
    if temporary_opening:
        return {
            "company": company,
            "purpose": "Opening Stock",
            "expense_account": temporary_opening,
            "items": items,
        }

    payload = {
        "company": company,
        "purpose": "Stock Reconciliation",
        "items": items,
    }
    stock_adjustment = _account_by_type(company, "Stock Adjustment")
    if stock_adjustment:
        payload["expense_account"] = stock_adjustment
    return payload


def _stock_signature(items: list[dict]) -> tuple:
    """Canonical signature for recognizing a seed reconciliation on reruns."""
    signature = []
    for item in items:
        item_code = str(item.get("item_code") or "").strip()
        warehouse = str(item.get("warehouse") or "").strip()
        if not item_code or not warehouse:
            return ()
        try:
            qty = Decimal(str(item.get("qty") or 0)).normalize()
            valuation_rate = Decimal(
                str(item.get("valuation_rate") or 0)
            ).normalize()
        except (InvalidOperation, TypeError, ValueError):
            return ()
        signature.append((item_code, warehouse, qty, valuation_rate))
    return tuple(sorted(signature))


def _existing_stock_reconciliation(
    company: str, items: list[dict]
) -> tuple[str, int] | None:
    """Find the same non-cancelled seed reconciliation, draft or submitted."""
    expected = _stock_signature(items)
    if not expected:
        return None

    rows = erpnext.get_list(
        "Stock Reconciliation",
        filters=[["company", "=", company], ["docstatus", "!=", 2]],
        fields=["name", "docstatus"],
        limit=500,
    )
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        doc = erpnext.get_doc("Stock Reconciliation", name)
        if _stock_signature(doc.get("items") or []) == expected:
            return name, int(doc.get("docstatus") or row.get("docstatus") or 0)
    return None


def main(datos: Datos | None = None) -> None:
    datos = datos if datos is not None else dataset()
    print(f"Catálogo: {datos.nombre}")
    print("Grupos...")
    _ensure("Item Group", datos.grupo, {
        "item_group_name": datos.grupo,
        "parent_item_group": "All Item Groups",
        "is_group": 0,
    })
    for g in datos.grupos_cliente:
        _ensure("Customer Group", g, {
            "customer_group_name": g,
            "parent_customer_group": "All Customer Groups",
            "is_group": 0,
        })

    print("Unidades...")
    for u in datos.unidades:
        _ensure("UOM", u, {"uom_name": u})

    print("Productos...")
    if datos.productos:
        _revisar_moneda(datos)
    for code, nombre, uom, precio in datos.productos:
        _ensure("Item", code, {
            "item_code": code,
            "item_name": nombre,
            "item_group": datos.grupo,
            "stock_uom": uom,
            "is_stock_item": 1,
            "description": nombre,
        })
        existentes = erpnext.get_list(
            "Item Price",
            filters=[["item_code", "=", code], ["selling", "=", 1]],
            fields=["name"], limit=1,
        )
        if not existentes:
            erpnext.create_doc("Item Price", {
                "item_code": code,
                "price_list": LISTA_DE_PRECIOS,
                "price_list_rate": precio,
                "selling": 1,
                # LAS DOS EXPLÍCITAS, y ninguna es decorativa. Sin `currency`,
                # ERPNext hereda la de la lista y un catálogo en dólares se
                # guarda como pesos. Sin `uom`, el precio no matchea NINGUNA
                # línea de pedido: `policy._precio_autorizado` exige que la
                # unidad del precio sea la de la línea, así que un catálogo
                # sembrado sin unidad no puede auto-confirmar nada.
                "currency": datos.moneda,
                "uom": uom,
            })
            print(f"    precio {code}: {precio:,} {datos.moneda} por {uom}")

    print("Clientes...")
    for nombre, tel, grupo in datos.clientes:
        _ensure("Customer", nombre, {
            "customer_name": nombre,
            "customer_group": grupo,
            "customer_type": "Company",
            "territory": "All Territories",
            "mobile_no": tel,
        })

    print("Stock inicial...")
    empresa, dep = erpnext.default_context()
    print(f"  empresa: {empresa} · deposito: {dep}")

    # Valuation ~60% of selling price so margin reports are not nonsense.
    costo = {code: round(precio * 0.6, 2) for code, _, _, precio in datos.productos}
    items = [
        {
            "item_code": c,
            "warehouse": dep,
            "qty": q,
            "valuation_rate": costo.get(c, 1),
        }
        for c, q in datos.stock.items()
    ]

    existing = _existing_stock_reconciliation(empresa, items)
    if existing:
        stock_name, stock_docstatus = existing
        estado = "BORRADOR" if stock_docstatus == 0 else "YA CONFIRMADO"
        print(f"  = Stock Reconciliation {stock_name} ({estado})")
    else:
        # ERPNext identifies the opening account by account_type="Temporary";
        # account names are customizable and localized. If no such account
        # exists, create a normal reconciliation against Stock Adjustment.
        payload = _stock_reconciliation_payload(empresa, items)
        doc = erpnext.create_doc("Stock Reconciliation", payload)
        stock_name = doc["name"]
        stock_docstatus = 0
        print(f"  + Stock Reconciliation {stock_name} ({payload['purpose']})")
        print("    BORRADOR - confirmalo en ERPNext para que cargue el stock.")

    print("\nListo. Ahora:")
    if stock_docstatus == 0:
        print("  1. Confirma el Stock Reconciliation en ERPNext para cargar el stock.")
    else:
        print(f"  1. El Stock Reconciliation {stock_name} ya estaba confirmado.")
    print("  2. Poné el numero de prueba de Meta y tu numero en TELEFONOS_EQUIPO.")
    print(f"  3. Escribile al bot: '{datos.pregunta}'")
    if datos.nombre == "en":
        # El catálogo en inglés no alcanza solo: el bot sigue escribiendo los
        # montos y los botones como diga el despliegue.
        print("  4. Para la demo en inglés: IDIOMA_GERENCIA=en y LOCALE=en_US.")
    print("\nOJO: los precios son inventados. Pedile la lista real al cliente.")


def _pedido_en_la_linea(argv: list[str]) -> Datos:
    """`--dataset en`, o lo que diga el entorno, o el español."""
    parser = argparse.ArgumentParser(
        description="Siembra un ERPNext vacío con un lácteo de demostración."
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help="es (default) | en. También se puede por SEED_DATASET.",
    )
    return dataset(parser.parse_args(argv).dataset)


if __name__ == "__main__":
    try:
        main(_pedido_en_la_linea(sys.argv[1:]))
    except MonedaEquivocada as problema:
        # Un traceback acá no le dice nada al que está sembrando un ERPNext.
        print(f"\nABORTADO: {problema}")
        raise SystemExit(2) from None
