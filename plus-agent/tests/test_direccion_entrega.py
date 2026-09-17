"""A dónde va el pedido: leerlo, cambiarlo, y no poder cambiar el de otro.

POR QUÉ ESTE ARCHIVO EXISTE
`TOOLS_CLIENTES` no tenía con qué leer ni cambiar una dirección de entrega, así
que el agente tomaba pedidos con la que estaba en la ficha sin decírselo a
nadie: un pedido real informó «Entrega: purnia, purnia» sin que nunca se
hubiera preguntado a dónde iba. La pregunta que faltaba —«¿va a la misma de
siempre o a otra?»— no es que no se hacía: no se PODÍA hacer.

LO QUE MÁS SE PRUEBA ACÁ, Y POR QUÉ
Una herramienta de cliente que ESCRIBE es la superficie más sensible del
sistema, y el modo de falla no es que no funcione: es que funcione para la
cuenta equivocada. Así que la mitad de este archivo es una sola afirmación
mirada desde varios lados — **la identidad no la puede poner el que llama**:
  * no hay parámetro para un teléfono, un código de cliente ni un nombre de
    Address, y eso se deriva del ESQUEMA de verdad (el que ve el modelo), no de
    una lista escrita a mano acá;
  * con el `customer_code` AUSENTE del config, la cuenta se resuelve por el
    teléfono del webhook — que es la prueba de que sale de ahí y no de un dato
    que le pasaron;
  * y con otro cliente cargado en el mismo ERPNext, su dirección queda intacta.

EL DOBLE FILTRA DE VERDAD (`fakes.listar`), y no es cosmético: `direcciones_de`
busca los `Dynamic Link` de UN cliente, así que un doble que devolviera todas
las filas no podría estar en desacuerdo con el código sobre de quién es cada
dirección — que es exactamente lo único que este archivo viene a comprobar.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes import LeaseDoble, listar

from app import clientes, erpnext, idioma
from app.tools import pedidos

TELEFONO = "5493511234567"
TELEFONO_DE_OTRO = "5493519999999"

CORDOBA = {
    "calle": "Av. Colón 1234",
    "localidad": "Córdoba",
    "codigo_postal": "5000",
    "referencia": "",
}
OTRA_DE_CORDOBA = {
    "calle": "Belgrano 456",
    "localidad": "Córdoba",
    "codigo_postal": "5000",
    "referencia": "piso 2",
}
LEJOS = {
    "calle": "Station Road 12",
    "localidad": "Purnia",
    "codigo_postal": "854301",
    "referencia": "",
}


def _config(
    telefono: str = TELEFONO, scope: str = "customer", customer: str = ""
) -> dict:
    """El config lo llena app/graph.py con lo que resolvió el webhook.

    `customer` va VACÍO por defecto a propósito: es el caso que prueba que la
    cuenta se resuelve por el teléfono. Un test que le pusiera la cuenta ya
    resuelta no podría distinguir «la resolvió» de «se la dieron».
    """
    return {
        "configurable": {
            "thread_id": "cli:thread",
            "actor_scope": scope,
            "customer_code": customer,
            "actor_phone": telefono,
            "inbound_message_id": "wamid.dir-001",
        }
    }


class ErpDePrueba:
    """ERPNext con memoria: Customers, Addresses y sus Dynamic Link.

    Las listas pasan por `fakes.listar`, así que HONRA los filtros que le
    manda el código: el `link_name` con el que `direcciones_de` pide las
    direcciones de un cliente decide de verdad qué vuelve.
    """

    def __init__(self) -> None:
        self.customers: list[dict] = []
        self.addresses: dict[str, dict] = {}
        self.enlaces: list[dict] = []
        self.creados: list[tuple[str, dict]] = []
        self.no_puedo_leer: set[str] = set()

    # ---------------------------------------------------------------- altas
    def cargar_cliente(self, nombre: str, telefono: str) -> str:
        self.customers.append(
            {"name": nombre, "customer_name": nombre, "mobile_no": telefono}
        )
        return nombre

    def cargar_direccion(self, cliente: str, nombre: str, **campos) -> str:
        self.addresses[nombre] = {"name": nombre, **campos}
        self.enlaces.append(
            {
                "parent": nombre,
                "parenttype": "Address",
                "link_doctype": "Customer",
                "link_name": cliente,
            }
        )
        return nombre

    # -------------------------------------------------------------- lecturas
    def get_list(
        self, doctype, filters=None, fields=None, limit=20, parent=None,
        order_by=None, start=0,
    ):
        if doctype == "Customer":
            return listar(self.customers, filters, limit=limit, order_by=order_by, start=start)
        if doctype == "Dynamic Link":
            return listar(self.enlaces, filters, limit=limit, order_by=order_by, start=start)
        if doctype == "Sales Order":
            return []
        raise AssertionError(f"get_list inesperado: {doctype}")

    def get_doc(self, doctype, name):
        if doctype == "Address" and name in self.no_puedo_leer:
            raise erpnext.ERPNextError("403")
        if doctype == "Address" and name in self.addresses:
            return dict(self.addresses[name])
        raise erpnext.ERPNextError(f"404 {doctype} {name}")

    def create_doc(self, doctype, payload):
        self.creados.append((doctype, dict(payload)))
        if doctype == "Address":
            nombre = f"{payload['address_title']}-Shipping-{len(self.addresses) + 1}"
            self.cargar_direccion(
                payload["links"][0]["link_name"],
                nombre,
                **{k: v for k, v in payload.items() if k != "links"},
            )
            return {"name": nombre}
        raise AssertionError(f"create_doc inesperado: {doctype}")

    # --------------------------------------------------------------- ayudas
    def direcciones_de(self, cliente: str) -> list[str]:
        return sorted(
            fila["parent"] for fila in self.enlaces if fila["link_name"] == cliente
        )


@pytest.fixture
def erp(monkeypatch: pytest.MonkeyPatch) -> ErpDePrueba:
    falso = ErpDePrueba()
    monkeypatch.setattr(erpnext, "get_list", falso.get_list)
    monkeypatch.setattr(erpnext, "get_doc", falso.get_doc)
    monkeypatch.setattr(erpnext, "create_doc", falso.create_doc)
    monkeypatch.setattr(erpnext, "add_comment", Mock())
    # Nada de lo que hay acá puede emitir. Si alguna vez lo intenta, el test
    # que lo intente se muere en el acto y no en una revisión.
    monkeypatch.setattr(
        erpnext, "submit_doc", Mock(side_effect=AssertionError("nunca se emite"))
    )
    monkeypatch.setenv("ZONAS_ENTREGA_CP", "5000")
    monkeypatch.setenv("ZONAS_ENTREGA_LOCALIDADES", "Córdoba")
    falso.cargar_cliente("CUST-001", TELEFONO)
    return falso


@pytest.fixture
def locks_tomados(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    tomados: list[str] = []

    @contextmanager
    def lock(nombre, **kwargs):
        tomados.append(nombre)
        yield LeaseDoble()

    monkeypatch.setattr(clientes, "distributed_lock", lock)
    return tomados


@pytest.fixture
def redis_falso(monkeypatch: pytest.MonkeyPatch):
    """El Redis de la dirección del turno y del idioma del cliente."""
    from conftest import FakeRedis

    falso = FakeRedis()
    monkeypatch.setattr(clientes.locks, "conexion", lambda: falso)
    return falso


def _leer(config: dict | None = None) -> str:
    return pedidos.direccion_de_entrega.invoke({}, config=config or _config())


def _cambiar(direccion: dict, config: dict | None = None) -> str:
    return pedidos.cambiar_direccion_de_entrega.invoke(
        {"direccion": direccion}, config=config or _config()
    )


# `config=None` no se prueba: con eso langchain toma el config AMBIENTE de un
# contextvar, que en la misma corrida se lo deja puesto la llamada anterior. El
# caso que se ve en producción es un `configurable` sin datos, y ése sí está.


# ==========================================================================
# La identidad no la pone el que llama
# ==========================================================================
def test_ninguna_de_las_dos_acepta_una_identidad_como_argumento() -> None:
    """El parámetro no existe, así que no hay prompt que lo pueda mandar.

    Se recorre el esquema COMPLETO —incluido el modelo anidado de la
    dirección— porque es ahí donde un campo de más pasaría desapercibido, y
    los nombres salen del esquema de verdad: una lista escrita acá se queda
    vieja el día que alguien agregue un campo.
    """
    def campos(esquema: dict) -> set[str]:
        nombres = set(esquema.get("properties") or {})
        for definicion in (esquema.get("$defs") or {}).values():
            nombres |= set(definicion.get("properties") or {})
        return nombres

    lectura = pedidos.direccion_de_entrega.args_schema.model_json_schema()
    escritura = pedidos.cambiar_direccion_de_entrega.args_schema.model_json_schema()

    # La de lectura no recibe NADA: no hay nada que elegir.
    assert campos(lectura) == set()
    # La de escritura recibe UN parámetro y son los datos que dijo el cliente.
    # Se afirma exacto a propósito: un campo nuevo tiene que romper este test
    # y que alguien lo mire, porque el único campo peligroso es uno que diga
    # de QUIÉN es la dirección o CUÁL de ellas.
    assert set(escritura["properties"]) == {"direccion"}
    assert campos(escritura) == {
        "direccion", "calle", "localidad", "codigo_postal", "referencia"
    }
    # Y ninguno nombra a una persona ni a un documento: `direccion` es el
    # CONTENIDO —calle, localidad, CP— y no el nombre de una Address guardada,
    # que es lo que haría falta para apuntar a la de otro.
    prohibido = ("tel", "phone", "cliente", "customer", "cuenta", "account", "address")
    for campo in campos(lectura) | campos(escritura):
        assert not any(palabra in campo.lower() for palabra in prohibido), campo
        assert not campo.lower().endswith(("_nombre", "_name", "_id")), campo


def test_la_cuenta_se_resuelve_con_el_telefono_del_webhook(
    erp, locks_tomados, redis_falso
) -> None:
    """Sin `customer_code` en el config, la cuenta sale del teléfono.

    Es la afirmación central del archivo: el config va SIN la cuenta resuelta,
    así que la única forma de escribir en CUST-001 es haberla buscado por el
    número que firmó el webhook.
    """
    erp.cargar_cliente("CUST-OTRO", TELEFONO_DE_OTRO)

    respuesta = _cambiar(CORDOBA)

    assert "Av. Colón 1234" in respuesta
    assert erp.direcciones_de("CUST-001") == ["CUST-001-Shipping-1"]
    assert erp.direcciones_de("CUST-OTRO") == []


def test_la_direccion_de_otro_cliente_no_se_toca(erp, locks_tomados, redis_falso) -> None:
    """Un ERPNext con dos clientes: el que escribe cambia la suya y nada más."""
    erp.cargar_cliente("CUST-OTRO", TELEFONO_DE_OTRO)
    erp.cargar_direccion(
        "CUST-OTRO", "CUST-OTRO-Shipping-1", address_line1="Sarmiento 1", city="Córdoba"
    )

    _cambiar(OTRA_DE_CORDOBA)

    assert erp.direcciones_de("CUST-OTRO") == ["CUST-OTRO-Shipping-1"]
    assert erp.addresses["CUST-OTRO-Shipping-1"]["address_line1"] == "Sarmiento 1"
    creadas = [payload for doctype, payload in erp.creados if doctype == "Address"]
    assert [p["links"][0]["link_name"] for p in creadas] == ["CUST-001"]


def test_un_argumento_de_identidad_de_mas_no_llega_a_ninguna_parte(
    erp, locks_tomados, redis_falso
) -> None:
    """Y si el modelo los manda igual, no cambian a quién le escribe.

    Los tres nombres son los que haría falta colar para mover la dirección de
    otra persona. Lo que se afirma no es cómo los rechaza pydantic —eso puede
    cambiar de versión— sino el resultado: la dirección la recibe la cuenta del
    teléfono del webhook, y la del otro sigue intacta.
    """
    erp.cargar_cliente("CUST-OTRO", TELEFONO_DE_OTRO)
    erp.cargar_direccion(
        "CUST-OTRO", "CUST-OTRO-Shipping-1", address_line1="Sarmiento 1", city="Córdoba"
    )

    try:
        pedidos.cambiar_direccion_de_entrega.invoke(
            {
                "direccion": CORDOBA,
                "telefono": TELEFONO_DE_OTRO,
                "cliente": "CUST-OTRO",
                "direccion_nombre": "CUST-OTRO-Shipping-1",
            },
            config=_config(),
        )
    except Exception:
        # Que el esquema lo rechace también está bien: lo que no puede pasar
        # es que lo acepte y le cambie la dirección al otro.
        pass

    assert erp.addresses["CUST-OTRO-Shipping-1"]["address_line1"] == "Sarmiento 1"
    assert erp.direcciones_de("CUST-OTRO") == ["CUST-OTRO-Shipping-1"]


@pytest.mark.parametrize(
    "config",
    [
        _config(scope="management"),
        _config(telefono=""),
        _config(telefono="", customer="CUST-001"),
        {"configurable": {}},
    ],
)
def test_solo_un_cliente_autenticado_lee_o_cambia_una_direccion(
    erp, locks_tomados, redis_falso, config
) -> None:
    erp.cargar_direccion(
        "CUST-001", "CUST-001-Shipping-1", address_line1="Av. Colón 1234", city="Córdoba"
    )

    assert "crear_cliente" in _leer(config)
    assert "crear_cliente" in _cambiar(CORDOBA, config)
    assert erp.creados == []
    assert erp.direcciones_de("CUST-001") == ["CUST-001-Shipping-1"]


def test_un_remitente_sin_cuenta_va_a_crear_cliente(erp, locks_tomados, redis_falso) -> None:
    """Sin ficha no hay dirección que cambiar: el alta ya toma la dirección."""
    respuesta = _cambiar(CORDOBA, _config(telefono=TELEFONO_DE_OTRO))

    assert "crear_cliente" in respuesta
    assert erp.creados == []


# ==========================================================================
# Leer: lo que dice tiene que ser lo que el pedido va a hacer
# ==========================================================================
def test_lee_la_direccion_guardada_y_manda_a_preguntar(erp, redis_falso) -> None:
    erp.cargar_direccion(
        "CUST-001",
        "CUST-001-Shipping-1",
        address_line1="Av. Colón 1234",
        city="Córdoba",
        pincode="5000",
    )

    respuesta = _leer()

    assert "Av. Colón 1234, Córdoba (CP 5000)" in respuesta
    # No alcanza con decir la dirección: la herramienta existe para que se
    # PREGUNTE, que es lo que no pasó en la conversación que la motivó.
    assert "preguntale si va a esa misma dirección o a otra" in respuesta


def test_lee_la_que_va_a_usar_el_pedido_y_no_la_primera_alfabetica(
    erp, redis_falso
) -> None:
    """La dirección dada EN ESTA CONVERSACIÓN gana, igual que en crear_pedido.

    Es el defecto que documenta `app/clientes.py`: «X-Shipping» ordena antes
    que «X-Shipping-1», así que la elección alfabética devuelve la VIEJA. Si
    esta herramienta leyera por su cuenta, le leería al cliente una dirección
    distinta de la que va a llevar el pedido — y sonaría a que se le pregunta.
    """
    erp.cargar_direccion(
        "CUST-001", "CUST-001-Shipping", address_line1="La vieja 1", city="Córdoba"
    )
    erp.cargar_direccion(
        "CUST-001", "CUST-001-Shipping-1", address_line1="La nueva 2", city="Córdoba"
    )
    clientes.recordar_direccion(TELEFONO, "CUST-001-Shipping-1")

    respuesta = _leer()

    assert "La nueva 2" in respuesta
    assert "La vieja 1" not in respuesta


def test_dice_que_hay_mas_de_una_guardada(erp, redis_falso) -> None:
    erp.cargar_direccion(
        "CUST-001", "CUST-001-Shipping-1", address_line1="Una 1", city="Córdoba"
    )
    una_sola = _leer()
    erp.cargar_direccion(
        "CUST-001", "CUST-001-Shipping-2", address_line1="Otra 2", city="Córdoba"
    )
    dos = _leer()

    assert "direcciones guardadas" not in una_sola
    assert "2 direcciones guardadas" in dos


def test_sin_ninguna_direccion_guardada_pide_la_direccion(erp, redis_falso) -> None:
    respuesta = _leer()

    assert "no tiene ninguna dirección de entrega guardada" in respuesta
    assert "cambiar_direccion_de_entrega" in respuesta


def test_una_direccion_que_no_se_puede_leer_no_se_adivina(erp, redis_falso) -> None:
    erp.cargar_direccion(
        "CUST-001", "CUST-001-Shipping-1", address_line1="Av. Colón 1234", city="Córdoba"
    )
    erp.no_puedo_leer.add("CUST-001-Shipping-1")

    respuesta = _leer()

    assert "No pude leer la dirección guardada" in respuesta
    assert "No adivines" in respuesta


def test_una_direccion_sin_calle_vale_lo_mismo_que_no_poder_leerla(
    erp, redis_falso
) -> None:
    """`texto_direccion` tiene su propio texto de relleno, y es castellano.

    Adentro de una respuesta en inglés sería una fuga de idioma, y en
    cualquier idioma sería leerle al cliente una dirección que no dice nada.
    """
    erp.cargar_direccion("CUST-001", "CUST-001-Shipping-1", address_line1="", city="")

    assert "No pude leer la dirección guardada" in _leer()


# ==========================================================================
# Cambiar: queda anotada, el pedido la usa, y no promete nada
# ==========================================================================
def test_la_anota_y_el_proximo_pedido_sale_a_esa_direccion(
    erp, locks_tomados, redis_falso
) -> None:
    """Las DOS mitades, y son dos: la Address creada y la que va a usar el pedido.

    Crear el documento y que `crear_pedido` lo elija son dos consumidores del
    mismo nombre derivado, así que se afirman por separado: sin
    `recordar_direccion`, la Address existe y el pedido sigue saliendo a la
    vieja, que es el defecto exacto que ya pasó una vez.
    """
    erp.cargar_direccion(
        "CUST-001", "CUST-001-Shipping", address_line1="La vieja 1", city="Córdoba"
    )

    respuesta = _cambiar(OTRA_DE_CORDOBA)

    assert "Anotada: Belgrano 456, piso 2, Córdoba (CP 5000)" in respuesta
    nueva = "CUST-001-Shipping-2"
    assert erp.addresses[nueva]["address_line1"] == "Belgrano 456"
    assert erp.addresses[nueva]["city"] == "Córdoba"
    assert clientes.direccion_para_pedido("CUST-001", TELEFONO) == nueva


def test_la_misma_direccion_dos_veces_es_una_sola_y_lo_dice(
    erp, locks_tomados, redis_falso
) -> None:
    primera = _cambiar(CORDOBA)
    segunda = _cambiar({**CORDOBA, "calle": "av colon 1234", "localidad": "CORDOBA"})

    assert "Anotada:" in primera
    assert "Ya la tenía anotada" in segunda
    assert erp.direcciones_de("CUST-001") == ["CUST-001-Shipping-1"]
    assert sum(1 for doctype, _ in erp.creados if doctype == "Address") == 1


def test_una_zona_que_no_consta_no_es_un_no(erp, locks_tomados, redis_falso) -> None:
    """Se toma el pedido igual y NO se le dice que no repartimos ahí.

    «Purnia» es la localidad del pedido real que salió con «Entrega: purnia,
    purnia». Que no esté en la lista de zonas no autoriza a cerrarle la
    puerta: el pedido queda pendiente de revisión y lo mira una persona.
    """
    respuesta = _cambiar(LEJOS)

    assert "Anotada: Station Road 12, Purnia (CP 854301)" in respuesta
    assert "pendiente de revisión" in respuesta
    assert "no me consta" in respuesta
    assert "no repartimos" not in respuesta.replace("NO le digas que no repartimos", "")
    # Y queda guardada igual: el pedido tiene que poder decir a dónde iba.
    assert erp.direcciones_de("CUST-001") == ["CUST-001-Shipping-1"]


def test_si_no_se_puede_releer_la_direccion_tampoco_se_promete_la_entrega(
    erp, locks_tomados, redis_falso, monkeypatch
) -> None:
    """Guardada quedó; la zona no se pudo mirar. No saber no es un «sí»."""
    original = erp.get_doc

    def get_doc(doctype, name):
        if doctype == "Address" and name.endswith("-1"):
            raise erpnext.ERPNextError("403")
        return original(doctype, name)

    monkeypatch.setattr(erpnext, "get_doc", get_doc)

    respuesta = _cambiar(CORDOBA)

    assert "pendiente de revisión" in respuesta
    assert erp.direcciones_de("CUST-001") == ["CUST-001-Shipping-1"]


def test_toma_el_mismo_lock_que_el_alta(erp, locks_tomados, redis_falso) -> None:
    """Las dos escriben una Address para el mismo teléfono: una sección crítica.

    Sin esto, dos mensajes en vuelo —Meta reintenta, y la gente manda la
    dirección dos veces— pasan los dos por la comparación de
    `asegurar_direccion` antes de que exista la primera Address.
    """
    _cambiar(CORDOBA)

    assert locks_tomados == [f"alta-cliente:{TELEFONO}"]


def test_un_error_de_erpnext_no_dice_que_quedo_anotada(
    erp, locks_tomados, redis_falso, monkeypatch
) -> None:
    monkeypatch.setattr(
        erpnext, "create_doc", Mock(side_effect=erpnext.ERPNextError("500"))
    )

    respuesta = _cambiar(CORDOBA)

    assert "No pude guardar la dirección" in respuesta
    assert "No le digas que quedó anotada" in respuesta
    assert clientes.direccion_recordada(TELEFONO) == ""


def test_si_no_se_puede_coordinar_no_se_reintenta(
    erp, redis_falso, monkeypatch
) -> None:
    from app.locks import CoordinationError

    @contextmanager
    def lock_ocupado(nombre, **kwargs):
        raise CoordinationError("ocupado")
        yield  # pragma: no cover

    monkeypatch.setattr(clientes, "distributed_lock", lock_ocupado)

    respuesta = _cambiar(CORDOBA)

    assert "no la reintentes ahora" in respuesta
    assert erp.creados == []


def test_ninguna_de_las_dos_emite_ni_crea_un_pedido(
    erp, locks_tomados, redis_falso
) -> None:
    """Lo peor que puede dejar la de escritura es una Address de más."""
    _cambiar(CORDOBA)
    _leer()

    assert {doctype for doctype, _ in erp.creados} == {"Address"}


# ==========================================================================
# Los dos idiomas
# ==========================================================================
def test_le_contesta_en_el_idioma_del_cliente(erp, locks_tomados, redis_falso) -> None:
    """La lengua sale del teléfono del webhook, nunca del texto del mensaje.

    Sin esto, un cliente en inglés recibe el castellano escrito a mano que
    este módulo tenía en todas sus otras herramientas.
    """
    idioma.recordar_cliente(TELEFONO, "en")

    cambio = _cambiar(CORDOBA)
    lectura = _leer()

    assert "Saved:" in cambio and "Anotada" not in cambio
    assert "The next order would go to:" in lectura
    assert "próximo pedido" not in lectura


def test_sin_cuenta_tambien_contesta_en_su_idioma(erp, redis_falso) -> None:
    """El que no tiene cuenta es el que más probablemente escribe por primera
    vez, así que es justo la respuesta que no puede salir en castellano."""
    idioma.recordar_cliente(TELEFONO_DE_OTRO, "en")

    respuesta = _leer(_config(telefono=TELEFONO_DE_OTRO))

    assert "There is no account yet" in respuesta
