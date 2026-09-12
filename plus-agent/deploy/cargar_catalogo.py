"""Carga el catálogo REAL del cliente desde un CSV. Lo corre UNA PERSONA.

SCRIPT DE DESPLIEGUE, NO UNA HERRAMIENTA. Igual que `deploy/seed_dairy.py` y
`deploy/cuentas_inventario.py`: lo ejecuta el dueño con credenciales de
Administrator pasadas POR PROCESO, no cuelga de ningún tool del agente y no lo
puede invocar un mensaje de WhatsApp. Nadie lo cablee después a `app/tools/`:
la separación de las tres identidades (cliente / gerencia / política) es lo que
hace que este producto se pueda vender, y esto vive afuera de las tres.

Se corre DESDE `plus-agent/`, en el host y con el venv del proyecto — igual
que `make seed`. La imagen de producción sólo copia `app/`, así que dentro del
contenedor no existe `/srv/deploy` (si sólo tenés el contenedor, montalo para
esa corrida: ver `deploy/cuentas_inventario.py`).

    # 1. la plantilla, para llenar en Excel o Google Sheets
    .venv/bin/python deploy/cargar_catalogo.py --ejemplo

    # 2. el plan completo, sin escribir NADA (esto es el default)
    ERPNEXT_API_KEY=... ERPNEXT_API_SECRET=... \
        .venv/bin/python deploy/cargar_catalogo.py catalogo.csv

    # 3. recién ahora se escribe
    ERPNEXT_API_KEY=... ERPNEXT_API_SECRET=... \
        .venv/bin/python deploy/cargar_catalogo.py catalogo.csv --aplicar

    # 4. ¿ERPNext dice lo mismo que el archivo?
    ERPNEXT_API_KEY=... ERPNEXT_API_SECRET=... \
        .venv/bin/python deploy/cargar_catalogo.py catalogo.csv --verificar

POR QUÉ EXISTE
Los precios que siembra `seed_dairy.py` son inventados y el propio script lo
dice. Un agente que cotiza solo con precios inventados se equivoca solo. Cargar
la lista real del cliente a mano en la interfaz de ERPNext son horas de clicks,
y las horas de clicks se hacen una vez y después nadie las repite: la lista
queda vieja. Esto es un comando, así que se puede repetir.

LAS TRES REGLAS QUE LO HACEN USABLE
1. El simulacro es el default. Sin `--aplicar` mira, valida y cuenta todo lo
   que haría. Nada se escribe. El dueño ve el plan entero antes que ERPNext.
2. Valida TODO antes de escribir NADA. Un archivo con tres errores se rechaza
   entero con la lista de los tres, numerada y con el número de línea. Un
   catálogo cargado por la mitad es peor que uno no cargado: hay que saber
   cuál mitad.
3. Es idempotente. Correrlo dos veces con el mismo archivo no cambia nada;
   correrlo con tres filas nuevas agrega tres productos. El dueño lo va a
   correr más de una vez y no tiene que tener miedo.

LO QUE CREA Y LO QUE NO
Crea Items, Item Prices, los Item Groups que el archivo nombre y un BORRADOR de
Stock Reconciliation con el stock inicial. No confirma (submit) nada: el ajuste
de stock queda en borrador para que una persona lo mire y lo confirme en
ERPNext, igual que el del seed.

NO crea unidades de medida. Una UOM que no existe es un ERROR del archivo, no
algo para arreglar en silencio: «kg», «Kg» y «KG» son tres UOM distintas para
ERPNext y el día que existan las tres, la mitad del catálogo deja de cotizar.
Los grupos sí se crean, porque la lista de un cliente real trae rubros que este
ERPNext todavía no conoce, y un grupo de más no rompe nada.
"""
from __future__ import annotations

import argparse
import ast
import csv
import os
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import erpnext
from deploy import cuentas_inventario as cuentas

COLUMNAS = ("codigo", "nombre", "unidad", "precio", "stock_inicial", "grupo")

# Las filas que escribe `--ejemplo`. El código empieza con esto a propósito:
# si alguien llena la plantilla y se olvida de borrarlas, el validador las
# reconoce y no las carga. Un catálogo real con un «producto de ejemplo»
# adentro es algo que alguien descubre cuando el bot se lo ofrece a un cliente.
PREFIJO_EJEMPLO = "EJEMPLO-"

# Cuántas UOM / Item Group se leen para poder comparar contra el archivo.
TOPE_VOCABULARIO = 2000

EJEMPLO = [
    (f"{PREFIJO_EJEMPLO}1", "Borrá esta fila: leche entera 1 L", "Unidad", "1200", "400", "Lacteos"),
    (f"{PREFIJO_EJEMPLO}2", "Borrá esta fila: queso cremoso", "Kg", "9800.50", "45", "Lacteos"),
]

AYUDA_COLUMNAS = [
    ("codigo", "El código con el que el cliente lo pide. SALE POR WHATSAPP. Único."),
    ("nombre", "El nombre que lee el cliente. Es lo que busca el bot cuando le preguntan."),
    ("unidad", "Tiene que existir YA en ERPNext, tal cual, con mayúsculas y todo."),
    ("precio", "Sólo números y punto decimal: 1200 o 9800.50. NI comas NI signo $."),
    ("stock_inicial", "Cuánto hay hoy. 0 si no querés cargar stock de ese producto."),
    ("grupo", "El rubro. Si no existe en ERPNext, se crea."),
]


@dataclass(frozen=True)
class Fila:
    """Una línea del CSV, ya convertida y con el número de línea que ve el dueño."""

    linea: int
    codigo: str
    nombre: str
    unidad: str
    precio: Decimal
    stock: Decimal
    grupo: str


@dataclass
class EnErpnext:
    """Lo que ERPNext ya tiene, leído de una vez antes de decidir nada."""

    items: dict[str, dict] = field(default_factory=dict)
    precios: dict[str, dict] = field(default_factory=dict)
    stock: dict[str, Decimal] = field(default_factory=dict)
    ajustes: dict[str, str] = field(default_factory=dict)
    unidades: set[str] = field(default_factory=set)
    grupos: set[str] = field(default_factory=set)


@dataclass
class Plan:
    """Todo lo que pasaría, separado por lo que el dueño necesita decidir."""

    grupos_nuevos: list[str] = field(default_factory=list)
    items_nuevos: list[Fila] = field(default_factory=list)
    items_cambiados: list[tuple[Fila, dict]] = field(default_factory=list)
    items_iguales: list[Fila] = field(default_factory=list)
    precios_nuevos: list[Fila] = field(default_factory=list)
    precios_cambiados: list[tuple[Fila, Decimal, str]] = field(default_factory=list)
    precios_iguales: list[Fila] = field(default_factory=list)
    stock_a_cargar: list[Fila] = field(default_factory=list)
    stock_omitido: list[tuple[Fila, str]] = field(default_factory=list)

    @property
    def hay_algo(self) -> bool:
        return bool(
            self.grupos_nuevos
            or self.items_nuevos
            or self.items_cambiados
            or self.precios_nuevos
            or self.precios_cambiados
            or self.stock_a_cargar
        )


# ---------------------------------------------------------------- el archivo


def _decimal(crudo: str) -> Decimal | None:
    """Un número o nada. No adivina: «1.200,50» puede ser dos cosas distintas."""
    texto = crudo.strip()
    if not texto or "," in texto:
        return None
    try:
        valor = Decimal(texto)
    except InvalidOperation:
        return None
    return None if not valor.is_finite() else valor


def leer(ruta: Path) -> tuple[list[Fila], list[str]]:
    """El CSV como filas y como problemas. Los problemas nombran archivo y línea.

    Devuelve TODOS los problemas, no el primero: el dueño arregla el archivo de
    una sentada en vez de correr esto seis veces.
    """
    try:
        crudo = ruta.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return [], [f"no pude leer {ruta}: {exc.strerror or exc}"]

    lineas = crudo.splitlines()
    if not lineas:
        return [], [f"{ruta} está vacío"]
    # Excel en español guarda con punto y coma. Que el archivo del dueño abra
    # bien es su problema con Excel; que este script lo lea es el nuestro.
    separador = ";" if lineas[0].count(";") > lineas[0].count(",") else ","

    filas: list[Fila] = []
    problemas: list[str] = []
    vistos: dict[str, int] = {}
    lector = csv.reader(lineas, delimiter=separador)
    encabezado = [c.strip().lower() for c in next(lector, [])]
    if tuple(encabezado) != COLUMNAS:
        return [], [
            f"{ruta} línea 1: el encabezado tiene que ser exactamente "
            f"«{separador.join(COLUMNAS)}» y dice «{separador.join(encabezado)}». "
            f"Sacá la plantilla de nuevo con --ejemplo y copiá tus datos ahí."
        ]

    for numero, campos in enumerate(lector, start=2):
        if not any(c.strip() for c in campos):
            continue  # una línea en blanco al final no es un error
        donde = f"{ruta} línea {numero}"
        # Los problemas de ESTA fila aparte: una fila con cualquier problema no
        # entra en `filas`. Que un código repetido o un precio de 0 se
        # reportaran Y se devolvieran igual sería dejar que la fila rechazada
        # llegue a cualquiera que use `leer()` sin mirar la otra mitad.
        malo: list[str] = []
        if len(campos) != len(COLUMNAS):
            problemas.append(
                f"{donde}: tiene {len(campos)} columna(s) y son {len(COLUMNAS)}. "
                f"Si algún nombre lleva «{separador}», ponelo entre comillas."
            )
            continue
        codigo, nombre, unidad, precio_crudo, stock_crudo, grupo = (c.strip() for c in campos)

        if not codigo:
            malo.append(f"{donde}: falta el código, y es con lo que el cliente lo pide")
        elif codigo.upper().startswith(PREFIJO_EJEMPLO):
            malo.append(
                f"{donde}: «{codigo}» es una de las filas de ejemplo de la plantilla. "
                f"Borrala: un producto de ejemplo en el catálogo se lo termina "
                f"ofreciendo el bot a un cliente."
            )
        elif codigo in vistos:
            malo.append(
                f"{donde}: el código «{codigo}» ya estaba en la línea {vistos[codigo]}. "
                f"Dos productos no pueden tener el mismo código."
            )
        else:
            vistos[codigo] = numero
        if not nombre:
            malo.append(f"{donde}: falta el nombre, y es lo que el bot le muestra al cliente")
        if not unidad:
            malo.append(f"{donde}: falta la unidad (por ejemplo Unidad, o Kg)")
        if not grupo:
            malo.append(f"{donde}: falta el grupo")

        precio = _decimal(precio_crudo)
        if precio is None:
            malo.append(
                f"{donde}: el precio «{precio_crudo}» no es un número. Sólo dígitos y "
                f"punto decimal: 1200 o 9800.50, sin $, sin espacios y sin comas."
            )
        elif precio <= 0:
            malo.append(
                f"{donde}: el precio es {precio}. Un precio de 0 o negativo no es un "
                f"precio, y cargarlo sería volver a poner un valor de relleno en ERPNext."
            )

        stock = _decimal(stock_crudo)
        if stock is None:
            malo.append(
                f"{donde}: el stock inicial «{stock_crudo}» no es un número. "
                f"Poné 0 si no querés cargar stock de este producto."
            )
        elif stock < 0:
            malo.append(f"{donde}: el stock inicial es {stock}, y no puede ser negativo")

        problemas.extend(malo)
        if not malo:
            filas.append(Fila(numero, codigo, nombre, unidad, precio, stock, grupo))

    if not filas and not problemas:
        problemas.append(f"{ruta}: el encabezado está bien pero no hay ni una fila de datos")
    return filas, problemas


def escribir_ejemplo(ruta: Path) -> None:
    """La plantilla. Se niega a pisar un archivo que ya existe, a propósito."""
    if ruta.exists():
        raise FileExistsError(
            f"{ruta} ya existe y no lo piso: si es tu catálogo lleno, te lo estaría borrando. "
            f"Elegí otro nombre: --ejemplo otro-nombre.csv"
        )
    with ruta.open("w", encoding="utf-8", newline="") as destino:
        escritor = csv.writer(destino)
        escritor.writerow(COLUMNAS)
        escritor.writerows(EJEMPLO)
    print(f"Plantilla escrita en {ruta}. Abrila con Excel o Google Sheets.\n")
    print("Una fila por producto. Las columnas, en este orden:")
    for columna, explicacion in AYUDA_COLUMNAS:
        print(f"  {columna:<14} {explicacion}")
    print(f"\nBorrá las dos filas «{PREFIJO_EJEMPLO}…»: están para mostrar la forma.")
    print("Cuando lo tengas, mirá el plan (no escribe nada):")
    print(f"  python deploy/cargar_catalogo.py {ruta}")


# ------------------------------------------------------- la lista de precios


def lista_de_precios(fuente: Path | None = None) -> str:
    """En qué Price List escribe los precios el seed. LEÍDA DE SU CÓDIGO.

    No está escrita acá de nuevo: si mañana `seed_dairy.py` siembra en otra
    lista, este script la sigue sin que nadie se acuerde de tocar dos archivos.
    Una lista distinta a la del seed (y a la de `AUTO_CONFIRM_PRICE_LIST`) es
    un catálogo que el bot no puede cotizar, y eso no se ve hasta que un
    cliente pregunta un precio.

    Se lee con `ast` y no importando el módulo: importarlo lo haría hablar con
    ERPNext, y esto tiene que poder contestar sin red.
    """
    fuente = fuente or Path(__file__).with_name("seed_dairy.py")
    for nodo in ast.walk(ast.parse(fuente.read_text(encoding="utf-8"))):
        if not isinstance(nodo, ast.Call) or len(nodo.args) != 2:
            continue
        doctype, payload = nodo.args
        if not (isinstance(doctype, ast.Constant) and doctype.value == "Item Price"):
            continue
        if not isinstance(payload, ast.Dict):
            continue
        for clave, valor in zip(payload.keys, payload.values, strict=False):
            if (
                isinstance(clave, ast.Constant)
                and clave.value == "price_list"
                and isinstance(valor, ast.Constant)
                and str(valor.value).strip()
            ):
                return str(valor.value).strip()
    raise erpnext.ERPNextError(
        f"no encontré en qué Price List escribe {fuente.name}: si dejó de crear "
        f"Item Price, decidí a mano en cuál va este catálogo antes de cargarlo."
    )


def moneda_de(lista: str) -> str:
    """La moneda de esa lista de precios, según ERPNext. Vacía si no se puede leer."""
    try:
        return str(erpnext.get_doc("Price List", lista).get("currency") or "").strip()
    except erpnext.ERPNextError:
        return ""


# ------------------------------------------------------------ lo que ya hay


def _en_tandas(codigos: list[str], tamano: int = 90):
    for inicio in range(0, len(codigos), tamano):
        yield codigos[inicio : inicio + tamano]


def relevar(filas: list[Fila], deposito: str, lista: str) -> EnErpnext:
    """Todo lo que ERPNext ya sabe de estos códigos, en pocas consultas.

    De a tandas y no de a uno: un catálogo de doscientos productos serían
    ochocientas llamadas, y el dueño estaría mirando una pantalla quieta.
    """
    codigos = [f.codigo for f in filas]
    actual = EnErpnext()

    # Las UOM y los grupos se leen ENTEROS y no filtrados por lo que pide el
    # archivo: hace falta la lista completa para poder decir «¿querías Kg?»
    # cuando el archivo dice «kg». Son vocabularios chicos y fijos (ERPNext
    # trae ~100 UOM de fábrica), pero si alguno llegara al tope, una unidad que
    # existe se reportaría como inexistente — así que el tope se avisa.
    for doctype, destino in (("UOM", actual.unidades), ("Item Group", actual.grupos)):
        filas = erpnext.get_list(doctype, fields=["name"], limit=TOPE_VOCABULARIO)
        destino.update(str(f.get("name") or "").strip() for f in filas)
        if len(filas) >= TOPE_VOCABULARIO:
            print(
                f"  ! leí {TOPE_VOCABULARIO} {doctype} y puede haber más: si te marca "
                f"como inexistente algo que existe, es esto."
            )

    for tanda in _en_tandas(codigos):
        for item in erpnext.get_list(
            "Item",
            filters=[["item_code", "in", tanda]],
            fields=["item_code", "item_name", "stock_uom", "item_group"],
            limit=len(tanda),
        ):
            actual.items[str(item.get("item_code") or "").strip()] = item
        for precio in erpnext.get_list(
            "Item Price",
            filters=[
                ["item_code", "in", tanda],
                ["price_list", "=", lista],
                ["selling", "=", 1],
            ],
            fields=["name", "item_code", "price_list_rate"],
            limit=len(tanda) * 2,
        ):
            actual.precios.setdefault(str(precio.get("item_code") or "").strip(), precio)
        for bin_ in erpnext.get_list(
            "Bin",
            filters=[["item_code", "in", tanda], ["warehouse", "=", deposito]],
            fields=["item_code", "actual_qty"],
            limit=len(tanda),
        ):
            actual.stock[str(bin_.get("item_code") or "").strip()] = Decimal(
                str(bin_.get("actual_qty") or 0)
            )

    # Un ajuste en BORRADOR todavía no movió el stock, así que el Bin sigue en
    # cero y sin esto cada corrida agregaría otro borrador para lo mismo.
    #
    # Se pregunta por los RENGLONES y no por las cabeceras. Recorrer las
    # cabeceras obliga a leer cada documento entero para ver qué trae, y a
    # cortar la lista en algún número: el día que el ajuste que interesa quede
    # afuera de ese corte, esto duplica el borrador en silencio. Preguntar por
    # la tabla hija filtrando por los códigos del archivo no tiene ese corte y
    # es una sola consulta por tanda. Es el mismo camino que usa
    # `app/inventario.py` para encontrar el último conteo de un producto.
    for tanda in _en_tandas(codigos):
        for renglon in erpnext.get_list(
            "Stock Reconciliation Item",
            filters=[
                ["item_code", "in", tanda],
                ["warehouse", "=", deposito],
                ["docstatus", "!=", 2],
            ],
            fields=["parent", "item_code"],
            limit=len(tanda) * 10,
            parent="Stock Reconciliation",
            order_by="modified desc",
        ):
            codigo = str(renglon.get("item_code") or "").strip()
            padre = str(renglon.get("parent") or "").strip()
            if codigo and padre:
                actual.ajustes.setdefault(codigo, padre)
    return actual


def problemas_contra_erpnext(filas: list[Fila], actual: EnErpnext) -> list[str]:
    """Lo que sólo se puede saber mirando ERPNext, en la misma lista numerada."""
    problemas = []
    for fila in filas:
        if fila.unidad not in actual.unidades:
            parecidas = sorted(
                u for u in actual.unidades if u.lower() == fila.unidad.lower()
            )
            pista = (
                f" ¿Querías «{parecidas[0]}»? Es la misma palabra con otras mayúsculas, "
                f"y para ERPNext son dos unidades distintas."
                if parecidas
                else " Creala en ERPNext primero, o usá una de las que ya existen."
            )
            problemas.append(f"línea {fila.linea}: la unidad «{fila.unidad}» no existe en ERPNext.{pista}")
        ya = actual.items.get(fila.codigo)
        if ya and str(ya.get("stock_uom") or "").strip() != fila.unidad:
            problemas.append(
                f"línea {fila.linea}: «{fila.codigo}» ya existe en ERPNext con unidad "
                f"«{ya.get('stock_uom')}» y el archivo dice «{fila.unidad}». ERPNext no deja "
                f"cambiarle la unidad a un producto con movimientos: arreglá el archivo, "
                f"o creá un código nuevo."
            )
    return problemas


# ---------------------------------------------------------------- el plan


def planificar(filas: list[Fila], actual: EnErpnext) -> Plan:
    plan = Plan()
    for grupo in dict.fromkeys(f.grupo for f in filas):
        if grupo not in actual.grupos:
            plan.grupos_nuevos.append(grupo)

    for fila in filas:
        ya = actual.items.get(fila.codigo)
        if ya is None:
            plan.items_nuevos.append(fila)
        else:
            cambios = {}
            if str(ya.get("item_name") or "").strip() != fila.nombre:
                cambios["item_name"] = fila.nombre
            if str(ya.get("item_group") or "").strip() != fila.grupo:
                cambios["item_group"] = fila.grupo
            (plan.items_cambiados.append((fila, cambios)) if cambios
             else plan.items_iguales.append(fila))

        precio = actual.precios.get(fila.codigo)
        if precio is None:
            plan.precios_nuevos.append(fila)
        else:
            vigente = Decimal(str(precio.get("price_list_rate") or 0))
            (plan.precios_iguales.append(fila) if vigente == fila.precio
             else plan.precios_cambiados.append(
                 (fila, vigente, str(precio.get("name") or ""))
             ))

        if fila.stock <= 0:
            plan.stock_omitido.append((fila, "el archivo dice 0"))
        elif actual.stock.get(fila.codigo, Decimal(0)) > 0:
            plan.stock_omitido.append(
                (fila, f"ya hay {actual.stock[fila.codigo]} en el depósito")
            )
        elif fila.codigo in actual.ajustes:
            plan.stock_omitido.append(
                (fila, f"ya está en el ajuste {actual.ajustes[fila.codigo]}")
            )
        else:
            plan.stock_a_cargar.append(fila)
    return plan


def imprimir_plan(plan: Plan, lista: str, moneda: str, costo_pct: Decimal) -> None:
    def bloque(titulo: str, lineas: list[str]) -> None:
        print(f"\n{titulo} ({len(lineas)})")
        for linea in lineas[:40]:
            print(f"  {linea}")
        if len(lineas) > 40:
            print(f"  … y {len(lineas) - 40} más")

    bloque("Grupos a crear", [f"+ {g}" for g in plan.grupos_nuevos])
    bloque(
        "Productos",
        [f"+ {f.codigo}  {f.nombre}" for f in plan.items_nuevos]
        + [
            f"~ {f.codigo}  {', '.join(f'{k} → {v}' for k, v in c.items())}"
            for f, c in plan.items_cambiados
        ]
        + [f"= {f.codigo}  sin cambios" for f in plan.items_iguales],
    )
    bloque(
        f"Precios (lista «{lista}»{f', {moneda}' if moneda else ''})",
        [f"+ {f.codigo}  {f.precio}" for f in plan.precios_nuevos]
        + [f"~ {f.codigo}  {vieja} → {f.precio}" for f, vieja, _ in plan.precios_cambiados]
        + [f"= {f.codigo}  {f.precio}" for f in plan.precios_iguales],
    )
    bloque(
        "Stock inicial (un solo Stock Reconciliation, en BORRADOR)",
        [f"+ {f.codigo}  {f.stock}" for f in plan.stock_a_cargar]
        + [f"= {f.codigo}  {motivo}" for f, motivo in plan.stock_omitido],
    )
    if plan.stock_a_cargar:
        print(
            f"\n  La valuación del ajuste es el {costo_pct}% del precio de venta, que es "
            f"un número INVENTADO (el mismo que usa seed_dairy.py). Es el valor contable "
            f"del inventario: no cambia lo que cotiza el bot. Poné el real con --costo-pct."
        )


# --------------------------------------------------------------- escribir


def aplicar(plan: Plan, deposito: str, empresa: str, lista: str, costo_pct: Decimal) -> None:
    for grupo in plan.grupos_nuevos:
        erpnext.create_doc("Item Group", {
            "item_group_name": grupo,
            "parent_item_group": "All Item Groups",
            "is_group": 0,
        })
        print(f"  + Item Group {grupo}")

    for fila in plan.items_nuevos:
        erpnext.create_doc("Item", {
            "item_code": fila.codigo,
            "item_name": fila.nombre,
            "item_group": fila.grupo,
            "stock_uom": fila.unidad,
            "is_stock_item": 1,
            "description": fila.nombre,
        })
        print(f"  + Item {fila.codigo}")
    for fila, cambios in plan.items_cambiados:
        cuentas.pedido_admin("PUT", cuentas.ruta_recurso("Item", fila.codigo), cambios)
        print(f"  ~ Item {fila.codigo}: {', '.join(cambios)}")

    for fila in plan.precios_nuevos:
        erpnext.create_doc("Item Price", {
            "item_code": fila.codigo,
            "price_list": lista,
            "price_list_rate": float(fila.precio),
            "selling": 1,
        })
        print(f"  + precio {fila.codigo} = {fila.precio}")
    for fila, vieja, nombre in plan.precios_cambiados:
        cuentas.pedido_admin(
            "PUT", cuentas.ruta_recurso("Item Price", nombre),
            {"price_list_rate": float(fila.precio)},
        )
        print(f"  ~ precio {fila.codigo}: {vieja} → {fila.precio}")

    if not plan.stock_a_cargar:
        print("  = stock: no hay nada nuevo que cargar")
        return
    items = [
        {
            "item_code": fila.codigo,
            "warehouse": deposito,
            "qty": float(fila.stock),
            "valuation_rate": float(round(fila.precio * costo_pct / 100, 2)),
        }
        for fila in plan.stock_a_cargar
    ]
    try:
        doc = erpnext.create_doc(
            "Stock Reconciliation", cuentas.payload_reconciliacion(empresa, items)
        )
    except erpnext.ERPNextError as exc:
        print(f"  ✗ el ajuste de stock NO se creó: {exc}")
        print(
            "    Si dice 417, a la compañía le faltan las cuentas de inventario. "
            "Corré primero:  python deploy/cuentas_inventario.py --aplicar --probar"
        )
        raise
    print(f"  + Stock Reconciliation {doc['name']} con {len(items)} producto(s), EN BORRADOR")
    print("    Confirmalo en ERPNext para que el stock exista de verdad.")


# --------------------------------------------------------------- verificar


def configuracion_incompatible(lista: str, moneda: str) -> str:
    """¿El bot va a poder LEER los precios que estoy por escribir?

    El runtime no lee «la lista de precios»: lee EXACTAMENTE la que nombra
    `AUTO_CONFIRM_PRICE_LIST`, y filtra por `AUTO_CONFIRM_CURRENCY`
    (`app/tools/catalogo.py` para cotizar, `app/policy.py` para
    auto-confirmar). Los precios se escriben en la lista del seed, que es la
    que corresponde; pero si el despliegue está configurado para leer OTRA, el
    catálogo entra y el bot no ve ni un precio.

    Eso no es un aviso: es cargar un catálogo invisible. Se rechaza antes de
    escribir, con las dos puntas nombradas, y lo arregla el que sabe cuál de
    las dos está mal — el .env o la lista.
    """
    configurada = os.getenv("AUTO_CONFIRM_PRICE_LIST", "").strip()
    if configurada and configurada != lista:
        return (
            f"escribiría los precios en «{lista}» (la lista del seed) y el despliegue "
            f"lee AUTO_CONFIRM_PRICE_LIST=«{configurada}». El bot no vería ni uno de "
            f"estos precios. Igualá las dos: o cambiás el .env, o el catálogo va a "
            f"la lista que ya está configurada."
        )
    esperada = os.getenv("AUTO_CONFIRM_CURRENCY", "").strip()
    if moneda and esperada and moneda != esperada:
        return (
            f"la lista «{lista}» está en {moneda} y el despliegue lee "
            f"AUTO_CONFIRM_CURRENCY={esperada}. Con esa diferencia el bot tampoco "
            f"podría cotizar estos precios."
        )
    return ""


def verificar(filas: list[Fila], actual: EnErpnext, lista: str) -> int:
    """¿ERPNext dice lo mismo que el archivo? En las dos direcciones.

    El stock NO se compara y no es un olvido: el stock se mueve solo cada vez
    que se vende algo, así que una diferencia ahí es el negocio funcionando, no
    una deriva. Lo que no tiene por qué moverse solo es el precio.
    """
    faltan = [f for f in filas if f.codigo not in actual.items]
    distintos = [
        (f, Decimal(str(actual.precios[f.codigo].get("price_list_rate") or 0)))
        for f in filas
        if f.codigo in actual.precios
        and Decimal(str(actual.precios[f.codigo].get("price_list_rate") or 0)) != f.precio
    ]
    sin_precio = [f for f in filas if f.codigo in actual.items and f.codigo not in actual.precios]

    grupos = sorted({f.grupo for f in filas})
    del_archivo = {f.codigo for f in filas}
    sobran = [
        str(item.get("item_code") or "")
        for item in erpnext.get_list(
            "Item",
            filters=[["item_group", "in", grupos], ["disabled", "=", 0]],
            fields=["item_code"],
            limit=1000,
        )
        if str(item.get("item_code") or "") not in del_archivo
    ]

    print(f"\nEn el archivo pero NO en ERPNext ({len(faltan)})")
    for fila in faltan:
        print(f"  - {fila.codigo}  {fila.nombre}  (línea {fila.linea})")
    print(f"\nEn el archivo, en ERPNext, pero sin precio en «{lista}» ({len(sin_precio)})")
    for fila in sin_precio:
        print(f"  - {fila.codigo}  el bot no lo puede cotizar")
    print(f"\nEn ERPNext pero NO en el archivo ({len(sobran)})")
    print(f"  (miro sólo los grupos que nombra el archivo: {', '.join(grupos)})")
    for codigo in sobran:
        print(f"  - {codigo}")
    print(f"\nPrecios distintos ({len(distintos)})")
    for fila, en_erp in distintos:
        print(f"  - {fila.codigo}  ERPNext dice {en_erp}, el archivo dice {fila.precio} (línea {fila.linea})")

    total = len(faltan) + len(sin_precio) + len(sobran) + len(distintos)
    print(f"\n{'Coinciden.' if not total else f'{total} diferencia(s).'}")
    return 0 if not total else 1


# --------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    opciones = _opciones(argv if argv is not None else sys.argv[1:])
    if opciones.ejemplo is not None:
        try:
            escribir_ejemplo(Path(opciones.ejemplo))
        except FileExistsError as exc:
            print(exc)
            return 1
        return 0
    if not opciones.archivo:
        print("Falta el archivo CSV. Sacá la plantilla con:  --ejemplo")
        return 1

    ruta = Path(opciones.archivo)
    filas, problemas = leer(ruta)
    if not filas:
        # Sin una sola fila sana no hay nada que preguntarle a ERPNext: el
        # encabezado está mal, o el archivo entero lo está.
        return _rechazar(ruta, problemas)

    # Las dos mitades de la validación se juntan ANTES de rechazar. Separadas,
    # el dueño arreglaba los cinco errores del archivo, volvía a correrlo, y
    # recién ahí se enteraba del sexto (la unidad que ERPNext no conoce).
    lista = lista_de_precios()
    empresa, deposito = erpnext.default_context()
    actual = relevar(filas, deposito, lista)
    problemas += [f"{ruta} {p}" for p in problemas_contra_erpnext(filas, actual)]
    if problemas:
        return _rechazar(ruta, problemas)

    print(f"{ruta}: {len(filas)} producto(s). Empresa: {empresa} · depósito: {deposito}")
    if opciones.verificar:
        return verificar(filas, actual, lista)

    moneda = moneda_de(lista)
    desacuerdo = configuracion_incompatible(lista, moneda)
    if desacuerdo:
        print(f"\nNO CARGUÉ NADA. {desacuerdo}")
        return 1

    plan = planificar(filas, actual)
    imprimir_plan(plan, lista, moneda, opciones.costo_pct)
    if not opciones.aplicar:
        print(
            "\nSIMULACRO: no se escribió nada."
            + (" Volvé a correrlo con --aplicar." if plan.hay_algo else " Y no hay nada que escribir.")
        )
        return 0
    if not plan.hay_algo:
        print("\nNo hay nada que escribir: ERPNext ya dice lo mismo que el archivo.")
        return 0
    print("\nEscribiendo:")
    aplicar(plan, deposito, empresa, lista, opciones.costo_pct)
    print("\nListo.")
    return 0


def _rechazar(ruta: Path, problemas: list[str]) -> int:
    """Todo lo que está mal, numerado y en el orden en que se ve el archivo."""
    def _linea(texto: str) -> int:
        encontrado = re.search(r"línea (\d+)", texto)
        return int(encontrado.group(1)) if encontrado else 0

    print(f"NO CARGUÉ NADA. {len(problemas)} problema(s) en {ruta}:")
    for numero, problema in enumerate(sorted(problemas, key=_linea), start=1):
        print(f"  {numero}. {problema}")
    print("\nArreglá el archivo y volvé a correrlo. Nada de esto se escribió en ERPNext.")
    return 1


def _porcentaje(crudo: str) -> Decimal:
    valor = _decimal(crudo)
    if valor is None or not 0 < valor <= 100:
        raise argparse.ArgumentTypeError(f"«{crudo}» tiene que ser un número entre 0 y 100")
    return valor


def _opciones(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Carga el catálogo real del cliente en ERPNext desde un CSV. "
            "Sin --aplicar no escribe nada: sólo te muestra el plan."
        )
    )
    parser.add_argument("archivo", nargs="?", default="", help="el CSV con el catálogo")
    parser.add_argument(
        "--ejemplo", nargs="?", const="catalogo-ejemplo.csv", default=None,
        metavar="RUTA", help="escribir una plantilla para llenar (default: catalogo-ejemplo.csv)",
    )
    parser.add_argument("--aplicar", action="store_true", help="escribir de verdad en ERPNext")
    parser.add_argument(
        "--verificar", action="store_true",
        help="comparar ERPNext contra el archivo y mostrar las diferencias",
    )
    parser.add_argument(
        "--costo-pct", type=_porcentaje, default=Decimal(60),
        help="valuación del stock inicial como %% del precio de venta (default: 60)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
