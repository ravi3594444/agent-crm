"""Un cliente nuevo pide en la misma conversación — sin que nada se duplique.

  * El teléfono no es un parámetro. Sale del webhook firmado. Ninguna
    herramienta lo acepta, así que ningún mensaje puede dar de alta a otra
    persona ni pedir en su nombre.
  * Meta reintenta y la gente manda dos mensajes seguidos: el alta corre bajo
    lock por teléfono y vuelve a buscar adentro. Una persona, una ficha; una
    dirección, una Address.
  * Después del alta, crear_pedido resuelve la cuenta por ese mismo teléfono y
    le pone la dirección al pedido, para que la política pueda mirarla.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import clientes, erpnext
from app.tools import pedidos

TELEFONO = "5493511234567"
DIRECCION = {
    "calle": "Av. Colón 1234",
    "localidad": "Córdoba",
    "codigo_postal": "5000",
    "referencia": "",
}


def _config(telefono: str = TELEFONO, scope: str = "customer", customer: str = "") -> dict:
    return {
        "configurable": {
            "thread_id": "cli:thread",
            "actor_scope": scope,
            "customer_code": customer,
            "actor_phone": telefono,
            "inbound_message_id": "wamid.alta-001",
        }
    }


class _ErpDePrueba:
    """An ERPNext with a memory: Customers by phone, Addresses by customer."""

    def __init__(self) -> None:
        self.customers: dict[str, dict] = {}
        self.addresses: dict[str, dict] = {}
        self.creados: list[tuple[str, dict]] = []
        self.fallar_customer_una_vez = False

    def get_list(self, doctype, filters=None, fields=None, limit=20, parent=None, order_by=None, start=0):
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
            return []
        return []

    def get_doc(self, doctype, name):
        if doctype == "Address" and name in self.addresses:
            return dict(self.addresses[name])
        if doctype == "Customer" and name in self.customers:
            return dict(self.customers[name])
        raise erpnext.ERPNextError("404")

    def create_doc(self, doctype, payload):
        self.creados.append((doctype, dict(payload)))
        if doctype == "Customer":
            if self.fallar_customer_una_vez:
                self.fallar_customer_una_vez = False
                raise erpnext.ERPNextError("Duplicate entry")
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
        raise AssertionError(f"create_doc inesperado: {doctype}")


@pytest.fixture
def erp(monkeypatch: pytest.MonkeyPatch) -> _ErpDePrueba:
    falso = _ErpDePrueba()
    monkeypatch.setattr(erpnext, "get_list", falso.get_list)
    monkeypatch.setattr(erpnext, "get_doc", falso.get_doc)
    monkeypatch.setattr(erpnext, "create_doc", falso.create_doc)
    monkeypatch.setattr(erpnext, "add_comment", Mock())
    monkeypatch.setattr(erpnext, "submit_doc", Mock(side_effect=AssertionError("nunca")))
    monkeypatch.setenv("ZONAS_ENTREGA_CP", "5000")
    monkeypatch.setenv("ZONAS_ENTREGA_LOCALIDADES", "Córdoba")
    return falso


@pytest.fixture
def locks_tomados(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the lock names instead of talking to Redis."""
    tomados: list[str] = []

    @contextmanager
    def lock(nombre, **kwargs):
        tomados.append(nombre)
        yield

    monkeypatch.setattr(clientes, "distributed_lock", lock)
    monkeypatch.setattr(pedidos, "distributed_lock", lock)
    return tomados


@pytest.fixture
def memoria_de_direcciones(monkeypatch: pytest.MonkeyPatch):
    """The 'address given in this conversation' marker, without a Redis server."""
    from conftest import FakeRedis

    falso = FakeRedis()
    monkeypatch.setattr(clientes.locks, "conexion", lambda: falso)
    return falso


# ------------------------------------------------ the phone is not a parameter
def test_the_model_cannot_name_a_phone_number() -> None:
    """The parameter does not exist, so no prompt can supply one."""
    campos = set(pedidos.crear_cliente.args_schema.model_json_schema()["properties"])

    assert campos == {"nombre", "direccion"}
    assert not any("tel" in campo or "phone" in campo for campo in campos)


def test_the_customer_is_created_with_the_webhook_phone(erp, locks_tomados) -> None:
    reply = pedidos.crear_cliente.invoke(
        {"nombre": "Almacén Don José", "direccion": DIRECCION}, config=_config()
    )

    assert "Cuenta lista: CUST-001" in reply
    assert erp.customers["CUST-001"]["mobile_no"] == TELEFONO
    assert erp.customers["CUST-001"]["customer_name"] == "Almacén Don José"
    assert f"alta-cliente:{TELEFONO}" in locks_tomados


@pytest.mark.parametrize(
    "config",
    [
        _config(scope="management"),
        _config(telefono=""),
        {"configurable": {}},
    ],
)
def test_nobody_but_an_authenticated_customer_can_register(erp, locks_tomados, config) -> None:
    reply = pedidos.crear_cliente.invoke(
        {"nombre": "Alguien", "direccion": DIRECCION}, config=config
    )

    assert "No pude autenticar" in reply
    assert erp.creados == []


# ------------------------------------------------------------- no duplicates
def test_the_same_phone_twice_is_one_customer(erp, locks_tomados) -> None:
    """Meta retries; people send the same message twice. One person, one
    record — the second call finds the first and says so."""
    primera = pedidos.crear_cliente.invoke(
        {"nombre": "Almacén Don José", "direccion": DIRECCION}, config=_config()
    )
    segunda = pedidos.crear_cliente.invoke(
        {"nombre": "Almacen Don Jose", "direccion": DIRECCION}, config=_config()
    )

    assert "CUST-001" in primera and "CUST-001" in segunda
    assert "ya tenía cuenta" in segunda
    assert len(erp.customers) == 1
    assert sum(1 for d, _ in erp.creados if d == "Customer") == 1


def test_the_same_address_twice_is_one_address(erp, locks_tomados) -> None:
    for _ in range(3):
        pedidos.crear_cliente.invoke(
            {"nombre": "Almacén Don José", "direccion": DIRECCION}, config=_config()
        )

    assert len(erp.addresses) == 1


def test_the_same_address_written_differently_is_still_one_address(erp, locks_tomados) -> None:
    pedidos.crear_cliente.invoke(
        {"nombre": "Almacén Don José", "direccion": DIRECCION}, config=_config()
    )
    pedidos.crear_cliente.invoke(
        {
            "nombre": "Almacén Don José",
            "direccion": {**DIRECCION, "calle": "av colon 1234", "localidad": "CORDOBA", "codigo_postal": " 5000 "},
        },
        config=_config(),
    )

    assert len(erp.addresses) == 1


def test_a_changed_address_is_a_second_address_on_the_same_customer(erp, locks_tomados) -> None:
    """He moved. Same person, same phone, one more place to deliver to — and
    the new place has to earn its own approval."""
    pedidos.crear_cliente.invoke(
        {"nombre": "Almacén Don José", "direccion": DIRECCION}, config=_config()
    )
    reply = pedidos.crear_cliente.invoke(
        {
            "nombre": "Almacén Don José",
            "direccion": {**DIRECCION, "calle": "Ruta 9 km 300", "localidad": "Villa Rara", "codigo_postal": "X9999"},
        },
        config=_config(),
    )

    assert len(erp.customers) == 1
    assert len(erp.addresses) == 2
    assert "ATENCIÓN" in reply
    assert "no le prometas la entrega" in reply


def test_a_name_collision_on_create_is_resolved_by_phone(erp, locks_tomados) -> None:
    """Two businesses called "Almacén" or a worker that created the record a
    moment ago: the create fails, the phone lookup finds who we are."""
    erp.customers["CUST-077"] = {"name": "CUST-077", "customer_name": "Almacén", "mobile_no": TELEFONO}
    erp.fallar_customer_una_vez = True
    # The first lookup finds nothing (simulating the race), the retry finds it.
    original = erp.get_list
    llamadas = {"n": 0}

    def get_list(doctype, *args, **kwargs):
        if doctype == "Customer":
            llamadas["n"] += 1
            if llamadas["n"] == 1:
                return []
        return original(doctype, *args, **kwargs)

    erp.get_list = get_list
    import app.erpnext as erpmod

    erpmod.get_list = get_list

    reply = pedidos.crear_cliente.invoke(
        {"nombre": "Almacén", "direccion": DIRECCION}, config=_config()
    )

    assert "CUST-077" in reply
    assert len(erp.customers) == 1


def test_a_lock_that_cannot_be_taken_creates_nothing(erp, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.locks import CoordinationError

    @contextmanager
    def lock(nombre, **kwargs):
        raise CoordinationError("sin lock")
        yield  # pragma: no cover

    monkeypatch.setattr(clientes, "distributed_lock", lock)

    reply = pedidos.crear_cliente.invoke(
        {"nombre": "Almacén Don José", "direccion": DIRECCION}, config=_config()
    )

    assert "No pude coordinar" in reply
    assert erp.creados == []


def test_a_missing_postcode_is_registered_but_flagged(erp, locks_tomados, monkeypatch) -> None:
    """No postcode and a locality that is not on the list: the account is
    created — that is fine — but the model is told plainly not to promise."""
    monkeypatch.setenv("ZONAS_ENTREGA_LOCALIDADES", "")
    reply = pedidos.crear_cliente.invoke(
        {"nombre": "Almacén Don José", "direccion": {**DIRECCION, "codigo_postal": ""}},
        config=_config(),
    )

    assert "Cuenta lista" in reply
    assert "ATENCIÓN" in reply
    assert "no le prometas la entrega" in reply
    assert "confirmado" in reply  # ...ni le digas que está confirmado


def test_an_address_in_zone_is_said_so_without_promising_an_order(erp, locks_tomados) -> None:
    reply = pedidos.crear_cliente.invoke(
        {"nombre": "Almacén Don José", "direccion": DIRECCION}, config=_config()
    )

    assert "Entregamos en esa zona" in reply
    assert "crear_pedido" in reply
    assert "confirmado" not in reply.lower()


# ------------------------------------- the order, in the same conversation
def test_the_order_finds_the_account_by_phone_and_carries_the_address(
    erp, locks_tomados, monkeypatch: pytest.MonkeyPatch
) -> None:
    """crear_cliente ran a second ago, so the webhook's config still has no
    customer_code. The order resolves it from the verified phone and states
    where it goes, so policy can look at the address."""
    from datetime import date

    pedidos.crear_cliente.invoke(
        {"nombre": "Almacén Don José", "direccion": DIRECCION}, config=_config()
    )
    monkeypatch.setattr(pedidos, "_hoy_del_negocio", lambda: date(2026, 8, 29))
    monkeypatch.setattr(pedidos, "_find_existing", lambda customer, key: None)
    monkeypatch.setattr(
        pedidos,
        "_validated_lines",
        lambda lineas: ([{"item_code": "LECHE-1L", "qty": 5, "uom": "Unidad"}], None),
    )
    monkeypatch.setattr(erpnext, "default_context", lambda: ("Lácteos Plus SA", "Depósito A - LP"))
    creados_so: list[dict] = []
    crear_original = erp.create_doc

    def create_doc(doctype, payload):
        if doctype == "Sales Order":
            creados_so.append(dict(payload))
            return {"name": "SO-NEW", "docstatus": 0, **payload}
        return crear_original(doctype, payload)

    monkeypatch.setattr(erpnext, "create_doc", create_doc)
    monkeypatch.setattr(pedidos, "_after_create", lambda order, validated, delivery: "PEDIDO_PENDIENTE. Número real: SO-NEW.")

    reply = pedidos.crear_pedido.invoke(
        {
            "lineas": [{"item_code": "LECHE-1L", "cantidad": 5, "unidad": "unidad"}],
            "fecha_entrega": "2026-08-30",
        },
        config=_config(customer=""),
    )

    assert reply.startswith("PEDIDO_PENDIENTE")
    assert len(creados_so) == 1
    pedido = creados_so[0]
    assert pedido["customer"] == "CUST-001"
    direccion = next(iter(erp.addresses))
    assert pedido["shipping_address_name"] == direccion
    assert pedido["customer_address"] == direccion


def test_the_order_never_takes_a_customer_from_anywhere_but_the_phone(erp, locks_tomados) -> None:
    """Nothing in the tool schema lets the model say who is ordering."""
    campos = set(pedidos.crear_pedido.args_schema.model_json_schema()["properties"])

    assert campos == {"lineas", "fecha_entrega"}


# --------------------------------------------- the order goes where he just said
def _pedido_capturado(monkeypatch: pytest.MonkeyPatch, erp: _ErpDePrueba) -> list[dict]:
    from datetime import date

    monkeypatch.setattr(pedidos, "_hoy_del_negocio", lambda: date(2026, 8, 29))
    monkeypatch.setattr(pedidos, "_find_existing", lambda customer, key: None)
    monkeypatch.setattr(
        pedidos,
        "_validated_lines",
        lambda lineas: ([{"item_code": "LECHE-1L", "qty": 5, "uom": "Unidad"}], None),
    )
    monkeypatch.setattr(erpnext, "default_context", lambda: ("Lácteos Plus SA", "Depósito A - LP"))
    monkeypatch.setattr(
        pedidos, "_after_create", lambda order, validated, delivery: "PEDIDO_PENDIENTE. Número real: SO-NEW."
    )
    creados: list[dict] = []
    crear_original = erp.create_doc

    def create_doc(doctype, payload):
        if doctype == "Sales Order":
            creados.append(dict(payload))
            return {"name": "SO-NEW", "docstatus": 0, **payload}
        return crear_original(doctype, payload)

    monkeypatch.setattr(erpnext, "create_doc", create_doc)
    return creados


def _pedir(customer: str = "") -> str:
    return pedidos.crear_pedido.invoke(
        {
            "lineas": [{"item_code": "LECHE-1L", "cantidad": 5, "unidad": "unidad"}],
            "fecha_entrega": "2026-08-30",
        },
        config=_config(customer=customer),
    )


def test_a_known_customer_who_gives_a_new_address_gets_the_order_sent_there(
    erp, locks_tomados, memoria_de_direcciones, monkeypatch
) -> None:
    """He moved. The second Address sorts AFTER the first in ERPNext
    ("X-Shipping-1" < "X-Shipping-2"), so the alphabetical default would have
    sent the order — and the delivery check — to the OLD address, and the new
    one would never have been evaluated."""
    pedidos.crear_cliente.invoke(
        {"nombre": "Almacén Don José", "direccion": DIRECCION}, config=_config()
    )
    vieja = next(iter(erp.addresses))
    pedidos.crear_cliente.invoke(
        {
            "nombre": "Almacén Don José",
            "direccion": {**DIRECCION, "calle": "Ruta 9 km 300", "localidad": "Villa Rara", "codigo_postal": "X9999"},
        },
        config=_config(),
    )
    nueva = next(n for n in erp.addresses if n != vieja)
    assert sorted([vieja, nueva])[0] == vieja  # the old one IS the alphabetical default

    creados = _pedido_capturado(monkeypatch, erp)
    assert _pedir(customer="CUST-001").startswith("PEDIDO_PENDIENTE")

    assert creados[0]["shipping_address_name"] == nueva
    assert creados[0]["customer_address"] == nueva


def test_restating_the_old_address_sends_the_order_to_the_old_address(
    erp, locks_tomados, memoria_de_direcciones, monkeypatch
) -> None:
    """Two addresses on file; today he repeats the first one. No new Address
    is created, and the order goes to the one he named — not to the newest."""
    pedidos.crear_cliente.invoke(
        {"nombre": "Almacén Don José", "direccion": DIRECCION}, config=_config()
    )
    primera = next(iter(erp.addresses))
    pedidos.crear_cliente.invoke(
        {
            "nombre": "Almacén Don José",
            "direccion": {**DIRECCION, "calle": "Ruta 9 km 300", "localidad": "Villa Rara", "codigo_postal": "X9999"},
        },
        config=_config(),
    )
    pedidos.crear_cliente.invoke(
        {"nombre": "Almacén Don José", "direccion": DIRECCION}, config=_config()
    )
    assert len(erp.addresses) == 2

    creados = _pedido_capturado(monkeypatch, erp)
    _pedir(customer="CUST-001")
    assert creados[0]["shipping_address_name"] == primera


def test_without_a_remembered_address_the_deterministic_default_still_applies(
    erp, locks_tomados, memoria_de_direcciones, monkeypatch
) -> None:
    """Nothing said in this conversation (or the marker expired): the order
    carries the same alphabetical default as before, never no address."""
    pedidos.crear_cliente.invoke(
        {"nombre": "Almacén Don José", "direccion": DIRECCION}, config=_config()
    )
    memoria_de_direcciones.strings.clear()

    creados = _pedido_capturado(monkeypatch, erp)
    _pedir(customer="CUST-001")
    assert creados[0]["shipping_address_name"] == sorted(erp.addresses)[0]


def test_a_remembered_address_of_another_customer_is_ignored(
    erp, locks_tomados, memoria_de_direcciones, monkeypatch
) -> None:
    """The marker is only a hint: it must belong to THIS customer's addresses
    or it is not used. Fail-safe towards the deterministic default."""
    pedidos.crear_cliente.invoke(
        {"nombre": "Almacén Don José", "direccion": DIRECCION}, config=_config()
    )
    clientes.recordar_direccion(TELEFONO, "Otro Cliente-Shipping-9")

    creados = _pedido_capturado(monkeypatch, erp)
    _pedir(customer="CUST-001")
    assert creados[0]["shipping_address_name"] == sorted(erp.addresses)[0]


def test_a_redis_outage_never_blocks_the_order_or_leaks_the_phone(
    erp, locks_tomados, memoria_de_direcciones, monkeypatch, capsys
) -> None:
    memoria_de_direcciones.caido = True
    pedidos.crear_cliente.invoke(
        {"nombre": "Almacén Don José", "direccion": DIRECCION}, config=_config()
    )
    creados = _pedido_capturado(monkeypatch, erp)
    assert _pedir(customer="CUST-001").startswith("PEDIDO_PENDIENTE")
    assert creados[0]["shipping_address_name"] == sorted(erp.addresses)[0]
    assert TELEFONO not in capsys.readouterr().out
