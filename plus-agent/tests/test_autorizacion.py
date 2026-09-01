"""El límite de autorización: un cliente solo ve y toca lo suyo.

QUÉ PRUEBA ESTO
El agujero original: `crear_pedido(cliente=...)`, `pedido_habitual(cliente=...)`
y `estado_pedido(numero)` recibían el identificador como PARÁMETRO DEL MODELO.
El código de cliente iba en el texto del system prompt, así que lo único que
frenaba a alguien que escribiera "¿qué pide siempre Almacén Don José?" era una
línea del prompt. Un prompt no es un control de acceso: basta con que el
modelo se equivoque una vez.

Estos tests fallan si alguien vuelve a poner el cliente como parámetro.
"""

from __future__ import annotations

from app.tools import alcance
from app.tools.catalogo import estado_pedido, pedido_habitual
from app.tools.pedidos import crear_pedido

# Config como la arma el webhook: el código de cliente sale del teléfono.
CONF_DON_JOSE = {
    "configurable": {
        "thread_id": "cli:5493511111111",
        "alcance": "cliente",
        "cliente_code": "CUST-DONJOSE",
        "telefono": "5493511111111",
    }
}
CONF_DESCONOCIDO = {
    "configurable": {
        "thread_id": "cli:5493519999999",
        "alcance": "cliente",
        "cliente_code": "",
        "telefono": "5493519999999",
    }
}
CONF_GERENCIA = {
    "configurable": {
        "thread_id": "ger:5493511111111",
        "alcance": "gerencia",
        "telefono": "5493511111111",
    }
}


# --------------------------------------------------------------------------
# 1. El modelo no puede ni nombrar a otro cliente: el parámetro no existe.
# --------------------------------------------------------------------------


def test_el_modelo_no_puede_elegir_el_cliente_en_crear_pedido():
    """`cliente` no está en el schema que ve el modelo. Este test es el que
    impide que el agujero vuelva."""
    assert "cliente" not in crear_pedido.args
    assert "config" not in crear_pedido.args  # inyectado, no expuesto
    assert set(crear_pedido.args) == {"lineas", "fecha_entrega"}


def test_el_modelo_no_puede_elegir_el_cliente_en_pedido_habitual():
    assert "cliente" not in pedido_habitual.args
    assert pedido_habitual.args == {}


def test_estado_pedido_solo_expone_el_numero():
    assert set(estado_pedido.args) == {"numero_pedido"}


# --------------------------------------------------------------------------
# 2. Lectura cruzada de pedidos: bloqueada.
# --------------------------------------------------------------------------


def test_cliente_no_puede_ver_el_pedido_de_otro(erp):
    erp.docs[("Sales Order", "SO-0042")] = {
        "name": "SO-0042",
        "customer": "CUST-OTRO",
        "docstatus": 1,
        "grand_total": 50000,
        "delivery_date": "2026-09-02",
    }
    salida = estado_pedido.invoke({"numero_pedido": "SO-0042"}, config=CONF_DON_JOSE)
    assert "No encontré" in salida
    # No se filtra NADA del pedido ajeno: ni el monto, ni el cliente, ni la fecha.
    assert "50" not in salida
    assert "CUST-OTRO" not in salida
    assert "2026-09-02" not in salida


def test_cliente_si_puede_ver_su_propio_pedido(erp):
    erp.docs[("Sales Order", "SO-0001")] = {
        "name": "SO-0001",
        "customer": "CUST-DONJOSE",
        "docstatus": 0,
        "grand_total": 12000,
        "delivery_date": "2026-09-02",
    }
    salida = estado_pedido.invoke({"numero_pedido": "SO-0001"}, config=CONF_DON_JOSE)
    assert "SO-0001" in salida
    assert "borrador" in salida


def test_desconocido_no_ve_ningun_pedido(erp):
    """Alguien sin ficha en ERPNext no puede leer pedidos de nadie, ni
    adivinando el número."""
    erp.docs[("Sales Order", "SO-0001")] = {
        "name": "SO-0001",
        "customer": "CUST-DONJOSE",
        "docstatus": 1,
        "grand_total": 12000,
        "delivery_date": "2026-09-02",
    }
    salida = estado_pedido.invoke({"numero_pedido": "SO-0001"}, config=CONF_DESCONOCIDO)
    assert "No encontré" in salida


def test_gerencia_si_ve_cualquier_pedido(erp):
    erp.docs[("Sales Order", "SO-0042")] = {
        "name": "SO-0042",
        "customer": "CUST-OTRO",
        "docstatus": 1,
        "grand_total": 50000,
        "delivery_date": "2026-09-02",
    }
    salida = estado_pedido.invoke({"numero_pedido": "SO-0042"}, config=CONF_GERENCIA)
    assert "SO-0042" in salida
    assert "confirmado" in salida


# --------------------------------------------------------------------------
# 3. Historial de compras de otro: bloqueado.
# --------------------------------------------------------------------------


def test_pedido_habitual_usa_el_cliente_del_telefono(erp):
    """Sin importar lo que el modelo quiera, consulta al cliente del config."""
    erp.listas["Sales Order"] = [{"name": "SO-0001"}]
    erp.docs[("Sales Order", "SO-0001")] = {
        "name": "SO-0001",
        "customer": "CUST-DONJOSE",
        "transaction_date": "2026-08-01",
        "grand_total": 12000,
        "items": [{"item_code": "LEC-ENT-1L", "qty": 20, "item_name": "Leche entera"}],
    }
    salida = pedido_habitual.invoke({}, config=CONF_DON_JOSE)
    assert "LEC-ENT-1L" in salida

    condiciones_cliente = [
        cond
        for dt, filtros in erp.consultas
        if dt == "Sales Order" and filtros
        for cond in filtros
        if cond[0] == "customer"
    ]
    assert condiciones_cliente, "tiene que filtrar por cliente"
    assert all(cond[2] == "CUST-DONJOSE" for cond in condiciones_cliente)


def test_desconocido_no_obtiene_historial(erp):
    salida = pedido_habitual.invoke({}, config=CONF_DESCONOCIDO)
    assert "no tengo" in salida.lower()
    assert not [dt for dt, _ in erp.consultas if dt == "Sales Order"]


# --------------------------------------------------------------------------
# 4. Crear pedido: siempre a nombre del que escribió.
# --------------------------------------------------------------------------


def test_crear_pedido_usa_el_cliente_del_telefono(erp, lock_ocupado, wa):
    salida = crear_pedido.invoke(
        {"lineas": [{"item_code": "LEC-ENT-1L", "cantidad": 10}]},
        config=CONF_DON_JOSE,
    )
    so = erp.ultimo_creado("Sales Order")
    assert so["customer"] == "CUST-DONJOSE"
    assert so["docstatus"] == 0, "los pedidos se crean SIEMPRE en borrador"
    assert "SO" in salida or "tomado" in salida


def test_desconocido_no_puede_crear_pedido(erp, wa):
    salida = crear_pedido.invoke(
        {"lineas": [{"item_code": "LEC-ENT-1L", "cantidad": 10}]},
        config=CONF_DESCONOCIDO,
    )
    assert not erp.creados_de("Sales Order")
    assert "crear_lead" in salida


def test_sin_config_falla_cerrado(erp, wa):
    """Si por un bug el config no llega, no se crea nada a nombre de nadie."""
    salida = crear_pedido.invoke({"lineas": [{"item_code": "LEC-ENT-1L", "cantidad": 10}]})
    assert not erp.creados_de("Sales Order")
    assert "no tengo la ficha" in salida.lower()


def test_alcance_por_defecto_es_el_restrictivo():
    """Config sin `alcance` = cliente, no gerencia."""
    assert alcance.alcance(None) == alcance.CLIENTE
    assert alcance.alcance({}) == alcance.CLIENTE
    assert alcance.alcance({"configurable": {}}) == alcance.CLIENTE
    assert not alcance.es_gerencia({"configurable": {"alcance": "GERENCIA"}})
    assert alcance.es_gerencia({"configurable": {"alcance": "gerencia"}})


def test_ninguna_herramienta_de_cliente_permite_submit():
    """La propiedad central del diseño: el agente de clientes no tiene una
    ruta a submit. Si alguien agrega una, esto falla."""
    from app.graph import TOOLS_CLIENTES

    for herramienta in TOOLS_CLIENTES:
        nombres = set(herramienta.args)
        assert "docstatus" not in nombres
        assert not any("submit" in n.lower() or "confirm" in n.lower() for n in nombres)
