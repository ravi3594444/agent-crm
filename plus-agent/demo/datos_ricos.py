"""Un catálogo y una cartera de clientes creíbles, para mostrarle el agente al dueño.

Por qué existe aparte de `demo/datos.py`
----------------------------------------
`datos.py` es el mínimo que los escenarios de release necesitan, y
`tests/test_demo.py` CUENTA sus filas: «6 items», «4 que no son estos dos»,
«exactamente uno con stock entre 3 y 4». Agregarle productos rompería esos
tests sin que nada del sistema haya cambiado. Así que esto se SUMA a aquello:
`sembrar()` llama primero a `datos.sembrar()` —los 6 productos y los 3 clientes
de siempre quedan igual— y después agrega el resto. Los escenarios existentes
siguen pasando; los nuevos tienen con qué.

Los precios son INVENTADOS. Son verosímiles para una distribuidora de lácteos
de Córdoba en 2026 y coherentes entre sí (el litro de sachet vale menos que el
de botella, la horma por kilo más que el rallado por 100 g), pero no salieron
de ninguna lista real y no se le muestran a un cliente como si lo fueran.

LOS TRES CAMPOS DE UN ITEM PRICE
--------------------------------
Cada Item Price se escribe con `price_list`, `currency` Y `uom`, los tres,
siempre. No es cosmético:

  * `app/policy.py::_precio_estandar` filtra los Item Price por los tres y
    hace `continue` sobre cualquiera al que le falte uno. Sin `uom` ninguna
    fila matchea, la línea queda «precio fuera de lista» y NADA se
    auto-confirma jamás.
  * `app/tools/catalogo.py::_catalog_price_is_valid` filtra por los mismos
    tres. Sin ellos `buscar_producto` contesta «precio a confirmar» en vez del
    precio.

Ninguno de los dos caminos informa un error: el agente simplemente deja de
cotizar y de confirmar. `verificar_precios()` acá abajo lo afirma sobre los
datos sembrados, para que el defecto no pueda volver a entrar por acá.

Además `_precio_estandar` exige que la `uom` de la línea del pedido sea IGUAL
al `stock_uom` del Item, así que cada producto se vende en su propia unidad y
no hay conversiones: el queso por Kg, los huevos por Maple, el resto por
Unidad.
"""
from __future__ import annotations

from datetime import date, timedelta

from demo import datos
from demo.falso_erpnext import DEPOSITO, EMPRESA, LISTA_PRECIOS, MONEDA, Almacen

# Los teléfonos siguen la convención de `datos.py`: prefijo 54935 (Córdoba) y
# un cuerpo obviamente inventado, con dos dígitos distintos en total. Ninguno
# puede parecerse al de una persona.
#
# Y hay una segunda razón para que sean tan parecidos entre sí: `app/clientes.py`
# busca al remitente con un LIKE interleaved sobre los últimos OCHO dígitos
# (%1%1%1%1%0%0%0%3%). Dos teléfonos cuyos patrones se contengan resolverían al
# cliente equivocado. Acá sólo cambia el último dígito, que es justo el que
# cierra el patrón, así que cada uno matchea únicamente al suyo.
# `verificar_telefonos()` lo comprueba sobre los datos sembrados en vez de
# confiar en este párrafo.
TELEFONO_DOS_HERMANOS = "5493511110003"
TELEFONO_TREBOL = "5493511110004"
TELEFONO_ESPERANZA = "5493511110005"
TELEFONO_SAN_CAYETANO = "5493511110006"
TELEFONO_LA_FAMILIA = "5493511110007"
TELEFONO_BUEN_SABOR = "5493511110008"
# No existe en ERPNext: es el alta de un cliente nuevo de verdad.
TELEFONO_SIN_CUENTA = "5493511110000"

CLIENTE_DOS_HERMANOS = "Almacen Los Dos Hermanos"
CLIENTE_TREBOL = "Kiosco El Trebol"
CLIENTE_ESPERANZA = "Panaderia La Esperanza"
CLIENTE_SAN_CAYETANO = "Fiambreria San Cayetano"
CLIENTE_LA_FAMILIA = "Autoservicio La Familia"
CLIENTE_BUEN_SABOR = "Rotiseria El Buen Sabor"

# (código, nombre de catálogo, unidad, precio de lista, stock en el depósito)
#
# El nombre es el que ve el cliente y el que busca `buscar_producto` con un
# LIKE: por eso dice el formato y el tamaño («sachet 1 L», «horma por kilo»,
# «maple 30 unidades»). Un catálogo que dice sólo «Leche» no se puede pedir.
CATALOGO_EXTRA = [
    # --- leches
    ("LECHE-ENT-SCH-3L", "Leche entera sachet 3 L", "Unidad", 3480.0, 180.0),
    ("LECHE-ENT-BOT-1L", "Leche entera botella 1 L", "Unidad", 1690.0, 120.0),
    ("LECHE-DESC-BOT-1L", "Leche descremada botella 1 L", "Unidad", 1740.0, 96.0),
    ("LECHE-CHOC-200", "Leche chocolatada 200 ml", "Unidad", 690.0, 480.0),
    # --- yogures
    ("YOG-BEB-FRUT-1L", "Yogur bebible frutilla 1 L", "Unidad", 2890.0, 144.0),
    ("YOG-BEB-VAIN-1L", "Yogur bebible vainilla 1 L", "Unidad", 2890.0, 108.0),
    ("YOG-FIRME-DUR-190", "Yogur firme durazno 190 g", "Unidad", 820.0, 360.0),
    # --- quesos (los tres por kilo, como los pide un almacén)
    ("MUZZA-KG", "Muzzarella horma por kilo", "Kg", 10500.0, 45.0),
    ("QUESO-PORTSALUT-KG", "Queso port salut horma por kilo", "Kg", 9800.0, 28.0),
    ("QUESO-RALL-100", "Queso rallado 100 g", "Unidad", 1980.0, 200.0),
    ("RICOTA-KG", "Ricota por kilo", "Kg", 6200.0, 18.0),
    # --- manteca, crema y postres
    ("MANTECA-500", "Manteca 500 g", "Unidad", 4750.0, 60.0),
    ("CREMA-200", "Crema de leche 200 ml", "Unidad", 1580.0, 240.0),
    ("CREMA-1L", "Crema de leche 1 L", "Unidad", 6900.0, 72.0),
    ("DDL-REPOST-1K", "Dulce de leche repostero 1 kg", "Unidad", 5400.0, 85.0),
    ("FLAN-100", "Postre flan vainilla 100 g", "Unidad", 620.0, 300.0),
    # --- lo que una distribuidora de lácteos reparte igual, y estrena unidad
    ("HUEVO-BLANCO-M30", "Huevos blancos maple 30 unidades", "Maple", 9600.0, 64.0),
]

# (código de cuenta, nombre, teléfono, calle, localidad, CP, rubro)
#
# La localidad importa: `app/entrega.py` autoriza por ZONAS_ENTREGA_LOCALIDADES
# y ZONAS_ENTREGA_CP. Unquillo NO está en la zona del banco de pruebas, y está
# a propósito: es el cliente con el que se ve qué hace el agente cuando la
# dirección no la puede decidir solo.
CLIENTES_EXTRA = [
    (CLIENTE_DOS_HERMANOS, CLIENTE_DOS_HERMANOS, TELEFONO_DOS_HERMANOS,
     "Av. Colon 2450", "Cordoba", "5000", "Comercios"),
    (CLIENTE_TREBOL, CLIENTE_TREBOL, TELEFONO_TREBOL,
     "Bv. San Juan 730", "Cordoba", "5000", "Comercios"),
    (CLIENTE_ESPERANZA, CLIENTE_ESPERANZA, TELEFONO_ESPERANZA,
     "Los Alamos 120", "Villa Allende", "5105", "Comercios"),
    (CLIENTE_SAN_CAYETANO, CLIENTE_SAN_CAYETANO, TELEFONO_SAN_CAYETANO,
     "Deán Funes 1890", "Cordoba", "5001", "Comercios"),
    (CLIENTE_LA_FAMILIA, CLIENTE_LA_FAMILIA, TELEFONO_LA_FAMILIA,
     "Rivadavia 640", "Unquillo", "5009", "Comercios"),
    (CLIENTE_BUEN_SABOR, CLIENTE_BUEN_SABOR, TELEFONO_BUEN_SABOR,
     "27 de Abril 1150", "Cordoba", "5000", "Comercios"),
]

# El pedido que «Los Dos Hermanos» repite todas las semanas. Es lo que devuelve
# `pedido_habitual`, así que tiene que tener MÁS DE UNA LÍNEA: con una sola, «lo
# de siempre» se ve bien por casualidad y no prueba que el agente sepa repetir
# una lista. (código, cantidad, unidad)
PEDIDO_DE_SIEMPRE = [
    ("LECHE-ENT-1L", 24, "Unidad"),
    ("YOG-BEB-FRUT-1L", 6, "Unidad"),
    ("MANTECA-200", 4, "Unidad"),
]

_PRECIOS = {codigo: precio for codigo, _n, _u, precio, _s in CATALOGO_EXTRA}
_PRECIOS.update({codigo: precio for codigo, _n, _u, precio, _s in datos.CATALOGO})
_UNIDADES = {codigo: unidad for codigo, _n, unidad, _p, _s in CATALOGO_EXTRA}
_UNIDADES.update({codigo: unidad for codigo, _n, unidad, _p, _s in datos.CATALOGO})


def _crear_producto(almacen: Almacen, codigo: str, nombre: str, unidad: str,
                    precio: float, stock: float, pol: dict) -> None:
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
            "item_code": codigo,
            # Los TRES, explícitos, siempre. Ver el docstring del módulo: sin
            # uno de ellos el agente deja de cotizar y de confirmar en
            # silencio, sin un error en ninguna parte.
            "price_list": LISTA_PRECIOS,
            "currency": MONEDA,
            "uom": unidad,
            "selling": 1,
            "price_list_rate": precio,
            "valid_from": None, "valid_upto": None,
            "customer": None, "batch_no": None,
        },
        **pol,
    )
    almacen.crear(
        "Bin",
        {
            "item_code": codigo, "warehouse": DEPOSITO,
            "actual_qty": stock, "reserved_qty": 0.0, "projected_qty": stock,
        },
        **pol,
    )


def sembrar(almacen: Almacen, *, hoy: date | None = None) -> None:
    """Los datos de `datos.sembrar` MÁS el catálogo y la cartera ampliados."""
    hoy = hoy or date.today()
    pol = {"puede_confirmar": True}

    # Primero lo de siempre: los escenarios de release dependen de eso y no se
    # toca. Trae la Company, los 6 productos, los 3 clientes, la factura
    # vencida, el historial y el conteo de stock de HOY.
    datos.sembrar(almacen, hoy=hoy)

    for codigo, nombre, unidad, precio, stock in CATALOGO_EXTRA:
        _crear_producto(almacen, codigo, nombre, unidad, precio, stock, pol)

    for codigo, nombre, telefono, calle, localidad, cp, grupo in CLIENTES_EXTRA:
        almacen.crear(
            "Customer",
            {
                "name": codigo, "customer_name": nombre, "mobile_no": telefono,
                "customer_group": grupo, "territory": "Cordoba",
                "default_currency": MONEDA, "disabled": 0,
            },
            **pol,
        )
        almacen.crear(
            "Address",
            {
                "address_title": f"{nombre} - Principal",
                "address_type": "Shipping", "address_line1": calle,
                "city": localidad, "pincode": cp, "country": "Argentina",
                "is_primary_address": 1, "is_shipping_address": 1,
                "links": [{"link_doctype": "Customer", "link_name": codigo,
                           "parenttype": "Address"}],
            },
            **pol,
        )

    # El historial de «Los Dos Hermanos»: tres pedidos confirmados con la misma
    # lista. Son tres porque `app/policy.py` exige AUTO_CONFIRM_MIN_ORDERS (3)
    # pedidos CONFIRMADOS antes de mirar siquiera el promedio; con dos, el
    # escenario de «lo de siempre» probaría el rechazo por falta de historial y
    # no la repetición del pedido.
    for semanas in (3, 2, 1):
        almacen.crear(
            "Sales Order",
            {
                "customer": CLIENTE_DOS_HERMANOS,
                "customer_name": CLIENTE_DOS_HERMANOS,
                "transaction_date": (hoy - timedelta(weeks=semanas)).isoformat(),
                "delivery_date": (hoy - timedelta(weeks=semanas)
                                  + timedelta(days=1)).isoformat(),
                "selling_price_list": LISTA_PRECIOS, "currency": MONEDA,
                "order_type": "Sales", "docstatus": 1, "status": "Completed",
                "items": [
                    {"item_code": codigo, "qty": cantidad,
                     "rate": _PRECIOS[codigo], "uom": unidad,
                     "warehouse": DEPOSITO, "conversion_factor": 1}
                    for codigo, cantidad, unidad in PEDIDO_DE_SIEMPRE
                ],
            },
            **pol,
        )

    # El conteo de stock de HOY para los productos nuevos. `datos.sembrar` ya
    # dejó uno para los suyos; `app/inventario.py` no le cree a un producto que
    # no esté en un conteo CONFIRMADO dentro de STOCK_CONFIABLE_HORAS, así que
    # sin esto los 17 productos nuevos serían «stock no confiable» y ninguno
    # podría auto-confirmarse nunca.
    almacen.crear(
        "Stock Reconciliation",
        {
            "purpose": "Stock Reconciliation",
            "posting_date": hoy.isoformat(), "posting_time": "07:45:00",
            "docstatus": 1,
            "items": [
                {"item_code": codigo, "warehouse": DEPOSITO, "qty": stock,
                 "valuation_rate": precio * 0.62}
                for codigo, _n, _u, precio, stock in CATALOGO_EXTRA
            ],
        },
        **pol,
    )
    assert almacen.tabla("Company")[EMPRESA]["company_name"] == EMPRESA


# ------------------------------------------------------------ autoverificación


def verificar_precios(almacen: Almacen) -> list[str]:
    """Todo Item vendible tiene UN precio con los tres campos y en su unidad.

    Devuelve la lista de problemas: vacía es que está bien. Se afirma sobre lo
    SEMBRADO, no sobre `CATALOGO_EXTRA`, porque el defecto que busca —un precio
    al que le falta `uom`— aparece al escribirlo, no al declararlo.
    """
    problemas: list[str] = []
    precios = list(almacen.tabla("Item Price").values())
    for item in almacen.tabla("Item").values():
        codigo = item["item_code"]
        unidad = item["stock_uom"]
        propios = [p for p in precios if p.get("item_code") == codigo]
        if not propios:
            problemas.append(f"{codigo}: no tiene ningún Item Price")
            continue
        validos = [
            p for p in propios
            if p.get("price_list") == LISTA_PRECIOS
            and p.get("currency") == MONEDA
            # La igualdad con stock_uom es la que exige `_precio_estandar`: no
            # alcanza con que `uom` esté puesta, tiene que ser ESA.
            and p.get("uom") == unidad
            and float(p.get("price_list_rate") or 0) > 0
        ]
        if not validos:
            faltan = sorted(
                campo for campo in ("price_list", "currency", "uom")
                if not str(propios[0].get(campo) or "").strip()
            )
            problemas.append(
                f"{codigo}: ningún precio sirve para auto-confirmar "
                f"(unidad del item {unidad!r}; "
                + (f"falta {', '.join(faltan)}" if faltan
                   else f"uom del precio {propios[0].get('uom')!r}")
                + ")"
            )
    return problemas


def verificar_telefonos(almacen: Almacen) -> list[str]:
    """Cada teléfono sembrado resuelve a UN cliente con el LIKE de la app.

    `app/clientes.py` busca al remitente con «%5%4%9%…%» sobre los últimos ocho
    dígitos. Dos clientes cuyos patrones se solapen harían que el agente le
    conteste a uno el pedido del otro, y eso no se ve como un error: se ve como
    un pedido bien tomado a nombre equivocado.
    """
    problemas: list[str] = []
    clientes = list(almacen.tabla("Customer").values())
    telefonos = [(c["name"], str(c.get("mobile_no") or "")) for c in clientes]
    for nombre, telefono in telefonos:
        if not telefono:
            problemas.append(f"{nombre}: sin mobile_no")
            continue
        patron = telefono[-8:]
        coinciden = [
            otro for otro, candidato in telefonos
            if _subsecuencia(patron, candidato)
        ]
        if coinciden != [nombre]:
            problemas.append(
                f"{telefono} ({nombre}) matchea a {', '.join(coinciden)}"
            )
    return problemas


def _subsecuencia(patron: str, texto: str) -> bool:
    """Lo mismo que hace «%1%1%0%» en SQL: en orden, con huecos."""
    resto = iter(texto)
    return all(c in resto for c in patron)
