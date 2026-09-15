"""La superficie de escritura: lo que abre, y sobre todo lo que sigue cerrado.

La línea que se decidió no es leer-contra-escribir sino IRREVERSIBLE × PLATA, y
un archivo de tests es donde esa frase se vuelve comprobable. Lo que se prueba:

  · ninguna de las cinco deja que el MODELO ponga un precio;
  · el cliente HTTP se niega a modificar `Item Price`, `Sales Invoice` y
    `Delivery Note`, así que la promesa no depende de qué herramientas existan;
  · un PUT no puede emitir, porque `docstatus` se borra del payload;
  · `editar_borrador` relee y se niega sobre un pedido ya emitido;
  · las cinco pasan por `require_management`.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from app import erpnext
from app.tools.crm import (
    actualizar_cliente,
    actualizar_producto,
    anotar_en_ficha,
    armar_presupuesto,
    editar_borrador,
)

# ESTE ARCHIVO AFIRMA CASTELLANO, así que lo DECLARA en vez de heredarlo.
#
# Las cinco herramientas dejaron de tener el texto escrito adentro: ahora sale
# de `idioma.t(...)` en el idioma que fijó el dueño. Sin esta marca, las
# aserciones de abajo («confirmado», «cancelado», «la mitad») son las de un
# idioma y el entorno elige el otro — y ahí no falla la herramienta, falla la
# celda de CI que corre con IDIOMA_GERENCIA=en, que es exactamente lo que pasó.
# Ver el marcador `idioma` en pytest.ini y `_idioma_declarado` en conftest.py.
pytestmark = pytest.mark.idioma("es")

GERENTE = "5493511234567"
AJENO = "5493519999999"
FICHA = {"name": "CUST-0009", "customer_name": "Panadería San José"}

TODAS = [
    actualizar_cliente, anotar_en_ficha, armar_presupuesto,
    editar_borrador, actualizar_producto,
]


@pytest.fixture(autouse=True)
def equipo(monkeypatch: pytest.MonkeyPatch):
    from app import router, telefono

    monkeypatch.setattr(router, "STAFF", [telefono.normalizar(GERENTE)])


def _config(quien: str = GERENTE) -> dict:
    return {"configurable": {
        "thread_id": "ger:t", "actor_scope": "management",
        "actor_phone": quien, "inbound_message_id": "w",
    }}


@pytest.fixture
def erp(monkeypatch: pytest.MonkeyPatch):
    """Un ERPNext de mentira que RECUERDA lo que se le mandó."""
    estado = {"update": [], "create": [], "comentarios": [], "doc": {"docstatus": 0}}

    def get_list(doctype, filters=None, fields=None, limit=None, **kw):
        if doctype == "Customer":
            return [dict(FICHA)]
        if doctype == "Item Reorder":
            return [{"name": "IR-1", "warehouse": "Principal", "warehouse_reorder_level": 5}]
        return []

    def update_doc(doctype, name, payload):
        estado["update"].append((doctype, name, dict(payload)))
        return {"name": name}

    def create_doc(doctype, payload):
        estado["create"].append((doctype, dict(payload)))
        return {"name": f"{doctype[:3].upper()}-0001"}

    monkeypatch.setattr(erpnext, "get_list", get_list)
    monkeypatch.setattr(erpnext, "update_doc", update_doc)
    monkeypatch.setattr(erpnext, "create_doc", create_doc)
    monkeypatch.setattr(erpnext, "get_doc", lambda dt, n, **kw: dict(estado["doc"]))
    monkeypatch.setattr(
        erpnext, "registrar_comentario",
        lambda dt, n, t: estado["comentarios"].append((dt, n, t)),
    )
    return estado


# ============================================================= EL PRECIO

def test_ninguna_herramienta_de_gerencia_deja_que_el_modelo_ponga_un_precio():
    """LA PROPIEDAD MÁS IMPORTANTE DE ESTE ARCHIVO, y vale para las 24.

    `erpnext_sales_order_create` del servidor de terceros que se midió en
    `docs/MCP.md` declara `items[].required = ["item_code", "qty", "rate"]`: el
    modelo es la autoridad de precios y `policy._precio_autorizado` —price_list,
    currency y uom— queda fuera del camino. Eso no se arregla con permisos,
    porque es la forma del esquema.

    Acá se afirma la forma. Se recorre el esquema ENTERO, `$defs` incluidos,
    porque las líneas son un modelo anidado y un `rate` viviría ahí adentro.

    MUTACIÓN: agregarle `precio: float` a `LineaSimple`. Cae éste y sólo éste.
    LA EXCEPCIÓN, Y POR QUÉ SIGUE SIENDO UN GUARDIÁN
    ------------------------------------------------
    `cambiar_precio` publica `precio` a propósito: el dueño pidió cambiar
    precios por WhatsApp sin confirmar cada vez, sabiendo que escribir la LISTA
    influye en lo que se auto-confirma. Esa decisión es suya.

    Lo que NO se afloja es la propiedad. La excepción se nombra UNA vez acá, y
    el test afirma además que es la ÚNICA: si mañana otra herramienta gana un
    campo de plata, o si `cambiar_precio` desaparece y alguien deja la excusa
    puesta, esto se pone rojo. Un guardián con una lista de excepciones abierta
    no es un guardián; con una lista cerrada y comprobada, sí.

    MUTACIÓN CORRIDA: agregarle `precio: float` a `LineaSimple`. Cae éste y
    sólo éste — `armar_presupuesto` y `editar_borrador` lo publicarían y no
    están exceptuados.
    """
    from app import graph

    prohibidos = {"rate", "price", "precio", "amount", "importe",
                  "price_list_rate", "discount", "descuento"}
    # Cerrada y comprobada más abajo. Agregar un nombre acá es una decisión del
    # dueño sobre su plata, no una forma de poner un test en verde.
    EXCEPTUADAS = {"cambiar_precio"}

    def campos(nodo) -> set[str]:
        encontrados: set[str] = set()
        if isinstance(nodo, dict):
            for clave, valor in nodo.items():
                if clave == "properties" and isinstance(valor, dict):
                    encontrados |= set(valor)
                encontrados |= campos(valor)
        elif isinstance(nodo, list):
            for x in nodo:
                encontrados |= campos(x)
        return encontrados

    con_plata = set()
    for herramienta in graph.TOOLS_GERENCIA:
        publicados = campos(herramienta.tool_call_schema.model_json_schema())
        colision = {c for c in publicados if c.lower() in prohibidos}
        if colision:
            con_plata.add(herramienta.name)
        if herramienta.name in EXCEPTUADAS:
            continue
        assert not colision, f"{herramienta.name} deja poner {colision}"

    # La otra mitad: la lista de excepciones no puede quedar vieja en ninguna de
    # las dos direcciones. Sin esto, borrar `cambiar_precio` dejaría una excusa
    # abierta para la próxima herramienta que se llame igual.
    assert con_plata == EXCEPTUADAS, (
        f"las herramientas con un campo de plata son {con_plata}, "
        f"y las exceptuadas {EXCEPTUADAS}"
    )


def test_el_presupuesto_no_manda_ningun_precio_a_erpnext(erp):
    """Y la otra mitad: que tampoco lo ponga el código por su cuenta."""
    armar_presupuesto.invoke(
        {"cliente": "San José",
         "lineas": [{"item_code": "LECHE-1L", "cantidad": 10, "unidad": "Unidad"}]},
        config=_config(),
    )
    doctype, payload = erp["create"][0]
    assert doctype == "Quotation"
    for linea in payload["items"]:
        assert "rate" not in linea
        assert "price_list_rate" not in linea
        assert "discount_percentage" not in linea


# ====================================================== LO QUE NO SE TOCA

@pytest.mark.parametrize(
    "doctype", ["Item Price", "Sales Invoice", "Delivery Note", "Stock Reconciliation"]
)
def test_el_cliente_http_se_niega_a_modificar_lo_que_mueve_plata(doctype, monkeypatch):
    """La negativa es del CLIENTE, no de la buena conducta de las herramientas.

    Que ninguna herramienta llame a `Item Price` es una propiedad de la lista de
    herramientas de HOY. Que el cliente se niegue es una propiedad del proceso, y
    sobrevive a la próxima herramienta que alguien escriba.

    Item Price es el que más importa: un precio gobierna en silencio lo que se
    auto-confirma, y escribir uno mal no da error en ninguna parte.

    SE AFIRMA QUE NO SALIÓ LA PETICIÓN, no que hubo una excepción. Escrito como
    un `pytest.raises` pelado, este test pasaba CON la mutación puesta: con el
    doctype adentro de la lista la llamada seguía hasta `_request`, que contra
    `http://erpnext.test` falla igual y levanta el MISMO `ERPNextError`. O sea
    que afirmaba «tiró una excepción» —siempre cierto— en vez de «se negó».
    Lo encontró su propia mutación.

    MUTACIÓN: agregar el doctype a `DOCTYPES_EDITABLES`. Cae su caso y sólo ése.
    """
    salio = Mock(side_effect=AssertionError("no tenía que salir ninguna petición"))
    monkeypatch.setattr(erpnext, "_request", salio)

    with pytest.raises(erpnext.ERPNextError, match="No se puede modificar"):
        erpnext.update_doc(doctype, "X-1", {"algo": 1})
    salio.assert_not_called()


def test_un_PUT_no_puede_emitir_un_documento(monkeypatch):
    """`docstatus` 0 -> 1 ES el submit. Un PUT que lo acepte es un submit.

    Es como se cuela en los clientes que reenvían el cuerpo del llamador tal
    cual. Acá se borra del payload antes de salir.

    MUTACIÓN: sacar el filtro `if k != "docstatus"`. Cae éste y sólo éste.
    """
    visto: dict = {}

    def _request(cliente, metodo, ruta, *, operation, **kw):
        visto.update(kw.get("json") or {})
        return {"data": {"name": "SO-1"}}

    monkeypatch.setattr(erpnext, "_request", _request)
    erpnext.update_doc("Sales Order", "SO-1", {"docstatus": 1, "delivery_date": "2026-01-01"})

    assert "docstatus" not in visto
    assert visto["delivery_date"] == "2026-01-01"


def test_un_PUT_que_solo_traia_docstatus_no_sale_como_un_PUT_vacio(monkeypatch):
    """Sacarle el único campo y mandarlo igual sería un PUT sin contenido."""
    monkeypatch.setattr(erpnext, "_request", Mock(side_effect=AssertionError("no salir")))
    with pytest.raises(erpnext.ERPNextError):
        erpnext.update_doc("Sales Order", "SO-1", {"docstatus": 1})


# ================================================= EL BORRADOR ES UN BORRADOR

@pytest.mark.parametrize("estado,palabra", [(1, "confirmado"), (2, "cancelado")])
def test_no_se_edita_un_pedido_que_ya_no_es_borrador(erp, estado, palabra):
    """Se comprueba sobre el documento y en el momento, no sobre lo que se creía.

    Entre que el dueño dice «el que está esperando» y esto corre, el barrido o
    una persona pueden haberlo emitido.

    MUTACIÓN: `if estado != 0:` -> `if False:`. Caen los dos casos y nada más.
    """
    erp["doc"] = {"docstatus": estado}

    respuesta = editar_borrador.invoke(
        {"pedido": "SO-0001", "fecha_entrega": "2026-02-02"}, config=_config()
    )

    assert palabra in respuesta
    assert erp["update"] == []


def test_un_borrador_si_se_edita_y_sigue_siendo_borrador(erp):
    erp["doc"] = {"docstatus": 0}

    respuesta = editar_borrador.invoke(
        {"pedido": "SO-0001", "fecha_entrega": "2026-02-02"}, config=_config()
    )

    doctype, nombre, payload = erp["update"][0]
    assert (doctype, nombre) == ("Sales Order", "SO-0001")
    assert payload == {"delivery_date": "2026-02-02"}
    assert "BORRADOR" in respuesta


# ============================================================ AUTORIZACIÓN

@pytest.mark.parametrize("herramienta,argumentos", [
    (actualizar_cliente, {"cliente": "San José", "grupo": "Comercial"}),
    (anotar_en_ficha, {"sobre": "cliente", "cual": "CUST-0009", "nota": "x"}),
    (armar_presupuesto, {"cliente": "San José",
                            "lineas": [{"item_code": "A", "cantidad": 1, "unidad": "U"}]}),
    (editar_borrador, {"pedido": "SO-1", "fecha_entrega": "2026-02-02"}),
    (actualizar_producto, {"item_code": "A", "descripcion": "x"}),
])
def test_un_numero_de_afuera_no_escribe_nada(erp, herramienta, argumentos):
    """Cinco herramientas, cinco puertas. Una sola que se abra mal alcanza."""
    respuesta = herramienta.invoke(argumentos, config=_config(AJENO))

    assert "autorizado" in respuesta or "authorized" in respuesta
    assert erp["update"] == [] and erp["create"] == [] and erp["comentarios"] == []


# ============================================================ LO DEMÁS

def test_el_telefono_del_cliente_NO_se_puede_cambiar_desde_el_agente():
    """Y no es un olvido: es la identidad del cliente en este sistema.

    `clientes.buscar_por_telefono` es como el webhook decide de quién es un
    mensaje entrante. Cambiar ese número desde acá deja los mensajes del número
    viejo sin dueño — o sea deja a una persona sin poder escribir—, y eso no es
    reversible en el sentido que importa aunque el campo se pueda reescribir.

    Lo agarró `test_no_management_tool_accepts_a_phone_or_an_identity_argument`,
    que prohíbe `telefono` como argumento de CUALQUIER herramienta de gerencia.
    Esa guarda es intencionalmente burda —no distingue «el teléfono de quien
    llama» de «el teléfono que se escribe»— y vale más burda que afinada: lo que
    protege es que ningún argumento pueda decir quién sos.

    MUTACIÓN: devolverle el parámetro `telefono` a `actualizar_cliente`. Cae
    éste, y también el guardián de identidad, que es exactamente lo correcto.
    """
    publicados = set(actualizar_cliente.tool_call_schema.model_json_schema()["properties"])
    assert "telefono" not in publicados
    assert publicados == {"cliente", "grupo", "condicion_de_pago"}


def test_sin_nada_que_cambiar_no_se_escribe_por_las_dudas(erp):
    respuesta = actualizar_cliente.invoke({"cliente": "San José"}, config=_config())
    assert erp["update"] == []
    assert "no me dijiste" in respuesta.lower()


def test_si_la_nota_quedo_y_la_tarea_no_se_dice_que_paso_la_mitad(erp, monkeypatch):
    """«No pasó nada» y «pasó la mitad» son cosas distintas para el que lee.

    MUTACIÓN: devolver el mismo texto de éxito en el `except` del ToDo. Cae éste
    y sólo éste.
    """
    monkeypatch.setattr(
        erpnext, "create_doc",
        Mock(side_effect=erpnext.ERPNextError("ToDo rechazado")),
    )
    respuesta = anotar_en_ficha.invoke(
        {"sobre": "cliente", "cual": "CUST-0009", "nota": "reclama",
         "recordarle_a": "vendedor@lacteos.test"},
        config=_config(),
    )
    assert erp["comentarios"], "la nota sí tenía que quedar"
    assert "no pude crearle la tarea" in respuesta.lower()


def test_el_punto_de_reposicion_necesita_un_deposito(erp):
    """El mismo producto puede tener uno distinto en cada depósito, así que
    escribirlo sin nombrarlo es escribirlo en el que salga primero."""
    respuesta = actualizar_producto.invoke(
        {"item_code": "LECHE-1L", "punto_de_reposicion": 20}, config=_config()
    )
    assert erp["update"] == []
    assert "depósito" in respuesta.lower()


def test_el_punto_de_reposicion_se_escribe_en_la_fila_de_ese_deposito(erp):
    actualizar_producto.invoke(
        {"item_code": "LECHE-1L", "punto_de_reposicion": 20, "deposito": "Principal"},
        config=_config(),
    )
    doctype, nombre, payload = erp["update"][0]
    assert (doctype, nombre) == ("Item Reorder", "IR-1")
    assert payload == {"warehouse_reorder_level": 20.0}


# ================================ lo que una revisión encontró después

def test_el_telefono_del_cliente_se_niega_aunque_lo_pida_el_cliente_HTTP(monkeypatch):
    """Sacarlo de la firma de la herramienta no alcanza, y ésa es la lección.

    `actualizar_cliente` ya no publica `telefono`, pero `update_doc` reenviaba
    cualquier campo que le dieran sobre un doctype editable: un llamador directo
    —la próxima herramienta que alguien escriba— podía cambiar `mobile_no` igual.
    Una propiedad de la firma de UNA función de hoy contra una propiedad del
    proceso; ésta es la segunda.

    MUTACIÓN: sacar `Customer` de `CAMPOS_PROHIBIDOS`. Cae éste y sólo éste.
    """
    salio = Mock(side_effect=AssertionError("no tenía que salir"))
    monkeypatch.setattr(erpnext, "_request", salio)

    with pytest.raises(erpnext.ERPNextError, match="mobile_no"):
        erpnext.update_doc("Customer", "CUST-0009", {"mobile_no": "549351000"})
    salio.assert_not_called()


def test_los_demas_campos_del_cliente_siguen_pasando(monkeypatch):
    """La negativa es de UN campo, no del doctype: si no, no queda herramienta."""
    visto: dict = {}

    def _request(cliente, metodo, ruta, *, operation, **kw):
        visto.update(kw.get("json") or {})
        return {"data": {"name": "CUST-0009"}}

    monkeypatch.setattr(erpnext, "_request", _request)
    erpnext.update_doc("Customer", "CUST-0009", {"customer_group": "Comercial"})

    assert visto == {"customer_group": "Comercial"}


def test_un_nombre_que_le_queda_a_DOS_clientes_no_escribe_nada(erp, monkeypatch):
    """Escribirle al primero de dos es cambiarle los datos al equivocado.

    Y el dueño no tiene cómo enterarse: la respuesta le repite el nombre que él
    escribió. Leer de más es un informe incompleto; escribir de más es un daño
    silencioso, así que el `like` del camino de escritura pide DOS y con dos se
    planta.

    MUTACIÓN: volver a `limit=1` en la consulta aproximada. Cae éste y sólo éste.
    """
    def get_list(doctype, filters=None, fields=None, limit=None, **kw):
        if doctype != "Customer":
            return []
        for campo, operador, _ in filters or []:
            if campo == "name" and operador == "=":
                return []          # no es un código exacto
        return [
            {"name": "CUST-0009", "customer_name": "Panadería San José"},
            {"name": "CUST-0042", "customer_name": "Almacén San José"},
        ][: (limit or 2)]

    monkeypatch.setattr(erpnext, "get_list", get_list)

    respuesta = actualizar_cliente.invoke(
        {"cliente": "San José", "grupo": "Comercial"}, config=_config()
    )

    assert erp["update"] == []
    assert "más de un cliente" in respuesta
    assert "CUST-0009" in respuesta and "CUST-0042" in respuesta


def test_un_punto_de_reposicion_negativo_se_rechaza(erp):
    """Un negativo apaga el informe de stock bajo sin que nadie lo note: nada
    queda nunca por debajo de -5, así que el aviso deja de existir en silencio.

    MUTACIÓN: sacar `ge=0` del Field. Cae éste y sólo éste.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        actualizar_producto.invoke(
            {"item_code": "LECHE-1L", "punto_de_reposicion": -5, "deposito": "Principal"},
            config=_config(),
        )
    assert erp["update"] == []


def test_si_la_descripcion_quedo_y_el_resto_falla_se_dice_que_paso_la_mitad(
    erp, monkeypatch
):
    """Dos escrituras que no son atómicas: contestar sólo el error esconde una.

    El dueño leería «no pude cambiar el punto de reposición» y creería que no
    pasó nada, cuando la descripción YA está guardada. Mismo criterio que
    `anotar_en_ficha` con su nota y su tarea.

    MUTACIÓN: devolver `problema` pelado en `_con_lo_hecho`. Cae éste y sólo éste.
    """
    def get_list(doctype, filters=None, fields=None, limit=None, **kw):
        return [] if doctype == "Item Reorder" else [dict(FICHA)]

    monkeypatch.setattr(erpnext, "get_list", get_list)

    respuesta = actualizar_producto.invoke(
        {"item_code": "LECHE-1L", "descripcion": "Leche entera",
         "punto_de_reposicion": 20, "deposito": "Principal"},
        config=_config(),
    )

    assert ("Item", "LECHE-1L", {"description": "Leche entera"}) in erp["update"]
    assert "Quedó cambiado" in respuesta
    assert "descripción" in respuesta
    assert "no tiene una regla de reposición" in respuesta
