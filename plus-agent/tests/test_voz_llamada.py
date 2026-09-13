"""UNA LLAMADA ENTERA: un desconocido llama y termina con un pedido cargado.

Los otros tests de voz miran una pieza cada uno. Éste mira lo que el dueño le
va a mostrar a un cliente: alguien que no está en el sistema llama, pregunta por
un producto, se da de alta y deja el pedido — todo por `voz.herramientas`, que
es exactamente el camino que recorre un `tool.call` del relay.

Sirve para algo que ninguna pieza sola puede contestar: que las once
herramientas encajen ENTRE ELLAS con el contexto de una llamada. `crear_cliente`
da de alta con el teléfono del contexto; `crear_pedido` resuelve la cuenta por
ese mismo teléfono. Si el contexto de voz llenara mal un solo campo, cada test
de unidad seguiría verde y esto se caería.

El modelo no está acá y no hace falta: lo que se prueba es que las herramientas
que el modelo va a llamar hacen su trabajo con la identidad que les arma el
canal. Qué decide llamar el modelo es cosa del prompt, y eso lo miran
`test_voz.py` y el banco de pruebas.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import clientes, erpnext, router
from app.tools import pedidos
from app.voz import herramientas, identidad

TELEFONO = "+5493511234567"
HOY = date(2026, 9, 14)


class _ErpDeLaLlamada:
    """Un ERPNext con memoria: catálogo, clientes, direcciones y pedidos."""

    def __init__(self) -> None:
        self.customers: dict[str, dict] = {}
        self.addresses: dict[str, dict] = {}
        self.pedidos: dict[str, dict] = {}
        self.creados: list[tuple[str, dict]] = []
        self.submits: list[str] = []

    def get_list(self, doctype, filters=None, fields=None, limit=20, parent=None,
                 order_by=None, start=0):
        if doctype == "Item":
            return [
                {
                    "item_code": "MUZZA-1K",
                    "item_name": "Muzzarella La Serenísima 1kg",
                    "stock_uom": "Kg",
                    "description": "Muzzarella en barra",
                }
            ]
        if doctype == "Item Price":
            # Con price_list, currency Y uom: sin los tres, `_precio_autorizado`
            # los saltea y el agente no cotiza nada. Ver CLAUDE.md.
            return [
                {
                    "price_list_rate": 4800.0,
                    "price_list": "Venta",
                    "currency": "ARS",
                    "uom": "Kg",
                    "valid_from": None,
                    "valid_upto": None,
                }
            ]
        if doctype == "Customer":
            return [dict(c) for c in self.customers.values()]
        if doctype == "Dynamic Link":
            cliente = next(f[2] for f in filters if f[0] == "link_name")
            return [
                {"parent": nombre, "link_name": cliente, "parenttype": "Address"}
                for nombre, doc in self.addresses.items()
                if doc["_cliente"] == cliente
            ]
        if doctype == "Sales Order":
            return [dict(p) for p in self.pedidos.values()]
        return []

    def get_doc(self, doctype, name):
        if doctype == "Item" and name == "MUZZA-1K":
            return {
                "item_code": "MUZZA-1K",
                "item_name": "Muzzarella La Serenísima 1kg",
                "stock_uom": "Kg",
                "disabled": 0,
                "is_sales_item": 1,
            }
        if doctype == "Address" and name in self.addresses:
            return dict(self.addresses[name])
        if doctype == "Customer" and name in self.customers:
            return dict(self.customers[name])
        if doctype == "Sales Order" and name in self.pedidos:
            return dict(self.pedidos[name])
        raise erpnext.ERPNextError("404")

    def create_doc(self, doctype, payload):
        self.creados.append((doctype, dict(payload)))
        if doctype == "Customer":
            nombre = f"CUST-{len(self.customers) + 1:03d}"
            self.customers[nombre] = {"name": nombre, **payload}
            return {"name": nombre}
        if doctype == "Address":
            nombre = f"{payload['address_title']}-Shipping-{len(self.addresses) + 1}"
            self.addresses[nombre] = {
                "name": nombre,
                "_cliente": payload["links"][0]["link_name"],
                **{k: v for k, v in payload.items() if k != "links"},
            }
            return {"name": nombre}
        if doctype == "Sales Order":
            nombre = f"SAL-ORD-2026-{len(self.pedidos) + 1:05d}"
            self.pedidos[nombre] = {
                "name": nombre,
                "docstatus": 0,
                "grand_total": 4800.0 * 15,
                **payload,
            }
            return {"name": nombre}
        raise AssertionError(f"create_doc inesperado: {doctype}")


@pytest.fixture
def erp(monkeypatch: pytest.MonkeyPatch) -> _ErpDeLaLlamada:
    falso = _ErpDeLaLlamada()
    monkeypatch.setattr(erpnext, "get_list", falso.get_list)
    monkeypatch.setattr(erpnext, "get_doc", falso.get_doc)
    monkeypatch.setattr(erpnext, "create_doc", falso.create_doc)
    monkeypatch.setattr(erpnext, "add_comment", Mock())
    # El canal de voz NO puede hacer Submit, igual que WhatsApp: la única
    # credencial que confirma es la de política y no la alcanza ninguna
    # herramienta. Si esto se llama, el test falla y ése es el punto.
    monkeypatch.setattr(
        erpnext,
        "submit_doc",
        Mock(side_effect=AssertionError("una herramienta de voz hizo Submit")),
    )
    monkeypatch.setenv("ZONAS_ENTREGA_CP", "5000")
    monkeypatch.setenv("ZONAS_ENTREGA_LOCALIDADES", "Córdoba")
    monkeypatch.setenv("AUTO_CONFIRM_PRICE_LIST", "Venta")
    monkeypatch.setenv("AUTO_CONFIRM_CURRENCY", "ARS")
    monkeypatch.setattr(pedidos, "_hoy_del_negocio", lambda: HOY)
    return falso


@pytest.fixture
def locks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    tomados: list[str] = []

    @contextmanager
    def lock(nombre, **kwargs):
        tomados.append(nombre)
        yield

    monkeypatch.setattr(clientes, "distributed_lock", lock)
    monkeypatch.setattr(pedidos, "distributed_lock", lock)
    return tomados


@pytest.fixture
def contexto(erp: _ErpDeLaLlamada, monkeypatch: pytest.MonkeyPatch) -> dict:
    """El contexto que arma el canal de voz para una llamada identificada.

    El lookup NO se mockea: lo contesta el ERPNext falso, que en este momento
    todavía no tiene clientes. Mockearlo a `None` habría sido cómodo para que
    `de_parametro` vea un número sin cuenta, y habría roto justo lo que este
    archivo prueba — `crear_pedido` resuelve la cuenta por el mismo teléfono, y
    con el doble clavado en `None` no la encuentra nunca. Un doble que ignora lo
    que pasó antes no puede discrepar con el código sobre el orden de las cosas.
    """
    monkeypatch.setenv("VOZ_NUMERO_POR_PARAMETRO", "1")
    monkeypatch.setattr(router, "es_equipo", lambda numero: False)
    assert not erp.customers, "la llamada empieza con el que llama sin cuenta"
    return identidad.de_parametro(TELEFONO, id_llamada="llamada-1")


def _llamar(nombre: str, argumentos: dict, contexto: dict) -> str:
    texto, es_error = herramientas.ejecutar(nombre, argumentos, configurable=contexto)
    assert not es_error, f"{nombre} devolvió error: {texto}"
    return texto


def test_un_desconocido_llama_y_termina_con_el_pedido_cargado(erp, locks, contexto):
    """La llamada entera, en el orden en que la va a hacer el agente."""
    catalogo = _llamar("buscar_producto", {"consulta": "muzzarella"}, contexto)
    assert "MUZZA-1K" in catalogo
    assert "Kg" in catalogo

    alta = _llamar(
        "crear_cliente",
        {
            "nombre": "Almacén Don José",
            "direccion": {
                "calle": "Laprida 420",
                "localidad": "Córdoba",
                "codigo_postal": "5000",
                "referencia": "",
            },
        },
        contexto,
    )
    assert "crear_pedido" in alta

    pedido = _llamar(
        "crear_pedido",
        {
            "lineas": [{"item_code": "MUZZA-1K", "cantidad": 15, "unidad": "Kg"}],
            "fecha_entrega": "2026-09-17",
        },
        contexto,
    )

    assert erp.pedidos, "la llamada no dejó ningún pedido en ERPNext"
    (numero,) = erp.pedidos
    assert numero in pedido, "el agente no recibió el número real del pedido"
    # BORRADOR. Es la regla dura: por voz tampoco se confirma nada solo.
    assert erp.pedidos[numero]["docstatus"] == 0


def test_el_pedido_de_la_llamada_queda_atado_a_la_cuenta_recien_dada_de_alta(
    erp, locks, contexto
):
    """La costura que ningún test de unidad ve: `crear_cliente` da de alta con el
    teléfono del contexto y `crear_pedido` resuelve la cuenta por ESE teléfono.
    Un canal que llenara mal el contexto dejaría los dos tests de unidad verdes
    y el pedido colgado de nadie."""
    _llamar(
        "crear_cliente",
        {
            "nombre": "Almacén Don José",
            "direccion": {
                "calle": "Laprida 420",
                "localidad": "Córdoba",
                "codigo_postal": "5000",
                "referencia": "",
            },
        },
        contexto,
    )
    _llamar(
        "crear_pedido",
        {
            "lineas": [{"item_code": "MUZZA-1K", "cantidad": 15, "unidad": "Kg"}],
            "fecha_entrega": "2026-09-17",
        },
        contexto,
    )
    (cuenta,) = erp.customers
    (numero,) = erp.pedidos
    assert erp.pedidos[numero]["customer"] == cuenta


def test_la_llamada_no_puede_confirmar_nada(erp, locks, contexto):
    """`submit_doc` está armado para explotar: si alguna herramienta de este
    canal confirmara un pedido, este test es el que lo dice."""
    _llamar(
        "crear_cliente",
        {
            "nombre": "Almacén Don José",
            "direccion": {
                "calle": "Laprida 420",
                "localidad": "Córdoba",
                "codigo_postal": "5000",
                "referencia": "",
            },
        },
        contexto,
    )
    _llamar(
        "crear_pedido",
        {
            "lineas": [{"item_code": "MUZZA-1K", "cantidad": 15, "unidad": "Kg"}],
            "fecha_entrega": "2026-09-17",
        },
        contexto,
    )
    assert all(doc["docstatus"] == 0 for doc in erp.pedidos.values())


def test_una_llamada_anonima_no_puede_dar_de_alta_a_nadie(erp, locks):
    """Sin teléfono no hay a quién dar de alta, y la herramienta lo dice sin
    hablar de sistemas ni de contextos."""
    anonimo = identidad.de_navegador(id_llamada="llamada-2")
    texto, es_error = herramientas.ejecutar(
        "crear_cliente",
        {
            "nombre": "Almacén Don José",
            "direccion": {
                "calle": "Laprida 420",
                "localidad": "Córdoba",
                "codigo_postal": "5000",
                "referencia": "",
            },
        },
        configurable=anonimo,
    )
    assert not erp.customers
    assert es_error is False  # un resultado normal, no una excepción
    assert "no registré la cuenta" in texto
