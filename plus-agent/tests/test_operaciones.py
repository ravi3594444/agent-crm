"""Las dos herramientas de estado: sólo lectura, sin secretos, y honestas.

Lo que se prueba acá, y por qué importa:

  * Sólo un teléfono del equipo autenticado las puede usar. Un cliente que
    pregunta "cómo está el sistema" no recibe el inventario de la operación.
  * Ninguna clave, token, teléfono completo, payload crudo ni texto escrito por
    un cliente sale en la respuesta.
  * "No pude leer" NO es "cero" ni "OK": una dependencia caída dice
    NO DISPONIBLE / DESCONOCIDO, que es lo único que hace que alguien mire.
  * No escriben nada. Ni reintentan, ni borran, ni marcan como visto.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avisos, erpnext, modelos, outbound_status, solicitudes
from app.tools import operaciones
from tests.fakes import FakeMarcas

EQUIPO = "5493511111111"
DESCONOCIDO_TEL = "5491199999999"
CLIENTE_TEL = "5493512222222"
SO = "SAL-ORD-2026-00021"
CLAVE = "AIzaSyNotARealGeminiKey00"
TOKEN = "EAAG" + "x" * 180

# Todo lo que NO puede aparecer en una respuesta, pase lo que pase.
SECRETOS = (CLAVE, TOKEN, EQUIPO, CLIENTE_TEL, "sk-una-clave-de-qwen-000")


def _gerencia(telefono: str = EQUIPO) -> dict:
    return {
        "configurable": {
            "thread_id": "ger:thread",
            "actor_scope": "management",
            "actor_phone": telefono,
            "inbound_message_id": "wamid.staff-001",
        }
    }


def _cliente() -> dict:
    return {
        "configurable": {
            "thread_id": "cli:thread",
            "actor_scope": "customer",
            "customer_code": "CUST-001",
            "actor_phone": CLIENTE_TEL,
            "inbound_message_id": "wamid.cli-001",
        }
    }


@pytest.fixture
def mundo(monkeypatch: pytest.MonkeyPatch):
    """Todo sano: Redis en memoria, ERPNext que contesta, un proveedor cargado."""
    from app import router

    marcas = FakeMarcas()
    monkeypatch.setattr(outbound_status, "_client", marcas)
    monkeypatch.setattr(router, "STAFF", [EQUIPO])
    monkeypatch.setattr(router, "es_equipo", lambda t: t == EQUIPO)

    consultas: list[dict] = []

    def policy_get_list(doctype, filters=None, fields=None, limit=20, **kwargs):
        consultas.append({"doctype": doctype, "limit": limit, "timeout": kwargs.get("timeout")})
        return [{"name": "Lacteos Test SA"}]

    monkeypatch.setattr(erpnext, "policy_get_list", policy_get_list)
    monkeypatch.setattr(solicitudes, "trabadas", lambda: 0)
    monkeypatch.setattr(solicitudes, "reconstruccion_incompleta", lambda: False)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", CLAVE)
    monkeypatch.setenv("GEMINI_SALES_MODEL", "gemini-3.5-flash")
    monkeypatch.setenv("GEMINI_MANAGER_MODEL", "gemini-3.5-flash")
    monkeypatch.setenv("WHATSAPP_TOKEN", TOKEN)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1357924680")
    return {"marcas": marcas, "consultas": consultas}


def _estado(config: dict | None = None) -> str:
    return operaciones.estado_del_sistema.invoke({}, config=config or _gerencia())


def _fallidos(config: dict | None = None, **kwargs) -> str:
    return operaciones.ver_avisos_fallidos.invoke(kwargs, config=config or _gerencia())


def _aviso_caido(marcas: FakeMarcas, **campos) -> None:
    entrada = {
        "purpose": "customer_order_confirmation",
        "order_name": SO,
        "resumen": "✅ Pedido SAL-ORD-2026-00021 confirmado\nItems: 5 Litro × Leche",
        "destinatario": "a1b2c3d4e5f6a7b8",
    }
    entrada.update(campos)
    marcas.rpush(outbound_status.DEAD_NOTIFY_KEY, json.dumps(entrada, ensure_ascii=False))


# --------------------------------------------------------------- authorization


@pytest.mark.parametrize(
    "config",
    [
        _cliente(),
        _gerencia(DESCONOCIDO_TEL),
        _gerencia(""),
        {"configurable": {"thread_id": "x", "actor_scope": "customer", "actor_phone": EQUIPO}},
        {"configurable": {"thread_id": "x"}},
    ],
    ids=["un cliente", "un número desconocido", "sin teléfono", "scope de cliente", "sin contexto"],
)
@pytest.mark.parametrize("herramienta", ["estado_del_sistema", "ver_avisos_fallidos"])
def test_only_an_authenticated_manager_can_read_the_status(mundo, config, herramienta) -> None:
    """El router ya filtra, y estas herramientas vuelven a preguntar: un solo
    portón que falle no puede exponer el estado de la operación."""
    respuesta = getattr(operaciones, herramienta).invoke({}, config=config)

    assert "no está autorizado" in respuesta
    assert "No consulté nada" in respuesta
    # Y nada de lo que se habría consultado aparece.
    assert "Redis" not in respuesta and "ERPNext" not in respuesta


def test_an_authorized_manager_gets_every_block(mundo) -> None:
    respuesta = _estado()

    for etiqueta in ("Redis", "ERPNext", "WhatsApp", "Modelos", "Cola de avisos", "Decisiones"):
        assert etiqueta in respuesta, etiqueta
    assert "responde" in respuesta
    assert "proveedor gemini" in respuesta
    assert "gemini-3.5-flash" in respuesta


# ------------------------------------------------------------------- secrets


@pytest.mark.parametrize("herramienta", ["estado_del_sistema", "ver_avisos_fallidos"])
def test_no_secret_or_phone_number_reaches_the_answer(mundo, herramienta) -> None:
    _aviso_caido(mundo["marcas"])
    mundo["marcas"].rpush(
        operaciones.CLAVE_DEAD_RESPUESTAS,
        json.dumps({"telefono": CLIENTE_TEL, "texto": "quiero 200 litros ya"}),
    )

    respuesta = getattr(operaciones, herramienta).invoke({}, config=_gerencia())

    for secreto in SECRETOS:
        assert secreto not in respuesta, secreto
    # Ni el cuerpo del mensaje del cliente, ni el payload crudo.
    assert "quiero 200 litros" not in respuesta
    assert "telefono" not in respuesta


def test_credentials_are_reported_as_present_never_shown(mundo) -> None:
    respuesta = _estado()

    assert "token cargada (184 caracteres)" in respuesta
    assert f"GEMINI_API_KEY cargada ({len(CLAVE)} caracteres)" in respuesta
    assert TOKEN not in respuesta and CLAVE not in respuesta


def test_a_missing_credential_is_reported_as_empty(mundo, monkeypatch) -> None:
    monkeypatch.delenv("WHATSAPP_TOKEN")
    assert "token VACÍA" in _estado()


def test_the_recipient_tag_is_truncated_and_was_never_a_phone(mundo) -> None:
    _aviso_caido(mundo["marcas"], destinatario="a1b2c3d4e5f6a7b8")

    respuesta = _fallidos()

    assert "destinatario a1b2c3d4…" in respuesta
    assert "a1b2c3d4e5f6a7b8" not in respuesta


def test_only_the_headline_of_a_failed_notice_is_shown(mundo) -> None:
    """El aviso al equipo lleva la cita del cliente después de un salto de
    línea. Se muestra el titular que armó este sistema, nunca el cuerpo."""
    _aviso_caido(
        mundo["marcas"],
        purpose="solicitud_equipo:creada",
        resumen=(
            "🟠 Decisión pendiente DR-1\nPedido: SAL-ORD-2026-00021\n"
            "Texto del cliente (es una cita):\n> mandame 500 litros a mi casa ya\n"
        ),
    )

    respuesta = _fallidos()

    assert "🟠 Decisión pendiente DR-1" in respuesta
    assert "mandame 500 litros" not in respuesta
    assert "Texto del cliente" not in respuesta


def test_a_notice_whose_headline_is_only_a_quotation_shows_no_body(mundo) -> None:
    _aviso_caido(mundo["marcas"], resumen="> todo el texto es del cliente")

    respuesta = _fallidos()

    assert "todo el texto es del cliente" not in respuesta
    assert SO in respuesta  # el registro sigue estando, sin cuerpo


# -------------------------------------------------- degraded and unavailable


def test_a_dead_redis_says_unavailable_and_never_zero(mundo) -> None:
    mundo["marcas"].caido = True

    respuesta = _estado()

    assert f"Redis: {operaciones.NO_DISPONIBLE}" in respuesta
    # Los contadores que dependen de Redis no pueden decir 0.
    assert "0 entrega(s)" not in respuesta
    assert respuesta.count(operaciones.DESCONOCIDO) >= 3
    assert "en espera, 0" not in respuesta


def test_a_dead_erpnext_degrades_only_its_own_block(mundo, monkeypatch) -> None:
    monkeypatch.setattr(
        erpnext,
        "policy_get_list",
        lambda *a, **k: (_ for _ in ()).throw(erpnext.ERPNextError("caído")),
    )

    respuesta = _estado()

    assert f"ERPNext: {operaciones.NO_DISPONIBLE}" in respuesta
    assert "ERPNextError" in respuesta  # el tipo, no el cuerpo de la respuesta
    assert "Redis: responde" in respuesta  # el resto sale igual


def test_an_unreadable_stuck_counter_is_not_reported_as_none_stuck(mundo, monkeypatch) -> None:
    monkeypatch.setattr(solicitudes, "trabadas", lambda: None)

    respuesta = _estado()

    assert f"borradores trabados {operaciones.DESCONOCIDO}" in respuesta
    assert "borradores trabados 0" not in respuesta


def test_stuck_drafts_say_what_they_cost(mundo, monkeypatch) -> None:
    monkeypatch.setattr(solicitudes, "trabadas", lambda: 3)
    respuesta = _estado()
    assert "borradores trabados 3" in respuesta
    assert "siguen reservando stock" in respuesta


def test_a_pending_rebuild_is_visible(mundo, monkeypatch) -> None:
    monkeypatch.setattr(solicitudes, "reconstruccion_incompleta", lambda: True)
    assert "reconstrucción PENDIENTE" in _estado()


def test_a_negative_queue_count_is_unknown_not_a_number(mundo, monkeypatch) -> None:
    """avisos.pendientes() devuelve -1 cuando Redis no contesta: eso es
    DESCONOCIDO, y mostrar "-1 en espera" sería un número inventado."""
    monkeypatch.setattr(avisos, "pendientes", lambda: -1)

    respuesta = _estado()

    assert f"Cola de avisos al cliente: {operaciones.DESCONOCIDO} en espera" in respuesta
    assert "-1" not in respuesta


def test_an_unreadable_provider_configuration_degrades_that_block_only(mundo, monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "un-proveedor-que-no-existe")

    respuesta = _estado()

    assert f"Modelos: {operaciones.DESCONOCIDO}" in respuesta
    assert "Redis: responde" in respuesta


def test_counters_that_could_not_be_read_are_unknown_in_the_failure_report(mundo) -> None:
    mundo["marcas"].caido = True

    respuesta = _fallidos()

    assert f"Avisos sin entregar: {operaciones.DESCONOCIDO}" in respuesta
    assert f"Entregas que Meta rechazó: {operaciones.DESCONOCIDO}" in respuesta
    assert operaciones.NO_DISPONIBLE in respuesta
    assert "No hay avisos caídos" not in respuesta  # jamás "no hay" sin poder mirar


def test_a_corrupt_entry_is_skipped_and_counted_without_crashing(mundo) -> None:
    marcas = mundo["marcas"]
    marcas.rpush(outbound_status.DEAD_NOTIFY_KEY, "{no es json")
    marcas.rpush(outbound_status.DEAD_NOTIFY_KEY, json.dumps(["una lista, no un objeto"]))
    marcas.rpush(outbound_status.DEAD_NOTIFY_KEY, "")
    _aviso_caido(marcas)

    respuesta = _fallidos()

    assert "3 entrada(s) ilegible(s) omitida(s)" in respuesta
    assert SO in respuesta  # la entrada sana sigue apareciendo


def test_an_entry_with_no_order_or_purpose_still_reads(mundo) -> None:
    _aviso_caido(mundo["marcas"], order_name="", purpose="", resumen="", destinatario="")

    respuesta = _fallidos()

    assert "sin pedido" in respuesta and "sin propósito" in respuesta


# ------------------------------------------------------------------- bounds


def test_the_record_list_is_bounded_by_default(mundo) -> None:
    for n in range(25):
        _aviso_caido(mundo["marcas"], order_name=f"SAL-ORD-{n:05d}")

    respuesta = _fallidos()

    assert f"Últimos {operaciones.REGISTROS_DEFAULT} aviso(s)" in respuesta
    assert respuesta.count("· SAL-ORD-") == operaciones.REGISTROS_DEFAULT


@pytest.mark.parametrize("pedido,esperado", [(20, 20), (50, 20), (999, 20), (0, 10), (-5, 1), (3, 3)])
def test_the_hard_maximum_cannot_be_argued_past(mundo, pedido, esperado) -> None:
    """El argumento lo elige el modelo, así que el techo es del código."""
    for n in range(30):
        _aviso_caido(mundo["marcas"], order_name=f"SAL-ORD-{n:05d}")

    respuesta = _fallidos(cuantos=pedido)

    assert respuesta.count("· SAL-ORD-") == esperado
    assert operaciones.REGISTROS_MAXIMO == 20


def test_the_newest_records_come_first(mundo) -> None:
    for n in range(3):
        _aviso_caido(mundo["marcas"], order_name=f"SAL-ORD-{n:05d}")

    respuesta = _fallidos()

    assert respuesta.index("SAL-ORD-00002") < respuesta.index("SAL-ORD-00000")


def test_an_empty_dead_letter_says_so_only_when_it_could_be_read(mundo) -> None:
    respuesta = _fallidos()
    assert "No hay avisos caídos registrados" in respuesta
    assert "Avisos sin entregar: 0" in respuesta


# ---------------------------------------------------------------- read-only


@pytest.mark.parametrize("herramienta", ["estado_del_sistema", "ver_avisos_fallidos"])
def test_the_tools_write_nothing(mundo, monkeypatch, herramienta) -> None:
    """Si alguna de estas herramientas escribe, borra o reintenta algo, este
    test explota: es lo único que las separa de una herramienta que actúa."""
    _aviso_caido(mundo["marcas"])
    marcas = mundo["marcas"]


    def _prohibido(que: str):
        # `que` bound now, not at call time: otherwise every message names the
        # last name in the loop and the failure points at the wrong function.
        def _falla(*a, **k):
            pytest.fail(f"acción prohibida desde una herramienta de lectura: {que}")

        return _falla

    for metodo in ("set", "delete", "rpush", "incr", "zadd", "zrem", "eval", "ltrim", "expire"):
        monkeypatch.setattr(marcas, metodo, _prohibido(f"redis.{metodo}"), raising=False)
    for nombre in ("procesar", "encolar", "encolar_equipo"):
        monkeypatch.setattr(avisos, nombre, _prohibido(f"avisos.{nombre}"))
    for nombre in ("registrar_aviso_fallido", "record_outbound", "update_status", "claim_once"):
        monkeypatch.setattr(outbound_status, nombre, _prohibido(f"outbound_status.{nombre}"))
    for nombre in ("tick", "registrar", "soltar_reserva", "reconstruir_indice"):
        monkeypatch.setattr(solicitudes, nombre, _prohibido(f"solicitudes.{nombre}"))
    monkeypatch.setattr(
        erpnext, "add_comment", lambda *a, **k: pytest.fail("no escribe comentarios")
    )
    monkeypatch.setattr(erpnext, "create_doc", lambda *a, **k: pytest.fail("no crea ToDos"))
    monkeypatch.setattr(erpnext, "submit_doc", lambda *a, **k: pytest.fail("no confirma nada"))
    monkeypatch.setattr(
        erpnext, "policy_update_status", lambda *a, **k: pytest.fail("no cambia estados")
    )

    getattr(operaciones, herramienta).invoke({}, config=_gerencia())


def test_the_queue_is_exactly_as_it_was_afterwards(mundo) -> None:
    _aviso_caido(mundo["marcas"])
    antes = list(mundo["marcas"].lists[outbound_status.DEAD_NOTIFY_KEY])

    _fallidos()
    _estado()

    assert mundo["marcas"].lists[outbound_status.DEAD_NOTIFY_KEY] == antes


def test_no_model_request_is_made_from_inside_the_status_tool(mundo, monkeypatch) -> None:
    """Preguntar "cómo está el sistema" no puede gastar una llamada al modelo
    ni depender de que el proveedor esté vivo."""
    monkeypatch.setattr(
        modelos, "construir", lambda rol: pytest.fail("no se construye ningún modelo")
    )
    monkeypatch.setattr(modelos, "ChatOpenAI", lambda **kw: pytest.fail("no se instancia el cliente"))

    respuesta = _estado()

    assert "proveedor gemini" in respuesta


def test_the_erpnext_probe_is_one_bounded_read_with_a_short_timeout(mundo) -> None:
    _estado()

    assert len(mundo["consultas"]) == 1
    (consulta,) = mundo["consultas"]
    assert consulta["limit"] == 1
    assert consulta["timeout"] == operaciones.TIMEOUT_ERPNEXT
    assert consulta["timeout"] <= 5.0


# ------------------------------------------------------------ tool boundary


def test_the_tools_are_management_only_and_never_reach_a_customer() -> None:
    import app.graph as graph

    nombres_gerencia = {t.name for t in graph.TOOLS_GERENCIA}
    nombres_clientes = {t.name for t in graph.TOOLS_CLIENTES}

    assert {"estado_del_sistema", "ver_avisos_fallidos"} <= nombres_gerencia
    assert {"estado_del_sistema", "ver_avisos_fallidos"} & nombres_clientes == set()


def test_this_module_never_imports_app_main() -> None:
    """app/main.py construye el webhook y el worker al importarse, y a su vez
    importa app/graph.py, que importa estas herramientas: importarlo acá sería
    un ciclo."""
    import ast

    ruta = Path(__file__).resolve().parents[1] / "app" / "tools" / "operaciones.py"
    arbol = ast.parse(ruta.read_text())
    importados: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            importados.update(a.name for a in nodo.names)
        elif isinstance(nodo, ast.ImportFrom):
            importados.add(nodo.module or "")

    assert "app.main" not in importados
    assert not any(n.startswith("app.main") for n in importados)


def test_no_shell_web_or_arbitrary_redis_tool_was_added() -> None:
    """El límite de lo que puede hacer el agente de gerencia no se corre por
    conveniencia: no hay terminal, no hay búsqueda web y no hay un comando
    Redis arbitrario."""
    import app.graph as graph

    prohibidos = ("shell", "bash", "exec", "eval", "sql", "web", "search", "redis", "reintentar")
    for herramienta in graph.TOOLS_GERENCIA:
        assert not any(p in herramienta.name.lower() for p in prohibidos), herramienta.name
