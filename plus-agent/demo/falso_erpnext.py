"""Un ERPNext de mentira, con la forma exacta de la API que usa app/erpnext.py.

NO es un ERPNext. Es el subconjunto de la REST de Frappe que este sistema
toca, en memoria, para que el banco de pruebas de demo/ pueda correr los diez
escenarios sin ERPNext, sin MariaDB y sin red.

QUÉ IMITA A PROPÓSITO
  * el sobre  {"data": …}  de /api/resource, y  {"message": …}  de /api/method
  * los filtros de Frappe en su forma de tres elementos [campo, op, valor],
    con =, !=, like, in, not in, >, >=, <, <=
  * las tablas hijas: una fila de "Sales Order Item" se consulta como su propio
    doctype pasando parent="Sales Order", y su docstatus sigue al del padre
  * order_by / limit_page_length / limit_start, porque la reconstrucción del
    índice de solicitudes pagina comentarios por "creation desc" y una
    paginación sin orden estable le haría saltear y repetir eventos
  * un PUT es un SAVE, no un write de campo: se guarda, se recalculan los
    totales y se devuelve el documento COMPLETO
  * LOS TRES PERMISOS. La credencial de agente y la de gerencia NO pueden
    llevar un documento a docstatus 1 ni 2; sólo la de política. Si el código
    de la app se equivocara de identidad para confirmar, acá da 403, igual que
    en un ERPNext bien configurado. El banco de pruebas valida eso, no lo
    supone.

QUÉ NO IMITA
  Nada de contabilidad real, ni asientos, ni reservas de stock, ni permisos por
  campo, ni workflows. Los totales se calculan con la única regla que el código
  de la app verifica: agregar un cargo de $X sube el grand_total en $X.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime, timedelta
from typing import Any

# ------------------------------------------------------------------ permisos

# (clave, secreto) -> (usuario, puede_confirmar). Son de mentira y no se
# parecen a ninguna credencial real: el banco de pruebas verifica que las que
# llegan sean EXACTAMENTE éstas antes de arrancar (demo/guardas.py).
IDENTIDADES = {
    ("demo-agente-key", "demo-agente-secret"): ("agente@demo.invalid", False),
    ("demo-gerencia-key", "demo-gerencia-secret"): ("gerencia@demo.invalid", False),
    ("demo-politica-key", "demo-politica-secret"): ("politica@demo.invalid", True),
}

EMPRESA = "Lacteos Demo SA"
DEPOSITO = "Principal - LD"
MONEDA = "ARS"
LISTA_PRECIOS = "Standard Selling"

# Doctypes que son tablas hijas, y por qué campo cuelgan del padre.
HIJAS = {
    "Sales Order Item": ("Sales Order", "items"),
    "Delivery Note Item": ("Delivery Note", "items"),
    "Stock Reconciliation Item": ("Stock Reconciliation", "items"),
    "Dynamic Link": ("Address", "links"),
}

# Doctypes cuyo nombre es un campo del documento, no una serie.
NOMBRE_POR_CAMPO = {
    "Customer": "customer_name",
    "Item": "item_code",
    "Company": "company_name",
    "Address": "address_title",
}

SERIES = {
    "Sales Order": "SAL-ORD-{anio}-{n:05d}",
    "Delivery Note": "MAT-DN-{anio}-{n:05d}",
    "Sales Invoice": "ACC-SINV-{anio}-{n:05d}",
    "Stock Reconciliation": "MAT-RECO-{anio}-{n:05d}",
    "Lead": "CRM-LEAD-{anio}-{n:05d}",
    "Comment": "comment-{n:08d}",
    "ToDo": "todo-{n:06d}",
    "Item Price": "price-{n:06d}",
    "Bin": "bin-{n:06d}",
    "Item Reorder": "reorder-{n:06d}",
    "Dynamic Link": "link-{n:06d}",
}


class Rechazo(Exception):
    """Un error que el ERPNext de mentira devuelve como HTTP."""

    def __init__(self, estado: int, mensaje: str) -> None:
        super().__init__(mensaje)
        self.estado = estado
        self.mensaje = mensaje


# ------------------------------------------------------------------ filtros


def _like(valor: str, patron: str) -> bool:
    """El LIKE de SQL: % es cualquier cosa, _ es un carácter.

    Los comodines se separan ANTES de escapar. re.escape no toca el %, así que
    escapar primero y reemplazar la barra-% después no encuentra nada y un filtro
    `like` devuelve cero filas — que es como "no existe" y no como "no se
    pudo buscar". app/clientes.py busca el teléfono con
    `mobile_no like %5%4%9%…%`, así que un like roto es un cliente que el
    sistema deja de reconocer.
    """
    salida = []
    for trozo in re.split(r"([%_])", str(patron)):
        if trozo == "%":
            salida.append(".*")
        elif trozo == "_":
            salida.append(".")
        else:
            salida.append(re.escape(trozo))
    return re.fullmatch(
        "".join(salida), str(valor), flags=re.IGNORECASE | re.DOTALL
    ) is not None


def _comparable(a: Any, b: Any) -> tuple[Any, Any]:
    """Números con números, fechas con fechas, y si no, texto con texto."""
    for tipo in (int, float):
        if isinstance(a, tipo) or isinstance(b, tipo):
            try:
                return float(a), float(b)
            except (TypeError, ValueError):
                break
    return str(a), str(b)


def _cumple(doc: dict, campo: str, op: str, valor: Any) -> bool:
    actual = doc.get(campo)
    op = str(op).strip().lower()
    if op in {"=", "=="}:
        return str(actual) == str(valor)
    if op in {"!=", "not ="}:
        return str(actual) != str(valor)
    if op == "like":
        return _like("" if actual is None else actual, valor)
    if op == "not like":
        return not _like("" if actual is None else actual, valor)
    if op == "in":
        return str(actual) in {str(v) for v in (valor or [])}
    if op == "not in":
        return str(actual) not in {str(v) for v in (valor or [])}
    if op in {">", "<", ">=", "<="}:
        if actual is None:
            return False
        izq, der = _comparable(actual, valor)
        return {
            ">": izq > der, "<": izq < der, ">=": izq >= der, "<=": izq <= der
        }[op]
    raise Rechazo(417, f"operador de filtro no soportado por el doble: {op!r}")


def _filtrar(docs: list[dict], filtros: Any) -> list[dict]:
    if not filtros:
        return docs
    if not isinstance(filtros, list):
        raise Rechazo(417, "filters tiene que ser una lista")
    salida = docs
    for f in filtros:
        if not isinstance(f, list):
            raise Rechazo(417, "cada filtro tiene que ser una lista")
        if len(f) == 3:
            campo, op, valor = f
        elif len(f) == 4:
            # [doctype, campo, op, valor] — la otra forma que acepta Frappe.
            _, campo, op, valor = f
        else:
            raise Rechazo(417, f"filtro de largo {len(f)} no soportado")
        salida = [d for d in salida if _cumple(d, str(campo), str(op), valor)]
    return salida


def _ordenar(docs: list[dict], order_by: str | None) -> list[dict]:
    """Ordena como Frappe: "campo desc", "campo asc" o "campo"."""
    if not order_by:
        return docs
    partes = str(order_by).replace("`", " ").split(",")[0].strip().split()
    if not partes:
        return docs
    campo = partes[0].split(".")[-1].strip()
    reves = len(partes) > 1 and partes[1].lower().startswith("desc")

    def clave(d: dict) -> tuple[int, str]:
        v = d.get(campo)
        # Los que no tienen el campo van siempre al final, en los dos sentidos.
        return (1, "") if v is None else (0, str(v))

    ordenados = sorted(docs, key=clave, reverse=reves)
    if reves:
        # sorted(reverse=True) también invierte el "van al final": corregilo.
        faltantes = [d for d in ordenados if d.get(campo) is None]
        presentes = [d for d in ordenados if d.get(campo) is not None]
        ordenados = presentes + faltantes
    return ordenados


# ------------------------------------------------------------------ almacén


class Almacen:
    """Los documentos, en memoria, con un candado porque el server es threaded."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, dict]] = {}
        self.candado = threading.RLock()
        self._n = 0
        self._reloj = 0
        # Todo lo que entró por HTTP, para el informe del banco de pruebas.
        self.pedidos_http: list[tuple[str, str]] = []

    # -- utilidades

    def _siguiente(self, doctype: str) -> str:
        self._n += 1
        plantilla = SERIES.get(doctype, doctype.lower().replace(" ", "-") + "-{n:06d}")
        return plantilla.format(anio=date.today().year, n=self._n)

    def _ahora(self) -> str:
        """Un instante distinto y creciente por documento.

        Frappe decide con `creation` cuál evento es el último, y la
        reconstrucción del índice de solicitudes pagina por ahí. Dos
        comentarios con el mismo microsegundo harían que el orden dependa del
        azar, que es exactamente el bug que ese código evita.
        """
        self._reloj += 1
        return (datetime(2026, 9, 1) + timedelta(microseconds=self._reloj)).isoformat(
            sep=" ", timespec="microseconds"
        )

    def tabla(self, doctype: str) -> dict[str, dict]:
        return self.docs.setdefault(doctype, {})

    # -- lectura

    def listar(
        self,
        doctype: str,
        *,
        filtros: Any = None,
        campos: list[str] | None = None,
        limite: int = 20,
        inicio: int = 0,
        order_by: str | None = None,
        parent: str | None = None,
    ) -> list[dict]:
        with self.candado:
            if doctype in HIJAS and not parent:
                # Frappe se niega igual: una tabla hija no se lista sin padre.
                raise Rechazo(
                    403, f"{doctype} es una tabla hija: falta parent"
                )
            docs = list(self.tabla(doctype).values())
            docs = _filtrar(docs, filtros)
            docs = _ordenar(docs, order_by)
            recorte = docs[inicio : inicio + max(0, int(limite))]
            if not campos or "*" in campos:
                return [dict(d) for d in recorte]
            return [{c: d.get(c) for c in campos} for d in recorte]

    def leer(self, doctype: str, nombre: str) -> dict:
        with self.candado:
            doc = self.tabla(doctype).get(nombre)
            if doc is None:
                raise Rechazo(404, f"{doctype} {nombre} no existe")
            return dict(doc)

    # -- escritura

    def crear(self, doctype: str, payload: dict, *, puede_confirmar: bool) -> dict:
        with self.candado:
            estado = int(payload.get("docstatus") or 0)
            if estado != 0 and not puede_confirmar:
                raise Rechazo(403, "esta credencial no puede crear fuera de borrador")
            nombre = str(payload.get("name") or "").strip()
            if not nombre:
                campo = NOMBRE_POR_CAMPO.get(doctype)
                if campo and str(payload.get(campo) or "").strip():
                    nombre = str(payload[campo]).strip()
                else:
                    nombre = self._siguiente(doctype)
            if nombre in self.tabla(doctype):
                raise Rechazo(409, f"{doctype} {nombre} ya existe")
            momento = self._ahora()
            doc = {
                **payload,
                "name": nombre,
                "doctype": doctype,
                "docstatus": estado,
                "owner": payload.get("owner") or "demo@demo.invalid",
                "creation": payload.get("creation") or momento,
                "modified": momento,
            }
            doc.setdefault("company", EMPRESA)
            self._recalcular(doc)
            self.tabla(doctype)[nombre] = doc
            self._sincronizar_hijas(doc)
            return dict(doc)

    def guardar(
        self, doctype: str, nombre: str, cambios: dict, *, puede_confirmar: bool
    ) -> dict:
        """Un PUT de Frappe: mergea, revalida, recalcula y devuelve todo."""
        with self.candado:
            doc = self.tabla(doctype).get(nombre)
            if doc is None:
                raise Rechazo(404, f"{doctype} {nombre} no existe")
            antes = int(doc.get("docstatus") or 0)
            despues = int(cambios.get("docstatus", antes) or 0)
            if despues != antes:
                # ACÁ está la frontera de permisos que el sistema real delega a
                # ERPNext. Sin esto el banco de pruebas no probaría nada.
                if not puede_confirmar:
                    raise Rechazo(
                        403,
                        "esta credencial no tiene permiso de Submit/Cancel "
                        f"({doctype} {nombre}: {antes} -> {despues})",
                    )
                if antes == 1 and despues == 0:
                    raise Rechazo(417, "un documento confirmado no vuelve a borrador")
                if antes == 2:
                    raise Rechazo(417, "un documento cancelado no cambia de estado")
            if antes != 0 and any(
                c not in {"docstatus", "status"} for c in cambios
            ) and despues == antes:
                raise Rechazo(417, f"{doctype} {nombre} no es un borrador: no se edita")
            doc.update(cambios)
            doc["docstatus"] = despues
            doc["modified"] = self._ahora()
            if despues == 1 and antes == 0 and "status" not in cambios:
                doc["status"] = {
                    "Sales Order": "To Deliver and Bill",
                    "Delivery Note": "To Bill",
                }.get(doctype, "Submitted")
            if despues == 2:
                doc["status"] = "Cancelled"
            self._recalcular(doc)
            self._sincronizar_hijas(doc)
            return dict(doc)

    def borrar(self, doctype: str, nombre: str, *, puede_confirmar: bool) -> None:
        with self.candado:
            doc = self.tabla(doctype).get(nombre)
            if doc is None:
                raise Rechazo(404, f"{doctype} {nombre} no existe")
            if int(doc.get("docstatus") or 0) != 0:
                raise Rechazo(417, "sólo se borra un borrador")
            del self.tabla(doctype)[nombre]
            for hija, (padre, _campo) in HIJAS.items():
                if padre != doctype:
                    continue
                for fila in [
                    f for f in self.tabla(hija).values()
                    if str(f.get("parent")) == nombre
                ]:
                    self.tabla(hija).pop(str(fila.get("name")), None)

    # -- derivados

    def _recalcular(self, doc: dict) -> None:
        """Los totales, con la única regla que el código de la app verifica.

        grand_total = (suma de las líneas - descuento) + suma de los cargos,
        así que agregar un cargo de $X sube el grand_total en exactamente $X,
        que es lo que policy_agregar_cargo lee de vuelta y exige.
        """
        if doc.get("doctype") not in {"Sales Order", "Delivery Note", "Sales Invoice"}:
            return
        filas = [f for f in (doc.get("items") or []) if isinstance(f, dict)]
        lista = str(doc.get("selling_price_list") or "").strip() or LISTA_PRECIOS
        total = 0.0
        for fila in filas:
            cantidad = float(fila.get("qty") or 0)
            precio = float(fila.get("rate") or 0)
            if precio <= 0:
                # ERPNext valoriza la línea con la lista de precios de venta:
                # app/tools/pedidos.py manda item_code, qty y uom y NADA MÁS,
                # a propósito, para que el precio no lo elija un modelo. Un
                # doble que dejara la línea en 0 haría que todo pedido valga 0
                # y que los topes del dueño se evalúen contra la nada: los
                # escenarios pasarían sin probar ningún límite.
                precio = self._precio(str(fila.get("item_code") or ""),
                                      str(fila.get("uom") or ""), lista)
                fila["rate"] = precio
                fila["price_list_rate"] = precio
            fila["amount"] = round(cantidad * precio, 2)
            total += fila["amount"]
        doc["total_qty"] = round(sum(float(f.get("qty") or 0) for f in filas), 3)
        doc["total"] = round(total, 2)
        doc["net_total"] = round(total, 2)
        pct = float(doc.get("additional_discount_percentage") or 0)
        descuento = round(total * pct / 100.0, 2)
        doc["discount_amount"] = descuento
        cargos = sum(
            float(f.get("tax_amount") or 0)
            for f in (doc.get("taxes") or [])
            if isinstance(f, dict)
        )
        doc["total_taxes_and_charges"] = round(cargos, 2)
        doc["grand_total"] = round(total - descuento + cargos, 2)
        doc["rounded_total"] = doc["grand_total"]
        doc.setdefault("currency", MONEDA)

    def _precio(self, item_code: str, uom: str, lista: str) -> float:
        """El precio de venta vigente, como lo buscaría ERPNext al guardar."""
        if not item_code:
            return 0.0
        for fila in self.tabla("Item Price").values():
            if str(fila.get("item_code")) != item_code:
                continue
            if str(fila.get("price_list")) != lista:
                continue
            if not int(fila.get("selling") or 0):
                continue
            if uom and str(fila.get("uom") or uom) != uom:
                continue
            if fila.get("customer") or fila.get("batch_no"):
                continue
            return float(fila.get("price_list_rate") or 0)
        return 0.0

    def _sincronizar_hijas(self, doc: dict) -> None:
        """Publica las filas como su propio doctype, con el docstatus del padre.

        Frappe hace justo esto: una fila de tabla hija tiene su propio nombre y
        su docstatus sigue al del padre. app/inventario.py depende de eso —
        filtra "Stock Reconciliation Item" por docstatus 1 para no creerle a un
        conteo que todavía es borrador.
        """
        doctype = str(doc.get("doctype") or "")
        for hija, (padre, campo) in HIJAS.items():
            if padre != doctype:
                continue
            nombre_padre = str(doc.get("name"))
            viejas = {
                n: f for n, f in self.tabla(hija).items()
                if str(f.get("parent")) == nombre_padre
            }
            for n in viejas:
                del self.tabla(hija)[n]
            for orden, fila in enumerate(doc.get(campo) or [], start=1):
                if not isinstance(fila, dict):
                    continue
                nombre = str(fila.get("name") or "").strip() or self._siguiente(hija)
                fila["name"] = nombre
                # ERPNext copia stock_uom del Item al guardar el renglón, y
                # app/policy.py::_precio_estandar EXIGE que uom == stock_uom:
                # sin el campo, ninguna línea queda "a precio de lista" y nada
                # se auto-confirma nunca. Un doble sin esto haría creer que la
                # auto-confirmación está rota cuando lo que falta es el campo.
                if not str(fila.get("stock_uom") or "").strip():
                    item = self.tabla("Item").get(str(fila.get("item_code") or ""))
                    if item:
                        fila["stock_uom"] = item.get("stock_uom")
                fila.setdefault("conversion_factor", 1)
                self.tabla(hija)[nombre] = {
                    **fila,
                    "name": nombre,
                    "doctype": hija,
                    "parent": nombre_padre,
                    "parenttype": padre,
                    "parentfield": campo,
                    "idx": orden,
                    "docstatus": int(doc.get("docstatus") or 0),
                    "creation": doc.get("creation"),
                    "modified": doc.get("modified"),
                }


# --------------------------------------------------------------- reportes


def _accounts_receivable(almacen: Almacen, filtros: dict) -> list[dict]:
    """Las facturas con saldo, como las devuelve el reporte oficial.

    app/policy.py exige due_date en toda fila con outstanding_amount > 0: sin
    fecha levanta ERPNextError en vez de tratar la deuda como no vencida.
    """
    clientes = filtros.get("customer")
    if isinstance(clientes, str):
        clientes = [clientes]
    filas = []
    for doc in almacen.listar("Sales Invoice", limite=500):
        saldo = float(doc.get("outstanding_amount") or 0)
        if saldo <= 0:
            continue
        if clientes and str(doc.get("customer")) not in {str(c) for c in clientes}:
            continue
        filas.append(
            {
                "party": doc.get("customer"),
                "customer_name": doc.get("customer_name") or doc.get("customer"),
                "voucher_no": doc.get("name"),
                "due_date": doc.get("due_date"),
                "outstanding_amount": saldo,
                "invoiced_amount": float(doc.get("grand_total") or saldo),
            }
        )
    return filas


def _stock_balance(almacen: Almacen, filtros: dict) -> list[dict]:
    return [
        {
            "item_code": b.get("item_code"),
            "warehouse": b.get("warehouse"),
            "actual_qty": float(b.get("actual_qty") or 0),
        }
        for b in almacen.listar("Bin", limite=500)
    ]


REPORTES = {
    "Accounts Receivable": _accounts_receivable,
    "Stock Balance": _stock_balance,
}


# ------------------------------------------------------------------- HTTP


def manejar(
    almacen: Almacen,
    metodo: str,
    ruta: str,
    consulta: dict[str, list[str]],
    cuerpo: bytes,
    autorizacion: str,
) -> tuple[int, dict]:
    """Una request. Devuelve (estado, cuerpo json). No levanta."""
    try:
        return _manejar(almacen, metodo, ruta, consulta, cuerpo, autorizacion)
    except Rechazo as r:
        return r.estado, {"exception": r.mensaje, "exc_type": "DemoRechazo"}
    except Exception as exc:
        return 500, {"exception": f"{type(exc).__name__}: {exc}"}


def _identidad(autorizacion: str) -> tuple[str, bool]:
    crudo = str(autorizacion or "").strip()
    if not crudo.lower().startswith("token "):
        raise Rechazo(401, "falta el header Authorization: token clave:secreto")
    resto = crudo.split(None, 1)[1]
    if ":" not in resto:
        raise Rechazo(401, "el token va como clave:secreto")
    clave, secreto = resto.split(":", 1)
    quien = IDENTIDADES.get((clave.strip(), secreto.strip()))
    if quien is None:
        # Nunca repite la credencial que llegó.
        raise Rechazo(401, "credencial desconocida para el ERPNext de prueba")
    return quien


def _uno(consulta: dict[str, list[str]], clave: str, default: str = "") -> str:
    valores = consulta.get(clave) or []
    return valores[0] if valores else default


def _json(consulta: dict[str, list[str]], clave: str) -> Any:
    crudo = _uno(consulta, clave)
    if not crudo:
        return None
    try:
        return json.loads(crudo)
    except ValueError as exc:
        raise Rechazo(417, f"{clave} no es JSON válido") from exc


def _manejar(
    almacen: Almacen,
    metodo: str,
    ruta: str,
    consulta: dict[str, list[str]],
    cuerpo: bytes,
    autorizacion: str,
) -> tuple[int, dict]:
    almacen.pedidos_http.append((metodo, ruta))
    usuario, puede_confirmar = _identidad(autorizacion)

    if ruta == "/api/method/frappe.auth.get_logged_user":
        return 200, {"message": usuario}

    if ruta == "/api/method/frappe.desk.query_report.run":
        nombre = _uno(consulta, "report_name")
        filtros = _json(consulta, "filters") or {}
        fabrica = REPORTES.get(nombre)
        if fabrica is None:
            raise Rechazo(404, f"el doble no implementa el reporte {nombre!r}")
        return 200, {"message": {"result": fabrica(almacen, filtros)}}

    if not ruta.startswith("/api/resource/"):
        raise Rechazo(404, f"el doble no implementa {ruta}")

    from urllib.parse import unquote

    resto = ruta[len("/api/resource/") :]
    partes = [unquote(p) for p in resto.split("/") if p]
    if not partes:
        raise Rechazo(404, "falta el doctype")
    doctype = partes[0]
    nombre = partes[1] if len(partes) > 1 else None

    if metodo == "GET" and nombre is None:
        limite = _uno(consulta, "limit_page_length", "20")
        inicio = _uno(consulta, "limit_start", "0")
        filas = almacen.listar(
            doctype,
            filtros=_json(consulta, "filters"),
            campos=_json(consulta, "fields"),
            limite=int(limite or 20),
            inicio=int(inicio or 0),
            order_by=_uno(consulta, "order_by") or None,
            parent=_uno(consulta, "parent") or None,
        )
        return 200, {"data": filas}

    if metodo == "GET":
        return 200, {"data": almacen.leer(doctype, nombre)}

    if metodo == "POST" and nombre is None:
        payload = _cuerpo_json(cuerpo)
        return 200, {"data": almacen.crear(doctype, payload, puede_confirmar=puede_confirmar)}

    if metodo == "PUT" and nombre is not None:
        payload = _cuerpo_json(cuerpo)
        return 200, {
            "data": almacen.guardar(doctype, nombre, payload, puede_confirmar=puede_confirmar)
        }

    if metodo == "DELETE" and nombre is not None:
        almacen.borrar(doctype, nombre, puede_confirmar=puede_confirmar)
        # app/erpnext.py exige que el cuerpo sea un dict, también al borrar.
        return 202, {"message": "ok"}

    raise Rechazo(405, f"{metodo} {ruta} no está soportado por el doble")


def _cuerpo_json(cuerpo: bytes) -> dict:
    if not cuerpo:
        return {}
    try:
        datos = json.loads(cuerpo.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise Rechazo(417, "el cuerpo no es JSON válido") from exc
    if not isinstance(datos, dict):
        raise Rechazo(417, "el cuerpo tiene que ser un objeto")
    return datos
