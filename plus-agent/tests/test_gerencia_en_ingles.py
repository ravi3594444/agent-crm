"""El dueño que habla inglés recibe sus informes en inglés.

EL HUECO QUE ESTO CIERRA
`app/tools/operaciones.py` traducía las 40 cosas que devuelve y
`app/tools/gerencia.py` ninguna de las suyas — y los dos devuelven bloques que
el modelo relata casi textuales. O sea que con `IDIOMA_GERENCIA=en`, «¿de qué
estoy corto?» volvía con «Stock bajo:» y «mínimo», y la única negativa de
siete herramientas salía en castellano.

No era un criterio distinto para cada archivo: era que a éste nadie lo había
traducido. El propio `idioma.regla_prompt` dice la regla — «el modelo NUNCA
decide el idioma de un texto de Python».

CÓMO SE PRUEBA
Cada rama se corre DOS VECES, con los mismos datos, una vez en cada idioma, y
se afirma que los dos textos difieren y que ninguno es la clave cruda. Afirmar
sólo «está en inglés» no podría distinguir una traducción de una clave sin
traducir (`idioma.t` devuelve la clave cuando no existe), y afirmar sólo que
«tiene la palabra X» pasaría con la mitad del bloque todavía en castellano.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from app import erpnext, router, telefono
from app.tools.gerencia import ficha_cliente, informe

GERENTE = "5493511234567"

_PEDIDOS = [{
    "name": "SAL-ORD-2026-00001", "customer": "CUST-0009",
    "customer_name": "Panadería San José", "grand_total": 12000.0,
    "delivery_date": "2026-09-16", "creation": "2026-09-14 08:00:00",
    "transaction_date": "2026-09-14", "status": "To Deliver",
}]
_REORDEN = [{"parent": "LECHE-ENT-1L", "warehouse": "Dep", "warehouse_reorder_level": 10}]
_CLIENTE = {"name": "CUST-0009", "customer_name": "Panadería San José",
            "customer_group": "Comercios", "mobile_no": "5493516667777"}


@pytest.fixture
def erp(monkeypatch: pytest.MonkeyPatch):
    def get_list(doctype, filters=None, fields=None, limit=None, parent=None, **kw):
        if doctype == "Sales Order":
            return [dict(p) for p in _PEDIDOS]
        if doctype == "Item Reorder":
            return [dict(r) for r in _REORDEN]
        if doctype == "Bin":
            return [{"actual_qty": 2}]
        if doctype == "Customer":
            return [dict(_CLIENTE)]
        return []

    # La antigüedad se fija en vez de salir del reloj: `_pedidos_pendientes`
    # llama a `pendientes.edad_horas(fila)` sin momento, y la suite tiene un
    # guard que falla cualquier test que lea la hora real. De paso queda
    # determinista y ejercita la clave «hace N h» en los dos idiomas, que es
    # justo una de las traducidas.
    from app import pendientes

    monkeypatch.setattr(pendientes, "edad_horas", lambda *a, **kw: 5.0)
    monkeypatch.setattr(router, "STAFF", [telefono.normalizar(GERENTE)])
    monkeypatch.setattr(erpnext, "get_list", get_list)
    monkeypatch.setattr(erpnext, "default_company", Mock(return_value="Co"))
    monkeypatch.setattr(erpnext, "run_report", Mock(return_value=[
        {"customer_name": "Panadería San José", "outstanding_amount": 5000.0},
    ]))
    yield


def _config() -> dict:
    return {"configurable": {"thread_id": "g:t", "actor_scope": "management",
                             "actor_phone": GERENTE, "inbound_message_id": "w"}}


def _en_idioma(monkeypatch: pytest.MonkeyPatch, lengua: str, herramienta, args: dict) -> str:
    monkeypatch.setenv("IDIOMA_DEFAULT", lengua)
    monkeypatch.setenv("IDIOMA_GERENCIA", lengua)
    return str(herramienta.invoke(dict(args), config=_config()))


CASOS = [
    (informe, {"que": "pendientes"}, "hace 5 h", "5 h ago"),
    (informe, {"que": "ventas"}, "Últimos", "Last"),
    (informe, {"que": "stock_bajo"}, "Stock bajo", "Low stock"),
    (informe, {"que": "cobranzas"}, "Total a cobrar", "outstanding across"),
    (ficha_cliente, {"nombre_o_codigo": "CUST-0009"}, "Últimos pedidos", "Latest orders"),
]


@pytest.mark.parametrize(
    ("herramienta", "args", "en_es", "en_en"), CASOS,
    ids=[f"{h.name}:{next(iter(a.values()))}" for h, a, _, _ in CASOS],
)
def test_cada_informe_sale_en_el_idioma_del_dueno(
    erp, monkeypatch: pytest.MonkeyPatch, herramienta, args, en_es: str, en_en: str
) -> None:
    castellano = _en_idioma(monkeypatch, "es", herramienta, args)
    ingles = _en_idioma(monkeypatch, "en", herramienta, args)

    assert en_es in castellano, castellano
    assert en_en in ingles, ingles
    # Los MISMOS datos y dos textos distintos: sin esto, una clave sin traducir
    # —que `idioma.t` devuelve tal cual— pasaría las dos afirmaciones de arriba
    # si por casualidad contuviera las palabras buscadas.
    assert castellano != ingles
    for texto in (castellano, ingles):
        assert "gerencia." not in texto, f"clave sin traducir: {texto[:120]}"


def test_la_negativa_de_gerencia_tambien_esta_en_ingles(
    erp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Siete herramientas devolvían la constante `SIN_PERMISO`, en castellano y
    sin gemelo: la ÚNICA frase que un número no autorizado llegaba a ver salía
    en el idioma equivocado."""
    monkeypatch.setattr(router, "STAFF", [])
    ajeno = {"configurable": {"thread_id": "g:t", "actor_scope": "management",
                              "actor_phone": "5493519999999", "inbound_message_id": "w"}}

    monkeypatch.setenv("IDIOMA_GERENCIA", "en")
    monkeypatch.setenv("IDIOMA_DEFAULT", "en")
    ingles = str(informe.invoke({"que": "pendientes"}, config=ajeno))

    assert "not authorized" in ingles, ingles
    assert "no está autorizado" not in ingles
