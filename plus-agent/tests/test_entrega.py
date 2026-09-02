"""La zona de reparto la decide el sistema, no el modelo.

Si el LLM pudiera opinar sobre si una dirección "está cerca", un cliente lo
convencería con un mensaje. Acá sólo hay comparaciones contra las zonas que
configuró el negocio, y un pedido CONFIRMADO anterior a la misma dirección
como única otra evidencia. Todo lo demás —sin dirección, sin código postal y
localidad desconocida, fuera de zona, ERPNext que no contesta— deja el pedido
en borrador. Nunca "confirmado".
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import entrega, erpnext


@pytest.fixture(autouse=True)
def zonas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZONAS_ENTREGA_CP", "5000, X5000, 5001")
    monkeypatch.setenv("ZONAS_ENTREGA_LOCALIDADES", "Córdoba, Villa Allende")


def _direccion(**campos) -> dict:
    base = {
        "name": "Cliente Uno-Shipping",
        "address_line1": "Av. Colón 1234",
        "city": "Córdoba",
        "pincode": "5000",
    }
    base.update(campos)
    return base


# ------------------------------------------------------------ en_zona
def test_a_postal_code_in_the_list_is_deliverable() -> None:
    assert entrega.en_zona(_direccion(pincode="5000")) == (True, "")
    # Spacing and case do not matter for a code.
    assert entrega.en_zona(_direccion(pincode=" x 5000 ")) == (True, "")


def test_a_postal_code_outside_the_list_is_not_and_the_reason_names_it() -> None:
    dentro, motivo = entrega.en_zona(_direccion(pincode="9410"))

    assert dentro is False
    assert "9410" in motivo
    assert "zonas de reparto" in motivo


def test_without_a_postal_code_the_locality_decides_accents_and_case_aside() -> None:
    assert entrega.en_zona(_direccion(pincode="", city="cordoba")) == (True, "")
    assert entrega.en_zona(_direccion(pincode="", city="VILLA  ALLENDE")) == (True, "")


def test_without_a_postal_code_an_unknown_locality_is_refused() -> None:
    dentro, motivo = entrega.en_zona(_direccion(pincode="", city="Ushuaia"))

    assert dentro is False
    assert "Ushuaia" in motivo


def test_neither_postal_code_nor_locality_is_refused() -> None:
    dentro, motivo = entrega.en_zona(_direccion(pincode="", city=""))

    assert dentro is False
    assert "ni localidad" in motivo


def test_with_a_postal_code_present_the_code_wins_over_the_locality() -> None:
    """A code that says 200 km away is not rescued by a locality that sounds
    right. The more precise datum decides."""
    dentro, motivo = entrega.en_zona(_direccion(pincode="9410", city="Córdoba"))

    assert dentro is False
    assert "9410" in motivo


def test_no_zones_configured_means_nothing_is_deliverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty allowlist is not "deliver everywhere". It is "nobody set this
    up", and the safe reading is that every delivery needs a person."""
    monkeypatch.delenv("ZONAS_ENTREGA_CP")
    monkeypatch.delenv("ZONAS_ENTREGA_LOCALIDADES")

    dentro, motivo = entrega.en_zona(_direccion())

    assert dentro is False
    assert "no hay zonas" in motivo


def test_zones_are_read_on_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    assert entrega.en_zona(_direccion(pincode="9410"))[0] is False
    monkeypatch.setenv("ZONAS_ENTREGA_CP", "9410")
    assert entrega.en_zona(_direccion(pincode="9410"))[0] is True


def test_the_address_is_rendered_for_a_human() -> None:
    texto = entrega.texto_direccion(
        _direccion(address_line1="Ruta 9 km 300", address_line2="Casa azul", city="Villa Rara", pincode="X9999")
    )
    assert texto == "Ruta 9 km 300, Casa azul, Villa Rara (CP X9999)"
    assert entrega.texto_direccion({}) == "sin datos de dirección"


# ------------------------------------------------------------ autorizada
def _pedido(**campos) -> dict:
    base = {
        "name": "SO-0001",
        "customer": "CUST-001",
        "shipping_address_name": "Cliente Uno-Shipping",
    }
    base.update(campos)
    return base


def _erp(monkeypatch: pytest.MonkeyPatch, *, direccion: dict | None, previos: list[dict] | None = None):
    leer = Mock(return_value=direccion) if direccion is not None else Mock(
        side_effect=erpnext.ERPNextError("404")
    )
    monkeypatch.setattr(erpnext, "policy_get_doc", leer)
    if previos is None:
        listar = Mock(side_effect=erpnext.ERPNextError("caído"))
    else:
        listar = Mock(return_value=list(previos))
    monkeypatch.setattr(erpnext, "policy_get_list", listar)
    return leer, listar


def test_an_order_whose_address_is_in_zone_is_authorised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _erp(monkeypatch, direccion=_direccion(), previos=[])

    assert entrega.autorizada(_pedido()) == (True, "")


def test_an_order_with_no_address_at_all_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _erp(monkeypatch, direccion=_direccion(), previos=[])

    ok, motivo = entrega.autorizada(_pedido(shipping_address_name="", customer_address=""))

    assert ok is False
    assert motivo.startswith(entrega.MOTIVO)
    assert "no tiene dirección" in motivo


def test_an_address_that_cannot_be_read_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _erp(monkeypatch, direccion=None, previos=[])

    ok, motivo = entrega.autorizada(_pedido())

    assert ok is False
    assert motivo.startswith(entrega.MOTIVO)


def test_the_billing_address_is_used_when_there_is_no_shipping_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leer, _ = _erp(monkeypatch, direccion=_direccion(), previos=[])

    assert entrega.autorizada(
        _pedido(shipping_address_name="", customer_address="Cliente Uno-Billing")
    )[0] is True
    assert leer.call_args.args == ("Address", "Cliente Uno-Billing")


def test_outside_the_zone_but_delivered_there_before_is_authorised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A person already approved a delivery to this exact address and the
    truck arrived. That is stronger evidence than any configured zone — and it
    is what keeps long-standing customers with no postcode on file from
    suddenly waiting for approval."""
    _erp(
        monkeypatch,
        direccion=_direccion(pincode="", city="Pueblo sin CP"),
        previos=[
            {
                "name": "SO-OLD",
                "customer": "CUST-001",
                "shipping_address_name": "Cliente Uno-Shipping",
                "customer_address": "",
            }
        ],
    )

    assert entrega.autorizada(_pedido()) == (True, "")


def test_a_known_customer_using_a_new_address_is_reviewed_like_anyone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Previous deliveries went somewhere else. This address has never been
    approved, so it goes through the zone check like a stranger's would."""
    _erp(
        monkeypatch,
        direccion=_direccion(name="Cliente Uno-Nueva", pincode="9410", city="Ushuaia"),
        previos=[
            {
                "name": "SO-OLD",
                "customer": "CUST-001",
                "shipping_address_name": "Cliente Uno-Shipping",
                "customer_address": "Cliente Uno-Shipping",
            }
        ],
    )

    ok, motivo = entrega.autorizada(_pedido(shipping_address_name="Cliente Uno-Nueva"))

    assert ok is False
    assert "9410" in motivo
    assert "Ushuaia" in motivo  # the alert carries the address itself


def test_a_delivery_to_the_same_address_for_another_customer_does_not_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _erp(
        monkeypatch,
        direccion=_direccion(pincode="9410"),
        previos=[
            {
                "name": "SO-OTHER",
                "customer": "CUST-999",
                "shipping_address_name": "Cliente Uno-Shipping",
                "customer_address": "",
            }
        ],
    )

    assert entrega.autorizada(_pedido())[0] is False


def test_only_submitted_orders_count_as_previous_deliveries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, listar = _erp(monkeypatch, direccion=_direccion(pincode="9410"), previos=[])

    entrega.autorizada(_pedido())

    filtros = listar.call_args.kwargs["filters"]
    assert ["docstatus", "=", 1] in filtros
    assert ["customer", "=", "CUST-001"] in filtros


def test_a_failed_previous_deliveries_lookup_does_not_authorise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _erp(monkeypatch, direccion=_direccion(pincode="9410"), previos=None)

    ok, motivo = entrega.autorizada(_pedido())

    assert ok is False
    assert motivo.startswith(entrega.MOTIVO)


def test_autorizada_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """policy.evaluar calls this on every order; an exception there would
    turn "review the delivery" into "policy crashed"."""
    monkeypatch.setattr(erpnext, "policy_get_doc", Mock(return_value="not a dict"))

    ok, motivo = entrega.autorizada(_pedido())

    assert ok is False
    assert motivo.startswith(entrega.MOTIVO)
