"""El 417: la compañía no tiene cuentas de inventario y ERPNext no escribe stock.

Lo que estos tests cuidan no es que el script "corra": es que no mienta. Dice
qué encontró, qué cambiaría y qué dejó como estaba; y si ERPNext le contesta
200 sin haber guardado, eso es un error y no un éxito.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import cuentas_inventario as cuentas


@pytest.fixture(autouse=True)
def sin_red(monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    """Cualquier frontera con ERPNext que un test no reemplace, revienta."""
    bordes = {}
    for nombre in ("get_list", "get_doc", "create_doc", "default_context"):
        borde = Mock(side_effect=AssertionError(f"llamada inesperada a ERPNext: {nombre}"))
        monkeypatch.setattr(cuentas.erpnext, nombre, borde)
        bordes[nombre] = borde
    admin = Mock(side_effect=AssertionError("llamada inesperada a pedido_admin"))
    monkeypatch.setattr(cuentas, "pedido_admin", admin)
    bordes["pedido_admin"] = admin
    return bordes


def _empresa(bordes: dict[str, Mock], **campos: str) -> None:
    bordes["get_doc"].side_effect = None
    bordes["get_doc"].return_value = {"name": "Lácteos Plus SA", **campos}


def _cuentas(bordes: dict[str, Mock], *respuestas: list[dict]) -> None:
    bordes["get_list"].side_effect = list(respuestas)


def test_si_la_empresa_ya_tiene_la_cuenta_no_se_toca_nada(sin_red: dict[str, Mock]) -> None:
    """Correrlo dos veces es un no-op, y lo dice en vez de fingir que hizo algo."""
    _empresa(sin_red, default_inventory_account="Stock In Hand - LP")

    cuenta = cuentas.resolver("Lácteos Plus SA", cuentas.INVENTARIO)

    assert cuenta.accion == "ya está"
    assert cuenta.nombre == "Stock In Hand - LP"
    assert cuenta.hay_que_tocar is False
    sin_red["get_list"].assert_not_called()


def test_la_cuenta_se_busca_por_TIPO_y_no_por_nombre(sin_red: dict[str, Mock]) -> None:
    """Un plan de cuentas está en el idioma de quien lo instaló: «Stock In Hand»
    puede llamarse «Mercadería en stock». Buscar por nombre sería no encontrarla."""
    _empresa(sin_red, default_inventory_account="")
    _cuentas(sin_red, [{"name": "Mercadería en stock - LP"}])

    cuenta = cuentas.resolver("Lácteos Plus SA", cuentas.INVENTARIO)

    assert cuenta.accion == "se asigna"
    assert cuenta.nombre == "Mercadería en stock - LP"
    assert cuenta.padre == ""
    sin_red["get_list"].assert_called_once_with(
        "Account",
        filters=[
            ["company", "=", "Lácteos Plus SA"],
            ["is_group", "=", 0],
            ["account_type", "=", "Stock"],
            ["disabled", "=", 0],
        ],
        fields=["name"],
        limit=50,
    )
    assert "account_name" not in repr(sin_red["get_list"].call_args)


def test_sin_hoja_se_crea_bajo_un_grupo_del_mismo_tipo(sin_red: dict[str, Mock]) -> None:
    _empresa(sin_red, stock_adjustment_account="")
    _cuentas(sin_red, [], [{"name": "Gastos de Stock - LP"}])

    cuenta = cuentas.resolver("Lácteos Plus SA", cuentas.AJUSTE)

    assert cuenta.accion == "se crea y se asigna"
    assert cuenta.nombre == "Stock Adjustment"
    assert cuenta.padre == "Gastos de Stock - LP"
    assert cuenta.descripcion == "Stock Adjustment (nueva, bajo Gastos de Stock - LP)"


def test_sin_ningun_grupo_se_niega_en_vez_de_adivinar(sin_red: dict[str, Mock]) -> None:
    """Una cuenta creada en la rama equivocada de un plan de cuentas real la
    desarma después alguien a mano. Mejor pedir el padre que inventarlo."""
    _empresa(sin_red, default_inventory_account="")
    _cuentas(sin_red, [], [])

    cuenta = cuentas.resolver("Lácteos Plus SA", cuentas.INVENTARIO)

    assert cuenta.accion == "falta"
    assert cuenta.hay_que_tocar is False
    assert "--padre-inventario" in cuenta.detalle


def test_el_padre_que_pide_el_dueno_le_gana_a_la_busqueda(sin_red: dict[str, Mock]) -> None:
    _empresa(sin_red, default_inventory_account="")
    _cuentas(sin_red, [], [{"name": "Un grupo cualquiera - LP"}])

    cuenta = cuentas.resolver(
        "Lácteos Plus SA", cuentas.INVENTARIO, padre="Activo Corriente - LP", nombre="Mercadería"
    )

    assert cuenta.padre == "Activo Corriente - LP"
    assert cuenta.nombre == "Mercadería"


def test_un_200_que_no_guardo_es_un_error_y_no_un_exito(
    monkeypatch: pytest.MonkeyPatch, sin_red: dict[str, Mock]
) -> None:
    """Un PUT de Frappe es un save: el validate() puede contestar 200 con el
    valor viejo. Creerle sería decirle al dueño que ya está cuando no está."""
    monkeypatch.setattr(
        cuentas,
        "pedido_admin",
        Mock(return_value={"data": {"default_inventory_account": ""}}),
    )

    with pytest.raises(cuentas.erpnext.ERPNextError) as excepcion:
        cuentas.asignar("Lácteos Plus SA", {cuentas.INVENTARIO: "Stock In Hand - LP"})

    assert "Default Inventory Account" in str(excepcion.value)
    assert "vacío" in str(excepcion.value)


def test_si_guardo_de_verdad_no_se_queja(
    monkeypatch: pytest.MonkeyPatch, sin_red: dict[str, Mock]
) -> None:
    admin = Mock(return_value={"data": {cuentas.INVENTARIO: "Stock In Hand - LP"}})
    monkeypatch.setattr(cuentas, "pedido_admin", admin)

    cuentas.asignar("Lácteos Plus SA", {cuentas.INVENTARIO: "Stock In Hand - LP"})

    admin.assert_called_once_with(
        "PUT",
        "/api/resource/Company/Lácteos Plus SA",
        {cuentas.INVENTARIO: "Stock In Hand - LP"},
    )


def test_la_prueba_crea_un_borrador_y_lo_borra(
    monkeypatch: pytest.MonkeyPatch,
    sin_red: dict[str, Mock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """El 417 salta al GUARDAR, así que la única prueba honesta es guardar. Y un
    borrador que se queda es basura que alguien limpia: se borra en el finally."""
    sin_red["get_list"].side_effect = [
        [{"item_code": "LEC-ENT-1L"}],          # el producto de la prueba
        [{"name": "Apertura Transitoria - LP"}],  # la cuenta transitoria
    ]
    crear = sin_red["create_doc"]
    crear.side_effect = None
    crear.return_value = {"name": "MAT-RECO-2026-00009"}
    admin = Mock(return_value={})
    monkeypatch.setattr(cuentas, "pedido_admin", admin)

    assert cuentas.probar_paso_de_stock("Lácteos Plus SA", "Productos Terminados - LP") is True

    payload = crear.call_args.args[1]
    assert payload["purpose"] == "Opening Stock"
    assert payload["items"] == [
        {
            "item_code": "LEC-ENT-1L",
            "warehouse": "Productos Terminados - LP",
            "qty": 1,
            "valuation_rate": 1,
        }
    ]
    admin.assert_called_once_with(
        "DELETE", "/api/resource/Stock Reconciliation/MAT-RECO-2026-00009"
    )
    assert "ACEPTÓ" in capsys.readouterr().out


def test_si_sigue_fallando_lo_dice_con_el_estado_que_devolvio(
    sin_red: dict[str, Mock], capsys: pytest.CaptureFixture[str]
) -> None:
    sin_red["get_list"].side_effect = [
        [{"item_code": "LEC-ENT-1L"}],
        [{"name": "Apertura Transitoria - LP"}],
    ]
    sin_red["create_doc"].side_effect = cuentas.erpnext.ERPNextError(
        "ERPNext rechazó la creación de Stock Reconciliation (estado 417)"
    )

    assert cuentas.probar_paso_de_stock("Lácteos Plus SA", "Productos Terminados - LP") is False

    salida = capsys.readouterr().out
    assert "SIGUE FALLANDO" in salida
    assert "417" in salida


def test_sin_cuenta_transitoria_el_ajuste_va_contra_la_de_ajustes(
    sin_red: dict[str, Mock],
) -> None:
    _cuentas(sin_red, [], [{"name": "Ajustes de Inventario - LP"}])

    payload = cuentas.payload_reconciliacion("Lácteos Plus SA", [{"item_code": "X"}])

    assert payload["purpose"] == "Stock Reconciliation"
    assert payload["expense_account"] == "Ajustes de Inventario - LP"


def test_sin_aplicar_no_escribe_nada(
    sin_red: dict[str, Mock], capsys: pytest.CaptureFixture[str]
) -> None:
    """El diagnóstico es el default: mira, cuenta lo que haría, y no toca."""
    contexto = sin_red["default_context"]
    contexto.side_effect = None
    contexto.return_value = ("Lácteos Plus SA", "Productos Terminados - LP")
    sin_red["get_doc"].side_effect = None
    sin_red["get_doc"].return_value = {"name": "Lácteos Plus SA"}
    sin_red["get_list"].side_effect = [
        [{"name": "Mercadería en stock - LP"}],
        [{"name": "Ajustes de Inventario - LP"}],
    ]

    assert cuentas.main([]) == 0

    sin_red["create_doc"].assert_not_called()
    sin_red["pedido_admin"].assert_not_called()
    salida = capsys.readouterr().out
    assert "2 cambio(s) pendiente(s)" in salida
    assert "--aplicar" in salida
