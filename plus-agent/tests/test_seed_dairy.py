from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import Mock, call

import pytest

os.environ.setdefault("ERPNEXT_URL", "http://erpnext.test")
os.environ.setdefault("ERPNEXT_API_KEY", "test-key")
os.environ.setdefault("ERPNEXT_API_SECRET", "test-secret")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from idioma_captura import restos_en_espanol

from deploy import seed_dairy as seed


@pytest.fixture(autouse=True)
def blocked_erpnext_network(monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    """Fail any ERPNext boundary that a test did not explicitly replace."""
    boundaries = {}
    for name in (
        "get_list",
        "get_doc",
        "create_doc",
        "default_context",
        "submit_doc",
    ):
        boundary = Mock(side_effect=AssertionError(f"unexpected ERPNext call: {name}"))
        monkeypatch.setattr(seed.erpnext, name, boundary)
        boundaries[name] = boundary
    return boundaries


def _item(
    code: str = "LEC-ENT-1L",
    warehouse: str = "Productos Terminados - LP",
    qty: object = 5,
    valuation_rate: object = 720,
) -> dict:
    return {
        "item_code": code,
        "warehouse": warehouse,
        "qty": qty,
        "valuation_rate": valuation_rate,
    }


def test_opening_account_is_selected_by_structural_type(
    blocked_erpnext_network: dict[str, Mock],
) -> None:
    get_list = blocked_erpnext_network["get_list"]
    get_list.side_effect = None
    get_list.return_value = [{"name": "Apertura Transitoria - LP"}]
    items = [_item()]

    payload = seed._stock_reconciliation_payload("Lácteos Plus SA", items)

    assert payload == {
        "company": "Lácteos Plus SA",
        "purpose": "Opening Stock",
        "expense_account": "Apertura Transitoria - LP",
        "items": items,
    }
    get_list.assert_called_once_with(
        "Account",
        filters=[
            ["company", "=", "Lácteos Plus SA"],
            ["is_group", "=", 0],
            ["account_type", "=", "Temporary"],
        ],
        fields=["name"],
        limit=1,
    )
    assert "account_name" not in repr(get_list.call_args)


def test_fallback_stock_adjustment_is_also_selected_by_type(
    blocked_erpnext_network: dict[str, Mock],
) -> None:
    get_list = blocked_erpnext_network["get_list"]
    get_list.side_effect = [[], [{"name": "Ajustes de Inventario - LP"}]]
    items = [_item()]

    payload = seed._stock_reconciliation_payload("Lácteos Plus SA", items)

    assert payload == {
        "company": "Lácteos Plus SA",
        "purpose": "Stock Reconciliation",
        "expense_account": "Ajustes de Inventario - LP",
        "items": items,
    }
    assert get_list.call_args_list == [
        call(
            "Account",
            filters=[
                ["company", "=", "Lácteos Plus SA"],
                ["is_group", "=", 0],
                ["account_type", "=", "Temporary"],
            ],
            fields=["name"],
            limit=1,
        ),
        call(
            "Account",
            filters=[
                ["company", "=", "Lácteos Plus SA"],
                ["is_group", "=", 0],
                ["account_type", "=", "Stock Adjustment"],
            ],
            fields=["name"],
            limit=1,
        ),
    ]
    assert "account_name" not in repr(get_list.call_args_list)


def test_existing_reconciliation_matches_numbers_and_item_order(
    blocked_erpnext_network: dict[str, Mock],
) -> None:
    expected = [
        _item("LEC-ENT-1L", qty=5, valuation_rate=720),
        _item("QUE-CRE", qty=2, valuation_rate=5880),
    ]
    get_list = blocked_erpnext_network["get_list"]
    get_list.side_effect = None
    get_list.return_value = [{"name": "MAT-RECO-2026-00001", "docstatus": 1}]
    get_doc = blocked_erpnext_network["get_doc"]
    get_doc.side_effect = None
    get_doc.return_value = {
        "name": "MAT-RECO-2026-00001",
        "docstatus": 1,
        "items": [
            _item("QUE-CRE", qty="2.000", valuation_rate="5880.00"),
            _item("LEC-ENT-1L", qty="5.0", valuation_rate="720.000"),
        ],
    }

    assert seed._existing_stock_reconciliation(
        "Lácteos Plus SA", expected
    ) == ("MAT-RECO-2026-00001", 1)
    get_list.assert_called_once_with(
        "Stock Reconciliation",
        filters=[
            ["company", "=", "Lácteos Plus SA"],
            ["docstatus", "!=", 2],
        ],
        fields=["name", "docstatus"],
        limit=500,
    )


def test_main_reuses_matching_reconciliation_and_never_submits(
    monkeypatch: pytest.MonkeyPatch,
    blocked_erpnext_network: dict[str, Mock],
) -> None:
    monkeypatch.setattr(seed, "_ensure", Mock())
    monkeypatch.setattr(seed, "PRODUCTOS", [])
    monkeypatch.setattr(seed, "CLIENTES", [])
    monkeypatch.setattr(seed, "STOCK_INICIAL", {"LEC-ENT-1L": 5})
    monkeypatch.setattr(
        seed,
        "_existing_stock_reconciliation",
        Mock(return_value=("MAT-RECO-2026-00001", 0)),
    )
    context = blocked_erpnext_network["default_context"]
    context.side_effect = None
    context.return_value = ("Lácteos Plus SA", "Productos Terminados - LP")

    seed.main()

    blocked_erpnext_network["create_doc"].assert_not_called()
    blocked_erpnext_network["submit_doc"].assert_not_called()


def test_main_creates_only_a_draft_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    blocked_erpnext_network: dict[str, Mock],
) -> None:
    monkeypatch.setattr(seed, "_ensure", Mock())
    monkeypatch.setattr(seed, "PRODUCTOS", [])
    monkeypatch.setattr(seed, "CLIENTES", [])
    monkeypatch.setattr(seed, "STOCK_INICIAL", {"LEC-ENT-1L": 5})
    monkeypatch.setattr(seed, "_existing_stock_reconciliation", Mock(return_value=None))
    payload = {
        "company": "Lácteos Plus SA",
        "purpose": "Opening Stock",
        "expense_account": "Apertura Transitoria - LP",
        "items": [_item(qty=5, valuation_rate=1)],
    }
    monkeypatch.setattr(
        seed, "_stock_reconciliation_payload", Mock(return_value=payload)
    )
    context = blocked_erpnext_network["default_context"]
    context.side_effect = None
    context.return_value = ("Lácteos Plus SA", "Productos Terminados - LP")
    create_doc = blocked_erpnext_network["create_doc"]
    create_doc.side_effect = None
    create_doc.return_value = {"name": "MAT-RECO-2026-00002"}

    seed.main()

    create_doc.assert_called_once_with("Stock Reconciliation", payload)
    assert payload.get("docstatus", 0) == 0
    blocked_erpnext_network["submit_doc"].assert_not_called()


# --------------------------------------------------- el catálogo en inglés
# Una demo es el producto: un prospecto que habla inglés viendo al bot contestar
# «Leche entera sachet 1 L» está viendo el producto de otro. El dataset en
# inglés es el mismo negocio con otras palabras, y estos tests fijan las dos
# mitades de eso: que sea EL MISMO (misma forma, mismos números) y que esté de
# verdad en inglés.


def test_el_dataset_por_defecto_es_el_de_siempre(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un despliegue que ya existe corre el script y siembra lo que sembró."""
    monkeypatch.delenv("SEED_DATASET", raising=False)
    datos = seed.dataset()

    assert datos.nombre == "es"
    assert datos.productos is seed.PRODUCTOS
    assert datos.clientes is seed.CLIENTES
    assert datos.stock is seed.STOCK_INICIAL
    assert datos.grupo == "Lacteos"


@pytest.mark.parametrize("dicho", ["en", "EN", " en ", "english", "en_US", "ingles"])
def test_el_ingles_se_pide_como_a_uno_se_le_ocurra(dicho: str) -> None:
    assert seed.dataset(dicho).nombre == "en"


def test_el_dataset_se_puede_pedir_por_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEED_DATASET", "en")
    assert seed.dataset().nombre == "en"
    # El argumento explícito le gana al entorno.
    assert seed.dataset("es").nombre == "es"


def test_el_dataset_se_puede_pedir_por_argumento(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SEED_DATASET", raising=False)
    assert seed._pedido_en_la_linea(["--dataset", "en"]).nombre == "en"
    assert seed._pedido_en_la_linea([]).nombre == "es"


def test_un_dataset_desconocido_siembra_el_espanol_y_lo_dice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sembrar el catálogo equivocado en un ERPNext real es un catálogo que
    alguien tiene que borrar a mano: ante la duda, el de siempre."""
    assert seed.dataset("fr").nombre == "es"
    assert "dataset desconocido" in capsys.readouterr().out


def test_los_dos_datasets_tienen_exactamente_la_misma_forma() -> None:
    """Mismo negocio, otras palabras: un escenario escrito contra uno camina el
    otro. Si un día se le agrega un producto a uno solo, esto se cae."""
    es, en = seed.dataset("es"), seed.dataset("en")

    assert len(en.productos) == len(es.productos) == 13
    assert len(en.clientes) == len(es.clientes) == 7
    assert len(en.stock) == len(es.stock)
    assert len(en.unidades) == len(es.unidades)
    assert len(en.grupos_cliente) == len(es.grupos_cliente)
    # Las mismas cantidades, producto por producto y en el mismo orden.
    assert list(en.stock.values()) == list(es.stock.values())
    # Y la misma relación de precios, que es lo que hace que un pedido de la
    # demo caiga del mismo lado de un tope en los dos catálogos.
    precios_es = [p for *_, p in es.productos]
    precios_en = [p for *_, p in en.productos]
    assert [round(p / precios_en[0], 4) for p in precios_en] == [
        round(p / precios_es[0], 4) for p in precios_es
    ]


@pytest.mark.parametrize("dicho", ["es", "en"])
def test_cada_dataset_es_coherente_consigo_mismo(dicho: str) -> None:
    """El stock nombra productos que existen, y cada producto y cliente usa una
    unidad y un grupo que el script crea antes."""
    datos = seed.dataset(dicho)
    codigos = {code for code, *_ in datos.productos}

    assert set(datos.stock) == codigos
    assert {uom for _, _, uom, _ in datos.productos} <= set(datos.unidades)
    assert {grupo for _, _, grupo in datos.clientes} <= set(datos.grupos_cliente)
    assert len({tel for _, tel, _ in datos.clientes}) == len(datos.clientes)


def test_el_catalogo_en_ingles_no_tiene_una_palabra_en_espanol() -> None:
    """INCLUIDOS LOS CÓDIGOS. El código del producto sale por WhatsApp —el aviso
    de un conteo dice «Count of QUE-CRE»— así que un catálogo en inglés con
    códigos en español le muestra al prospecto justo la palabra que el resto de
    la demo evita."""
    datos = seed.dataset("en")
    texto = " ".join(
        [datos.grupo, datos.pregunta, *datos.unidades, *datos.grupos_cliente]
        + [f"{code} {nombre}" for code, nombre, _, _ in datos.productos]
        + [f"{nombre} {grupo}" for nombre, _, grupo in datos.clientes]
    )

    assert restos_en_espanol(texto) == []


def test_el_catalogo_en_espanol_no_se_movio() -> None:
    """El de siempre es el default y no cambió: los mismos códigos, que son los
    que dicen los pedidos que ya están en ese ERPNext."""
    datos = seed.dataset("es")

    assert [code for code, *_ in datos.productos][:3] == [
        "LEC-ENT-1L", "LEC-DES-1L", "LEC-BOT-1L"
    ]
    assert datos.productos[0][1] == "Leche entera sachet 1 L"
    assert datos.clientes[0][0] == "Almacen Don Jose"


def test_main_siembra_el_catalogo_que_le_dan(
    monkeypatch: pytest.MonkeyPatch,
    blocked_erpnext_network: dict[str, Mock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure = Mock()
    monkeypatch.setattr(seed, "_ensure", ensure)
    monkeypatch.setattr(seed, "_existing_stock_reconciliation", Mock(return_value=None))
    monkeypatch.setattr(
        seed,
        "_stock_reconciliation_payload",
        Mock(return_value={"purpose": "Opening Stock", "items": []}),
    )
    get_list = blocked_erpnext_network["get_list"]
    get_list.side_effect = None
    get_list.return_value = [{"name": "ya tiene precio"}]
    get_doc = blocked_erpnext_network["get_doc"]
    get_doc.side_effect = None
    get_doc.return_value = {"name": "Standard Selling", "currency": "USD"}
    context = blocked_erpnext_network["default_context"]
    context.side_effect = None
    context.return_value = ("Dairy Plus LLC", "Finished Goods - DP")
    create_doc = blocked_erpnext_network["create_doc"]
    create_doc.side_effect = None
    create_doc.return_value = {"name": "MAT-RECO-2026-00003"}

    seed.main(seed.dataset("en"))

    sembrados = [c.args[1] for c in ensure.call_args_list]
    assert "Dairy" in sembrados
    assert "MILK-WHL-1L" in sembrados and "LEC-ENT-1L" not in sembrados
    assert "Riverside Grocery" in sembrados
    salida = capsys.readouterr().out
    # La nota de los precios inventados sigue estando, y la frase de prueba
    # nombra un producto del catálogo que se acaba de sembrar.
    assert "los precios son inventados" in salida
    assert "cream cheese" in salida


# ------------------------------------------------- la moneda del catálogo
# Un `Item Price` sin `currency` hereda la de la lista de precios. Los dos
# catálogos son plausibles por separado —1.20 es un litro de leche en dólares y
# 1200 lo es en pesos— así que sembrar el inglés contra una lista en ARS no da
# ningún error: da trece precios mil veces más baratos, que la pantalla muestra
# como `$1.20` con LOCALE=en_US mientras los libros leen un peso veinte.


def _lista_en(moneda: str) -> Mock:
    return Mock(return_value={"name": "Standard Selling", "currency": moneda})


def test_el_catalogo_en_ingles_no_se_siembra_en_una_lista_en_pesos(
    monkeypatch: pytest.MonkeyPatch,
    blocked_erpnext_network: dict[str, Mock],
) -> None:
    """El hallazgo, escrito como test: falla ANTES de escribir un solo precio."""
    monkeypatch.setattr(seed, "_ensure", Mock())
    get_doc = blocked_erpnext_network["get_doc"]
    get_doc.side_effect = None
    get_doc.return_value = {"name": "Standard Selling", "currency": "ARS"}

    with pytest.raises(seed.MonedaEquivocada) as problema:
        seed.main(seed.dataset("en"))

    dicho = str(problema.value)
    assert "ARS" in dicho and "USD" in dicho
    # Y no se escribió NADA: ni un precio, ni el stock inicial.
    blocked_erpnext_network["create_doc"].assert_not_called()


def test_el_catalogo_en_espanol_en_una_lista_en_pesos_sigue_andando(
    monkeypatch: pytest.MonkeyPatch,
    blocked_erpnext_network: dict[str, Mock],
) -> None:
    monkeypatch.setattr(seed, "_ensure", Mock())
    monkeypatch.setattr(seed, "_existing_stock_reconciliation", Mock(return_value=None))
    monkeypatch.setattr(
        seed,
        "_stock_reconciliation_payload",
        Mock(return_value={"purpose": "Opening Stock", "items": []}),
    )
    get_doc = blocked_erpnext_network["get_doc"]
    get_doc.side_effect = None
    get_doc.return_value = {"name": "Standard Selling", "currency": "ARS"}
    get_list = blocked_erpnext_network["get_list"]
    get_list.side_effect = None
    get_list.return_value = [{"name": "ya tiene precio"}]
    context = blocked_erpnext_network["default_context"]
    context.side_effect = None
    context.return_value = ("Lácteos Plus SA", "Principal - LT")
    create_doc = blocked_erpnext_network["create_doc"]
    create_doc.side_effect = None
    create_doc.return_value = {"name": "MAT-RECO-2026-00004"}

    seed.main(seed.dataset("es"))


def test_cada_precio_se_escribe_con_su_moneda_y_su_unidad(
    monkeypatch: pytest.MonkeyPatch,
    blocked_erpnext_network: dict[str, Mock],
) -> None:
    """Las dos explícitas. `currency` porque si no se hereda la de la lista;
    `uom` porque `policy._precio_autorizado` exige que la unidad del precio sea
    la de la línea del pedido — un precio sin unidad no auto-confirma nada."""
    monkeypatch.setattr(seed, "_ensure", Mock())
    monkeypatch.setattr(seed, "_existing_stock_reconciliation", Mock(return_value=None))
    monkeypatch.setattr(
        seed,
        "_stock_reconciliation_payload",
        Mock(return_value={"purpose": "Opening Stock", "items": []}),
    )
    get_doc = blocked_erpnext_network["get_doc"]
    get_doc.side_effect = None
    get_doc.return_value = {"name": "Standard Selling", "currency": "USD"}
    get_list = blocked_erpnext_network["get_list"]
    get_list.side_effect = None
    get_list.return_value = []  # ningún precio todavía
    context = blocked_erpnext_network["default_context"]
    context.side_effect = None
    context.return_value = ("Dairy Plus LLC", "Finished Goods - DP")
    create_doc = blocked_erpnext_network["create_doc"]
    create_doc.side_effect = None
    create_doc.return_value = {"name": "X"}

    datos = seed.dataset("en")
    seed.main(datos)

    precios = [
        c.args[1] for c in create_doc.call_args_list if c.args[0] == "Item Price"
    ]
    assert len(precios) == len(datos.productos)
    unidades = {code: uom for code, _, uom, _ in datos.productos}
    for pago in precios:
        assert pago["currency"] == "USD", pago
        assert pago["uom"] == unidades[pago["item_code"]], pago
        assert pago["price_list"] == seed.LISTA_DE_PRECIOS


def test_una_lista_de_precios_ilegible_no_frena_la_siembra(
    monkeypatch: pytest.MonkeyPatch,
    blocked_erpnext_network: dict[str, Mock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Todavía no existe, o no se puede leer: se sigue, y el `currency` explícito
    de cada precio hace que ERPNext rechace la mezcla si aparece con otra."""
    get_doc = blocked_erpnext_network["get_doc"]
    get_doc.side_effect = seed.erpnext.ERPNextError("no existe")

    seed._revisar_moneda(seed.dataset("en"))

    assert "no pude leer la moneda" in capsys.readouterr().out
