"""Los datos de mentira del banco de pruebas: una distribuidora de lácteos.

Nada de esto se parece a un cliente, un teléfono o un precio real. Los
teléfonos usan el prefijo 351 de Córdoba con cuerpos obviamente inventados
(1111222) y los dominios son .invalid, que por RFC 2606 no resuelve.

Las fechas se calculan EN EL MOMENTO de sembrar, no son constantes: el conteo
de stock tiene que caer dentro de STOCK_CONFIABLE_HORAS para que el sistema
considere el stock confiable, y una constante envejecería.
"""
from __future__ import annotations

from datetime import date, timedelta

from demo.falso_erpnext import DEPOSITO, EMPRESA, LISTA_PRECIOS, MONEDA, Almacen

# El dueño y el equipo. El agente de gerencia sólo le contesta a estos números.
TELEFONO_DUENO = "5493511110001"
TELEFONO_EQUIPO = "5493511110002"

# Clientes de mentira.
CLIENTE_HABITUAL = "Almacen Don Pedro"
TELEFONO_HABITUAL = "5493511111222"
CLIENTE_MOROSO = "Kiosco La Esquina"
TELEFONO_MOROSO = "5493512223333"
# Este no existe en ERPNext: es el alta del escenario de cliente nuevo.
TELEFONO_NUEVO = "5493514445555"

# (código, nombre, unidad, precio, stock)
CATALOGO = [
    ("LECHE-ENT-1L", "Leche entera sachet 1 L", "Unidad", 1250.0, 400.0),
    ("LECHE-DESC-1L", "Leche descremada sachet 1 L", "Unidad", 1290.0, 260.0),
    ("MANTECA-200", "Manteca 200 g", "Unidad", 2100.0, 90.0),
    # A propósito con poco stock: es el escenario de stock insuficiente.
    ("QUESO-CREM-1K", "Queso cremoso horma 1 kg", "Kg", 8900.0, 3.0),
    ("YOG-FRUT-190", "Yogur bebible frutilla 190 g", "Unidad", 780.0, 500.0),
    ("DDL-400", "Dulce de leche 400 g", "Unidad", 2450.0, 150.0),
]

# El que se queda corto, y de cuánto se pide en el escenario.
ITEM_SIN_STOCK = "QUESO-CREM-1K"
CANTIDAD_IMPOSIBLE = 20


def sembrar(almacen: Almacen, *, hoy: date | None = None) -> None:
    """Deja el almacén con el catálogo, dos clientes, stock y un conteo."""
    hoy = hoy or date.today()
    pol = {"puede_confirmar": True}

    almacen.crear("Company", {"company_name": EMPRESA, "abbr": "LD",
                              "default_currency": MONEDA}, **pol)

    for codigo, nombre, unidad, precio, stock in CATALOGO:
        almacen.crear(
            "Item",
            {
                "item_code": codigo, "item_name": nombre, "stock_uom": unidad,
                "description": nombre, "disabled": 0, "is_stock_item": 1,
                "item_group": "Lacteos",
            },
            **pol,
        )
        almacen.crear(
            "Item Price",
            {
                "item_code": codigo, "price_list": LISTA_PRECIOS, "selling": 1,
                "currency": MONEDA, "uom": unidad, "price_list_rate": precio,
                "valid_from": None, "valid_upto": None,
                "customer": None, "batch_no": None,
            },
            **pol,
        )
        almacen.crear(
            "Bin",
            {
                "item_code": codigo, "warehouse": DEPOSITO,
                "actual_qty": stock, "reserved_qty": 0.0,
                "projected_qty": stock,
            },
            **pol,
        )

    for cliente, telefono, calle in (
        (CLIENTE_HABITUAL, TELEFONO_HABITUAL, "Belgrano 1200"),
        (CLIENTE_MOROSO, TELEFONO_MOROSO, "Rivadavia 45"),
    ):
        almacen.crear(
            "Customer",
            {
                "customer_name": cliente, "mobile_no": telefono,
                "customer_group": "Comercios", "territory": "Cordoba",
                "default_currency": MONEDA, "disabled": 0,
            },
            **pol,
        )
        direccion = almacen.crear(
            "Address",
            {
                "address_title": f"{cliente} - Principal",
                "address_type": "Shipping", "address_line1": calle,
                "city": "Cordoba", "pincode": "5000", "country": "Argentina",
                "is_primary_address": 1, "is_shipping_address": 1,
                "links": [{"link_doctype": "Customer", "link_name": cliente,
                           "parenttype": "Address"}],
            },
            **pol,
        )
        assert direccion["name"]

    # Una factura vencida, para que el escenario de mora tenga de dónde salir.
    almacen.crear(
        "Sales Invoice",
        {
            "customer": CLIENTE_MOROSO, "customer_name": CLIENTE_MOROSO,
            "posting_date": (hoy - timedelta(days=45)).isoformat(),
            "due_date": (hoy - timedelta(days=15)).isoformat(),
            # El total se recalcula de las líneas, como en Frappe: que la
            # deuda y las líneas digan lo mismo (100 x 1840 = 184000).
            "outstanding_amount": 184000.0,
            "currency": MONEDA, "docstatus": 1,
            "items": [{"item_code": "LECHE-ENT-1L", "qty": 100, "rate": 1840.0,
                       "uom": "Unidad", "warehouse": DEPOSITO}],
        },
        **pol,
    )

    # Historial confirmado del cliente habitual. app/policy.py exige
    # AUTO_CONFIRM_MIN_ORDERS (3) pedidos CONFIRMADOS y que el pedido nuevo no
    # pase AUTO_CONFIRM_MULT (2x) su promedio. Sin esto la auto-confirmación no
    # puede pasar nunca y el escenario probaría el rechazo, no la aprobación.
    for cuantos in (12, 10, 14):
        almacen.crear(
            "Sales Order",
            {
                "customer": CLIENTE_HABITUAL, "customer_name": CLIENTE_HABITUAL,
                "transaction_date": (hoy - timedelta(days=30)).isoformat(),
                "delivery_date": (hoy - timedelta(days=29)).isoformat(),
                "selling_price_list": LISTA_PRECIOS, "currency": MONEDA,
                "order_type": "Sales", "docstatus": 1,
                "status": "Completed",
                "items": [{"item_code": "LECHE-ENT-1L", "qty": cuantos,
                           "rate": 1250.0, "uom": "Unidad",
                           "warehouse": DEPOSITO, "conversion_factor": 1}],
            },
            **pol,
        )

    # El conteo de stock confirmado de HOY: sin esto app/inventario.py no
    # considera confiable ningún producto y nada se auto-confirma.
    almacen.crear(
        "Stock Reconciliation",
        {
            "purpose": "Stock Reconciliation",
            "posting_date": hoy.isoformat(), "posting_time": "07:30:00",
            "docstatus": 1,
            "items": [
                {"item_code": codigo, "warehouse": DEPOSITO, "qty": stock,
                 "valuation_rate": precio * 0.6}
                for codigo, _n, _u, precio, stock in CATALOGO
            ],
        },
        **pol,
    )
