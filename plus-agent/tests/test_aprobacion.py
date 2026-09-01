"""Los botones del dueño, y el silencio que dejaban.

EL BUG PRINCIPAL DE ESTE ARCHIVO
Tocar [Rechazar] agregaba un comentario en ERPNext y listo. El cliente —al
que el bot le había dicho "te confirmo en unos minutos"— no se enteraba
nunca. Esperaba para siempre. El README declara ese silencio como el peor
fallo posible del sistema, y era el que estaba.
"""

from __future__ import annotations

import pytest

from app.aprobacion import manejar_boton

DUENO = "5493511111111"
EXTRANO = "5493519999999"
CLIENTE_TEL = "+54 9 351 888-8888"


@pytest.fixture
def con_pedido(erp):
    erp.docs[("Sales Order", "SO-0001")] = {
        "name": "SO-0001",
        "customer": "CUST-0007",
        "customer_name": "Almacen Don Jose",
        "docstatus": 0,
        "grand_total": 12000.0,
        "delivery_date": "2026-09-05",
        "items": [
            {"item_code": "LEC-ENT-1L", "item_name": "Leche entera", "qty": 10, "amount": 12000}
        ],
    }
    erp.docs[("Customer", "CUST-0007")] = {
        "name": "CUST-0007",
        "customer_name": "Almacen Don Jose",
        "mobile_no": CLIENTE_TEL,
    }
    return erp


# --------------------------------------------------------------------------
# Permisos
# --------------------------------------------------------------------------


def test_solo_el_equipo_puede_aprobar(con_pedido, wa, equipo):
    salida = manejar_boton("ok:SO-0001", EXTRANO)
    assert "permiso" in salida
    assert not con_pedido.enviados_submit


def test_el_equipo_puede_aprobar(con_pedido, wa, equipo):
    salida = manejar_boton("ok:SO-0001", equipo[0])
    assert ("Sales Order", "SO-0001") in con_pedido.enviados_submit
    assert "confirmado" in salida


def test_payload_basura_no_hace_nada(con_pedido, wa, equipo):
    for payload in ("", "sinseparador", "raro:SO-0001", "ok:", ":SO-0001"):
        assert not con_pedido.enviados_submit
        salida = manejar_boton(payload, equipo[0])
        assert "entend" in salida or "desconocida" in salida
    assert not con_pedido.enviados_submit


# --------------------------------------------------------------------------
# Confirmar: el cliente se entera
# --------------------------------------------------------------------------


def test_al_confirmar_se_le_avisa_al_cliente(con_pedido, wa, equipo):
    manejar_boton("ok:SO-0001", equipo[0])
    avisos = wa.textos_a(CLIENTE_TEL)
    assert avisos
    assert "SO-0001" in avisos[0]
    assert "onfirmado" in avisos[0]


def test_si_no_se_le_pudo_avisar_el_dueno_lo_sabe(con_pedido, wa, equipo):
    """Si el aviso no salió, el dueño tiene que enterarse EN EL MOMENTO, no
    descubrirlo cuando el cliente llama enojado."""
    con_pedido.docs[("Customer", "CUST-0007")] = {"name": "CUST-0007", "mobile_no": ""}
    salida = manejar_boton("ok:SO-0001", equipo[0])
    assert "NO pude avisarle" in salida


def test_si_el_submit_falla_lo_dice(con_pedido, wa, equipo):
    con_pedido.fallar_en.add("submit:Sales Order")
    salida = manejar_boton("ok:SO-0001", equipo[0])
    assert "No pude confirmar" in salida
    assert not wa.textos_a(CLIENTE_TEL)


def test_queda_auditado_quien_confirmo(con_pedido, wa, equipo):
    manejar_boton("ok:SO-0001", equipo[0])
    comentarios = [c[2] for c in con_pedido.comentarios]
    assert any(equipo[0] in c for c in comentarios)


# --------------------------------------------------------------------------
# Rechazar: EL SILENCIO QUE SE ARREGLÓ
# --------------------------------------------------------------------------


def test_al_rechazar_TAMBIEN_se_le_avisa_al_cliente(con_pedido, wa, equipo):
    """EL BUG. Antes esto no mandaba nada y el cliente quedaba esperando su
    leche para siempre."""
    manejar_boton("no:SO-0001", equipo[0])
    avisos = wa.textos_a(CLIENTE_TEL)
    assert avisos, "el cliente quedó en silencio después de un rechazo"
    assert "SO-0001" in avisos[0]


def test_el_aviso_de_rechazo_no_dice_confirmado(con_pedido, wa, equipo):
    manejar_boton("no:SO-0001", equipo[0])
    aviso = wa.textos_a(CLIENTE_TEL)[0]
    assert "onfirmado" not in aviso
    assert "equipo" in aviso.lower()


def test_rechazar_no_hace_submit(con_pedido, wa, equipo):
    manejar_boton("no:SO-0001", equipo[0])
    assert not con_pedido.enviados_submit


def test_rechazar_queda_auditado(con_pedido, wa, equipo):
    manejar_boton("no:SO-0001", equipo[0])
    assert any("Rechazado" in c[2] for c in con_pedido.comentarios)


# --------------------------------------------------------------------------
# Ver detalle
# --------------------------------------------------------------------------


def test_ver_detalle_muestra_los_renglones(con_pedido, wa, equipo):
    salida = manejar_boton("ver:SO-0001", equipo[0])
    assert "Leche entera" in salida
    assert "SO-0001" in salida
    assert "12.000" in salida or "12000" in salida


def test_ver_un_pedido_que_no_existe_no_explota(con_pedido, wa, equipo):
    """EL BUG: esta rama no tenía try/except y el fallo salía como 500 del
    webhook."""
    salida = manejar_boton("ver:NO-EXISTE", equipo[0])
    assert "No pude abrir" in salida


def test_confirmar_un_pedido_que_no_existe_no_explota(con_pedido, wa, equipo):
    con_pedido.fallar_en.add("submit:Sales Order")
    salida = manejar_boton("ok:NO-EXISTE", equipo[0])
    assert "No pude confirmar" in salida
