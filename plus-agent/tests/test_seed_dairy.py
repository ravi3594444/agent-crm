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


# ---------------------------------------------------------------------------
# LOS DOS DATASETS
#
# El inglés existe porque a un prospecto que habla inglés no se lo puede pasear
# por un catálogo de *Leche entera sachet 1 L*: se pasa la demo leyendo los
# nombres de los productos en vez de mirar trabajar al agente.
#
# Lo que se prueba acá es sobre todo que sean el MISMO dataset con otras
# palabras. Un dataset en inglés con menos productos es un dataset donde la
# mitad de la demo no tiene renglones que mostrar, y eso no se ve hasta que
# alguien está haciendo la demo.
# ---------------------------------------------------------------------------


def test_el_default_sigue_siendo_el_castellano(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un despliegue que corre el script sin argumentos recibe lo que recibía."""
    monkeypatch.delenv("SEED_DATASET", raising=False)
    assert seed.elegir() is seed.DATASETS["es"]
    assert seed.elegir("").grupo == "Lacteos"
    assert ("LEC-ENT-1L", "Leche entera sachet 1 L", "Unidad", 1200) in seed.elegir().productos


@pytest.mark.parametrize("pedido", ["en", "EN", " en ", "En"])
def test_el_dataset_ingles_se_pide_por_nombre(pedido) -> None:
    assert seed.elegir(pedido) is seed.DATASETS["en"]


def test_el_dataset_sale_del_entorno_cuando_no_hay_argumento(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEED_DATASET", "en")
    assert seed.elegir() is seed.DATASETS["en"]
    # Un argumento explícito le gana al entorno.
    assert seed.elegir("es") is seed.DATASETS["es"]


@pytest.mark.parametrize("basura", ["fr", "ingles", "english", "1", "  "])
def test_un_dataset_que_no_existe_cae_en_el_castellano(
    monkeypatch: pytest.MonkeyPatch, basura
) -> None:
    """Negarse a correr por un typo en una variable de entorno no ayuda a nadie.

    Esto es un script que alguien corre a mano antes de una demo, no un camino
    que autorice algo: la degradación correcta es el default, no una excepción.
    """
    monkeypatch.delenv("SEED_DATASET", raising=False)
    assert seed.elegir(basura) is seed.DATASETS["es"]


@pytest.mark.parametrize(
    "argv, esperado",
    [
        (["--dataset", "en"], "en"),
        (["--dataset=en"], "en"),
        (["--dataset", "es"], "es"),
        ([], ""),
        (["--dataset"], ""),
        (["otra-cosa"], ""),
    ],
)
def test_el_argumento_de_la_linea_de_comandos(argv, esperado) -> None:
    assert seed._del_argv(argv) == esperado


def test_los_dos_datasets_tienen_la_misma_forma() -> None:
    es, en = seed.DATASETS["es"], seed.DATASETS["en"]
    assert len(en.productos) == len(es.productos)
    assert len(en.clientes) == len(es.clientes)
    assert len(en.stock) == len(es.stock)
    assert len(en.grupos_cliente) == len(es.grupos_cliente)
    # La mezcla de productos por unidad y por peso es la misma: el camino que
    # pide «2 Kg de queso» tiene que existir en los dos.
    for datos in (es, en):
        unidades = [p[2] for p in datos.productos]
        assert unidades.count("Kg") == 4
        assert len(set(unidades)) == 2
    # Y los precios, iguales: son inventados en los dos, así que hacerlos
    # distintos sólo daría dos demos que no se pueden comparar.
    assert [p[3] for p in en.productos] == [p[3] for p in es.productos]
    assert sorted(en.stock.values()) == sorted(es.stock.values())


@pytest.mark.parametrize("clave", ["es", "en"])
def test_el_stock_nombra_exactamente_los_productos_del_catalogo(clave) -> None:
    """Una fila de stock con un código que no existe no carga nada y no avisa."""
    datos = seed.DATASETS[clave]
    assert set(datos.stock) == {p[0] for p in datos.productos}


@pytest.mark.parametrize("clave", ["es", "en"])
def test_los_codigos_y_los_telefonos_no_se_repiten(clave) -> None:
    datos = seed.DATASETS[clave]
    codigos = [p[0] for p in datos.productos]
    assert len(set(codigos)) == len(codigos)
    telefonos = [c[1] for c in datos.clientes]
    assert len(set(telefonos)) == len(telefonos)


def test_los_codigos_de_los_dos_datasets_no_se_pisan() -> None:
    """Sembrar los dos en un mismo ERPNext da dos catálogos, no uno mezclado.

    `_ensure` es idempotente POR CÓDIGO: si el inglés reusara «LEC-ENT-1L», la
    segunda corrida encontraría el código ya creado y lo dejaría con el nombre
    de la primera. El resultado sería un catálogo medio traducido, que es
    exactamente lo que un prospecto no puede ver.
    """
    es = {p[0] for p in seed.DATASETS["es"].productos}
    en = {p[0] for p in seed.DATASETS["en"].productos}
    assert es & en == set()
    assert seed.DATASETS["es"].grupo != seed.DATASETS["en"].grupo


# Lo que el dataset inglés PUEDE compartir con el castellano, y por qué cada
# cosa. Son nombres de producto que en inglés se dicen igual —no traducciones
# olvidadas— más la unidad de peso, que es la misma sigla en los dos.
_COMPARTIDAS = frozenset({
    "dulce", "de", "leche",  # el postre se llama así en inglés también
    "port", "salut",         # el queso, igual que «brie» o «gouda»
    "kg", "g", "l", "ml",    # unidades, no palabras: se escriben igual
})


def _palabras(datos) -> set[str]:
    import re

    texto = " ".join(
        [p[1] for p in datos.productos]
        + [p[2] for p in datos.productos]
        + [c[0] for c in datos.clientes]
        + [c[2] for c in datos.clientes]
        + [datos.grupo]
    )
    return {f.lower() for f in re.findall(r"[^\W\d_]+", texto, re.UNICODE)}


def test_el_catalogo_ingles_no_repite_una_palabra_del_castellano() -> None:
    """Es el punto entero: ni una palabra en castellano donde el prospecto mire.

    Se afirma contra el VOCABULARIO del otro dataset y no con un detector de
    español. El detector de tests/idioma_captura.py existe para prosa: mira
    tildes y una lista de palabras funcionales, y da limpio para «Leche entera
    sachet 1 L», que es exactamente el nombre que motivó todo esto. Un guard
    que no se puede romper no está guardando — así que el guard es «ninguna
    palabra del catálogo castellano aparece en el inglés», que sí se rompe en
    cuanto alguien deja un nombre sin traducir.
    """
    restos = (_palabras(seed.DATASETS["en"]) & _palabras(seed.DATASETS["es"])) - _COMPARTIDAS
    assert restos == set(), f"quedaron palabras del dataset castellano: {sorted(restos)}"


def test_ese_guard_se_rompe_con_un_nombre_sin_traducir() -> None:
    """El guard del guard: si no se cae con esto, no está comparando nada."""
    import dataclasses

    sin_traducir = dataclasses.replace(
        seed.DATASETS["en"],
        productos=[("WHL-MLK-1L", "Leche entera sachet 1 L", "Each", 1200)],
    )
    restos = (_palabras(sin_traducir) & _palabras(seed.DATASETS["es"])) - _COMPARTIDAS
    assert "leche" not in restos, "«leche» está permitida por el postre"
    assert {"entera", "sachet"} <= restos


def test_main_siembra_el_dataset_que_recibe(
    monkeypatch: pytest.MonkeyPatch,
    blocked_erpnext_network: dict[str, Mock],
) -> None:
    """El dataset es un parámetro, no una lectura escondida adentro de main."""
    ensure = Mock()
    monkeypatch.setattr(seed, "_ensure", ensure)
    monkeypatch.setattr(seed, "_existing_stock_reconciliation", Mock(return_value=("MAT-RECO-1", 1)))
    get_list = blocked_erpnext_network["get_list"]
    get_list.side_effect = None
    get_list.return_value = [{"name": "ya-tiene-precio"}]
    context = blocked_erpnext_network["default_context"]
    context.side_effect = None
    context.return_value = ("Dairy Plus LLC", "Finished Goods - DP")

    seed.main(seed.DATASETS["en"])

    creados = [c.args[1] for c in ensure.call_args_list]
    assert "Dairy" in creados
    assert "Whole milk 1 L pouch" not in creados  # se crea por CÓDIGO
    assert "WHL-MLK-1L" in creados
    assert "Corner Grocery" in creados
    assert "Each" in creados
    # Y nada del dataset castellano se coló.
    assert "Lacteos" not in creados
    assert "LEC-ENT-1L" not in creados


def test_main_sin_argumento_usa_los_nombres_del_modulo(
    monkeypatch: pytest.MonkeyPatch,
    blocked_erpnext_network: dict[str, Mock],
) -> None:
    """Lo que ya manejaba este script poniéndole `seed.PRODUCTOS` sigue andando."""
    ensure = Mock()
    monkeypatch.setattr(seed, "_ensure", ensure)
    monkeypatch.setattr(seed, "GRUPO", "Solo-Este-Grupo")
    monkeypatch.setattr(seed, "PRODUCTOS", [])
    monkeypatch.setattr(seed, "CLIENTES", [])
    monkeypatch.setattr(seed, "STOCK_INICIAL", {})
    monkeypatch.setattr(seed, "_existing_stock_reconciliation", Mock(return_value=("MAT-RECO-1", 1)))
    context = blocked_erpnext_network["default_context"]
    context.side_effect = None
    context.return_value = ("Lácteos Plus SA", "Productos Terminados - LP")

    seed.main()

    creados = [c.args[1] for c in ensure.call_args_list]
    assert "Solo-Este-Grupo" in creados
    assert "Lacteos" not in creados
