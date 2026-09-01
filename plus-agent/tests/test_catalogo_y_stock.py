"""Stock: el interruptor STOCK_CONFIABLE y los niveles.

LA REGLA DEL README
"Un bot que promete leche que ya está en la heladera de otro es peor que no
tener bot." Con STOCK_CONFIABLE=false —el estado de lanzamiento— el bot no
puede afirmar disponibilidad ni por accidente.
"""

from __future__ import annotations

import pytest

from app import formato
from app.tools.catalogo import buscar_producto, consultar_stock

CONF = {"configurable": {"alcance": "cliente", "cliente_code": "CUST-1"}}


@pytest.fixture
def stock_off(monkeypatch):
    monkeypatch.setenv("STOCK_CONFIABLE", "false")


@pytest.fixture
def stock_on(monkeypatch):
    monkeypatch.setenv("STOCK_CONFIABLE", "true")
    monkeypatch.setenv("STOCK_BUFFER_PCT", "20")
    monkeypatch.setenv("STOCK_POCO", "20")


# --------------------------------------------------------------------------
# Fase 1: STOCK_CONFIABLE=false
# --------------------------------------------------------------------------


def test_con_stock_no_confiable_nunca_afirma_disponibilidad(erp, stock_off):
    erp.listas["Bin"] = [{"actual_qty": 9999.0, "reserved_qty": 0.0}]
    salida = consultar_stock.invoke({"item_code": "LEC-ENT-1L"}, config=CONF)
    assert "DISPONIBLE" not in salida
    assert "no confirmes disponibilidad" in salida


def test_con_stock_no_confiable_no_gasta_llamadas(erp, stock_off):
    """No tiene sentido consultar el Bin si la respuesta no depende de él."""
    consultar_stock.invoke({"item_code": "LEC-ENT-1L"}, config=CONF)
    assert not [dt for dt, _ in erp.consultas if dt == "Bin"]


def test_el_interruptor_se_lee_en_caliente(erp, monkeypatch):
    """Girar el interruptor no tiene que requerir rebuild de la imagen."""
    erp.listas["Bin"] = [{"actual_qty": 500.0, "reserved_qty": 0.0}]
    monkeypatch.setenv("STOCK_CONFIABLE", "false")
    assert "DISPONIBLE" not in consultar_stock.invoke({"item_code": "X"}, config=CONF)
    monkeypatch.setenv("STOCK_CONFIABLE", "true")
    assert "DISPONIBLE" in consultar_stock.invoke({"item_code": "X"}, config=CONF)


# --------------------------------------------------------------------------
# Fase 2: niveles, nunca números exactos
# --------------------------------------------------------------------------


def test_stock_alto_es_disponible(erp, stock_on):
    erp.listas["Bin"] = [{"actual_qty": 500.0, "reserved_qty": 0.0}]
    assert "DISPONIBLE" in consultar_stock.invoke({"item_code": "X"}, config=CONF)


def test_stock_bajo_es_poco_stock(erp, stock_on):
    erp.listas["Bin"] = [{"actual_qty": 20.0, "reserved_qty": 0.0}]
    assert "POCO STOCK" in consultar_stock.invoke({"item_code": "X"}, config=CONF)


def test_sin_stock_lo_dice(erp, stock_on):
    erp.listas["Bin"] = [{"actual_qty": 0.0, "reserved_qty": 0.0}]
    assert "SIN STOCK" in consultar_stock.invoke({"item_code": "X"}, config=CONF)


def test_nunca_devuelve_la_cantidad_exacta(erp, stock_on):
    """El número exacto está desactualizado por las ventas offline. Si se
    filtrara, el bot le prometería una cantidad al cliente."""
    erp.listas["Bin"] = [{"actual_qty": 437.0, "reserved_qty": 12.0}]
    salida = consultar_stock.invoke({"item_code": "X"}, config=CONF)
    assert "437" not in salida
    assert "425" not in salida


def test_lo_reservado_baja_el_nivel(erp, stock_on):
    erp.listas["Bin"] = [{"actual_qty": 100.0, "reserved_qty": 95.0}]
    assert "POCO STOCK" in consultar_stock.invoke({"item_code": "X"}, config=CONF)


def test_sin_registro_de_bin_pide_confirmar(erp, stock_on):
    erp.listas["Bin"] = []
    assert "Confirmá con el equipo" in consultar_stock.invoke({"item_code": "X"}, config=CONF)


def test_si_erpnext_falla_no_inventa(erp, stock_on):
    erp.fallar_en.add("Bin")
    salida = consultar_stock.invoke({"item_code": "X"}, config=CONF)
    assert "DISPONIBLE" not in salida
    assert "verificás" in salida


# --------------------------------------------------------------------------
# Búsqueda de productos
# --------------------------------------------------------------------------


def test_buscar_producto_devuelve_codigo_y_precio(erp):
    erp.listas["Item"] = [
        {
            "item_code": "QUE-CRE",
            "item_name": "Queso cremoso",
            "stock_uom": "Kg",
            "description": "x",
        }
    ]
    erp.listas["Item Price"] = [{"price_list_rate": 9800, "currency": "ARS"}]
    salida = buscar_producto.invoke({"consulta": "queso"})
    assert "QUE-CRE" in salida
    assert "9800" in salida


def test_producto_sin_precio_no_inventa_uno(erp):
    erp.listas["Item"] = [
        {"item_code": "QUE-CRE", "item_name": "Queso cremoso", "stock_uom": "Kg", "description": ""}
    ]
    erp.listas["Item Price"] = []
    salida = buscar_producto.invoke({"consulta": "queso"})
    assert "precio a confirmar" in salida


def test_busqueda_sin_resultados(erp):
    erp.listas["Item"] = []
    assert "No se encontraron" in buscar_producto.invoke({"consulta": "caviar"})


def test_apostrofo_en_la_busqueda_no_rompe(erp):
    """El bug de serialización, desde el lado del usuario: un cliente escribe
    un apóstrofo y antes recibía "tuve un problema técnico"."""
    erp.listas["Item"] = []
    salida = buscar_producto.invoke({"consulta": "queso D'Angelo"})
    assert "No se encontraron" in salida


# --------------------------------------------------------------------------
# Formato de plata (se lee en la pantalla de bloqueo del dueño)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "monto,esperado",
    [
        (0, "$0"),
        (1200, "$1.200"),
        (12000, "$12.000"),
        (1234567, "$1.234.567"),
        (-5000, "-$5.000"),
        (None, "$0"),
    ],
)
def test_los_pesos_se_escriben_como_en_argentina(monto, esperado):
    """`f"${x:,.0f}"` daba $12,000, que un argentino lee como doce pesos con
    decimales. El dueño decide confirmar mirando ese número."""
    assert formato.pesos(monto) == esperado


def test_pesos_con_decimales():
    assert formato.pesos(1500.5, decimales=2) == "$1.500,50"


def test_pesos_con_basura_no_explota():
    assert formato.pesos("no es un número") == "$0"


@pytest.mark.parametrize("valor,esperado", [(10.0, "10"), (2.5, "2,5"), (0, "0")])
def test_cantidades_sin_ceros_de_mas(valor, esperado):
    assert formato.cantidad(valor) == esperado
