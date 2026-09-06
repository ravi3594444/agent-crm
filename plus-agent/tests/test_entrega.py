"""La zona de reparto la decide el sistema, no el modelo.

Si el LLM pudiera opinar sobre si una dirección "está cerca", un cliente lo
convencería con un mensaje. Acá sólo hay comparaciones contra las zonas que
configuró el negocio. Con las dos listas configuradas hacen falta los dos
datos y los dos tienen que estar permitidos; con una sola, manda esa; sin
ninguna, nada se entrega solo. Contradicción, dato faltante o ilegible, ERPNext
que no contesta: el pedido queda en borrador. Nunca "confirmado".
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
# Every combination of configured rule sets × address values (release gate).
CP_OK, CP_BAD, CP_MISSING, CP_GARBAGE = "5000", "9410", "", "???"
LOC_OK, LOC_BAD, LOC_MISSING, LOC_GARBAGE = "Córdoba", "Ushuaia", "", "###"

AMBAS = {"ZONAS_ENTREGA_CP": "5000, X5000, 5001", "ZONAS_ENTREGA_LOCALIDADES": "Córdoba, Villa Allende"}
SOLO_CP = {"ZONAS_ENTREGA_CP": "5000, X5000, 5001", "ZONAS_ENTREGA_LOCALIDADES": ""}
SOLO_LOC = {"ZONAS_ENTREGA_CP": "", "ZONAS_ENTREGA_LOCALIDADES": "Córdoba, Villa Allende"}
NINGUNA = {"ZONAS_ENTREGA_CP": "", "ZONAS_ENTREGA_LOCALIDADES": ""}


def _configurar(monkeypatch, modo: dict) -> None:
    for variable, valor in modo.items():
        monkeypatch.setenv(variable, valor)


@pytest.mark.parametrize(
    ("modo", "cp", "localidad", "dentro", "categoria"),
    [
        # both lists configured: both values must be present AND allowed
        (AMBAS, CP_OK, LOC_OK, True, entrega.OK),
        (AMBAS, CP_OK, LOC_BAD, False, entrega.CONTRADICCION),
        (AMBAS, CP_BAD, LOC_OK, False, entrega.CONTRADICCION),
        (AMBAS, CP_BAD, LOC_BAD, False, entrega.FUERA),
        (AMBAS, CP_MISSING, LOC_OK, False, entrega.FALTA),
        (AMBAS, CP_OK, LOC_MISSING, False, entrega.FALTA),
        (AMBAS, CP_MISSING, LOC_MISSING, False, entrega.FALTA),
        (AMBAS, CP_GARBAGE, LOC_OK, False, entrega.ERROR),
        (AMBAS, CP_OK, LOC_GARBAGE, False, entrega.ERROR),
        # only postal codes configured: the code decides, the locality is ignored
        (SOLO_CP, CP_OK, LOC_OK, True, entrega.OK),
        (SOLO_CP, CP_OK, LOC_BAD, True, entrega.OK),
        (SOLO_CP, CP_OK, LOC_MISSING, True, entrega.OK),
        (SOLO_CP, CP_OK, LOC_GARBAGE, True, entrega.OK),
        (SOLO_CP, CP_BAD, LOC_OK, False, entrega.FUERA),
        (SOLO_CP, CP_MISSING, LOC_OK, False, entrega.FALTA),
        (SOLO_CP, CP_GARBAGE, LOC_OK, False, entrega.ERROR),
        # only localities configured: symmetric
        (SOLO_LOC, CP_OK, LOC_OK, True, entrega.OK),
        (SOLO_LOC, CP_BAD, LOC_OK, True, entrega.OK),
        (SOLO_LOC, CP_MISSING, LOC_OK, True, entrega.OK),
        (SOLO_LOC, CP_GARBAGE, LOC_OK, True, entrega.OK),
        (SOLO_LOC, CP_OK, LOC_BAD, False, entrega.FUERA),
        (SOLO_LOC, CP_OK, LOC_MISSING, False, entrega.FALTA),
        (SOLO_LOC, CP_OK, LOC_GARBAGE, False, entrega.ERROR),
        # nothing configured: nothing is delivered automatically
        (NINGUNA, CP_OK, LOC_OK, False, entrega.SIN_ZONAS),
        (NINGUNA, CP_MISSING, LOC_MISSING, False, entrega.SIN_ZONAS),
    ],
)
def test_every_combination_of_rule_sets_and_address_values(
    monkeypatch: pytest.MonkeyPatch, modo, cp, localidad, dentro, categoria
) -> None:
    _configurar(monkeypatch, modo)
    evaluacion = entrega.evaluar_zona(_direccion(pincode=cp, city=localidad))
    assert (evaluacion.dentro, evaluacion.categoria) == (dentro, categoria)
    assert bool(evaluacion.motivo) is (not dentro)
    assert entrega.en_zona(_direccion(pincode=cp, city=localidad)) == (dentro, evaluacion.motivo)


def test_reasons_name_the_datum_so_the_team_can_act() -> None:
    contradiccion = entrega.evaluar_zona(_direccion(pincode="9410", city="Córdoba"))
    assert "9410" in contradiccion.motivo and "Córdoba" in contradiccion.motivo
    assert "se contradicen" in contradiccion.motivo

    fuera = entrega.evaluar_zona(_direccion(pincode="9410", city="Ushuaia"))
    assert "9410" in fuera.motivo and "Ushuaia" in fuera.motivo and "zonas de reparto" in fuera.motivo

    falta = entrega.evaluar_zona(_direccion(pincode="", city=""))
    assert "código postal ni localidad" in falta.motivo

    ilegible = entrega.evaluar_zona(_direccion(pincode="???"))
    assert "no se pudo interpretar" in ilegible.motivo


def test_a_postal_code_in_the_list_is_deliverable_whatever_the_spacing_or_case() -> None:
    assert entrega.en_zona(_direccion(pincode=" x 5000 ")) == (True, "")
    assert entrega.en_zona(_direccion(city="cordoba")) == (True, "")
    assert entrega.en_zona(_direccion(city="CÓRDOBA")) == (True, "")


def test_unicode_lookalikes_and_zero_width_characters_cannot_bypass_the_lists() -> None:
    cirilica = "C\u043erdoba"  # Cyrillic о
    assert entrega.en_zona(_direccion(city=cirilica))[0] is False
    assert entrega.en_zona(_direccion(pincode="5\u200b000"))[0] is True  # still exactly 5000
    assert entrega.en_zona(_direccion(pincode="50000"))[0] is False  # never a prefix match
    assert entrega.en_zona(_direccion(city="Córdoba Capital"))[0] is False  # never a substring match


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


def test_a_non_dict_address_is_a_parsing_failure_not_a_crash() -> None:
    evaluacion = entrega.evaluar_zona("not a dict")
    assert (evaluacion.dentro, evaluacion.categoria) == (False, entrega.ERROR)


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


def test_a_previous_delivery_to_the_address_no_longer_authorises_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Release gate: the zone lists ARE the rule. An address outside them stays
    pending even if a person once approved a delivery there; the owner adds
    the code or the locality to the list instead."""
    _erp(monkeypatch, direccion=_direccion(pincode="9410", city="Ushuaia"), previos=[
        {"name": "SO-OLD", "customer": "CUST-001", "shipping_address_name": "Cliente Uno-Shipping", "customer_address": ""}
    ])

    ok, motivo = entrega.autorizada(_pedido())

    assert ok is False
    assert "9410" in motivo and "Ushuaia" in motivo


def test_an_address_missing_a_required_value_is_pending_even_for_a_regular(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _erp(monkeypatch, direccion=_direccion(pincode="", city="Córdoba"), previos=[
        {"name": "SO-OLD", "customer": "CUST-001", "shipping_address_name": "Cliente Uno-Shipping", "customer_address": ""}
    ])

    ok, motivo = entrega.autorizada(_pedido())

    assert ok is False
    assert motivo.startswith(entrega.MOTIVO)
    assert "código postal" in motivo


def test_a_contradictory_address_is_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    _erp(monkeypatch, direccion=_direccion(pincode="9410", city="Córdoba"), previos=[])

    ok, motivo = entrega.autorizada(_pedido())

    assert ok is False
    assert "se contradicen" in motivo


def test_authorisation_never_reads_previous_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    _, listar = _erp(monkeypatch, direccion=_direccion(pincode="9410"), previos=None)

    ok, _motivo = entrega.autorizada(_pedido())

    assert ok is False
    listar.assert_not_called()


def test_a_known_customer_using_a_new_address_is_reviewed_like_anyone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _erp(monkeypatch, direccion=_direccion(name="Cliente Uno-Nueva", pincode="9410", city="Ushuaia"), previos=[])

    ok, motivo = entrega.autorizada(_pedido(shipping_address_name="Cliente Uno-Nueva"))

    assert ok is False
    assert "9410" in motivo
    assert "Ushuaia" in motivo  # the alert carries the address itself


def test_autorizada_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """policy.evaluar calls this on every order; an exception there would
    turn "review the delivery" into "policy crashed"."""
    monkeypatch.setattr(erpnext, "policy_get_doc", Mock(return_value="not a dict"))

    ok, motivo = entrega.autorizada(_pedido())

    assert ok is False
    assert motivo.startswith(entrega.MOTIVO)
