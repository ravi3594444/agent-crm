"""Seed an empty ERPNext with a plausible Argentine dairy business.

WHY YOU NEED THIS
The client is 100% on paper. There is no catalog to import, no customer list,
nothing. You cannot demo a WhatsApp sales agent against an empty database —
the bot will just say "no encontre ese producto" to everything.

This gives you a working demo in ~30 seconds. Replace it with his real
catalog and customers once you get them.

    python deploy/seed_dairy.py

PRICES ARE PLACEHOLDERS. Argentine prices move fast - do not show these to
the client as if they were real. Ask for his actual price list.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import erpnext  # noqa: E402

GRUPO = "Lacteos"

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


def main() -> None:
    print("Grupos...")
    _ensure("Item Group", GRUPO, {
        "item_group_name": GRUPO,
        "parent_item_group": "All Item Groups",
        "is_group": 0,
    })
    for g in ("Comercio", "Gastronomia"):
        _ensure("Customer Group", g, {
            "customer_group_name": g,
            "parent_customer_group": "All Customer Groups",
            "is_group": 0,
        })

    print("Unidades...")
    for u in ("Unidad", "Kg"):
        _ensure("UOM", u, {"uom_name": u})

    print("Productos...")
    for code, nombre, uom, precio in PRODUCTOS:
        _ensure("Item", code, {
            "item_code": code,
            "item_name": nombre,
            "item_group": GRUPO,
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
    for nombre, tel, grupo in CLIENTES:
        _ensure("Customer", nombre, {
            "customer_name": nombre,
            "customer_group": grupo,
            "customer_type": "Company",
            "territory": "All Territories",
            "mobile_no": tel,
        })

    print("Stock inicial...")
    dep = erpnext.default_warehouse()
    items = [
        {"item_code": c, "warehouse": dep, "qty": q, "valuation_rate": 1}
        for c, q in STOCK_INICIAL.items()
    ]
    doc = erpnext.create_doc("Stock Reconciliation", {
        "purpose": "Opening Stock",
        "items": items,
    })
    print(f"  + Stock Reconciliation {doc['name']} (BORRADOR - confirmalo en ERPNext)")

    print("\nListo. Ahora:")
    print("  1. Confirma el Stock Reconciliation en ERPNext para cargar el stock.")
    print("  2. Poné el numero de prueba de Meta y tu numero en TELEFONOS_EQUIPO.")
    print("  3. Escribile al bot: 'hola, tenes queso cremoso?'")
    print("\nOJO: los precios son inventados. Pedile la lista real al cliente.")


if __name__ == "__main__":
    main()
