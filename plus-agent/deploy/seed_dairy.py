"""Seed an empty ERPNext with a plausible dairy business.

WHY YOU NEED THIS
The client is 100% on paper. There is no catalog to import, no customer list,
nothing. You cannot demo a WhatsApp sales agent against an empty database —
the bot will just say "no encontre ese producto" to everything.

    python deploy/seed_dairy.py                  # Argentine data (default)
    python deploy/seed_dairy.py --dataset en     # English data
    SEED_DATASET=en python deploy/seed_dairy.py  # the same, from the environment

WHY THERE ARE TWO DATASETS
The Spanish one is the business this was written for. The English one exists
because a prospect who speaks English cannot be walked through a catalogue of
*Leche entera sachet 1 L*: he spends the demo reading the product names
instead of watching the agent work. Same shapes, same counts, same script —
only the words change, so a demo in either language exercises the same paths.

The default is the Spanish one, and stays that way: an existing deployment
that runs this script without arguments has to keep getting what it got.

PRICES ARE PLACEHOLDERS IN BOTH. Argentine prices move fast, and the English
numbers are invented outright - do not show these to the client as if they
were real. Ask for his actual price list.
"""
import os
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import erpnext  # noqa: E402


@dataclass(frozen=True)
class Datos:
    """One demo business: its item group, catalogue, customers and stock.

    The two instances below have to stay the same SHAPE — same number of
    products, same mix of unit-priced and weight-priced items, same customer
    groups — because the demo script walks the same steps in both. A dataset
    with fewer products is a dataset where half the demo has no lines to show.
    """

    grupo: str
    grupos_cliente: tuple[str, ...]
    unidades: tuple[str, ...]
    productos: list[tuple[str, str, str, int]]
    clientes: list[tuple[str, str, str]]
    stock: dict[str, int]


_ES = Datos(
    grupo="Lacteos",
    grupos_cliente=("Comercio", "Gastronomia"),
    unidades=("Unidad", "Kg"),
    productos=[
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
    ],
    clientes=[
        ("Almacen Don Jose",      "+5493511111111", "Comercio"),
        ("Kiosco La Esquina",     "+5493512222222", "Comercio"),
        ("Panaderia San Martin",  "+5493513333333", "Comercio"),
        ("Rotiseria El Fogon",    "+5493514444444", "Gastronomia"),
        ("Pizzeria Napoli",       "+5493515555555", "Gastronomia"),
        ("Supermercado Yrigoyen", "+5493516666666", "Comercio"),
        ("Cafe Belgrano",         "+5493517777777", "Gastronomia"),
    ],
    stock={
        "LEC-ENT-1L": 400, "LEC-DES-1L": 250, "LEC-BOT-1L": 180,
        "YOG-BEB-1L": 120, "YOG-FIR-190": 300,
        "QUE-CRE": 45, "QUE-MUZ": 60, "QUE-PSA": 30, "RIC-FRE": 18,
        "MAN-200": 90, "DDL-400": 140, "CRE-200": 70, "QUE-RAL-100": 110,
    },
)

# The item CODES are deliberately different from the Spanish ones. Seeding both
# into one ERPNext has to produce two catalogues and not one half-translated
# catalogue where "LEC-ENT-1L" is called Whole Milk because the English run
# went second and `_ensure` found the code already there.
_EN = Datos(
    grupo="Dairy",
    grupos_cliente=("Retail", "Foodservice"),
    unidades=("Each", "Kg"),
    productos=[
        ("WHL-MLK-1L",  "Whole milk 1 L pouch",        "Each",   1200),
        ("SKM-MLK-1L",  "Skim milk 1 L pouch",         "Each",   1250),
        ("WHL-MLK-BTL", "Whole milk 1 L bottle",       "Each",   1650),
        ("YOG-DRK-1L",  "Strawberry drinking yogurt 1 L", "Each", 1900),
        ("YOG-SET-190", "Vanilla set yogurt 190 g",    "Each",    650),
        ("CHS-CRM",     "Cream cheese",                "Kg",     9800),
        ("CHS-MOZ",     "Mozzarella",                  "Kg",    11500),
        ("CHS-PSL",     "Port salut cheese",           "Kg",    10900),
        ("RIC-FRSH",    "Fresh ricotta",               "Kg",     6200),
        ("BTR-200",     "Butter 200 g",                "Each",   2400),
        ("CRML-400",    "Dulce de leche 400 g",        "Each",   2100),
        ("CRM-200",     "Heavy cream 200 ml",          "Each",   1400),
        ("CHS-GRT-100", "Grated cheese 100 g",         "Each",   1300),
    ],
    clientes=[
        ("Corner Grocery",      "+15551110001", "Retail"),
        ("Maple Street Market", "+15551110002", "Retail"),
        ("Riverside Bakery",    "+15551110003", "Retail"),
        ("Oakwood Grill",       "+15551110004", "Foodservice"),
        ("Stone Oven Pizza",    "+15551110005", "Foodservice"),
        ("Hillcrest Supermarket", "+15551110006", "Retail"),
        ("Fifth Street Coffee", "+15551110007", "Foodservice"),
    ],
    stock={
        "WHL-MLK-1L": 400, "SKM-MLK-1L": 250, "WHL-MLK-BTL": 180,
        "YOG-DRK-1L": 120, "YOG-SET-190": 300,
        "CHS-CRM": 45, "CHS-MOZ": 60, "CHS-PSL": 30, "RIC-FRSH": 18,
        "BTR-200": 90, "CRML-400": 140, "CRM-200": 70, "CHS-GRT-100": 110,
    },
)

DATASETS = {"es": _ES, "en": _EN}
DATASET_POR_DEFECTO = "es"


def elegir(nombre: object = None) -> Datos:
    """The dataset an argument or the environment asks for. Default: Spanish.

    Anything unrecognised falls back to Spanish rather than failing: this is a
    seeding script someone runs by hand before a demo, and refusing to run over
    a typo in an environment variable helps nobody.
    """
    crudo = str(nombre or os.getenv("SEED_DATASET", "") or "").strip().lower()
    return DATASETS.get(crudo, DATASETS[DATASET_POR_DEFECTO])


def _del_argv(argv: list[str]) -> str:
    """`--dataset en` or `--dataset=en`, and nothing else to parse."""
    for indice, ficha in enumerate(argv):
        if ficha.startswith("--dataset="):
            return ficha.split("=", 1)[1]
        if ficha == "--dataset" and indice + 1 < len(argv):
            return argv[indice + 1]
    return ""


# The module-level names the script has always had. They are what `main()`
# reads by default, so the environment already selects a dataset for anything
# that imports and calls `main()` without arguments.
_ACTIVO = elegir()
GRUPO = _ACTIVO.grupo
PRODUCTOS = _ACTIVO.productos
CLIENTES = _ACTIVO.clientes
STOCK_INICIAL = _ACTIVO.stock
GRUPOS_CLIENTE = _ACTIVO.grupos_cliente
UNIDADES = _ACTIVO.unidades


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
    """Seed one dataset. `None` means the module-level one (see `elegir`).

    The module-level names stay the default rather than reading `elegir()` here
    so that anything already driving this script by setting `seed.PRODUCTOS`
    keeps working: the dataset is a parameter, not a hidden lookup.
    """
    grupo = datos.grupo if datos else GRUPO
    productos = datos.productos if datos else PRODUCTOS
    clientes = datos.clientes if datos else CLIENTES
    stock_inicial = datos.stock if datos else STOCK_INICIAL
    grupos_cliente = datos.grupos_cliente if datos else GRUPOS_CLIENTE
    unidades = datos.unidades if datos else UNIDADES

    print("Grupos...")
    _ensure("Item Group", grupo, {
        "item_group_name": grupo,
        "parent_item_group": "All Item Groups",
        "is_group": 0,
    })
    for g in grupos_cliente:
        _ensure("Customer Group", g, {
            "customer_group_name": g,
            "parent_customer_group": "All Customer Groups",
            "is_group": 0,
        })

    print("Unidades...")
    for u in unidades:
        _ensure("UOM", u, {"uom_name": u})

    print("Productos...")
    for code, nombre, uom, precio in productos:
        _ensure("Item", code, {
            "item_code": code,
            "item_name": nombre,
            "item_group": grupo,
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
                "price_list": "Standard Selling",
                "price_list_rate": precio,
                "selling": 1,
            })
            print(f"    precio {code}: ${precio:,}")

    print("Clientes...")
    for nombre, tel, grupo_cliente in clientes:
        _ensure("Customer", nombre, {
            "customer_name": nombre,
            "customer_group": grupo_cliente,
            "customer_type": "Company",
            "territory": "All Territories",
            "mobile_no": tel,
        })

    print("Stock inicial...")
    empresa, dep = erpnext.default_context()
    print(f"  empresa: {empresa} · deposito: {dep}")

    # Valuation ~60% of selling price so margin reports are not nonsense.
    costo = {code: round(precio * 0.6, 2) for code, _, _, precio in productos}
    items = [
        {
            "item_code": c,
            "warehouse": dep,
            "qty": q,
            "valuation_rate": costo.get(c, 1),
        }
        for c, q in stock_inicial.items()
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
    print("  3. Escribile al bot: 'hola, tenes queso cremoso?'")
    print("\nOJO: los precios son inventados. Pedile la lista real al cliente.")


if __name__ == "__main__":
    main(elegir(_del_argv(sys.argv[1:])))
