"""La frontera de gerencia: cada herramienta autoriza por su cuenta.

EL BUG QUE ESTO ARREGLA
app/main.py le pasaba a ``responder_gerencia`` el ``thread_tag`` —un sha256 del
teléfono— en el parámetro que iba a parar a ``configurable["actor_phone"]``.
``require_management`` le preguntaba entonces a ``router.es_equipo()`` por un
hash, que nunca está en la lista del equipo, así que siete herramientas le
contestaban "ese número no está autorizado" AL DUEÑO. Las otras nueve andaban
sólo porque no chequeaban nada: la lista de clientes con sus teléfonos, la
facturación, los márgenes, la deuda y tres herramientas que ESCRIBEN en
ERPNext quedaban al alcance de cualquiera que el router dejara pasar.

LOS DOS LADOS DE LA MISMA PRUEBA
1. Con el teléfono verificado, ninguna herramienta de gerencia se niega.
2. Sin él —un cliente, un desconocido, sin contexto, o un hash— TODAS se
   niegan, y ninguna llega a ERPNext ni a Redis.

La lista de herramientas se DERIVA de ``TOOLS_GERENCIA - TOOLS_CLIENTES``, no
se escribe a mano: una herramienta nueva sin su guarda hace fallar este archivo
sin que nadie se acuerde de venir a agregarla.
"""
from __future__ import annotations

import hashlib
from unittest.mock import Mock

import pytest

from app import avisos, erpnext, locks, outbound_status, router, solicitudes
from app.graph import TOOLS_CLIENTES, TOOLS_GERENCIA
from app.runtime_context import SIN_PERMISO, ActorContext, actor_context

GERENTE = "5493511234567"
CLIENTE = "5493510000000"
DESCONOCIDO = "5493519999999"
HASH_DEL_GERENTE = f"wa:{hashlib.sha256(GERENTE.encode()).hexdigest()}"

# Las herramientas que SÓLO tiene gerencia. Las compartidas con el agente de
# clientes (catálogo, estado de pedido, escalar) se excluyen a propósito: un
# cliente tiene que poder usarlas.
_COMPARTIDAS = {t.name for t in TOOLS_CLIENTES}
SOLO_GERENCIA = {t.name: t for t in TOOLS_GERENCIA if t.name not in _COMPARTIDAS}

# Argumentos mínimos válidos por herramienta. Las claves tienen que cubrir
# exactamente SOLO_GERENCIA — lo verifica el primer test.
ARGUMENTOS: dict[str, dict] = {
    "pedidos_pendientes": {},
    "ventas_del_periodo": {},
    "stock_bajo": {},
    "cobranzas_vencidas": {},
    "ficha_cliente": {"nombre_o_codigo": "Don José"},
    "ejecutar_reporte": {"nombre_reporte": "Stock Balance"},
    "registrar_venta_offline": {
        "cliente": "CUST-001",
        "lineas": [{"item_code": "LECHE-ENT-1L", "cantidad": 2}],
    },
    "contar_stock": {"item_code": "LECHE-ENT-1L", "cantidad_real": 12},
    "confirmar_entrega": {"numero_pedido": "SAL-ORD-2026-00001"},
    "redactar_mensaje_cliente": {"cliente": "Don José", "intencion": "llegó el queso"},
    "ver_limites": {},
    "proponer_limite": {"limite": "tope", "valor": "50000"},
    "historial_limites": {},
    "ver_reglas_de_entrega": {},
    "estado_del_sistema": {},
    "ver_avisos_fallidos": {},
    "detalle_de_pedido": {"pedido": "SAL-ORD-2026-00001"},
    "proponer_accion": {"accion": "confirmar", "pedido": "SAL-ORD-2026-00001"},
}

# Cómo se niega cada una. Casi todas comparten SIN_PERMISO; las que ya tenían su
# propio texto lo conservan, porque cambiarlo no hace nada más seguro.
NEGATIVAS = {
    "contar_stock": "No pude autenticar quién cuenta; no cargué el conteo.",
    "estado_del_sistema": "Ese número no está autorizado para ver el estado del sistema.",
    "ver_avisos_fallidos": "Ese número no está autorizado para ver el estado del sistema.",
    "ver_limites": "Ese número no está autorizado",
    "proponer_limite": "Ese número no está autorizado",
    "historial_limites": "Ese número no está autorizado",
    "ver_reglas_de_entrega": "Ese número no está autorizado",
}


@pytest.fixture(autouse=True)
def sin_whatsapp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ninguna prueba manda un WhatsApp de verdad.

    `proponer_limite` y `proponer_accion` terminan en
    app/notificar.py::pedir_codigo_de_ajuste, que abre una conexión a
    graph.facebook.com y la cierra con un error. Nunca cambió un resultado
    —los dos tratan «no se lo pude mandar» como el caso normal— pero la casa
    dice que los tests no salen a la red, y salían.
    """
    from app import whatsapp

    monkeypatch.setattr(
        whatsapp, "enviar_mensaje",
        Mock(side_effect=AssertionError("ningún test manda un WhatsApp real")),
    )
    monkeypatch.setattr(
        whatsapp, "enviar_botones",
        Mock(side_effect=AssertionError("ningún test manda un WhatsApp real")),
    )


def _config(scope: str, phone: str) -> dict:
    return {
        "configurable": {
            "thread_id": f"{scope}:thread",
            "actor_scope": scope,
            "customer_code": "CUST-001" if scope == "customer" else "",
            "actor_phone": phone,
            "inbound_message_id": "wamid.test",
        }
    }


NO_AUTORIZADOS = {
    # Un cliente registrado, con su teléfono real y verificado.
    "cliente": _config("customer", CLIENTE),
    # Alcance de gerencia, pero un número que no está en TELEFONOS_EQUIPO.
    "desconocido": _config("management", DESCONOCIDO),
    # Sin contexto: la invocación directa que no pasó por el webhook.
    "sin-contexto": {},
    "config-none": None,
    "configurable-vacio": {"configurable": {}},
    # EL BUG: el thread_tag hasheado donde iba el teléfono.
    "hash": _config("management", HASH_DEL_GERENTE),
    # Un hash sin el prefijo, que normalizado da algo con forma de número.
    "hash-crudo": _config("management", hashlib.sha256(GERENTE.encode()).hexdigest()),
}


@pytest.fixture(autouse=True)
def equipo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TELEFONOS_EQUIPO", GERENTE)
    router.recargar()
    yield
    monkeypatch.delenv("TELEFONOS_EQUIPO", raising=False)
    router.recargar()


@pytest.fixture
def nada_de_escrituras(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cualquier ERPNext o Redis desde un camino no autorizado es un fallo.

    Redis se corta en sus dos puertas —``locks.conexion`` para límites y
    solicitudes, ``outbound_status._redis`` para las colas— y también en los
    contadores que envuelven a una de ellas, para que la prueba no dependa de
    cuál de las dos usa cada herramienta.
    """
    for name in ("get_list", "get_doc", "create_doc", "submit_doc", "add_comment",
                 "run_report", "policy_get_list", "default_context",
                 "default_company", "registrar_comentario"):
        monkeypatch.setattr(
            erpnext, name,
            Mock(side_effect=AssertionError(f"erpnext.{name} no debe llamarse")),
        )
    monkeypatch.setattr(
        locks, "conexion",
        Mock(side_effect=AssertionError("locks.conexion no debe llamarse")),
    )
    monkeypatch.setattr(
        outbound_status, "_redis",
        Mock(side_effect=AssertionError("outbound_status no debe llamarse")),
    )
    monkeypatch.setattr(
        outbound_status, "contar_pendientes",
        Mock(side_effect=AssertionError("contar_pendientes no debe llamarse")),
    )
    monkeypatch.setattr(
        avisos, "pendientes",
        Mock(side_effect=AssertionError("avisos.pendientes no debe llamarse")),
    )
    monkeypatch.setattr(
        solicitudes, "trabadas",
        Mock(side_effect=AssertionError("solicitudes.trabadas no debe llamarse")),
    )


# --------------------------------------------------------------------------
# 0. La lista de herramientas y sus argumentos no se desincronizan.
# --------------------------------------------------------------------------


def test_every_management_only_tool_is_covered_by_this_file() -> None:
    assert set(ARGUMENTOS) == set(SOLO_GERENCIA), (
        "una herramienta sólo-de-gerencia sin caso de prueba: agregala a "
        "ARGUMENTOS con sus argumentos mínimos"
    )
    # 18 hoy. El número está acá para que un cambio de superficie se note.
    assert len(SOLO_GERENCIA) == 18


def test_no_management_tool_accepts_a_phone_or_an_identity_argument() -> None:
    """El teléfono no puede venir del modelo, del prompt ni de un argumento."""
    prohibidos = {
        "telefono", "phone", "actor_phone", "usuario", "user", "numero",
        "config", "actor_scope", "customer_code", "thread_id",
        "inbound_message_id",
    }
    for nombre, herramienta in SOLO_GERENCIA.items():
        visibles = set(herramienta.args)
        assert not (visibles & prohibidos), f"{nombre} expone identidad: {visibles}"
        esquema = set(herramienta.args_schema.model_json_schema()["properties"])
        assert esquema == visibles, nombre


# --------------------------------------------------------------------------
# 1. El teléfono verificado sí autoriza. Este es el lado que estaba roto.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("nombre", sorted(ARGUMENTOS))
def test_the_verified_manager_phone_is_never_refused(
    nombre: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con el número del dueño, ninguna herramienta contesta una negativa.

    No se afirma nada sobre el CONTENIDO: cada herramienta lee lo suyo y acá
    todo devuelve vacío. Lo único que se prueba es que pasó la guarda.
    """
    for name in ("get_list", "run_report", "policy_get_list"):
        monkeypatch.setattr(erpnext, name, Mock(return_value=[]))
    monkeypatch.setattr(
        erpnext, "get_doc",
        Mock(return_value={"name": "SAL-ORD-2026-00001", "docstatus": 0,
                           "customer": "CUST-001", "items": []}),
    )
    monkeypatch.setattr(erpnext, "create_doc", Mock(return_value={"name": "NEW-1"}))
    monkeypatch.setattr(erpnext, "add_comment", Mock(return_value=None))
    monkeypatch.setattr(erpnext, "default_context", Mock(return_value=("Co", "Dep")))
    monkeypatch.setattr(erpnext, "default_company", Mock(return_value="Co"))

    respuesta = str(
        SOLO_GERENCIA[nombre].invoke(
            dict(ARGUMENTOS[nombre]), config=_config("management", GERENTE)
        )
    )

    assert SIN_PERMISO not in respuesta
    assert "no está autorizado" not in respuesta
    assert "No pude autenticar" not in respuesta


def test_a_manager_phone_in_any_human_format_still_authorizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El número se normaliza antes de comparar: 0351 15… es el mismo número."""
    monkeypatch.setattr(erpnext, "get_list", Mock(return_value=[]))
    for crudo in ("+54 9 351 123-4567", "0351 15 123 4567", "5493511234567"):
        respuesta = str(
            SOLO_GERENCIA["pedidos_pendientes"].invoke(
                {}, config=_config("management", crudo)
            )
        )
        assert SIN_PERMISO not in respuesta, crudo


# --------------------------------------------------------------------------
# 2. Todo lo demás se niega, y no toca nada.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("nombre", sorted(ARGUMENTOS))
@pytest.mark.parametrize("caso", sorted(NO_AUTORIZADOS))
def test_unauthorized_identity_is_refused_and_writes_nothing(
    nombre: str, caso: str, nada_de_escrituras: None
) -> None:
    respuesta = str(
        SOLO_GERENCIA[nombre].invoke(
            dict(ARGUMENTOS[nombre]), config=NO_AUTORIZADOS[caso]
        )
    )

    assert NEGATIVAS.get(nombre, SIN_PERMISO) in respuesta, (
        f"{nombre} con identidad '{caso}' contestó: {respuesta[:200]}"
    )


@pytest.mark.parametrize("caso", sorted(NO_AUTORIZADOS))
def test_a_refusal_never_echoes_the_phone_it_refused(caso: str, nada_de_escrituras: None) -> None:
    """La negativa no dice qué número llamó ni repite el hash."""
    for nombre in sorted(ARGUMENTOS):
        respuesta = str(
            SOLO_GERENCIA[nombre].invoke(
                dict(ARGUMENTOS[nombre]), config=NO_AUTORIZADOS[caso]
            )
        )
        for secreto in (GERENTE, CLIENTE, DESCONOCIDO, HASH_DEL_GERENTE):
            assert secreto not in respuesta, f"{nombre}/{caso} filtró {secreto[:6]}…"


# --------------------------------------------------------------------------
# 3. Un hash no es una identidad, y un tag no es un teléfono.
# --------------------------------------------------------------------------


def test_a_hashed_phone_never_survives_as_a_phone() -> None:
    actor = actor_context(_config("management", HASH_DEL_GERENTE))

    assert actor.actor_phone != GERENTE
    assert not router.es_equipo(actor.actor_phone)


def test_the_context_phone_is_canonical_whatever_the_webhook_sent() -> None:
    for crudo in ("+54 9 351 123-4567", "0351 15 123 4567", "5493511234567"):
        assert actor_context(_config("management", crudo)).actor_phone == GERENTE


def test_the_tag_is_a_hash_and_is_not_the_phone() -> None:
    actor = actor_context(_config("management", GERENTE))

    assert actor.tag == hashlib.sha256(GERENTE.encode()).hexdigest()[:10]
    assert GERENTE not in actor.tag
    assert len(actor.tag) == 10


def test_an_empty_phone_has_a_tag_that_is_not_an_identity() -> None:
    """El tag de "" existe (es un hash) pero no autoriza a nadie."""
    actor = ActorContext("management", "", "", "", "")

    assert actor.tag
    assert not router.es_equipo(actor.actor_phone)


# --------------------------------------------------------------------------
# 4. Los logs no llevan teléfonos.
# --------------------------------------------------------------------------


class _RedisFalso:
    """Lo mínimo de Redis que usa app/limites.py, en un dict."""

    def __init__(self, propuesta: str) -> None:
        self.valores: dict[str, str] = {}
        self.propuesta = propuesta
        self.auditoria: list[str] = []

    def get(self, clave):
        return self.propuesta

    def hgetall(self, clave):
        return dict(self.valores)

    def hget(self, clave, campo):
        return self.valores.get(campo)

    def hset(self, clave, campo, valor):
        self.valores[campo] = valor

    def rpush(self, clave, valor):
        self.auditoria.append(valor)

    def ltrim(self, clave, desde, hasta):
        return None

    def delete(self, clave):
        self.propuesta = None
        return 1


def test_applying_a_limit_logs_a_tag_and_never_the_phone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """El único print de app/limites.py con un teléfono adentro ya no lo tiene."""
    import json

    from app import limites

    fake = _RedisFalso(
        json.dumps(
            {
                "limite": "AUTO_CONFIRM_MAX",
                "codigo": "1234",
                "telefono": GERENTE,
                "nuevo": "50000",
            }
        )
    )
    monkeypatch.setattr(limites.locks, "conexion", lambda: fake)
    monkeypatch.setattr(limites, "_auditar_en_erpnext", lambda entrada: None)
    capsys.readouterr()

    entrada = limites.aplicar("1234", GERENTE)
    salida = capsys.readouterr().out

    assert entrada["nuevo"] == "50000"
    assert "[limites] AUTO_CONFIRM_MAX" in salida
    assert GERENTE not in salida
    assert limites._tag(GERENTE) in salida


def test_the_proposal_key_is_the_same_whichever_half_builds_it() -> None:
    """Proponer y confirmar llegan por caminos distintos y tienen que coincidir."""
    from app import limites

    claves = {
        limites._clave_propuesta(crudo)
        for crudo in ("+54 9 351 123-4567", "0351 15 123 4567", GERENTE)
    }

    assert len(claves) == 1
    assert GERENTE in claves.pop()
