"""Deja que ERPNext acepte un movimiento de stock. Lo corre UNA PERSONA.

SCRIPT DE DESPLIEGUE, NO UNA HERRAMIENTA. Igual que `deploy/seed_dairy.py`:
lo ejecuta el dueño (o quien despliega) con credenciales de Administrator
pasadas POR PROCESO, no está colgado de ningún tool del agente y no lo puede
invocar un mensaje de WhatsApp. Nadie lo cablee después a `app/tools/`: la
separación de las tres identidades (cliente / gerencia / política) es lo que
hace que este producto se pueda vender, y esto vive afuera de las tres.

Se corre DESDE `plus-agent/`, en el host y con el venv del proyecto — igual
que `make seed`. La imagen de producción sólo copia `app/`, así que dentro del
contenedor no existe `/srv/deploy`; y poner ahí scripts que pueden escribir con
credenciales de Administrator sería ensanchar justo lo que este archivo se
cuida de no ensanchar.

    ERPNEXT_API_KEY=... ERPNEXT_API_SECRET=... \
        .venv/bin/python deploy/cuentas_inventario.py            # mira
    ERPNEXT_API_KEY=... ERPNEXT_API_SECRET=... \
        .venv/bin/python deploy/cuentas_inventario.py --aplicar --probar

Si sólo tenés el contenedor, montale el directorio para esa corrida y nada más:

    docker compose run --rm -v "$PWD/deploy:/srv/deploy:ro" \
        -e ERPNEXT_API_KEY=... -e ERPNEXT_API_SECRET=... \
        agente python /srv/deploy/cuentas_inventario.py --aplicar --probar

EL PROBLEMA QUE ARREGLA
El paso de stock de `seed_dairy.py` nunca cargó nada: ERPNext contesta 417 al
crear el Stock Reconciliation. 417 es lo que Frappe devuelve ante un
ValidationError, y `app/erpnext.py` lo convierte en «ERPNext rechazó la
creación de Stock Reconciliation (estado 417)» — que es todo lo que ve el
dueño. La validación que falla es la de siempre en un ERPNext recién
instalado: la compañía no tiene *Default Inventory Account* ni *Stock
Adjustment Account*, así que el asiento del movimiento no se puede imputar a
ninguna cuenta y ERPNext se niega antes de escribir nada.

Mientras eso siga así, `STOCK_CONFIABLE=true` es mentira: no hay un solo
conteo cargado que respaldarlo.

QUÉ HACE, Y QUÉ NO
Busca las dos cuentas POR TIPO (`account_type`), no por nombre: un plan de
cuentas está en el idioma que eligió quien lo instaló, y «Stock In Hand» puede
llamarse «Mercadería en stock». Si la cuenta existe, la usa. Si no existe, la
crea bajo un grupo del MISMO tipo.

Lo que NO hace es adivinar dónde colgarla cuando no hay ningún grupo de ese
tipo: se niega y pide `--padre-inventario` / `--padre-ajuste`. Una cuenta
creada en la rama equivocada de un plan de cuentas real es un lío que después
alguien desarma a mano, y este script no tiene con qué elegir bien.

Nunca confirma (submit) nada. La prueba de `--probar` crea un BORRADOR de
Stock Reconciliation y lo borra enseguida: un borrador no toca el mayor ni el
stock. Es la única forma de probar de verdad que el 417 se fue, porque la
validación que lo tiraba corre al guardar.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app import erpnext

# Los dos campos de la compañía que ERPNext exige para imputar un movimiento
# de stock, con el `account_type` que tiene que tener cada cuenta y el nombre
# con el que el propio ERPNext las crea cuando arma un plan de cuentas nuevo.
INVENTARIO = "default_inventory_account"
AJUSTE = "stock_adjustment_account"

TIPO = {INVENTARIO: "Stock", AJUSTE: "Stock Adjustment"}
RAIZ = {INVENTARIO: "Asset", AJUSTE: "Expense"}
NOMBRE_SUGERIDO = {INVENTARIO: "Stock In Hand", AJUSTE: "Stock Adjustment"}
SE_LLAMA = {
    INVENTARIO: "Default Inventory Account",
    AJUSTE: "Stock Adjustment Account",
}


@dataclass(frozen=True)
class Cuenta:
    """Una de las dos cuentas, como está y como habría que dejarla.

    `padre` sólo viene cuando hay que crearla; en los otros casos `nombre` ya
    es el nombre completo de una cuenta que existe.
    """

    campo: str
    actual: str
    nombre: str
    accion: str  # "ya está" | "se asigna" | "se crea y se asigna" | "falta"
    padre: str = ""
    detalle: str = ""

    @property
    def hay_que_tocar(self) -> bool:
        return self.accion in ("se asigna", "se crea y se asigna")

    @property
    def descripcion(self) -> str:
        if self.accion == "falta":
            return "(ninguna)"
        return f"{self.nombre} (nueva, bajo {self.padre})" if self.padre else self.nombre


def ruta_recurso(doctype: str, nombre: str | None = None) -> str:
    """La URL de un documento, con el nombre escapado.

    Un nombre de documento es texto libre: un código de producto con una barra
    («LEC/1L») pega una ruta que no es la del documento, y uno con `#` o `?` la
    corta. `app/erpnext.py` escapa con `safe=""` por esta misma razón; esto
    hace lo mismo para los pedidos que arma este script.
    """
    base = f"/api/resource/{quote(doctype, safe='')}"
    return base if nombre is None else f"{base}/{quote(nombre, safe='')}"


def pedido_admin(metodo: str, ruta: str, payload: dict | None = None) -> dict:
    """Un pedido con las credenciales que el humano pasó por proceso.

    `app/erpnext.py` no expone un update genérico NI DEBE EXPONERLO: cada
    escritura de ahí es angosta a propósito (un estado, una fecha, un cargo) y
    agregar un «PUT cualquier cosa» sería regalarle a la identidad de política
    el permiso que justamente no tiene. Este script corre con Administrator,
    que es otra cosa, así que arma su propio pedido acá y no ensancha nada de
    lo que usa el runtime.
    """
    url = os.environ["ERPNEXT_URL"].rstrip("/")
    clave = os.environ["ERPNEXT_API_KEY"].strip()
    secreto = os.environ["ERPNEXT_API_SECRET"].strip()
    try:
        respuesta = httpx.request(
            metodo,
            f"{url}{ruta}",
            headers={
                "Authorization": f"token {clave}:{secreto}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise erpnext.ERPNextError(f"ERPNext no disponible durante {metodo} {ruta}") from exc
    if respuesta.status_code >= 400:
        raise erpnext.ERPNextError(
            f"ERPNext rechazó {metodo} {ruta} (estado {respuesta.status_code})"
        )
    try:
        cuerpo = respuesta.json()
    except ValueError:
        return {}
    return cuerpo if isinstance(cuerpo, dict) else {}


def cuentas_por_tipo(empresa: str, account_type: str, *, grupo: bool) -> list[str]:
    """Las cuentas de ese tipo en esa compañía, hojas o grupos, no deshabilitadas."""
    filas = erpnext.get_list(
        "Account",
        filters=[
            ["company", "=", empresa],
            ["is_group", "=", 1 if grupo else 0],
            ["account_type", "=", account_type],
            ["disabled", "=", 0],
        ],
        fields=["name"],
        limit=50,
    )
    return [str(f.get("name") or "").strip() for f in filas if str(f.get("name") or "").strip()]


def _cuenta_de_la_empresa(empresa: str, campo: str) -> str:
    return str(erpnext.get_doc("Company", empresa).get(campo) or "").strip()


def resolver(
    empresa: str, campo: str, *, padre: str = "", nombre: str = "", cuenta: str = ""
) -> Cuenta:
    """¿Qué cuenta le corresponde a este campo, y hay que hacer algo para tenerla?

    No escribe: decide. Así el diagnóstico (sin `--aplicar`) y el arreglo
    dicen exactamente lo mismo, y el dueño lee el plan antes de que pase.
    """
    actual = _cuenta_de_la_empresa(empresa, campo)
    if actual:
        return Cuenta(campo, actual, actual, "ya está")

    if cuenta.strip():
        return Cuenta(
            campo, "", cuenta.strip(), "se asigna", detalle="la elegiste vos en la línea de comandos"
        )

    hojas = cuentas_por_tipo(empresa, TIPO[campo], grupo=False)
    if len(hojas) == 1:
        return Cuenta(
            campo, "", hojas[0], "se asigna",
            detalle=f"es la única con account_type={TIPO[campo]} en el plan de cuentas",
        )
    if len(hojas) > 1:
        # Elegir "la primera" de una lista que ERPNext no ordena es elegir al
        # azar en qué rama contable cae TODO el movimiento de stock, y quedaría
        # escrito en la compañía sin que nadie lo haya decidido.
        return Cuenta(
            campo, "", "", "falta",
            detalle=_elegí(campo, "cuenta", hojas, f"tienen account_type={TIPO[campo]}"),
        )

    if padre.strip():
        destino = padre.strip()
    else:
        grupos = cuentas_por_tipo(empresa, TIPO[campo], grupo=True)
        if len(grupos) > 1:
            return Cuenta(
                campo, "", "", "falta",
                detalle=_elegí(campo, "padre", grupos, f"son grupos con account_type={TIPO[campo]}"),
            )
        destino = next(iter(grupos), "")
    if not destino:
        return Cuenta(
            campo, "", "", "falta",
            detalle=(
                f"no hay ninguna cuenta con account_type={TIPO[campo]} en {empresa}, "
                f"ni hoja ni grupo: decime bajo qué grupo crearla con "
                f"--padre-{_sufijo(campo)} \"Nombre exacto de la cuenta grupo\""
            ),
        )
    return Cuenta(
        campo, "", (nombre or NOMBRE_SUGERIDO[campo]).strip(), "se crea y se asigna",
        padre=destino,
    )


def _sufijo(campo: str) -> str:
    return "inventario" if campo == INVENTARIO else "ajuste"


def _elegí(campo: str, flag: str, candidatas: list[str], porque: str) -> str:
    """El mensaje de «hay varias y no elijo yo», con la lista y el comando."""
    return (
        f"hay {len(candidatas)} candidatas y {porque}, así que no elijo yo: "
        + ", ".join(f"«{c}»" for c in sorted(candidatas))
        + f". Decime cuál con --{flag}-{_sufijo(campo)} \"Nombre exacto\""
    )


def crear_cuenta(empresa: str, campo: str, padre: str, nombre: str) -> str:
    """Crea la cuenta hoja y devuelve el nombre con el que quedó en ERPNext."""
    doc = erpnext.create_doc("Account", {
        "account_name": nombre,
        "parent_account": padre,
        "company": empresa,
        "account_type": TIPO[campo],
        "root_type": RAIZ[campo],
        "is_group": 0,
    })
    return str(doc["name"])


def asignar(empresa: str, cambios: dict[str, str]) -> None:
    """Escribe los campos en la compañía y VERIFICA que hayan quedado.

    Un PUT de Frappe es un save, no una escritura de campo: el `validate()`
    del doctype puede devolver 200 con el valor viejo adentro. Reportar eso
    como éxito sería justo la mentira que este script existe para no contar.
    """
    cuerpo = pedido_admin("PUT", ruta_recurso("Company", empresa), cambios)
    datos = cuerpo.get("data") if isinstance(cuerpo.get("data"), dict) else {}
    for campo, esperado in cambios.items():
        quedo = str(datos.get(campo) or "").strip()
        if quedo != esperado:
            raise erpnext.ERPNextError(
                f"ERPNext no dejó {SE_LLAMA[campo]} en «{esperado}»: "
                f"quedó en «{quedo or 'vacío'}»"
            )


def payload_reconciliacion(empresa: str, items: list[dict]) -> dict:
    """El payload de un ajuste de stock, con la cuenta de gasto que corresponda.

    Misma forma que arma `seed_dairy.py`, reescrita acá a propósito: ese
    archivo es de otra sesión esta semana y colgarse de una función privada
    suya sería romperse cuando la renombre.
    """
    transitoria = next(iter(cuentas_por_tipo(empresa, "Temporary", grupo=False)), "")
    if transitoria:
        return {
            "company": empresa,
            "purpose": "Opening Stock",
            "expense_account": transitoria,
            "items": items,
        }
    payload = {"company": empresa, "purpose": "Stock Reconciliation", "items": items}
    ajuste = next(iter(cuentas_por_tipo(empresa, "Stock Adjustment", grupo=False)), "")
    if ajuste:
        payload["expense_account"] = ajuste
    return payload


def probar_paso_de_stock(empresa: str, deposito: str) -> bool:
    """Crea un BORRADOR de Stock Reconciliation, dice qué contestó, y lo borra.

    Es la prueba y no una promesa: la validación que tiraba el 417 corre al
    guardar, así que la única forma de saber que se fue es guardar. Un
    borrador no mueve stock ni escribe en el mayor, y se borra en el `finally`.
    """
    articulos = erpnext.get_list(
        "Item",
        filters=[["is_stock_item", "=", 1], ["disabled", "=", 0]],
        fields=["item_code"],
        limit=1,
    )
    if not articulos:
        print("  ! no hay ningún producto: sembrá el catálogo antes de probar")
        return False
    codigo = str(articulos[0].get("item_code") or "").strip()

    payload = payload_reconciliacion(
        empresa,
        [{"item_code": codigo, "warehouse": deposito, "qty": 1, "valuation_rate": 1}],
    )
    print(f"  probando el paso de stock: {payload['purpose']} de 1 × {codigo} en {deposito}")
    if "expense_account" in payload:
        print(f"    cuenta de gasto: {payload['expense_account']}")

    nombre = ""
    try:
        nombre = str(erpnext.create_doc("Stock Reconciliation", payload)["name"])
    except erpnext.ERPNextError as exc:
        print(f"  ✗ SIGUE FALLANDO: {exc}")
        return False
    finally:
        if nombre:
            try:
                pedido_admin("DELETE", ruta_recurso("Stock Reconciliation", nombre))
                print(f"  ✓ ERPNext ACEPTÓ el borrador {nombre} (y se borró: era la prueba)")
            except erpnext.ERPNextError as exc:
                print(f"  ✓ ERPNext aceptó el borrador {nombre}, pero no se pudo borrar: {exc}")
                print("    borralo a mano en ERPNext: era sólo la prueba.")
    return True


def main(argv: list[str] | None = None) -> int:
    opciones = _opciones(argv if argv is not None else sys.argv[1:])
    empresa, deposito = erpnext.default_context()
    print(f"Empresa: {empresa} · depósito: {deposito}")
    print("Modo: ESCRIBE (--aplicar)" if opciones.aplicar else "Modo: sólo miro (sin --aplicar)")

    padres = {INVENTARIO: opciones.padre_inventario, AJUSTE: opciones.padre_ajuste}
    nombres = {INVENTARIO: opciones.nombre_inventario, AJUSTE: opciones.nombre_ajuste}
    elegidas = {INVENTARIO: opciones.cuenta_inventario, AJUSTE: opciones.cuenta_ajuste}
    cuentas = [
        resolver(
            empresa, campo,
            padre=padres[campo], nombre=nombres[campo], cuenta=elegidas[campo],
        )
        for campo in (INVENTARIO, AJUSTE)
    ]

    print("\nCuentas:")
    for cuenta in cuentas:
        marca = {"ya está": "=", "se asigna": "~", "se crea y se asigna": "+", "falta": "!"}
        print(f"  {marca[cuenta.accion]} {SE_LLAMA[cuenta.campo]}: {cuenta.descripcion}")
        print(f"      {cuenta.accion}" + (f" — {cuenta.detalle}" if cuenta.detalle else ""))

    if any(c.accion == "falta" for c in cuentas):
        print("\nNo puedo seguir: me falta saber qué cuenta usar o dónde crearla (ver arriba).")
        return 1

    pendientes = [c for c in cuentas if c.hay_que_tocar]
    if not pendientes:
        print("\nNo hay nada que cambiar: la compañía ya tiene las dos cuentas.")
    elif not opciones.aplicar:
        print(f"\n{len(pendientes)} cambio(s) pendiente(s). Nada de esto se escribió.")
        print("Volvé a correrlo con --aplicar para que pase.")
    else:
        cambios: dict[str, str] = {}
        for cuenta in pendientes:
            if cuenta.padre:
                creada = crear_cuenta(empresa, cuenta.campo, cuenta.padre, cuenta.nombre)
                print(f"  + cuenta creada: {creada}")
                cambios[cuenta.campo] = creada
            else:
                cambios[cuenta.campo] = cuenta.nombre
        asignar(empresa, cambios)
        for campo, valor in cambios.items():
            print(f"  ~ {SE_LLAMA[campo]} = {valor}")
        print(f"\n{len(cambios)} campo(s) escritos y verificados en la compañía.")

    if not opciones.probar:
        print("\nPara probar que el paso de stock ya no da 417, corré esto con --probar.")
        return 0

    print("\nLa prueba que cuenta:")
    return 0 if probar_paso_de_stock(empresa, deposito) else 1


def _opciones(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Deja la compañía de ERPNext con las cuentas que un movimiento de "
            "stock necesita. Sin --aplicar no escribe nada."
        )
    )
    parser.add_argument("--aplicar", action="store_true", help="escribir de verdad")
    parser.add_argument(
        "--probar",
        action="store_true",
        help="crear (y borrar) un borrador de Stock Reconciliation para ver si ERPNext lo acepta",
    )
    parser.add_argument("--cuenta-inventario", default="", help="usar ESTA cuenta de inventario, sin buscarla")
    parser.add_argument("--cuenta-ajuste", default="", help="usar ESTA cuenta de ajuste, sin buscarla")
    parser.add_argument("--padre-inventario", default="", help="cuenta grupo bajo la cual crear la de inventario")
    parser.add_argument("--padre-ajuste", default="", help="cuenta grupo bajo la cual crear la de ajuste")
    parser.add_argument("--nombre-inventario", default="", help=f"default: {NOMBRE_SUGERIDO[INVENTARIO]}")
    parser.add_argument("--nombre-ajuste", default="", help=f"default: {NOMBRE_SUGERIDO[AJUSTE]}")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
