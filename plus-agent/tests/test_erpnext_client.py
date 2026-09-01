"""El cliente REST: serialización de filtros y payloads que ERPNext acepta.

EL BUG QUE ESTE ARCHIVO CONGELA
`str(filters).replace("'", '"')` rompía con cualquier apóstrofo en el dato:

    ["item_name", "like", "%D'Angelo%"]
    -> [["item_name", "like", "%D"Angelo%"]]      <- JSON inválido
    -> ERPNext 400 -> el cliente recibe "tuve un problema técnico"

En Argentina hay apellidos con apóstrofo (D'Angelo, D'Amico, O'Brien) y
cualquiera puede escribir uno en un mensaje. Además le daba al que escribe
control sobre la ESTRUCTURA del filtro, no solo sobre el valor.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app import erpnext


class TransporteFalso(httpx.BaseTransport):
    """Captura los requests para poder afirmar sobre el query string."""

    def __init__(self, respuesta=None, status=200) -> None:
        self.requests: list[httpx.Request] = []
        self.respuesta = respuesta if respuesta is not None else {"data": []}
        self.status = status

    def handle_request(self, request):
        self.requests.append(request)
        return httpx.Response(self.status, json=self.respuesta, request=request)


@pytest.fixture
def transporte(monkeypatch):
    t = TransporteFalso()
    monkeypatch.setattr(
        erpnext,
        "_client",
        httpx.Client(base_url="http://erpnext.test", transport=t),
    )
    return t


# --------------------------------------------------------------------------
# Serialización de filtros
# --------------------------------------------------------------------------

VALORES_HOSTILES = [
    "queso",
    "D'Angelo",  # apóstrofo: el bug original
    "O'Brien",
    'comillas "dobles"',
    "café con acento",
    'x" ]] or 1=1 --',  # intento de romper la estructura
    "salto\nde línea",
    "emoji 🧀",
    "{'json':'raro'}",
    "back\\slash",
]


@pytest.mark.parametrize("valor", VALORES_HOSTILES)
def test_los_filtros_siempre_son_json_valido(transporte, valor):
    erpnext.get_list("Item", filters=[["item_name", "like", f"%{valor}%"], ["disabled", "=", 0]])
    enviado = transporte.requests[-1].url.params["filters"]
    parseado = json.loads(enviado)  # explota si no es JSON válido
    assert parseado[0][2] == f"%{valor}%"  # y el valor llega intacto
    assert parseado[1] == ["disabled", "=", 0]


@pytest.mark.parametrize("valor", VALORES_HOSTILES)
def test_el_valor_no_puede_cambiar_la_estructura_del_filtro(transporte, valor):
    """Lo importante no es solo que sea JSON válido: el valor tiene que
    quedar como UN valor, sin poder inyectar condiciones extra."""
    erpnext.get_list("Customer", filters=[["mobile_no", "like", f"%{valor}%"]])
    parseado = json.loads(transporte.requests[-1].url.params["filters"])
    assert len(parseado) == 1
    assert len(parseado[0]) == 3


def test_los_campos_tambien_van_como_json(transporte):
    erpnext.get_list("Item", fields=["item_code", "item_name"])
    assert json.loads(transporte.requests[-1].url.params["fields"]) == ["item_code", "item_name"]


def test_order_by_se_manda_cuando_se_pide(transporte):
    erpnext.get_list("Sales Order", order_by="transaction_date desc")
    assert transporte.requests[-1].url.params["order_by"] == "transaction_date desc"


def test_sin_order_by_no_se_manda(transporte):
    erpnext.get_list("Sales Order")
    assert "order_by" not in transporte.requests[-1].url.params


def test_los_filtros_de_reporte_tambien_son_json(transporte):
    transporte.respuesta = {"message": {"result": []}}
    erpnext.run_report("Accounts Receivable", {"party": ["D'Angelo SA"]})
    parseado = json.loads(transporte.requests[-1].url.params["filters"])
    assert parseado["party"] == ["D'Angelo SA"]


# --------------------------------------------------------------------------
# La garantía de borrador
# --------------------------------------------------------------------------


def test_create_doc_fuerza_borrador(transporte):
    transporte.respuesta = {"data": {"name": "SO-0001"}}
    erpnext.create_doc("Sales Order", {"customer": "X", "docstatus": 1})
    enviado = json.loads(transporte.requests[-1].content)
    assert enviado["docstatus"] == 0, "create_doc NUNCA puede mandar docstatus 1"


def test_create_doc_ignora_cualquier_intento_de_confirmar(transporte):
    """Aunque el payload venga con docstatus=1 (por un bug o una inyección),
    sale como borrador."""
    transporte.respuesta = {"data": {"name": "SI-0001"}}
    for intento in (1, "1", 2, True):
        erpnext.create_doc("Sales Invoice", {"docstatus": intento})
        assert json.loads(transporte.requests[-1].content)["docstatus"] == 0


# --------------------------------------------------------------------------
# Errores
# --------------------------------------------------------------------------


def test_un_400_levanta_ERPNextError(transporte):
    transporte.status = 400
    transporte.respuesta = {"exc": "ValidationError"}
    with pytest.raises(erpnext.ERPNextError):
        erpnext.get_list("Item")


def test_un_500_levanta_ERPNextError(transporte):
    transporte.status = 500
    with pytest.raises(erpnext.ERPNextError):
        erpnext.get_doc("Sales Order", "SO-0001")


def test_add_comment_nunca_levanta(transporte):
    """Una nota de auditoría que falla no puede tirar el pedido de un
    cliente. Pero sí queda en el log."""
    transporte.status = 500
    erpnext.add_comment("Sales Order", "SO-0001", "nota")  # no explota


def test_get_list_de_lista_vacia_devuelve_lista(transporte):
    transporte.respuesta = {"data": None}
    assert erpnext.get_list("Item") == []


# --------------------------------------------------------------------------
# Defaults de compañía y depósito
# --------------------------------------------------------------------------


def test_company_forzada_por_env_gana(monkeypatch, transporte):
    monkeypatch.setenv("ERPNEXT_COMPANY", "Mi Lacteo SA")
    erpnext.reset_caches()
    assert erpnext.default_company() == "Mi Lacteo SA"
    erpnext.reset_caches()


def test_warehouse_forzado_por_env_gana(monkeypatch, transporte):
    monkeypatch.setenv("ERPNEXT_WAREHOUSE", "Camara Frio - ML")
    erpnext.reset_caches()
    assert erpnext.default_warehouse() == "Camara Frio - ML"
    erpnext.reset_caches()
