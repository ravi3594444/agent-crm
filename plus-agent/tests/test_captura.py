"""Captura de ventas offline: los payloads que ERPNext tiene que aceptar.

POR QUÉ ESTE ARCHIVO SE VE ABURRIDO Y NO LO ES
Estos documentos se guardan bien como borrador y fallan al CONFIRMAR — o
sea, cuando el dueño toca Submit, confiado, y ERPNext le tira un error
incomprensible. Ese es el peor momento posible para descubrir un payload
mal armado.
"""

from __future__ import annotations

import pytest

from app.tools.captura import (
    confirmar_entrega,
    contar_stock,
    redactar_mensaje_cliente,
    registrar_venta_offline,
)

VENTA = {
    "cliente": "CUST-DONJOSE",
    "lineas": [{"item_code": "LEC-ENT-1L", "cantidad": 20}],
}


# --------------------------------------------------------------------------
# Factura de venta offline
# --------------------------------------------------------------------------


def test_la_venta_se_carga_en_borrador(erp):
    registrar_venta_offline.invoke(VENTA)
    factura = erp.ultimo_creado("Sales Invoice")
    assert factura["docstatus"] == 0
    assert factura["update_stock"] == 1


def test_sin_pos_profile_no_manda_is_pos(erp, monkeypatch):
    """EL BUG: `is_pos: 1` sin POS Profile ni tabla de pagos. El borrador se
    guarda, pero el submit del dueño falla pidiendo un perfil de POS."""
    monkeypatch.setattr("app.tools.captura.POS_PROFILE", "")
    registrar_venta_offline.invoke({**VENTA, "cobrado": True})
    factura = erp.ultimo_creado("Sales Invoice")
    assert "is_pos" not in factura
    assert "efectivo" in factura["remarks"].lower()


def test_con_pos_profile_si_lo_manda(erp, monkeypatch):
    monkeypatch.setattr("app.tools.captura.POS_PROFILE", "Mostrador")
    registrar_venta_offline.invoke({**VENTA, "cobrado": True})
    factura = erp.ultimo_creado("Sales Invoice")
    assert factura["is_pos"] == 1
    assert factura["pos_profile"] == "Mostrador"


def test_venta_no_cobrada_no_es_pos(erp, monkeypatch):
    monkeypatch.setattr("app.tools.captura.POS_PROFILE", "Mostrador")
    registrar_venta_offline.invoke({**VENTA, "cobrado": False})
    factura = erp.ultimo_creado("Sales Invoice")
    assert "is_pos" not in factura
    assert "no cobrada" in factura["remarks"].lower()


def test_la_venta_lleva_deposito(erp):
    """update_stock=1 sin depósito falla o usa uno inesperado."""
    registrar_venta_offline.invoke(VENTA)
    factura = erp.ultimo_creado("Sales Invoice")
    assert factura["set_warehouse"]
    assert all(i.get("warehouse") for i in factura["items"])


def test_precio_negociado_se_respeta(erp):
    registrar_venta_offline.invoke(
        {
            "cliente": "CUST-1",
            "lineas": [{"item_code": "QUE-CRE", "cantidad": 2, "precio_unitario": 9000}],
        }
    )
    factura = erp.ultimo_creado("Sales Invoice")
    assert factura["items"][0]["rate"] == 9000


def test_venta_sin_lineas_se_rechaza(erp):
    salida = registrar_venta_offline.invoke({"cliente": "CUST-1", "lineas": []})
    assert "qué productos" in salida
    assert not erp.creados_de("Sales Invoice")


def test_si_erpnext_rechaza_lo_dice_con_el_motivo(erp):
    """El del mostrador tiene que saber QUÉ arreglar, no un "no se pudo"."""
    erp.fallar_en.add("Sales Invoice")
    salida = registrar_venta_offline.invoke(VENTA)
    assert "No pude cargarlo" in salida
    assert "CUST-DONJOSE" in salida


# --------------------------------------------------------------------------
# Conteo físico
# --------------------------------------------------------------------------


def test_el_conteo_manda_valuation_rate(erp):
    """EL BUG: sin valuation_rate, ERPNext rechaza la Stock Reconciliation de
    un producto sin stock previo. deploy/seed_dairy.py sí lo mandaba: este
    archivo era el inconsistente."""
    erp.listas["Bin"] = [{"actual_qty": 20.0, "valuation_rate": 850.0}]
    contar_stock.invoke({"item_code": "QUE-CRE", "cantidad_real": 12})
    recon = erp.ultimo_creado("Stock Reconciliation")
    assert recon["items"][0]["valuation_rate"] == 850.0


def test_si_el_bin_no_tiene_valuacion_la_busca_en_el_item(erp):
    erp.listas["Bin"] = [{"actual_qty": 0.0, "valuation_rate": 0.0}]
    erp.listas["Item"] = [{"valuation_rate": 0, "last_purchase_rate": 700.0}]
    contar_stock.invoke({"item_code": "QUE-NUEVO", "cantidad_real": 5})
    recon = erp.ultimo_creado("Stock Reconciliation")
    assert recon["items"][0]["valuation_rate"] == 700.0


def test_sin_valuacion_avisa_en_vez_de_fallar_callado(erp):
    erp.listas["Bin"] = []
    erp.listas["Item"] = []
    salida = contar_stock.invoke({"item_code": "QUE-RARO", "cantidad_real": 5})
    assert "valuación" in salida
    recon = erp.ultimo_creado("Stock Reconciliation")
    assert "valuation_rate" not in recon["items"][0]


def test_el_conteo_dice_la_diferencia(erp):
    erp.listas["Bin"] = [{"actual_qty": 20.0, "valuation_rate": 850.0}]
    salida = contar_stock.invoke({"item_code": "QUE-CRE", "cantidad_real": 12})
    assert "20" in salida and "12" in salida
    assert "faltan" in salida


def test_el_conteo_detecta_sobrante(erp):
    erp.listas["Bin"] = [{"actual_qty": 10.0, "valuation_rate": 850.0}]
    salida = contar_stock.invoke({"item_code": "QUE-CRE", "cantidad_real": 15})
    assert "sobran" in salida


def test_el_conteo_queda_en_borrador(erp):
    erp.listas["Bin"] = [{"actual_qty": 20.0, "valuation_rate": 850.0}]
    contar_stock.invoke({"item_code": "QUE-CRE", "cantidad_real": 12})
    assert erp.ultimo_creado("Stock Reconciliation")["docstatus"] == 0


def test_cantidad_negativa_se_rechaza(erp):
    salida = contar_stock.invoke({"item_code": "QUE-CRE", "cantidad_real": -5})
    assert "negativa" in salida
    assert not erp.creados_de("Stock Reconciliation")


# --------------------------------------------------------------------------
# Remito de entrega
# --------------------------------------------------------------------------


@pytest.fixture
def pedido_confirmado(erp):
    erp.docs[("Sales Order", "SO-0042")] = {
        "name": "SO-0042",
        "customer": "CUST-DONJOSE",
        "docstatus": 1,
        "items": [
            {
                "name": "fila1",
                "item_code": "LEC-ENT-1L",
                "qty": 20,
                "delivered_qty": 0,
                "warehouse": "Principal - LT",
            },
        ],
    }
    return erp


def test_el_remito_referencia_el_pedido(pedido_confirmado):
    confirmar_entrega.invoke({"numero_pedido": "SO-0042"})
    remito = pedido_confirmado.ultimo_creado("Delivery Note")
    fila = remito["items"][0]
    assert fila["against_sales_order"] == "SO-0042"
    assert fila["so_detail"] == "fila1"
    assert fila["warehouse"]


def test_no_se_puede_entregar_un_borrador(pedido_confirmado):
    pedido_confirmado.docs[("Sales Order", "SO-0042")]["docstatus"] = 0
    salida = confirmar_entrega.invoke({"numero_pedido": "SO-0042"})
    assert "borrador" in salida
    assert not pedido_confirmado.creados_de("Delivery Note")


def test_solo_entrega_lo_que_falta(pedido_confirmado):
    """Si ya hubo un remito parcial, el nuevo lleva el resto. Antes mandaba
    la cantidad completa y ERPNext rechazaba la sobre-entrega."""
    pedido_confirmado.docs[("Sales Order", "SO-0042")]["items"][0]["delivered_qty"] = 15
    confirmar_entrega.invoke({"numero_pedido": "SO-0042"})
    remito = pedido_confirmado.ultimo_creado("Delivery Note")
    assert remito["items"][0]["qty"] == 5


def test_pedido_ya_entregado_no_crea_remito_vacio(pedido_confirmado):
    pedido_confirmado.docs[("Sales Order", "SO-0042")]["items"][0]["delivered_qty"] = 20
    salida = confirmar_entrega.invoke({"numero_pedido": "SO-0042"})
    assert "ya está entregado" in salida
    assert not pedido_confirmado.creados_de("Delivery Note")


def test_pedido_inexistente(erp):
    assert "No encontré" in confirmar_entrega.invoke({"numero_pedido": "SO-9999"})


# --------------------------------------------------------------------------
# Redactar mensaje: NO lo envía
# --------------------------------------------------------------------------


def test_redactar_no_envia_nada(erp, wa):
    erp.listas["Customer"] = [{"customer_name": "Almacen Don Jose", "mobile_no": "+5493511111111"}]
    salida = redactar_mensaje_cliente.invoke({"cliente": "Don Jose", "intencion": "llegó el queso"})
    assert not wa.mensajes, "esta herramienta NO puede mandar mensajes"
    assert "Don Jose" in salida


def test_redactar_cliente_inexistente(erp, wa):
    erp.listas["Customer"] = []
    assert "No encontré" in redactar_mensaje_cliente.invoke(
        {"cliente": "Fantasma", "intencion": "x"}
    )
