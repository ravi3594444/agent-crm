"""El catálogo real del cliente, cargado por un comando en vez de por clicks.

Tres cosas cuidan estos tests, y son las tres que hacen que el dueño se anime a
correrlo: que un archivo con errores no cargue NADA, que sin `--aplicar` no se
escriba NADA, y que correrlo dos veces con el mismo archivo no haga nada la
segunda vez.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import cargar_catalogo as cargar

BIEN = [
    "codigo,nombre,unidad,precio,stock_inicial,grupo",
    "LEC-ENT-1L,Leche entera sachet 1 L,Unidad,1200,400,Lacteos",
    "QUE-CRE,Queso cremoso,Kg,9800.50,45,Lacteos",
]


@pytest.fixture(autouse=True)
def sin_red(monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    """Cualquier frontera con ERPNext que un test no reemplace, revienta."""
    bordes = {}
    for nombre in ("get_list", "get_doc", "create_doc", "default_context"):
        borde = Mock(side_effect=AssertionError(f"llamada inesperada a ERPNext: {nombre}"))
        monkeypatch.setattr(cargar.erpnext, nombre, borde)
        bordes[nombre] = borde
    admin = Mock(side_effect=AssertionError("llamada inesperada a pedido_admin"))
    monkeypatch.setattr(cargar.cuentas, "pedido_admin", admin)
    bordes["pedido_admin"] = admin
    return bordes


def _csv(tmp_path: Path, *lineas: str) -> Path:
    ruta = tmp_path / "catalogo.csv"
    ruta.write_text("\n".join(lineas), encoding="utf-8")
    return ruta


def _erp(**kwargs) -> cargar.EnErpnext:
    base = {
        "unidades": {"Unidad", "Kg"},
        "grupos": {"Lacteos"},
    }
    return cargar.EnErpnext(**{**base, **kwargs})


# ------------------------------------------------------------ el validador


def test_un_archivo_sano_se_lee_entero(tmp_path: Path) -> None:
    filas, problemas = cargar.leer(_csv(tmp_path, *BIEN))

    assert problemas == []
    assert [f.codigo for f in filas] == ["LEC-ENT-1L", "QUE-CRE"]
    assert filas[1].precio == Decimal("9800.50")
    assert filas[1].stock == Decimal(45)
    # La línea que se reporta es la del EDITOR del dueño, contando el encabezado.
    assert [f.linea for f in filas] == [2, 3]


@pytest.mark.parametrize(
    ("fila", "esperado"),
    [
        (",Leche,Unidad,1200,400,Lacteos", "falta el código"),
        ("LEC,,Unidad,1200,400,Lacteos", "falta el nombre"),
        ("LEC,Leche,,1200,400,Lacteos", "falta la unidad"),
        ("LEC,Leche,Unidad,1200,400,", "falta el grupo"),
        ("LEC,Leche,Unidad,mil doscientos,400,Lacteos", "no es un número"),
        ("LEC,Leche,Unidad,$1200,400,Lacteos", "no es un número"),
        ("LEC,Leche,Unidad,\"1.200,50\",400,Lacteos", "no es un número"),
        ("LEC,Leche,Unidad,0,400,Lacteos", "no es un precio"),
        ("LEC,Leche,Unidad,-5,400,Lacteos", "no es un precio"),
        ("LEC,Leche,Unidad,1200,muchas,Lacteos", "no es un número"),
        ("LEC,Leche,Unidad,1200,-1,Lacteos", "no puede ser negativo"),
        ("LEC,Leche,Unidad,1200,400", "columna(s) y son 6"),
        ("EJEMPLO-1,Leche,Unidad,1200,400,Lacteos", "filas de ejemplo"),
    ],
)
def test_cada_fila_mala_se_rechaza_y_nombra_su_linea(
    tmp_path: Path, fila: str, esperado: str
) -> None:
    filas, problemas = cargar.leer(_csv(tmp_path, BIEN[0], fila))

    assert filas == []
    assert len(problemas) == 1
    assert esperado in problemas[0]
    assert "línea 2" in problemas[0]
    assert "catalogo.csv" in problemas[0]


def test_un_codigo_repetido_nombra_las_dos_lineas(tmp_path: Path) -> None:
    filas, problemas = cargar.leer(_csv(tmp_path, *BIEN, BIEN[1]))

    assert len(problemas) == 1
    assert "ya estaba en la línea 2" in problemas[0]
    assert "línea 4" in problemas[0]
    assert [f.codigo for f in filas] == ["LEC-ENT-1L", "QUE-CRE"]


def test_se_juntan_TODOS_los_problemas_y_no_el_primero(tmp_path: Path) -> None:
    """El dueño arregla el archivo de una sentada en vez de correrlo seis veces."""
    _, problemas = cargar.leer(
        _csv(
            tmp_path,
            BIEN[0],
            "LEC,Leche,Unidad,abc,400,Lacteos",
            ",Sin código,Unidad,1200,400,Lacteos",
            "QUE,Queso,Kg,1200,-3,Lacteos",
        )
    )

    assert len(problemas) == 3
    assert [p.split("línea ")[1][0] for p in problemas] == ["2", "3", "4"]


def test_un_encabezado_distinto_no_se_adivina(tmp_path: Path) -> None:
    _, problemas = cargar.leer(_csv(tmp_path, "code,name,uom,price,qty,group", BIEN[1]))

    assert len(problemas) == 1
    assert "encabezado" in problemas[0]
    assert "--ejemplo" in problemas[0]


def test_el_punto_y_coma_de_excel_en_espanol_se_lee(tmp_path: Path) -> None:
    filas, problemas = cargar.leer(
        _csv(
            tmp_path,
            "codigo;nombre;unidad;precio;stock_inicial;grupo",
            "LEC;Leche entera;Unidad;1200;400;Lacteos",
        )
    )

    assert problemas == []
    assert filas[0].precio == Decimal(1200)


def test_una_unidad_que_no_existe_es_un_error_y_no_una_UOM_nueva() -> None:
    """«kg», «Kg» y «KG» son tres UOM para ERPNext. Crearlas en silencio es
    cómo la mitad del catálogo deja de cotizar tres meses después."""
    fila = cargar.Fila(2, "LEC", "Leche", "kg", Decimal(1200), Decimal(0), "Lacteos")

    problemas = cargar.problemas_contra_erpnext([fila], _erp())

    assert len(problemas) == 1
    assert "no existe en ERPNext" in problemas[0]
    assert "¿Querías «Kg»?" in problemas[0]


def test_cambiarle_la_unidad_a_un_producto_que_ya_existe_se_rechaza() -> None:
    fila = cargar.Fila(2, "LEC", "Leche", "Unidad", Decimal(1200), Decimal(0), "Lacteos")
    actual = _erp(items={"LEC": {"item_code": "LEC", "stock_uom": "Kg", "item_name": "Leche"}})

    problemas = cargar.problemas_contra_erpnext([fila], actual)

    assert len(problemas) == 1
    assert "ya existe en ERPNext con unidad «Kg»" in problemas[0]


# ------------------------------------------------------------- la plantilla


def test_el_ejemplo_se_puede_volver_a_leer(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Lo que escribe --ejemplo tiene que pasar el propio validador... salvo por
    las filas de ejemplo, que el validador manda a borrar."""
    ruta = tmp_path / "plantilla.csv"
    cargar.escribir_ejemplo(ruta)

    filas, problemas = cargar.leer(ruta)

    assert filas == []
    assert len(problemas) == len(cargar.EJEMPLO)
    assert all("Borrala" in p for p in problemas)
    ayuda = capsys.readouterr().out
    assert all(columna in ayuda for columna in cargar.COLUMNAS)


def test_el_ejemplo_no_pisa_un_archivo_que_ya_existe(tmp_path: Path) -> None:
    """Si el dueño ya lo llenó, «--ejemplo» sería borrarle el trabajo."""
    ruta = _csv(tmp_path, *BIEN)

    with pytest.raises(FileExistsError) as excepcion:
        cargar.escribir_ejemplo(ruta)

    assert "ya existe y no lo piso" in str(excepcion.value)
    assert ruta.read_text(encoding="utf-8").splitlines() == BIEN


# --------------------------------------------------- la lista de precios


def test_la_lista_de_precios_sale_del_codigo_del_seed() -> None:
    assert cargar.lista_de_precios() == "Standard Selling"


def test_si_el_seed_cambia_de_lista_este_script_la_sigue(tmp_path: Path) -> None:
    """El punto de leerla del código: que nadie tenga que acordarse de tocar
    dos archivos el día que la lista cambie."""
    falso = tmp_path / "seed_dairy.py"
    falso.write_text(
        'erpnext.create_doc("Item Price", {"item_code": c, "price_list": "Lista Mayorista"})',
        encoding="utf-8",
    )

    assert cargar.lista_de_precios(falso) == "Lista Mayorista"


def test_si_el_seed_dejo_de_poner_precios_se_niega(tmp_path: Path) -> None:
    falso = tmp_path / "seed_dairy.py"
    falso.write_text('erpnext.create_doc("Item", {"item_code": c})', encoding="utf-8")

    with pytest.raises(cargar.erpnext.ERPNextError) as excepcion:
        cargar.lista_de_precios(falso)

    assert "no encontré en qué Price List" in str(excepcion.value)


# ------------------------------------------------------------------ el plan


def _filas() -> list[cargar.Fila]:
    return [
        cargar.Fila(2, "LEC-ENT-1L", "Leche entera sachet 1 L", "Unidad", Decimal(1200), Decimal(400), "Lacteos"),
        cargar.Fila(3, "QUE-CRE", "Queso cremoso", "Kg", Decimal("9800.50"), Decimal(45), "Lacteos"),
    ]


def test_un_erpnext_vacio_es_todo_para_crear() -> None:
    plan = cargar.planificar(_filas(), _erp(grupos=set()))

    assert plan.grupos_nuevos == ["Lacteos"]
    assert [f.codigo for f in plan.items_nuevos] == ["LEC-ENT-1L", "QUE-CRE"]
    assert [f.codigo for f in plan.precios_nuevos] == ["LEC-ENT-1L", "QUE-CRE"]
    assert [f.codigo for f in plan.stock_a_cargar] == ["LEC-ENT-1L", "QUE-CRE"]
    assert plan.hay_algo is True


def test_la_segunda_corrida_con_el_mismo_archivo_no_hace_nada() -> None:
    """LA propiedad que hace que el dueño no le tenga miedo a repetirlo."""
    filas = _filas()
    actual = _erp(
        items={
            f.codigo: {"item_code": f.codigo, "item_name": f.nombre, "stock_uom": f.unidad, "item_group": f.grupo}
            for f in filas
        },
        precios={f.codigo: {"name": f"P-{f.codigo}", "price_list_rate": f.precio} for f in filas},
        stock={f.codigo: f.stock for f in filas},
    )

    plan = cargar.planificar(filas, actual)

    assert plan.hay_algo is False
    assert [f.codigo for f in plan.items_iguales] == ["LEC-ENT-1L", "QUE-CRE"]
    assert [f.codigo for f in plan.precios_iguales] == ["LEC-ENT-1L", "QUE-CRE"]
    assert [motivo for _, motivo in plan.stock_omitido] == [
        "ya hay 400 en el depósito",
        "ya hay 45 en el depósito",
    ]


def test_tres_filas_nuevas_agregan_tres_productos() -> None:
    viejas = _filas()
    nueva = cargar.Fila(4, "YOG-BEB-1L", "Yogur bebible", "Unidad", Decimal(1900), Decimal(120), "Lacteos")
    actual = _erp(
        items={
            f.codigo: {"item_code": f.codigo, "item_name": f.nombre, "stock_uom": f.unidad, "item_group": f.grupo}
            for f in viejas
        },
        precios={f.codigo: {"name": f"P-{f.codigo}", "price_list_rate": f.precio} for f in viejas},
        stock={f.codigo: f.stock for f in viejas},
    )

    plan = cargar.planificar([*viejas, nueva], actual)

    assert [f.codigo for f in plan.items_nuevos] == ["YOG-BEB-1L"]
    assert [f.codigo for f in plan.stock_a_cargar] == ["YOG-BEB-1L"]


def test_un_borrador_pendiente_no_se_vuelve_a_crear() -> None:
    """Un ajuste en BORRADOR todavía no movió el Bin. Sin mirarlo, cada corrida
    agregaría otro borrador para el mismo stock."""
    filas = _filas()
    actual = _erp(
        items={
            f.codigo: {"item_code": f.codigo, "item_name": f.nombre, "stock_uom": f.unidad, "item_group": f.grupo}
            for f in filas
        },
        precios={f.codigo: {"name": f"P-{f.codigo}", "price_list_rate": f.precio} for f in filas},
        ajustes={"LEC-ENT-1L": "MAT-RECO-2026-00001"},
    )

    plan = cargar.planificar(filas, actual)

    assert [f.codigo for f in plan.stock_a_cargar] == ["QUE-CRE"]
    assert plan.stock_omitido[0][1] == "ya está en el ajuste MAT-RECO-2026-00001"


def test_un_precio_distinto_es_una_actualizacion_y_no_un_alta() -> None:
    filas = _filas()
    actual = _erp(
        items={
            f.codigo: {"item_code": f.codigo, "item_name": f.nombre, "stock_uom": f.unidad, "item_group": f.grupo}
            for f in filas
        },
        precios={
            "LEC-ENT-1L": {"name": "P-1", "price_list_rate": Decimal(1100)},
            "QUE-CRE": {"name": "P-2", "price_list_rate": Decimal("9800.50")},
        },
        stock={f.codigo: f.stock for f in filas},
    )

    plan = cargar.planificar(filas, actual)

    assert [(f.codigo, str(vieja), nombre) for f, vieja, nombre in plan.precios_cambiados] == [
        ("LEC-ENT-1L", "1100", "P-1")
    ]
    assert [f.codigo for f in plan.precios_iguales] == ["QUE-CRE"]
    assert plan.precios_nuevos == []


def test_un_nombre_distinto_actualiza_solo_ese_campo() -> None:
    filas = _filas()
    actual = _erp(
        items={
            "LEC-ENT-1L": {"item_code": "LEC-ENT-1L", "item_name": "Leche vieja", "stock_uom": "Unidad", "item_group": "Lacteos"},
        },
        precios={"LEC-ENT-1L": {"name": "P-1", "price_list_rate": Decimal(1200)}},
        stock={"LEC-ENT-1L": Decimal(400)},
    )

    plan = cargar.planificar(filas[:1], actual)

    assert plan.items_cambiados == [(filas[0], {"item_name": "Leche entera sachet 1 L"})]


# -------------------------------------------------------------- el simulacro


def _preparar_main(
    monkeypatch: pytest.MonkeyPatch, sin_red: dict[str, Mock], actual: cargar.EnErpnext
) -> None:
    contexto = sin_red["default_context"]
    contexto.side_effect = None
    contexto.return_value = ("Lácteos Plus SA", "Productos Terminados - LP")
    monkeypatch.setattr(cargar, "relevar", Mock(return_value=actual))
    monkeypatch.setattr(cargar, "moneda_de", Mock(return_value="ARS"))


def test_sin_aplicar_no_se_escribe_nada(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sin_red: dict[str, Mock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """El simulacro es el default, y el default tiene que ser inofensivo."""
    _preparar_main(monkeypatch, sin_red, _erp(grupos=set()))

    assert cargar.main([str(_csv(tmp_path, *BIEN))]) == 0

    sin_red["create_doc"].assert_not_called()
    sin_red["pedido_admin"].assert_not_called()
    salida = capsys.readouterr().out
    assert "SIMULACRO: no se escribió nada" in salida
    assert "+ LEC-ENT-1L" in salida
    assert "Standard Selling" in salida and "ARS" in salida


def test_un_archivo_sin_una_sola_fila_sana_no_llega_a_mirar_erpnext(
    tmp_path: Path, sin_red: dict[str, Mock], capsys: pytest.CaptureFixture[str]
) -> None:
    """Si no quedó ni una fila, no hay nada que preguntarle a ERPNext."""
    ruta = _csv(tmp_path, BIEN[0], "LEC,Leche,Unidad,abc,400,Lacteos")

    assert cargar.main([str(ruta)]) == 1

    sin_red["default_context"].assert_not_called()
    sin_red["create_doc"].assert_not_called()
    salida = capsys.readouterr().out
    assert "NO CARGUÉ NADA. 1 problema(s)" in salida
    assert "  1. " in salida


def test_los_errores_del_archivo_y_los_de_erpnext_salen_JUNTOS(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sin_red: dict[str, Mock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Separados, el dueño arreglaba el precio, volvía a correrlo, y recién ahí
    se enteraba de que la unidad no existía. Una sola lista, en orden de línea."""
    _preparar_main(monkeypatch, sin_red, _erp())
    ruta = _csv(
        tmp_path,
        BIEN[0],
        "LEC,Leche,kg,1200,400,Lacteos",       # la unidad la sabe ERPNext
        "QUE,Queso,Kg,abc,45,Lacteos",         # el precio lo sabe el archivo
    )

    assert cargar.main([str(ruta)]) == 1

    salida = capsys.readouterr().out
    assert "NO CARGUÉ NADA. 2 problema(s)" in salida
    assert salida.index("línea 2") < salida.index("línea 3")
    assert "la unidad «kg» no existe" in salida
    assert "el precio «abc» no es un número" in salida
    sin_red["create_doc"].assert_not_called()


def test_aplicar_sobre_un_erpnext_que_ya_esta_igual_no_escribe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sin_red: dict[str, Mock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    filas = _filas()
    _preparar_main(
        monkeypatch,
        sin_red,
        _erp(
            items={
                f.codigo: {"item_code": f.codigo, "item_name": f.nombre, "stock_uom": f.unidad, "item_group": f.grupo}
                for f in filas
            },
            precios={f.codigo: {"name": f"P-{f.codigo}", "price_list_rate": f.precio} for f in filas},
            stock={f.codigo: f.stock for f in filas},
        ),
    )

    assert cargar.main([str(_csv(tmp_path, *BIEN)), "--aplicar"]) == 0

    sin_red["create_doc"].assert_not_called()
    sin_red["pedido_admin"].assert_not_called()
    assert "No hay nada que escribir" in capsys.readouterr().out


def test_aplicar_escribe_el_ajuste_de_stock_en_borrador(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sin_red: dict[str, Mock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """El stock queda en BORRADOR: confirmarlo es de una persona, como en el seed."""
    _preparar_main(monkeypatch, sin_red, _erp())
    monkeypatch.setattr(
        cargar.cuentas,
        "payload_reconciliacion",
        lambda empresa, items: {"company": empresa, "purpose": "Opening Stock", "items": items},
    )
    crear = sin_red["create_doc"]
    crear.side_effect = None
    crear.return_value = {"name": "MAT-RECO-2026-00007"}

    assert cargar.main([str(_csv(tmp_path, *BIEN)), "--aplicar"]) == 0

    creados = {llamada.args[0] for llamada in crear.call_args_list}
    assert creados == {"Item", "Item Price", "Stock Reconciliation"}
    ajuste = next(c.args[1] for c in crear.call_args_list if c.args[0] == "Stock Reconciliation")
    assert ajuste["items"] == [
        {"item_code": "LEC-ENT-1L", "warehouse": "Productos Terminados - LP", "qty": 400.0, "valuation_rate": 720.0},
        {"item_code": "QUE-CRE", "warehouse": "Productos Terminados - LP", "qty": 45.0, "valuation_rate": 5880.3},
    ]
    assert "BORRADOR" in capsys.readouterr().out


def test_verificar_reporta_la_deriva_en_las_dos_direcciones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sin_red: dict[str, Mock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _preparar_main(
        monkeypatch,
        sin_red,
        _erp(
            items={"LEC-ENT-1L": {"item_code": "LEC-ENT-1L", "item_name": "Leche entera sachet 1 L", "stock_uom": "Unidad", "item_group": "Lacteos"}},
            precios={"LEC-ENT-1L": {"name": "P-1", "price_list_rate": Decimal(999)}},
        ),
    )
    lista = sin_red["get_list"]
    lista.side_effect = None
    lista.return_value = [{"item_code": "LEC-ENT-1L"}, {"item_code": "SOBRANTE"}]

    assert cargar.main([str(_csv(tmp_path, *BIEN)), "--verificar"]) == 1

    salida = capsys.readouterr().out
    assert "En el archivo pero NO en ERPNext (1)" in salida
    assert "- QUE-CRE" in salida
    assert "En ERPNext pero NO en el archivo (1)" in salida
    assert "- SOBRANTE" in salida
    assert "ERPNext dice 999, el archivo dice 1200" in salida
    sin_red["create_doc"].assert_not_called()
