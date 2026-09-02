"""Identidad: quién escribe, a partir del número que manda Meta.

Meta entrega dígitos pelados (5493511234567). En ERPNext el mobile_no lo
tipeó una persona, en cualquier formato. Estos tests cubren el cruce entre
ambos mundos (app/clientes.py) y el ruteo de equipo (app/router.py).

El fake de erpnext.get_list emula la semántica SQL de `like` (con `%` y
`_`) sobre el string guardado, así el test prueba de verdad que el patrón
que mandamos encuentra números con espacios y guiones.
"""

from __future__ import annotations

import re

import pytest

from app import clientes, erpnext, router, telefono

META = "5493511234567"  # lo que llega en el webhook

FORMATOS_A_MANO = [
    "+5493511234567",
    "+54 9 351 123-4567",
    "0351 15 123-4567",
    "351 1234567",
    "(0351) 15-123-4567",
    "54 9 351 123 4567",
]


def _like_a_regex(patron: str) -> re.Pattern[str]:
    partes = []
    for ch in patron:
        if ch == "%":
            partes.append(".*")
        elif ch == "_":
            partes.append(".")
        else:
            partes.append(re.escape(ch))
    return re.compile("".join(partes), re.DOTALL)


def _fake_get_list(clientes_erp: list[dict], llamadas: list | None = None):
    """Emula ERPNext: un `like` de SQL sobre Customer.mobile_no."""

    def get_list(doctype, filters=None, fields=None, limit=20):
        if llamadas is not None:
            llamadas.append((doctype, filters, fields, limit))
        assert doctype == "Customer"
        assert filters and len(filters) == 1
        campo, operador, valor = filters[0]
        assert campo == "mobile_no"
        assert operador == "like"
        regex = _like_a_regex(valor)
        encontrados = [
            c for c in clientes_erp if regex.fullmatch(c.get("mobile_no") or "")
        ]
        return encontrados[:limit]

    return get_list


@pytest.mark.parametrize("guardado", FORMATOS_A_MANO)
def test_encuentra_cliente_guardado_en_cualquier_formato(monkeypatch, guardado):
    llamadas: list = []
    monkeypatch.setattr(
        erpnext,
        "get_list",
        _fake_get_list(
            [
                {"name": "CUST-0001", "customer_name": "Almacén Don Pepe",
                 "mobile_no": guardado},
                {"name": "CUST-0002", "customer_name": "Otro",
                 "mobile_no": "+54 9 11 4567-8901"},
            ],
            llamadas,
        ),
    )

    cliente = clientes.buscar_por_telefono(META)

    assert cliente is not None
    assert cliente["name"] == "CUST-0001"
    # Una sola consulta, con el patrón intercalado que sobrevive separadores.
    assert len(llamadas) == 1
    patron = llamadas[0][1][0][2]
    assert patron == "%1%1%2%3%4%5%6%7%"
    assert "name" in llamadas[0][2]


def test_numero_que_solo_termina_igual_no_matchea(monkeypatch):
    # Mismos últimos 8 dígitos (11234567), distinto código de área: 11 vs 351.
    otro = "+54 9 11 1123-4567"
    assert telefono.clave_busqueda(otro) == telefono.clave_busqueda(META)
    monkeypatch.setattr(
        erpnext,
        "get_list",
        _fake_get_list([{"name": "CUST-AJENO", "mobile_no": otro}]),
    )

    assert clientes.buscar_por_telefono(META) is None


def test_el_like_trae_candidatos_pero_solo_devuelve_el_exacto(monkeypatch):
    monkeypatch.setattr(
        erpnext,
        "get_list",
        _fake_get_list(
            [
                {"name": "CUST-AJENO", "mobile_no": "+54 9 11 1123-4567"},
                {"name": "CUST-REAL", "mobile_no": "0351 15 123-4567"},
            ]
        ),
    )

    cliente = clientes.buscar_por_telefono(META)

    assert cliente is not None and cliente["name"] == "CUST-REAL"


def test_numero_extranjero_hace_ida_y_vuelta(monkeypatch):
    """Cliente real con número de India: no se le aplican reglas argentinas."""
    india = "918521169094"
    assert telefono.normalizar(india) == india
    monkeypatch.setattr(
        erpnext,
        "get_list",
        _fake_get_list(
            [
                {"name": "CUST-IN", "mobile_no": "+91 85211 69094"},
                {"name": "CUST-AR", "mobile_no": META},
            ]
        ),
    )

    cliente = clientes.buscar_por_telefono(india)

    assert cliente is not None and cliente["name"] == "CUST-IN"
    assert clientes.buscar_por_telefono("+91 85211 69094")["name"] == "CUST-IN"


def test_sin_candidatos_devuelve_none(monkeypatch):
    monkeypatch.setattr(erpnext, "get_list", _fake_get_list([]))
    assert clientes.buscar_por_telefono(META) is None


def test_basura_no_consulta_erpnext(monkeypatch):
    def explota(*args, **kwargs):
        raise AssertionError("no debería consultar ERPNext")

    monkeypatch.setattr(erpnext, "get_list", explota)
    assert clientes.buscar_por_telefono("") is None
    assert clientes.buscar_por_telefono("hola") is None


def test_telefono_duplicado_elige_deterministicamente(monkeypatch):
    monkeypatch.setattr(
        erpnext,
        "get_list",
        _fake_get_list(
            [
                {"name": "CUST-B", "mobile_no": "+5493511234567"},
                {"name": "CUST-A", "mobile_no": "351 123 4567"},
            ]
        ),
    )

    assert clientes.buscar_por_telefono(META)["name"] == "CUST-A"


def test_error_de_erpnext_se_propaga(monkeypatch):
    def falla(*args, **kwargs):
        raise erpnext.ERPNextError("ERPNext no disponible durante la consulta")

    monkeypatch.setattr(erpnext, "get_list", falla)
    with pytest.raises(erpnext.ERPNextError):
        clientes.buscar_por_telefono(META)


# --- router -----------------------------------------------------------------


@pytest.mark.parametrize(
    "configurado",
    ["+54 9 351 123-4567", "0351 15 123-4567", "351 1234567", "+5493511234567"],
)
def test_es_equipo_acepta_telefonos_equipo_escritos_a_mano(monkeypatch, configurado):
    monkeypatch.setenv("TELEFONOS_EQUIPO", configurado)
    router.recargar()
    try:
        assert router.es_equipo(META)
        assert router.es_equipo(f"+{META}")
        assert not router.es_equipo("5491145678901")
        assert not router.es_equipo("")
    finally:
        monkeypatch.delenv("TELEFONOS_EQUIPO", raising=False)
        router.recargar()


def test_staff_es_lista_ordenada_y_sin_duplicados(monkeypatch):
    monkeypatch.setenv(
        "TELEFONOS_EQUIPO",
        " +54 9 11 4567-8901, 0351 15 123-4567 ,5491145678901, basura, ,351 1234567",
    )
    router.recargar()
    try:
        assert isinstance(router.STAFF, list)
        # Orden de configuración (el primero es el que recibe las alertas),
        # duplicados en distinto formato colapsados, basura ignorada.
        assert router.STAFF == ["5491145678901", "5493511234567"]
        # Determinista entre recargas.
        antes = list(router.STAFF)
        router.recargar()
        assert antes == router.STAFF
        # notificar.py hace sorted(STAFF): tiene que seguir funcionando.
        assert sorted(router.STAFF) == ["5491145678901", "5493511234567"]
    finally:
        monkeypatch.delenv("TELEFONOS_EQUIPO", raising=False)
        router.recargar()


def test_staff_vacio_no_rutea_a_nadie(monkeypatch):
    monkeypatch.delenv("TELEFONOS_EQUIPO", raising=False)
    router.recargar()
    assert router.STAFF == []
    assert not router.es_equipo(META)
