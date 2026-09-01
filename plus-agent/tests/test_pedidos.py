"""El flujo de pedido completo: borrador -> política -> confirmación o humano.

Acá se prueba la propiedad central del diseño: el agente NUNCA confirma. La
política decide, y solo bajo lock.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.tools.pedidos import crear_lead, crear_pedido, escalar_a_humano

CONF = {
    "configurable": {
        "thread_id": "cli:5493519999999",
        "alcance": "cliente",
        "cliente_code": "CUST-DONJOSE",
        "telefono": "5493519999999",
    }
}

LINEAS = [{"item_code": "LEC-ENT-1L", "cantidad": 10}]


@pytest.fixture
def erp_listo(erp):
    """Estado que pasa todas las reglas de la política."""
    erp.listas["Sales Order"] = [{"grand_total": 10000.0} for _ in range(5)]
    erp.listas["Sales Order Item"] = []
    erp.listas["Bin"] = [{"actual_qty": 500.0, "reserved_qty": 0.0}]
    erp.listas["Item Price"] = [{"price_list_rate": 1200.0}]
    erp.reportes["Accounts Receivable"] = []
    return erp


def _preparar_release(erp, total=12000.0):
    """El pedido que crear_doc devuelve se relee con get_doc para evaluarlo.
    El doble lo guarda solo, pero le faltan los campos que la política mira."""
    original = erp.create_doc

    def create_doc(doctype, payload):
        doc = original(doctype, payload)
        if doctype == "Sales Order":
            doc.setdefault("grand_total", total)
            doc.setdefault("selling_price_list", "Standard Selling")
            doc["items"] = [{**i, "rate": 1200.0} for i in payload.get("items", [])]
        return doc

    erp.create_doc = create_doc


# --------------------------------------------------------------------------
# Siempre borrador
# --------------------------------------------------------------------------


def test_el_pedido_se_crea_en_borrador(erp_listo, lock_ocupado, wa):
    crear_pedido.invoke({"lineas": LINEAS}, config=CONF)
    so = erp_listo.ultimo_creado("Sales Order")
    assert so["docstatus"] == 0


def test_pedido_vacio_se_rechaza(erp_listo, wa):
    salida = crear_pedido.invoke({"lineas": []}, config=CONF)
    assert "vacío" in salida
    assert not erp_listo.creados_de("Sales Order")


def test_la_fecha_por_defecto_es_manana(erp_listo, lock_ocupado, wa):
    crear_pedido.invoke({"lineas": LINEAS}, config=CONF)
    so = erp_listo.ultimo_creado("Sales Order")
    assert so["delivery_date"] == (date.today() + timedelta(days=1)).isoformat()


def test_fecha_ilegible_se_rechaza_antes_de_crear(erp_listo, wa):
    salida = crear_pedido.invoke({"lineas": LINEAS, "fecha_entrega": "el jueves"}, config=CONF)
    assert "AAAA-MM-DD" in salida
    assert not erp_listo.creados_de("Sales Order")


def test_queda_auditado_en_erpnext(erp_listo, lock_ocupado, wa):
    crear_pedido.invoke({"lineas": LINEAS}, config=CONF)
    comentarios = [c for c in erp_listo.comentarios if c[0] == "Sales Order"]
    assert comentarios
    assert any("Agente IA" in c[2] for c in comentarios)


# --------------------------------------------------------------------------
# Auto-confirmación
# --------------------------------------------------------------------------


def test_con_la_politica_apagada_nunca_confirma(erp_listo, lock_libre, wa, monkeypatch):
    monkeypatch.setenv("AUTO_CONFIRM_MAX", "0")
    _preparar_release(erp_listo)
    salida = crear_pedido.invoke({"lineas": LINEAS}, config=CONF)
    assert not erp_listo.enviados_submit
    assert "NO digas que está confirmado" in salida


def test_pedido_bueno_se_confirma_al_instante(erp_listo, lock_libre, wa, auto_confirm_on):
    _preparar_release(erp_listo)
    salida = crear_pedido.invoke({"lineas": LINEAS}, config=CONF)
    assert ("Sales Order", "SAL-0001") in erp_listo.enviados_submit
    assert "CONFIRMADO" in salida


def test_sin_lock_no_auto_confirma(erp_listo, lock_ocupado, wa, auto_confirm_on):
    """LA CARRERA: si no se consigue el lock, va a revisión humana. Nunca se
    confirma "por las dudas"."""
    _preparar_release(erp_listo)
    salida = crear_pedido.invoke({"lineas": LINEAS}, config=CONF)
    assert not erp_listo.enviados_submit
    assert "NO digas que está confirmado" in salida
    assert any("lock" in c[2] for c in erp_listo.comentarios)


def test_si_el_submit_falla_no_le_miente_al_cliente(erp_listo, lock_libre, wa, auto_confirm_on):
    """La política aprueba pero ERPNext rechaza el submit. El cliente NO
    puede recibir "confirmado"."""
    _preparar_release(erp_listo)
    erp_listo.fallar_en.add("submit:Sales Order")
    salida = crear_pedido.invoke({"lineas": LINEAS}, config=CONF)
    assert "CONFIRMADO" not in salida
    assert "NO digas que está confirmado" in salida


def test_el_equipo_recibe_botones_cuando_hace_falta_su_ok(erp_listo, lock_ocupado, wa, equipo):
    _preparar_release(erp_listo)
    crear_pedido.invoke({"lineas": LINEAS}, config=CONF)
    assert wa.botones, "el dueño tiene que recibir los botones"
    _, _cuerpo, botones = wa.botones[0]
    ids = {b["id"].split(":")[0] for b in botones}
    assert ids == {"ok", "no", "ver"}


def test_notifica_al_primer_telefono_de_forma_determinista(erp_listo, lock_ocupado, wa, equipo):
    """EL BUG: STAFF era un `set` y se cortaba con break tras el primero, así
    que el "primero" era uno al azar en cada arranque: los avisos le llegaban
    a un empleado distinto cada vez."""
    _preparar_release(erp_listo)
    destinos = []
    for _ in range(5):
        wa.botones.clear()
        erp_listo.creados.clear()
        crear_pedido.invoke({"lineas": LINEAS}, config=CONF)
        destinos.append(wa.botones[0][0])
    assert len(set(destinos)) == 1
    from app import router

    assert destinos[0] == router.STAFF[0]


def test_si_erpnext_rechaza_la_creacion_no_promete_nada(erp_listo, wa):
    erp_listo.fallar_en.add("Sales Order")
    salida = crear_pedido.invoke({"lineas": LINEAS}, config=CONF)
    assert "escalar_a_humano" in salida
    assert "tomado" not in salida


# --------------------------------------------------------------------------
# Lead y escalamiento
# --------------------------------------------------------------------------


def test_crear_lead_usa_el_telefono_del_config(erp, wa):
    """El modelo no elige el teléfono: sale del mensaje."""
    assert "telefono" not in crear_lead.args
    crear_lead.invoke({"nombre": "Kiosco Nuevo"}, config=CONF)
    lead = erp.ultimo_creado("Lead")
    assert lead["mobile_no"] == "5493519999999"
    assert lead["source"] == "WhatsApp"


def test_escalar_avisa_al_equipo_por_whatsapp(erp, wa, equipo):
    """EL BUG: creaba un ToDo en ERPNext y nada más. Nadie ve un ToDo hasta
    que entra al sistema, así que un reclamo esperaba hasta mañana."""
    escalar_a_humano.invoke({"motivo": "pide descuento"}, config=CONF)
    assert erp.creados_de("ToDo")
    avisos = wa.textos_a(equipo[0])
    assert avisos, "el equipo no recibió el escalamiento"
    assert "descuento" in avisos[0]
    assert "5493519999999" in avisos[0]


def test_escalar_avisa_aunque_falle_el_todo(erp, wa, equipo):
    erp.fallar_en.add("ToDo")
    salida = escalar_a_humano.invoke({"motivo": "reclamo"}, config=CONF)
    assert wa.textos_a(equipo[0]), "el aviso no puede depender del ToDo"
    assert "Derivado" in salida
