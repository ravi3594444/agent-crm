"""Los límites los fija el DUEÑO, y nadie más.

Esto es lo que se prueba acá, y por qué importa:

  * Sólo un teléfono del equipo ve o cambia un límite. Estos números deciden
    qué pedidos salen sin que los mire una persona.
  * Nada cambia hasta que el dueño escribe el código. El LLM interpreta lo que
    dijo; no mueve un límite por su cuenta ni porque un mensaje se lo pida.
  * Cada cambio queda con teléfono, fecha, valor anterior y valor nuevo.
  * Un cambio rige en la evaluación siguiente, sin reiniciar nada.
  * Un valor imposible se rechaza; un almacén que no se puede leer deja los
    pedidos pendientes. Nunca se adivina un número.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import FakeRedis, entrega_autorizada, inventario_confiable

from app import limites, locks, main, policy, whatsapp
from app.tools import configuracion

# Captured before the autouse fixture in conftest replaces it: the two tests
# below are about the real ERPNext cross-check, not about a stub of it.
_CONSULTA_DURABLE_REAL = limites._hubo_cambios_durables
_CONSULTA_ENTREGA_REAL = limites._hubo_cambios_durables_entrega

EQUIPO = "5493511111111"
OTRO_DEL_EQUIPO = "5493512222222"
DESCONOCIDO = "5491199999999"


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
            "actor_phone": "5493510000000",
            "inbound_message_id": "wamid.cli-001",
        }
    }


@pytest.fixture
def almacen(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """A store that keeps what is written to it, and a staff list of two.

    Also captures what Python sends the owner DIRECTLY. The confirmation code
    does not come back through the tool any more, so `enviados` is where a test
    looks for it — which is exactly the point being made.
    """
    from app import router

    falso = FakeRedis()
    falso.enviados = []
    monkeypatch.setattr(locks, "conexion", lambda: falso)
    monkeypatch.setattr(router, "STAFF", [EQUIPO, OTRO_DEL_EQUIPO])
    monkeypatch.setattr(router, "es_equipo", lambda t: t in (EQUIPO, OTRO_DEL_EQUIPO))
    monkeypatch.setattr(limites, "_codigo", lambda: "4242")
    monkeypatch.setattr(
        whatsapp,
        "enviar_mensaje",
        lambda tel, texto: falso.enviados.append((tel, texto))
        or {"messages": [{"id": "wamid.x"}]},
    )
    return falso


def _codigo_enviado(almacen: FakeRedis, telefono: str = EQUIPO) -> str:
    """The code as the OWNER received it — the only place it exists."""
    for tel, texto in almacen.enviados:
        if tel != telefono:
            continue
        match = re.search(r"\*(\d{4})\*", texto)
        if match:
            return match.group(1)
    return ""


# --------------------------------------------------------------- authorization
@pytest.mark.parametrize(
    "config",
    [
        _cliente(),
        _gerencia(DESCONOCIDO),
        {"configurable": {"actor_scope": "management", "actor_phone": ""}},
        {},
    ],
)
def test_only_an_authorized_manager_can_see_or_change_a_limit(
    almacen: FakeRedis, config: dict
) -> None:
    """A customer, a stranger, or a request with no phone at all gets nothing —
    not the values, not the history, and above all not a change."""
    assert "no está autorizado" in configuracion.ver_limites.invoke(
        {}, config=config
    )
    assert "no está autorizado" in configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "999999"}, config=config
    )
    assert "no está autorizado" in configuracion.historial_limites.invoke(
        {}, config=config
    )
    # And nobody outside the staff list can confirm one either: the router only
    # reaches the handler for a phone router.es_equipo authenticated.
    assert main._codigo_de_ajuste("4242", DESCONOCIDO) is None
    assert almacen.hashes == {}


def test_a_manager_cannot_confirm_a_change_somebody_else_proposed(
    almacen: FakeRedis,
) -> None:
    """The pending change belongs to the phone that asked for it.

    Nothing is pending for the other manager, so his four digits are not a
    confirmation at all: the router hands the message to the agent like any
    other. What must not happen is the change being applied."""
    configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "5000"}, config=_gerencia(EQUIPO)
    )

    assert _confirmar(telefono=OTRO_DEL_EQUIPO) == ""
    assert limites.vigente("AUTO_CONFIRM_MAX") == "0"

    # And the manager who DID propose it can still confirm his own.
    assert "0 a 5000" in _confirmar(telefono=EQUIPO)


# ------------------------------------------------------ confirmation and audit
def test_nothing_changes_until_the_owner_writes_the_code(almacen: FakeRedis) -> None:
    propuesta = configuracion.proponer_limite.invoke(
        {"limite": "monto máximo", "valor": "30000"}, config=_gerencia()
    )

    # THE BOUNDARY: the code is not in what the model gets back. A code the
    # model has read is a code the model can supply, and then the second step
    # is the same turn as the first.
    assert "4242" not in propuesta
    assert "0 → 30000" in propuesta
    # It went to the OWNER instead, deterministically, to his own number.
    assert _codigo_enviado(almacen) == "4242"
    assert almacen.enviados[0][0] == EQUIPO
    # Proposed only: the limit in force is still the old one.
    assert limites.vigente("AUTO_CONFIRM_MAX") == "0"
    assert limites.configuracion().tope == 0.0

    aplicado = _confirmar()

    assert "0 a 30000" in aplicado
    assert limites.configuracion().tope == 30_000.0


def test_a_wrong_code_changes_nothing(almacen: FakeRedis) -> None:
    configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "30000"}, config=_gerencia()
    )

    respuesta = _confirmar("1111")

    assert "No apliqué nada" in respuesta
    assert limites.configuracion().tope == 0.0


def test_four_digits_with_nothing_pending_are_just_a_message(
    almacen: FakeRedis,
) -> None:
    """Not every number the owner types is a confirmation code. With nothing
    pending the handler declines to answer, so the message reaches the agent
    instead of getting a confusing "no apliqué nada"."""
    assert main._codigo_de_ajuste("4242", EQUIPO) is None
    assert almacen.hashes == {}


@pytest.mark.parametrize(
    "texto", ["42", "42424", "4242 5", "el código es 4242", "SAL-ORD-2026-00021"]
)
def test_only_four_digits_and_nothing_else_is_read_as_a_code(
    almacen: FakeRedis, texto: str
) -> None:
    _proponer("tope", "30000")

    assert main._codigo_de_ajuste(texto, EQUIPO) is None
    assert limites.configuracion().tope == 0.0


def test_a_pending_change_is_dropped_on_its_own(almacen: FakeRedis) -> None:
    """It expires instead of waiting for ever: a code from last Tuesday must
    not still move a limit."""
    configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "30000"}, config=_gerencia()
    )

    clave = f"{limites.CLAVE_PROPUESTA}:{EQUIPO}"
    assert almacen.ttls[clave] == limites.PROPUESTA_TTL_SEGUNDOS


def test_every_change_records_who_when_and_from_what_to_what(
    almacen: FakeRedis,
) -> None:
    configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "12000"}, config=_gerencia()
    )
    _confirmar()

    entradas = limites.auditoria()
    assert len(entradas) == 1
    entrada = entradas[0]
    assert entrada["limite"] == "AUTO_CONFIRM_MAX"
    assert entrada["anterior"] == "0"
    assert entrada["nuevo"] == "12000"
    assert entrada["telefono"] == EQUIPO
    assert entrada["ts"]

    historial = configuracion.historial_limites.invoke({}, config=_gerencia())
    assert "0 → 12000" in historial
    assert EQUIPO in historial


def test_the_audit_trail_does_not_grow_without_bound(almacen: FakeRedis) -> None:
    almacen.lists[limites.CLAVE_AUDITORIA] = [
        json.dumps({"limite": "AUTO_CONFIRM_MAX", "anterior": str(i), "nuevo": str(i + 1)})
        for i in range(limites.AUDITORIA_MAXIMA + 25)
    ]
    configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "1"}, config=_gerencia()
    )
    _confirmar()

    assert len(almacen.lists[limites.CLAVE_AUDITORIA]) == limites.AUDITORIA_MAXIMA


# ------------------------------------------------------------------ persistence
def test_a_confirmed_change_outlives_the_process_that_made_it(
    almacen: FakeRedis,
) -> None:
    """Nothing is cached in the process: a limit read after the change sees the
    new value, and so would a different worker reading the same store."""
    configuracion.proponer_limite.invoke(
        {"limite": "colchón de stock", "valor": "35"}, config=_gerencia()
    )
    _confirmar()

    assert almacen.hashes[limites.CLAVE_VALORES]["STOCK_BUFFER_PCT"] == "35"
    assert limites.configuracion().buffer == 0.35


def test_what_the_owner_set_beats_the_bootstrap_environment(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The environment is where the system starts, not where it is configured."""
    monkeypatch.setenv("AUTO_CONFIRM_MAX", "1000")
    assert limites.configuracion().tope == 1_000.0

    configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "7500"}, config=_gerencia()
    )
    _confirmar()

    assert limites.configuracion().tope == 7_500.0
    fila = next(f for f in limites.resumen() if f["nombre"] == "AUTO_CONFIRM_MAX")
    assert fila["origen"] == "dueño"


def _sin_redis(motivo: str) -> None:
    """A missing Redis is a skip on a laptop and a FAILURE in CI.

    CI runs a Redis Stack service on purpose. Without this, losing that service
    turns the one test that talks to a real Redis into a silent skip and CI goes
    green having not run it — which is exactly how this suite quietly stopped
    being the whole suite.
    """
    if os.getenv("REDIS_OBLIGATORIO", "").strip():
        pytest.fail(f"REDIS_OBLIGATORIO está puesto y {motivo}")
    pytest.skip(motivo)


def test_a_change_really_survives_in_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one test that talks to a real Redis: writes a limit, drops every
    connection and object, and reads it back."""
    import redis

    from app import router

    if not os.getenv("REDIS_URL", "").strip():
        _sin_redis("no hay REDIS_URL configurada")

    monkeypatch.setattr(router, "STAFF", [EQUIPO])
    monkeypatch.setattr(limites, "_codigo", lambda: "4242")
    clave = "plus-agent:test-limites"
    monkeypatch.setattr(limites, "CLAVE_VALORES", clave)
    monkeypatch.setattr(limites, "CLAVE_AUDITORIA", f"{clave}:auditoria")
    monkeypatch.setattr(limites, "CLAVE_PROPUESTA", f"{clave}:propuesta")
    # Opt out of the in-memory store the autouse fixture installs.
    monkeypatch.setattr(
        locks, "conexion", lambda: redis.Redis.from_url(os.environ["REDIS_URL"])
    )
    try:
        locks.conexion().ping()
    except redis.exceptions.RedisError:
        _sin_redis("Redis no responde")
    # The code now goes out over WhatsApp. No test may reach Meta.
    monkeypatch.setattr(whatsapp, "enviar_mensaje", lambda tel, texto: {"ok": True})
    cliente_directo = redis.Redis.from_url(os.environ["REDIS_URL"])
    try:
        configuracion.proponer_limite.invoke(
            {"limite": "tope", "valor": "4321"}, config=_gerencia()
        )
        _confirmar()

        # A different connection, as another worker would have.
        guardado = cliente_directo.hget(clave, "AUTO_CONFIRM_MAX")
        assert guardado is not None
        assert guardado.decode() == "4321"
        assert limites.configuracion().tope == 4_321.0
    finally:
        cliente_directo.delete(clave, f"{clave}:auditoria", f"{clave}:propuesta:{EQUIPO}")


def _pedido_verde() -> dict:
    return {
        "name": "SO-0001",
        "customer": "CUST-001",
        "docstatus": 0,
        "grand_total": 100.0,
        "selling_price_list": "Standard Selling",
        "currency": "ARS",
        "transaction_date": "2026-08-29",
        "delivery_date": "2026-08-30",
        "discount_amount": 0,
        "base_discount_amount": 0,
        "additional_discount_percentage": 0,
        "items": [
            {
                "item_code": "LECHE-1L",
                "qty": 5,
                "stock_qty": 5,
                "uom": "Unidad",
                "stock_uom": "Unidad",
                "conversion_factor": 1,
                "rate": 20,
                "price_list_rate": 20,
                "discount_percentage": 0,
                "discount_amount": 0,
                "distributed_discount_amount": 0,
                "warehouse": "Depósito A - LP",
            }
        ],
    }


def _todo_verde_menos_los_limites(monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything except the owner's limits passes, so a decision flips only
    when a limit does."""
    from datetime import date

    inventario_confiable(monkeypatch)
    entrega_autorizada(monkeypatch)
    monkeypatch.setattr(policy, "PRICE_LIST", "Standard Selling")
    monkeypatch.setattr(policy, "CURRENCY", "ARS")
    monkeypatch.setattr(policy, "MIN_PEDIDOS", 1)
    monkeypatch.setattr(policy, "_hoy_del_negocio", lambda: date(2026, 8, 29))
    monkeypatch.setattr(policy, "_saldo_vencido", lambda cliente: 0.0)
    monkeypatch.setattr(policy, "_hay_stock", lambda *a, **k: True)
    monkeypatch.setattr(policy, "_precio_estandar", lambda *a, **k: True)
    monkeypatch.setattr(
        policy.erpnext, "get_list", lambda *a, **k: [{"grand_total": 100}]
    )


def test_a_confirmed_change_decides_the_very_next_order_with_no_restart(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of storing limits outside the process: the owner changes one
    from WhatsApp and the next order feels it. No deploy, no restart, and no
    value cached from the first read."""
    _todo_verde_menos_los_limites(monkeypatch)
    monkeypatch.setenv("AUTO_CONFIRM_MAX", "1000")
    monkeypatch.setenv("AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO", "100")

    assert policy.evaluar(_pedido_verde()).auto is True

    configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "50"}, config=_gerencia()
    )
    _confirmar()

    decision = policy.evaluar(_pedido_verde())
    assert decision.auto is False
    assert any("supera el tope" in m for m in decision.motivos)

    # And back up again, in the same process.
    monkeypatch.setattr(limites, "_codigo", lambda: "9999")
    configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "2000"}, config=_gerencia()
    )
    _confirmar("9999")

    assert policy.evaluar(_pedido_verde()).auto is True


def test_the_llm_cannot_decide_a_confirmation_only_the_limits_can(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manager command is text the model interpreted. It can move a number;
    it cannot decide an order. With the ceiling at zero nothing confirms, no
    matter what any tool returns."""
    _todo_verde_menos_los_limites(monkeypatch)
    monkeypatch.setenv("AUTO_CONFIRM_MAX", "0")

    decision = policy.evaluar(_pedido_verde())

    assert decision == policy.Decision(False, ["auto-confirmación desactivada"])


# ------------------------------------------- the durable copy, and data loss
def test_every_change_is_also_recorded_in_erpnext(almacen: FakeRedis) -> None:
    """Redis is fast, not durable enough to be the only record. The copy in
    ERPNext is what survives a wiped container — and what lets this module
    tell "never configured" apart from "the store was lost"."""
    configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "8000"}, config=_gerencia()
    )
    _confirmar()

    limites.erpnext.registrar_comentario.assert_called_once()
    doctype, _nombre, texto = limites.erpnext.registrar_comentario.call_args.args
    assert doctype == "Company"
    assert limites.MARCA_DURABLE in texto
    assert "AUTO_CONFIRM_MAX: 0 -> 8000" in texto
    assert EQUIPO in texto


def test_a_change_that_cannot_be_recorded_durably_is_not_applied(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Better not to move the limit than to move it with no record: a change
    with no durable trace is a change that a restart could silently undo."""
    monkeypatch.setattr(
        limites.erpnext,
        "registrar_comentario",
        Mock(side_effect=limites.erpnext.ERPNextError("ERPNext no disponible")),
    )
    configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "8000"}, config=_gerencia()
    )

    respuesta = _confirmar()

    assert "No apliqué nada" in respuesta
    assert almacen.hashes == {}
    assert limites.configuracion().tope == 0.0


def test_an_empty_store_with_changes_on_record_is_data_loss_not_a_fresh_install(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE failure this guards against: Redis comes back empty after a restart
    and the bootstrap environment quietly restores a LOOSER ceiling than the
    one the owner had set. An empty store plus changes on record in ERPNext
    means the limits are missing, and nothing confirms until they are back."""
    monkeypatch.setenv("AUTO_CONFIRM_MAX", "1000000")  # the looser bootstrap
    monkeypatch.setattr(limites, "_hubo_cambios_durables", lambda: True)

    with pytest.raises(limites.LimiteError) as fallo:
        limites.configuracion()
    assert "restaurarlos" in str(fallo.value)

    decision = policy.evaluar({"name": "SO-1", "customer": "CUST-001"})
    assert decision.auto is False
    assert any("límites sin verificar" in m for m in decision.motivos)


def test_an_empty_store_with_nothing_on_record_is_simply_a_fresh_install(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same emptiness must not block a genuinely new deployment."""
    monkeypatch.setenv("AUTO_CONFIRM_MAX", "1000")
    monkeypatch.setattr(limites, "_hubo_cambios_durables", lambda: False)

    assert limites.configuracion().tope == 1_000.0


def test_the_durable_check_asks_erpnext_for_the_marked_comments(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    lector = Mock(return_value=[{"name": "COMMENT-1"}])
    monkeypatch.setattr(limites.erpnext, "policy_get_list", lector)
    monkeypatch.setattr(limites, "_hubo_cambios_durables", _CONSULTA_DURABLE_REAL)
    monkeypatch.setattr(limites, "_durable_cache", None)

    assert limites._hubo_cambios_durables() is True

    assert lector.call_args.args[0] == "Comment"
    filtros = lector.call_args.kwargs["filters"]
    assert ["reference_doctype", "=", "Company"] in filtros
    assert any(limites.MARCA_DURABLE in str(f[2]) for f in filtros if f[1] == "like")


def test_an_erpnext_that_cannot_be_asked_fails_closed(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"I could not check whether the limits were lost" is not "they were
    not"."""
    monkeypatch.setattr(
        limites.erpnext,
        "policy_get_list",
        Mock(side_effect=limites.erpnext.ERPNextError("caído")),
    )
    monkeypatch.setattr(limites, "_hubo_cambios_durables", _CONSULTA_DURABLE_REAL)
    monkeypatch.setattr(limites, "_durable_cache", None)

    with pytest.raises(limites.LimiteError):
        limites.configuracion()


# ------------------------------------------------------------ invalid and broken
@pytest.mark.parametrize(
    ("limite", "valor"),
    [
        ("tope", "-1"),
        ("tope", "muchisimo"),
        ("tope", "1e999"),
        ("tope", "999999999999"),
        ("cantidad por producto", "-5"),
        ("colchón de stock", "-10"),
        ("colchón de stock", "96"),
        ("deuda", "-1"),
        ("descuentos", "puede ser"),
        ("no existe ese limite", "5"),
    ],
)
def test_an_impossible_value_is_refused_and_nothing_is_stored(
    almacen: FakeRedis, limite: str, valor: str
) -> None:
    respuesta = configuracion.proponer_limite.invoke(
        {"limite": limite, "valor": valor}, config=_gerencia()
    )

    assert "No cambié nada" in respuesta
    assert almacen.hashes == {}
    assert almacen.strings == {}


def test_a_store_that_cannot_be_read_leaves_every_order_pending(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Redis outage must not be readable as "no limits, go ahead", and it
    must not silently fall back to the bootstrap environment either — that
    would undo a limit the owner had tightened."""
    monkeypatch.setenv("AUTO_CONFIRM_MAX", "1000")
    almacen.caido = True

    with pytest.raises(limites.LimiteError):
        limites.configuracion()

    decision = policy.evaluar({"name": "SO-1", "customer": "CUST-001"})
    assert decision.auto is False
    assert any("límites sin verificar" in m for m in decision.motivos)


def test_a_broken_stored_value_is_reported_not_replaced(
    almacen: FakeRedis,
) -> None:
    """Somebody editing Redis by hand must not quietly get a default."""
    almacen.hashes[limites.CLAVE_VALORES] = {"AUTO_CONFIRM_MAX": "cinco mil"}

    with pytest.raises(limites.LimiteError):
        limites.configuracion()

    aviso = configuracion.ver_limites.invoke({}, config=_gerencia())
    assert "mal configurado" in aviso


def test_the_owner_sees_where_each_value_comes_from(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTO_CONFIRM_MAX", "1000")

    vista = configuracion.ver_limites.invoke({}, config=_gerencia())

    assert "valor de arranque" in vista  # AUTO_CONFIRM_MAX, from the env
    assert "default del sistema" in vista  # the ones nobody has touched
    assert "monto maximo" in vista
    assert "cantidad maxima por producto" in vista
    assert "colchon de stock" in vista
    assert "tope cliente nuevo" in vista
    assert "deuda tolerada" in vista
    assert "descuentos" in vista


def test_the_owner_is_told_the_new_customer_ceiling_needs_a_checked_address(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting the ceiling is not the only thing between a new customer and an
    automatic order: the delivery address still has to check out. If we did not
    say so, he would raise the number and wonder why orders still wait."""
    from app import policy

    assert policy.CLIENTE_NUEVO_HABILITADO is True
    monkeypatch.setenv("AUTO_CONFIRM_MAX_CLIENTE_NUEVO", "5000")

    vista = configuracion.ver_limites.invoke({}, config=_gerencia())

    assert "zona de reparto configurada" in vista
    assert "queda en borrador igual" in vista


def test_one_setting_at_a_time(almacen: FakeRedis) -> None:
    """A proposal replaces any earlier one, so a confirmation can only ever
    apply the single change the owner just read back."""
    configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "5000"}, config=_gerencia()
    )
    configuracion.proponer_limite.invoke(
        {"limite": "deuda", "valor": "250"}, config=_gerencia()
    )
    _confirmar()

    almacenados = almacen.hashes[limites.CLAVE_VALORES]
    assert almacenados == {"AUTO_CONFIRM_MAX_DEBT": "250"}


def test_a_dead_store_cannot_be_talked_into_a_change(almacen: FakeRedis) -> None:
    almacen.caido = True

    propuesta = configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "5000"}, config=_gerencia()
    )

    assert "No cambié nada" in propuesta


# ---------------------------------------------------------------------------
# Las reglas de ENTREGA: mismo dueño, mismo código de dos pasos, misma
# auditoría durable — y ningún camino por el que el modelo las aplique solo.
# ---------------------------------------------------------------------------


def _proponer(limite: str, valor: str, telefono: str = EQUIPO) -> str:
    return configuracion.proponer_limite.invoke(
        {"limite": limite, "valor": valor}, config=_gerencia(telefono)
    )


def _confirmar(codigo: str = "4242", telefono: str = EQUIPO) -> str:
    """Confirm the way the owner does: an inbound message with four digits.

    Not a tool. There is no tool for this — the code never reaches the model, so
    the only thing that can apply a change is app/main.py's deterministic
    handler running on a signed webhook from an authenticated staff phone.
    """
    return main._codigo_de_ajuste(codigo, telefono) or ""


# ------------------------------------------------------------- authorization
@pytest.mark.parametrize(
    "config",
    [_cliente(), _gerencia(DESCONOCIDO)],
    ids=["un cliente", "un número desconocido"],
)
def test_only_an_authorized_manager_can_see_or_change_a_delivery_rule(
    almacen: FakeRedis, config: dict
) -> None:
    assert "no está autorizado" in configuracion.ver_reglas_de_entrega.invoke(
        {}, config=config
    )
    configuracion.proponer_limite.invoke(
        {"limite": "días de reparto", "valor": "martes"}, config=config
    )
    _confirmar(telefono=str(config["configurable"].get("actor_phone") or ""))

    assert almacen.hashes == {}
    assert limites.entrega().dias_reparto == ()


def test_a_manager_cannot_confirm_a_delivery_change_somebody_else_proposed(
    almacen: FakeRedis,
) -> None:
    _proponer("días de reparto", "martes y viernes", EQUIPO)

    assert _confirmar(telefono=OTRO_DEL_EQUIPO) == ""
    assert limites.entrega().dias_reparto == ()


# --------------------------------------------------- the confirmation code
def test_nothing_about_delivery_changes_until_the_owner_writes_the_code(
    almacen: FakeRedis,
) -> None:
    propuesta = _proponer("días de reparto", "martes y viernes")

    assert "4242" not in propuesta
    assert _codigo_enviado(almacen) == "4242"
    assert "martes,viernes" in propuesta
    # Proposed only.
    assert limites.entrega().dias_reparto == ()
    assert limites.vigente("ENTREGA_DIAS") == limites.NINGUNO

    aplicado = _confirmar()

    assert "días de reparto" in aplicado
    assert limites.entrega().dias_reparto == (1, 4)  # martes, viernes


def test_a_wrong_code_changes_no_delivery_rule(almacen: FakeRedis) -> None:
    _proponer("hora de reparto", "8")

    assert "No apliqué nada" in _confirmar("1111")
    assert limites.entrega().hora_reparto == ""


def test_the_llm_can_propose_a_delivery_rule_but_never_apply_one(
    almacen: FakeRedis,
) -> None:
    """The whole boundary in one test: every tool the management agent can
    call, and none of them moves a setting without the owner's own code."""
    from app import graph

    assert {t.name for t in graph.TOOLS_GERENCIA if "limite" in t.name} == {
        "ver_limites",
        "proponer_limite",
        "historial_limites",
    }
    # There is no confirm tool at all — not unregistered, ABSENT.
    assert not hasattr(configuracion, "confirmar_limite")

    # Reading changes nothing; proposing changes nothing.
    configuracion.ver_reglas_de_entrega.invoke({}, config=_gerencia())
    _proponer("retiro en el local", "sí")
    assert almacen.hashes == {}
    assert limites.entrega().retiro_activo is False

    _confirmar()
    assert limites.entrega().retiro_activo is True


# ------------------------------------------------------------------- audit
def test_every_delivery_change_is_audited_in_redis_and_in_erpnext(
    almacen: FakeRedis,
) -> None:
    _proponer("hora de retiro", "10:30")
    _confirmar()

    entrada = limites.auditoria()[0]
    assert entrada["limite"] == "RETIRO_LOCAL_HORA"
    assert entrada["anterior"] == limites.NINGUNO
    assert entrada["nuevo"] == "10:30"
    assert entrada["telefono"] == EQUIPO
    assert entrada["ts"]

    texto = limites.erpnext.registrar_comentario.call_args[0][2]
    # The DELIVERY marker, not the limits one. This assertion used to say
    # MARCA_DURABLE, which is precisely the coupling that let a schedule edit
    # arm the auto-confirm tripwire for ever.
    assert limites.MARCA_DURABLE_ENTREGA in texto
    assert limites.MARCA_DURABLE not in texto
    assert "RETIRO_LOCAL_HORA" in texto and "10:30" in texto and EQUIPO in texto

    historial = configuracion.historial_limites.invoke({}, config=_gerencia())
    assert "hora de retiro" in historial


def test_a_delivery_change_that_cannot_be_recorded_durably_is_not_applied(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rule as the limits: an audit that only lives in Redis disappears
    with Redis, and then a wiped store looks like a fresh install."""
    _proponer("días de reparto", "lunes")
    monkeypatch.setattr(
        limites.erpnext,
        "registrar_comentario",
        Mock(side_effect=limites.erpnext.ERPNextError("no")),
    )

    assert "No apliqué nada" in _confirmar()
    assert almacen.hashes == {}
    assert limites.entrega().dias_reparto == ()


# ------------------------------------------------- resolution order, restart
def test_what_the_owner_set_beats_the_bootstrap_environment_for_delivery(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENTREGA_DIAS", "lunes")
    assert limites.entrega().dias_reparto == (0,)  # bootstrap

    _proponer("días de reparto", "jueves")
    _confirmar()

    assert limites.entrega().dias_reparto == (3,)  # the owner wins
    filas = {f["nombre"]: f for f in limites.resumen()}
    assert filas["ENTREGA_DIAS"]["origen"] == "dueño"


def test_with_nothing_set_anywhere_a_delivery_rule_is_simply_unconfigured(
    almacen: FakeRedis,
) -> None:
    reglas = limites.entrega()

    assert reglas.dias_reparto == () and reglas.hora_reparto == ""
    assert reglas.excepcion_activa is False and reglas.retiro_activo is False
    assert reglas.excepcion_cargo is None and reglas.excepcion_minimo == 0.0
    filas = {f["nombre"]: f for f in limites.resumen()}
    assert filas["ENTREGA_DIAS"]["origen"] == "default"
    assert filas["ENTREGA_DIAS"]["problema"] == ""


def test_a_confirmed_delivery_rule_outlives_the_process_that_set_it(
    almacen: FakeRedis,
) -> None:
    _proponer("días de reparto", "martes")
    _confirmar()

    # A brand new store object over the SAME data: nothing cached in-process.
    from app import locks as _locks

    sobreviviente = FakeRedis(hashes=almacen.hashes, lists=almacen.lists)
    _locks.conexion = lambda: sobreviviente

    assert limites.entrega().dias_reparto == (1,)
    assert limites.vigente("ENTREGA_DIAS") == "martes"


def test_a_change_applies_to_the_very_next_operation_with_no_restart(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read per operation: app/excepciones.py asks again every time."""
    from app import excepciones

    entrega_autorizada(monkeypatch)
    assert excepciones.evaluar_respaldo({}, hoy=date(2026, 9, 7)).preautorizada is False

    _proponer("días de reparto", "martes")
    _confirmar()
    _proponer("hora de reparto", "08:00")
    _confirmar()

    evaluacion = excepciones.evaluar_respaldo({}, hoy=date(2026, 9, 7))
    assert evaluacion.preautorizada is True
    assert evaluacion.oferta.fecha == "2026-09-08"
    assert evaluacion.oferta.hora == "08:00"


def test_a_store_that_cannot_be_read_offers_no_delivery_at_all(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENTREGA_DIAS", "martes")
    monkeypatch.setenv("ENTREGA_HORA", "08:00")
    almacen.caido = True

    reglas = limites.entrega()

    assert reglas.dias_reparto == () and reglas.hora_reparto == ""
    # ...and it does NOT raise into the deterministic path, unlike the limits:
    # a delivery-rule outage must not stop every order from confirming.
    from app import excepciones

    assert excepciones.dias_reparto() == []
    assert excepciones.activa() is False


# ------------------------------------------------------------ malformed values
@pytest.mark.parametrize(
    "limite,valor,esperado",
    [
        ("días de reparto", "lunez", "no es un día de la semana"),
        ("días de reparto", "32", "no es un día de la semana"),
        ("hora de reparto", "25:00", "una hora tipo 08:00"),
        ("hora de reparto", "mañana", "una hora tipo 08:00"),
        ("hora de retiro", "8:75", "una hora tipo 08:00"),
        ("retiro en el local", "quizás", "tiene que ser sí o no"),
        ("cargo fuera de día", "gratis", "no es un número"),
        ("mínimo fuera de día", "-500", "no puede ser menor que 0"),
        ("cargo fuera de día", "99999999", "es imposible"),
    ],
)
def test_a_malformed_delivery_value_is_refused_and_nothing_is_stored(
    almacen: FakeRedis, limite: str, valor: str, esperado: str
) -> None:
    respuesta = _proponer(limite, valor)

    assert "No cambié nada" in respuesta
    assert esperado in respuesta
    assert almacen.strings == {} and almacen.hashes == {}


@pytest.mark.parametrize(
    "dicho,guardado",
    [
        ("martes y viernes", "martes,viernes"),
        ("Miércoles, Sábado", "miercoles,sabado"),
        ("viernes martes", "martes,viernes"),
        ("MARTES,martes", "martes"),
    ],
)
def test_however_the_owner_writes_the_days_one_normal_form_is_stored(
    almacen: FakeRedis, dicho: str, guardado: str
) -> None:
    _proponer("días de reparto", dicho)
    _confirmar()

    assert limites.vigente("ENTREGA_DIAS") == guardado


@pytest.mark.parametrize(
    "dicho,guardado", [("8", "08:00"), ("9:30", "09:30"), ("18.00", "18:00"), ("7 hs", "07:00")]
)
def test_however_the_owner_writes_the_time_one_normal_form_is_stored(
    almacen: FakeRedis, dicho: str, guardado: str
) -> None:
    _proponer("hora de reparto", dicho)
    _confirmar()

    assert limites.vigente("ENTREGA_HORA") == guardado


def test_a_setting_can_be_cleared_which_an_empty_value_cannot_do(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty string in the store reads as "unset" and falls back to the
    bootstrap environment, so "borrá los días" needs a real sentinel."""
    monkeypatch.setenv("ENTREGA_DIAS", "lunes")
    _proponer("días de reparto", "jueves")
    _confirmar()
    assert limites.entrega().dias_reparto == (3,)

    _proponer("días de reparto", "ninguno")
    _confirmar()

    assert limites.vigente("ENTREGA_DIAS") == limites.NINGUNO
    assert limites.entrega().dias_reparto == ()  # NOT back to the .env's lunes


def test_a_broken_bootstrap_value_reads_as_unconfigured_not_as_an_offer(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An environment variable never went through the confirmation code, so it
    is re-validated on the way out."""
    monkeypatch.setenv("ENTREGA_DIAS", "lunes,jueevs")
    monkeypatch.setenv("ENTREGA_HORA", "08:00")

    assert limites.entrega().dias_reparto == ()
    filas = {f["nombre"]: f for f in limites.resumen()}
    assert "no es un día de la semana" in filas["ENTREGA_DIAS"]["problema"]


def test_a_seven_digit_amount_is_stored_exactly(almacen: FakeRedis) -> None:
    """At :g a 1234567 value is stored as "1.23457e+06" and reads back as
    1234570 — silently rounding the owner's money."""
    _proponer("mínimo fuera de día", "1234567")
    _confirmar()

    assert limites.vigente("ENTREGA_EXCEPCION_MIN_TOTAL") == "1234567"
    assert limites.entrega().excepcion_minimo == 1234567.0
    assert "e+" not in limites.auditoria()[0]["nuevo"]


# --------------------------------------------------------- ambiguity, safety
def test_an_ambiguous_name_is_asked_about_instead_of_guessed(
    almacen: FakeRedis,
) -> None:
    """"hora" matches the approval timeout, the review deadline and four
    delivery times. Picking the first would let a vague word from the model
    move a setting the owner never mentioned."""
    respuesta = _proponer("hora", "08:00")

    assert "puede ser varias cosas" in respuesta
    assert "hora de reparto" in respuesta
    assert almacen.hashes == {}


def test_the_accounting_account_is_not_reachable_by_natural_language(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong account head does not break the bot — it unbalances the owner's
    books, and no model can check that an account exists."""
    assert limites.CUENTA_CARGO not in limites.TODOS
    assert not [
        d for d in limites.TODOS.values() if "cuenta" in " ".join(d.alias)
    ]

    for dicho in ("cuenta contable", "cuenta del cargo", "ENTREGA_CARGO_CUENTA"):
        respuesta = _proponer(dicho, "Fletes - LT")
        assert "No cambié nada" in respuesta

    assert almacen.hashes == {}
    # It is still configurable, on the server, where a person can check it.
    monkeypatch.setenv("ENTREGA_CARGO_CUENTA", "Fletes - LT")
    assert limites.cuenta_cargo() == "Fletes - LT"


def test_a_delivery_rule_cannot_stop_an_order_from_confirming(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the delivery settings are a separate registry: configuracion() runs
    once per order LINE and raises on the first bad value, so a typo in
    "martes" would have become an outage for every customer."""
    monkeypatch.setenv("ENTREGA_DIAS", "lunez")
    monkeypatch.setenv("ENTREGA_HORA", "no es una hora")

    assert limites.configuracion().tope == 0.0  # reads fine
    assert set(limites.ENTREGA) & set(limites.LIMITES) == set()
    for campo in limites.Configuracion.__dataclass_fields__:
        assert "entrega" not in campo and "retiro" not in campo


def test_one_delivery_setting_at_a_time(almacen: FakeRedis) -> None:
    """Two proposals from one phone: the second replaces the first, and only
    the confirmed one is stored."""
    _proponer("días de reparto", "martes")
    _proponer("hora de reparto", "08:00")

    _confirmar()

    assert almacen.hashes[limites.CLAVE_VALORES] == {"ENTREGA_HORA": "08:00"}
    assert limites.entrega().dias_reparto == ()


def test_a_dead_store_cannot_be_talked_into_a_delivery_change(
    almacen: FakeRedis,
) -> None:
    almacen.caido = True

    assert "No cambié nada" in _proponer("días de reparto", "martes")
    # Nothing could be written, so nothing is pending, so four digits are not a
    # confirmation. A dead store fails closed in BOTH steps.
    assert _confirmar() == ""
    assert limites.entrega().dias_reparto == ()


@pytest.mark.parametrize(
    "dicho,guardado",
    [
        ("1.500", "1500"),
        ("1.234.567", "1234567"),
        ("$ 2.000", "2000"),
        ("1500", "1500"),
        ("1500,50", "1500.5"),
        ("1.500,50", "1500.5"),
        ("1,5", "1.5"),
    ],
)
def test_money_reads_the_way_the_owner_writes_it(
    almacen: FakeRedis, dicho: str, guardado: str
) -> None:
    """"1.500" is fifteen hundred pesos to him and 1.5 to float(). The manager
    already types a delivery fee that way for a counter-offer
    (solicitudes.parsear_terminos strips the dots), so a setting must agree —
    otherwise the same three keystrokes mean two different amounts."""
    _proponer("mínimo fuera de día", dicho)
    _confirmar()

    assert limites.vigente("ENTREGA_EXCEPCION_MIN_TOTAL") == guardado


@pytest.mark.parametrize("dicho,esperado", [("1.5", 1.5), ("20", 20.0), ("1,5", 1.5)])
def test_a_percentage_confirmed_end_to_end_is_never_regrouped(
    almacen: FakeRedis, dicho: str, esperado: float
) -> None:
    """The thousands rule is for money only: 1.5% is one and a half percent.

    Renamed: this shared a name with the validar()-level test further down, so
    the second definition shadowed it and this one never ran. ruff cannot see
    it — tests/* ignores F811, because a fixture used as an argument reads as a
    redefinition. See test_no_test_in_this_suite_is_shadowed_by_another.
    """
    _proponer("colchón de stock", dicho)
    _confirmar()

    assert limites.configuracion().buffer == pytest.approx(esperado / 100.0)


# ---------------------------------------------------------------------------
# El valor que el dueño confirmó es el valor que se guarda.
#
# La regla de miles ("1.500" son mil quinientos pesos) NO es idempotente: al
# normalizar deja "1.125", y volver a normalizar ESO lee el punto como
# separador de miles y da 1125. proponer() normaliza una vez y aplicar()
# volvía a normalizar, así que el dueño confirmaba un número y se guardaba
# otro mil veces más grande — y en AUTO_CONFIRM_MAX eso ensancha por mil el
# tope de lo que se confirma sin que lo mire nadie.
# ---------------------------------------------------------------------------

# Los ajustes a los que se les aplica la regla de miles, sacados del código y
# no de una lista escrita a mano: si mañana aparece un séptimo, este test lo
# incluye solo en vez de dejarlo sin cubrir.
CON_MILES = tuple(
    nombre for nombre, defi in limites.TODOS.items() if defi.unidad in limites._CON_MILES
)


def test_the_thousands_rule_covers_exactly_the_six_settings_this_file_tests() -> None:
    """Pinned so the coverage claim below cannot go stale in silence."""
    assert set(CON_MILES) == {
        "AUTO_CONFIRM_MAX",
        "AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO",
        "AUTO_CONFIRM_MAX_CLIENTE_NUEVO",
        "AUTO_CONFIRM_MAX_DEBT",
        "ENTREGA_EXCEPCION_CARGO",
        "ENTREGA_EXCEPCION_MIN_TOTAL",
    }


@pytest.mark.parametrize("nombre", CON_MILES)
@pytest.mark.parametrize(
    "dicho,canonico",
    [
        ("1,125", "1.125"),
        ("0,125", "0.125"),
        ("9,999", "9.999"),
        ("999,125", "999.125"),
    ],
)
def test_a_decimal_amount_is_stored_as_the_owner_confirmed_it(
    almacen: FakeRedis, nombre: str, dicho: str, canonico: str
) -> None:
    """propose -> code -> confirm -> read back, for every setting with miles.

    "1,125" is one peso doce en es-AR. It normalizes to "1.125", and the whole
    bug is that re-normalizing THAT yields 1125: he confirms one number and a
    thousandfold different one is stored, audited and used to decide orders.
    """
    respuesta = _proponer(nombre, dicho)
    assert canonico in respuesta, respuesta

    codigo = _codigo_enviado(almacen)
    assert _confirmar(codigo) != ""

    assert limites.vigente(nombre) == canonico
    assert float(limites.vigente(nombre)) == float(canonico)
    # And the audit says the same number, in both copies.
    assert limites.auditoria()[0]["nuevo"] == canonico
    texto_durable = limites.erpnext.registrar_comentario.call_args.args[2]
    assert f"-> {canonico} " in texto_durable, texto_durable


@pytest.mark.parametrize("nombre", CON_MILES)
@pytest.mark.parametrize(
    "dicho",
    ["1,125", "0,125", "9,999", "999,125", "1.000", "1.500,50", "1500", "1,5", "999"],
)
def test_normalizing_an_already_normalized_amount_changes_nothing(
    nombre: str, dicho: str
) -> None:
    """validar() must be a fixed point on its own output.

    This is the property the propose/confirm path depends on, stated directly:
    whatever the owner typed, normalizing the RESULT again may not move it.
    """
    canonico = limites.validar(nombre, dicho)
    assert limites.validar(nombre, canonico, tecleado=False) == canonico
    # And re-reading a stored value is the same read the second time too.
    assert (
        limites.validar(nombre, limites.validar(nombre, canonico, tecleado=False), tecleado=False)
        == canonico
    )


@pytest.mark.parametrize("nombre", CON_MILES)
@pytest.mark.parametrize("dicho,esperado", [("1.000", "1000"), ("1.500", "1500")])
def test_a_typed_thousands_amount_still_means_thousands(
    nombre: str, dicho: str, esperado: str
) -> None:
    """The fix must not cost the rule it is protecting: "1.000" typed by a
    person is a thousand pesos, and a thousand litres, exactly as before."""
    assert limites.validar(nombre, dicho) == esperado


@pytest.mark.parametrize(
    "nombre",
    [
        nombre
        for nombre, defi in limites.TODOS.items()
        if defi.unidad in ("%", "h") and not defi.opcional
    ],
)
@pytest.mark.parametrize("dicho,esperado", [("1.5", "1.5"), ("1,5", "1.5"), ("20", "20")])
def test_a_percentage_or_an_hour_count_is_never_regrouped_in_either_reading(
    nombre: str, dicho: str, esperado: str
) -> None:
    """1.5% is one and a half per cent, and 1.5 h is ninety minutes. Nobody
    groups either with a dot, so the thousands rule must not reach them.

    Distinct from the validar()-level test of the same idea further down: this
    one asserts it for BOTH readings, typed and canonical. Same name would
    shadow it and ruff cannot see that — see
    test_no_test_in_this_suite_is_shadowed_by_another.
    """
    assert limites.validar(nombre, dicho) == esperado
    assert limites.validar(nombre, esperado, tecleado=False) == esperado


def test_a_stored_decimal_is_read_back_as_the_number_it_says(
    almacen: FakeRedis,
) -> None:
    """The read path has to agree with the write path.

    A canonical "1.125" sitting in the store is one peso twelve. Re-grouping it
    on the way OUT would hand app/policy.py and app/excepciones.py a number the
    owner never confirmed, with no change ever being applied.
    """
    almacen.hashes[limites.CLAVE_VALORES] = {
        "AUTO_CONFIRM_MAX": "1.125",
        "ENTREGA_EXCEPCION_CARGO": "1.125",
    }

    assert limites.vigente("AUTO_CONFIRM_MAX") == "1.125"
    assert limites.configuracion().tope == 1.125
    assert limites.entrega().excepcion_cargo == 1.125
    fila = next(f for f in limites.resumen() if f["nombre"] == "AUTO_CONFIRM_MAX")
    assert fila["valor"] == "1.125"


def test_a_bootstrap_environment_amount_is_still_read_as_typed(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The .env is written by a person, so it keeps the typed reading: nobody
    puts a canonical float in there, they put "1.000" meaning a thousand."""
    monkeypatch.setenv("AUTO_CONFIRM_MAX", "1.000")

    assert limites.configuracion().tope == 1000.0
    fila = next(f for f in limites.resumen() if f["nombre"] == "AUTO_CONFIRM_MAX")
    assert fila["origen"] == "arranque"
    assert fila["valor"] == "1000"


# ---------------------------------------------------------------------------
# Dos marcas durables, porque son dos hechos distintos.
#
# «Se perdió el almacén de límites» tiene que frenar las confirmaciones
# automáticas. «Se perdió el almacén de reglas de entrega» NO: cuesta un
# mensaje de WhatsApp. Con una sola marca, cambiar los días de reparto una vez
# dejaba armado el fusible de los límites para siempre.
# ---------------------------------------------------------------------------


def _historia_durable(
    monkeypatch: pytest.MonkeyPatch, *, limite: bool, entrega: bool
) -> Mock:
    """Lo que ERPNext contesta a «¿se configuró algo alguna vez?», por marca.

    Instala las consultas REALES: lo que se prueba acá es justamente qué marca
    pregunta cada una.
    """

    def lector(doctype, filters=None, fields=None, limit=None, **kwargs):
        buscado = next((str(f[2]) for f in (filters or []) if f[1] == "like"), "")
        if limites.MARCA_DURABLE_ENTREGA in buscado:
            return [{"name": "COMMENT-ENTREGA"}] if entrega else []
        if limites.MARCA_DURABLE in buscado:
            return [{"name": "COMMENT-LIMITE"}] if limite else []
        return []

    espia = Mock(side_effect=lector)
    monkeypatch.setattr(limites.erpnext, "policy_get_list", espia)
    monkeypatch.setattr(limites, "_hubo_cambios_durables", _CONSULTA_DURABLE_REAL)
    monkeypatch.setattr(
        limites, "_hubo_cambios_durables_entrega", _CONSULTA_ENTREGA_REAL
    )
    monkeypatch.setattr(limites, "_durable_cache", None)
    monkeypatch.setattr(limites, "_durable_cache_entrega", None)
    return espia


def _entorno_de_reparto(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bootstrap .env that offers MORE than an empty store would."""
    monkeypatch.setenv("ENTREGA_DIAS", "lunes,martes,miercoles,jueves,viernes")
    monkeypatch.setenv("ENTREGA_HORA", "09:00")
    monkeypatch.setenv("ENTREGA_EXCEPCION_ACTIVA", "sí")
    monkeypatch.setenv("ENTREGA_EXCEPCION_DIAS", "sabado,domingo")
    monkeypatch.setenv("ENTREGA_EXCEPCION_HORA", "10:00")


def test_a_delivery_only_history_does_not_arm_the_auto_confirm_tripwire(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE bug this commit fixes.

    He changed his delivery days months ago. Redis is lost today. With one
    shared marker, ERPNext answered "yes, something was configured" and every
    order stopped confirming because of a SCHEDULE edit.
    """
    monkeypatch.setenv("AUTO_CONFIRM_MAX", "1000")
    _historia_durable(monkeypatch, limite=False, entrega=True)
    almacen.hashes.clear()

    assert limites.configuracion().tope == 1_000.0
    decision = policy.evaluar({"name": "SO-1", "customer": "CUST-001"})
    assert not any("límites sin verificar" in m for m in decision.motivos)


def test_a_limit_only_history_still_fails_closed_after_a_wipe(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The protection that must NOT be lost in the split: a real limit change
    plus an empty store is data loss, and nothing confirms until it is back."""
    monkeypatch.setenv("AUTO_CONFIRM_MAX", "1000000")  # the looser bootstrap
    _historia_durable(monkeypatch, limite=True, entrega=False)
    almacen.hashes.clear()

    with pytest.raises(limites.LimiteError) as fallo:
        limites.configuracion()
    assert "restaurarlos" in str(fallo.value)

    decision = policy.evaluar({"name": "SO-1", "customer": "CUST-001"})
    assert decision.auto is False
    assert any("límites sin verificar" in m for m in decision.motivos)


def test_both_histories_still_fail_closed_after_a_wipe(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivery change does not EXCUSE a lost limit."""
    monkeypatch.setenv("AUTO_CONFIRM_MAX", "1000000")
    _historia_durable(monkeypatch, limite=True, entrega=True)
    almacen.hashes.clear()

    with pytest.raises(limites.LimiteError):
        limites.configuracion()


def test_no_history_at_all_is_a_fresh_install_for_both_registries(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty store with nothing on record is a new deployment, and the
    bootstrap environment is exactly what it is for — in BOTH registries."""
    monkeypatch.setenv("AUTO_CONFIRM_MAX", "1000")
    _entorno_de_reparto(monkeypatch)
    _historia_durable(monkeypatch, limite=False, entrega=False)
    almacen.hashes.clear()

    assert limites.configuracion().tope == 1_000.0
    reglas = limites.entrega()
    assert reglas.dias_reparto == (0, 1, 2, 3, 4)
    assert reglas.excepcion_activa is True


def test_a_wiped_delivery_store_offers_nothing_instead_of_the_environment(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing soft must not mean failing OPEN.

    Everything in Entrega only widens what the system offers by itself, so a
    wiped store falling back to the .env would restore a round he moved and an
    exception he turned off — and pre-authorise deliveries nobody authorised.
    Offering nothing costs one WhatsApp message to a person.
    """
    _entorno_de_reparto(monkeypatch)
    _historia_durable(monkeypatch, limite=False, entrega=True)
    almacen.hashes.clear()

    reglas = limites.entrega()
    assert reglas.dias_reparto == ()
    assert reglas.hora_reparto == ""
    assert reglas.excepcion_activa is False
    assert reglas.excepcion_dias == ()
    assert reglas.retiro_activo is False


def test_a_delivery_rule_the_owner_set_survives_a_restart_with_an_empty_redis(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And the store being POPULATED is what says the rules are his.

    A restart that still has Redis reads his days, not the .env's — the
    durable-marker question is only asked when the store has nothing.
    """
    _entorno_de_reparto(monkeypatch)
    _proponer("días de reparto", "martes y viernes")
    _confirmar(_codigo_enviado(almacen))
    _historia_durable(monkeypatch, limite=False, entrega=True)

    # A "restart": nothing in memory, the same store, the same ERPNext.
    assert limites.entrega().dias_reparto == (1, 4)


def test_an_erpnext_that_cannot_be_asked_about_delivery_offers_nothing(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"I could not check" is not "nothing was configured" — and it is not an
    exception either: a delivery rule must never be an outage."""
    _entorno_de_reparto(monkeypatch)
    monkeypatch.setattr(
        limites.erpnext,
        "policy_get_list",
        Mock(side_effect=limites.erpnext.ERPNextError("caído")),
    )
    monkeypatch.setattr(
        limites, "_hubo_cambios_durables_entrega", _CONSULTA_ENTREGA_REAL
    )
    monkeypatch.setattr(limites, "_durable_cache_entrega", None)
    almacen.hashes.clear()

    reglas = limites.entrega()
    assert reglas.dias_reparto == ()
    assert reglas.excepcion_activa is False


def test_readiness_agrees_with_the_decision_path_after_a_wipe(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the owner is SHOWN has to match what the system will DO.

    After a wipe the decision path offers nothing. resumen() used to resolve
    the delivery rows from the bootstrap environment anyway, and TWO surfaces
    read resumen(): readiness (make check-env) and ver_reglas_de_entrega, the
    tool the owner asks «qué días reparto». He was shown a Mon-Fri round the
    system would not offer, and never told his rules were gone — the one thing
    he needed in order to set them again. Now resumen() asks the same durable
    question entrega() asks and reports the rows as LOST.
    """
    _entorno_de_reparto(monkeypatch)
    _historia_durable(monkeypatch, limite=False, entrega=True)
    almacen.hashes.clear()

    # The decision path offers nothing, and that is what makes the loss safe.
    assert limites.entrega().dias_reparto == ()
    assert limites.entrega().excepcion_activa is False

    # The display says the same thing.
    fila = next(f for f in limites.resumen() if f["nombre"] == "ENTREGA_DIAS")
    assert fila["valor"] == ""
    assert fila["origen"] == limites.PERDIDO
    assert "se perdieron" in fila["problema"] and "vuelvas a fijar" in fila["problema"]


def test_after_a_wipe_every_delivery_row_reads_as_lost_and_no_limit_does(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loss is the whole delivery registry, and ONLY that registry."""
    monkeypatch.setenv("AUTO_CONFIRM_MAX", "1000")
    _entorno_de_reparto(monkeypatch)
    _historia_durable(monkeypatch, limite=False, entrega=True)
    almacen.hashes.clear()

    filas = {f["nombre"]: f for f in limites.resumen()}

    for nombre in limites.ENTREGA:
        assert (filas[nombre]["valor"], filas[nombre]["origen"]) == ("", limites.PERDIDO), nombre
        assert filas[nombre]["problema"] == limites.PROBLEMA_ENTREGA_PERDIDA
    # A delivery loss does not touch the limits: same answer as configuracion().
    assert (filas["AUTO_CONFIRM_MAX"]["valor"], filas["AUTO_CONFIRM_MAX"]["origen"]) == (
        "1000",
        "arranque",
    )
    assert limites.configuracion().tope == 1_000.0


def test_a_delivery_rule_in_the_store_is_never_reported_as_lost(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store being POPULATED is what says the rules are his — for the
    display exactly as for the decision."""
    _entorno_de_reparto(monkeypatch)
    _proponer("días de reparto", "martes y viernes")
    _confirmar(_codigo_enviado(almacen))
    _historia_durable(monkeypatch, limite=False, entrega=True)

    fila = next(f for f in limites.resumen() if f["nombre"] == "ENTREGA_DIAS")

    assert (fila["valor"], fila["origen"], fila["problema"]) == ("martes,viernes", "dueño", "")
    assert limites.entrega().dias_reparto == (1, 4)


def test_readiness_reports_lost_delivery_rules_as_a_failure_not_as_configured(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """make check-env reads resumen(). It must say the rules were lost — never
    OK for a round taken from the .env that the system will not run."""
    from app import readiness

    _entorno_de_reparto(monkeypatch)
    _historia_durable(monkeypatch, limite=False, entrega=True)
    almacen.hashes.clear()

    reporte = readiness.Reporte()
    readiness.chequear_entrega(os.environ, reporte, limites.resumen, http=None)
    niveles = {clave: nivel for nivel, clave, _ in reporte.lineas}
    texto = reporte.texto()

    assert not reporte.listo
    assert niveles["Entrega"] == readiness.ERROR
    assert "se PERDIERON" in texto and "vuelva a fijar" in texto
    assert "ENTREGA_DIAS" not in niveles  # no OK line for a round that will not run
    assert "lunes" not in texto  # the .env days are not shown as active
    assert "Respaldo de vencimiento" in texto  # and the consequence is spelled out
    assert "mal configurado" not in texto  # a loss is not ten typos


def test_the_owner_tool_says_the_delivery_rules_have_to_be_set_again(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ver_reglas_de_entrega is where he asks «qué días reparto». The answer
    has to be the loss, in words, and what to do about it."""
    _entorno_de_reparto(monkeypatch)
    _historia_durable(monkeypatch, limite=False, entrega=True)
    almacen.hashes.clear()

    vista = configuracion.ver_reglas_de_entrega.invoke({}, config=_gerencia())

    assert "Se perdieron tus reglas de entrega" in vista
    assert "vuelvas a fijar" in vista
    assert "sin valor vigente" in vista and "se perdió del almacén" in vista
    assert "lunes" not in vista and "valor de arranque" not in vista
    assert "mal configurado" not in vista


def test_a_proposal_after_a_wipe_shows_nothing_as_the_previous_value(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The «anterior» in a proposal and in the audit is what is in effect —
    nothing — not the .env's Mon-Fri."""
    _entorno_de_reparto(monkeypatch)
    _historia_durable(monkeypatch, limite=False, entrega=True)
    almacen.hashes.clear()

    respuesta = _proponer("días de reparto", "martes y viernes")

    assert "lunes" not in respuesta
    assert f"{limites.NINGUNO} → martes,viernes" in respuesta


def test_a_limit_change_is_recorded_under_the_limit_marker(
    almacen: FakeRedis,
) -> None:
    """The other half of the split: a real limit change still arms the
    tripwire, so the protection is not gone, only narrowed to what it is for."""
    _proponer("tope", "30000")
    _confirmar(_codigo_enviado(almacen))

    texto = limites.erpnext.registrar_comentario.call_args[0][2]
    assert limites.MARCA_DURABLE in texto
    assert limites.MARCA_DURABLE_ENTREGA not in texto


def test_the_two_durable_questions_ask_erpnext_for_different_markers(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stated directly, because the whole fix is which marker each one reads."""
    espia = _historia_durable(monkeypatch, limite=False, entrega=True)

    assert limites._hubo_cambios_durables() is False
    assert limites._hubo_cambios_durables_entrega() is True

    preguntados = [
        str(f[2])
        for llamada in espia.call_args_list
        for f in llamada.kwargs["filters"]
        if f[1] == "like"
    ]
    assert any(limites.MARCA_DURABLE in q for q in preguntados)
    assert any(limites.MARCA_DURABLE_ENTREGA in q for q in preguntados)


# ---------------------------------------------------------------------------
# Lo que salió de la revisión adversarial de las reglas de entrega.
# ---------------------------------------------------------------------------


def test_an_unreadable_order_minimum_blocks_the_exception_instead_of_removing_it(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one delivery setting that NARROWS. Failing soft on it fails OPEN.

    Every other value in Entrega can only widen what the system offers, so an
    unreadable one offers less. The order minimum is what stops a $200 order
    earning a free off-day trip, so losing it has to block the exception.
    """
    from app import excepciones

    entrega_autorizada(monkeypatch)
    almacen.hashes[limites.CLAVE_VALORES] = {
        "ENTREGA_EXCEPCION_ACTIVA": "true",
        "ENTREGA_EXCEPCION_DIAS": "sabado",
        "ENTREGA_EXCEPCION_HORA": "10:00",
        "ENTREGA_EXCEPCION_CARGO": "1500",
        "ENTREGA_EXCEPCION_MIN_TOTAL": "ocho mil",
    }

    assert limites.entrega().excepcion_minimo is None

    evaluacion = excepciones.evaluar_entrega({"grand_total": 200})

    assert evaluacion.preautorizada is False
    assert "no se pudo leer" in evaluacion.motivo
    # ...and a big order does not get through either: the rule is unknown, not
    # absent, so nothing is pre-authorized until a person fixes the value.
    assert excepciones.evaluar_entrega({"grand_total": 999_999}).preautorizada is False


def test_no_minimum_configured_still_means_no_minimum(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0 and "unreadable" must not be confused: 0 is a real answer."""
    from app import excepciones

    entrega_autorizada(monkeypatch)
    almacen.hashes[limites.CLAVE_VALORES] = {
        "ENTREGA_EXCEPCION_ACTIVA": "true",
        "ENTREGA_EXCEPCION_DIAS": "sabado",
        "ENTREGA_EXCEPCION_HORA": "10:00",
        "ENTREGA_EXCEPCION_CARGO": "1500",
        "ENTREGA_EXCEPCION_MIN_TOTAL": "0",
    }

    assert limites.entrega().excepcion_minimo == 0.0
    assert excepciones.evaluar_entrega({"grand_total": 200}).preautorizada is True


def test_a_thousands_separator_is_read_the_same_way_for_everything_countable(
    almacen: FakeRedis,
) -> None:
    """"1.000" is a thousand litres exactly as much as a thousand pesos."""
    assert limites.validar("AUTO_CONFIRM_MAX", "50.000") == "50000"
    assert limites.validar("AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO", "1.000") == "1000"
    assert limites.validar("AUTO_CONFIRM_MAX_DEBT", "2.000.000") == "2000000"


@pytest.mark.parametrize(
    "nombre,dicho,esperado",
    [
        # Nobody groups a percentage in es-AR, so the dot is a decimal point.
        ("STOCK_BUFFER_PCT", "12.5", "12.5"),
        ("AUTO_CONFIRM_MAX_DESCUENTO_PCT", "7.5", "7.5"),
        # Neither is an hour count.
        ("APROBACION_TIMEOUT_HORAS", "1.5", "1.5"),
        ("REVISION_TIMEOUT_HORAS", "0.5", "0.5"),
    ],
)
def test_a_percentage_or_an_hour_count_is_never_regrouped(
    almacen: FakeRedis, nombre: str, dicho: str, esperado: str
) -> None:
    assert limites.validar(nombre, dicho) == esperado


@pytest.mark.parametrize(
    "dicho,esperado",
    [
        ("cargo de envío", "ENTREGA_EXCEPCION_CARGO"),
        ("pedido mínimo", "ENTREGA_EXCEPCION_MIN_TOTAL"),
        ("días de reparto", "ENTREGA_DIAS"),
        ("dias de reparto", "ENTREGA_DIAS"),
        ("HORA DE RETIRO", "RETIRO_LOCAL_HORA"),
        ("colchón de stock", "STOCK_BUFFER_PCT"),
        ("colchon de stock", "STOCK_BUFFER_PCT"),
    ],
)
def test_the_owner_may_write_the_accents_or_not(
    almacen: FakeRedis, dicho: str, esperado: str
) -> None:
    """Every alias carries both spellings, so neither has to be guessed.

    A regression test, not a fix: this already works. It is here because the
    obvious tidy-up is to drop one spelling from each alias tuple, and that
    would send the owner a list of technical names to copy instead of
    understanding what he wrote.
    """
    assert limites.definicion(dicho).nombre == esperado


def test_a_vague_word_is_still_asked_about_rather_than_guessed(
    almacen: FakeRedis,
) -> None:
    """"Cambiá la hora" matches six settings across both registries."""
    for vago in ("hora", "dias", "retiro"):
        with pytest.raises(limites.LimiteError) as caido:
            limites.definicion(vago)
        assert "puede ser varias cosas" in str(caido.value)


# ------------------------------------- who actually applies the change, e2e
def test_the_code_is_applied_by_the_router_before_any_model_reads_it(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the second step, end to end.

    The owner's four digits arrive on a signed webhook from a phone es_equipo
    authenticated, and the deterministic handler applies the change and answers
    him. The management model is never called — so it cannot be steered into
    confirming, and it never learns the code even after the fact.
    """
    monkeypatch.setattr(main, "es_equipo", lambda t: t == EQUIPO)
    gerencia = Mock(side_effect=AssertionError("el modelo no interviene en esto"))
    monkeypatch.setattr(main, "responder_gerencia", gerencia)
    _proponer("monto máximo", "30000")
    codigo = _codigo_enviado(almacen)

    respuesta = main._generate_response(
        {"telefono": EQUIPO, "message_id": "wamid.1", "kind": "text", "data": codigo}
    )

    assert "0 a 30000" in respuesta
    assert limites.configuracion().tope == 30_000.0
    gerencia.assert_not_called()


def test_a_stranger_who_guesses_the_code_changes_nothing(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """es_equipo is the gate. A customer's message never reaches the handler,
    so a guessed code is just four digits the sales agent has to answer."""
    monkeypatch.setattr(main, "es_equipo", lambda t: t == EQUIPO)
    _proponer("monto máximo", "30000")

    assert main._codigo_de_ajuste("4242", DESCONOCIDO) is None
    assert limites.configuracion().tope == 0.0
    # ...and the owner's own pending change is untouched by the attempt.
    assert "0 a 30000" in _confirmar()


def test_a_change_whose_code_could_not_be_sent_is_dropped(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change waiting on a code he never saw cannot be confirmed and can
    confuse him ten minutes later."""
    monkeypatch.setattr(
        whatsapp, "enviar_mensaje", Mock(side_effect=RuntimeError("Meta caído"))
    )

    respuesta = _proponer("monto máximo", "30000")

    assert "NO pude mandarte el código" in respuesta
    assert "No cambié nada" in respuesta
    assert limites.pendiente(EQUIPO) is None
    assert _confirmar() == ""
    assert limites.configuracion().tope == 0.0


def test_what_is_left_pending_never_includes_the_code(almacen: FakeRedis) -> None:
    """limites.pendiente() is what the router reads to tell a code from an
    ordinary number. It must not become a second way to obtain one."""
    _proponer("monto máximo", "30000")

    pendiente = limites.pendiente(EQUIPO)

    assert pendiente["alias"] == "monto maximo"
    assert pendiente["nuevo"] == "30000"
    assert "codigo" not in pendiente
    assert "4242" not in json.dumps(pendiente, ensure_ascii=False)


# ---------------------------------------------------------------- zonas
# Las zonas de reparto eran la ÚNICA regla de entrega que el dueño no podía
# cambiar por WhatsApp: app/entrega.py las leía de ZONAS_ENTREGA_CP y
# ZONAS_ENTREGA_LOCALIDADES con os.getenv, así que "permití reparto en tal
# ciudad" pedía editar el .env y reiniciar. Ahora son dos límites como los
# demás y pasan por el mismo código de cuatro dígitos.


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        ("Villa Allende", "Villa Allende"),
        ("Villa Allende, Cordoba", "Villa Allende, Cordoba"),
        # Así escribe una lista una persona.
        ("Villa Allende y Cordoba", "Villa Allende, Cordoba"),
        ("Villa Allende, Cordoba y Unquillo", "Villa Allende, Cordoba, Unquillo"),
        # Dedup sin tildes ni caso, conservando cómo lo escribió el dueño.
        ("Córdoba, cordoba, CORDOBA", "Córdoba"),
        ("  Villa   Allende  ", "Villa Allende"),
        ("Villa Allende, villa allende", "Villa Allende"),
    ],
)
def test_localities_are_a_list_a_person_can_type(crudo: str, esperado: str) -> None:
    assert limites.validar("ZONAS_ENTREGA_LOCALIDADES", crudo) == esperado


def test_a_locality_is_never_split_on_a_space() -> None:
    """El bug obvio de reusar el validador de días: "Villa Allende" es UNA."""
    valor = limites.validar("ZONAS_ENTREGA_LOCALIDADES", "Villa Allende")

    assert valor == "Villa Allende"
    assert "," not in valor


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        ("5000", "5000"),
        ("5105, X5000ABC", "5105, X5000ABC"),
        ("x5000abc", "X5000ABC"),
        ("5000 5105", "5000, 5105"),
        ("5000 y 5105", "5000, 5105"),
        # CPA argentino, que se escribe sin espacios.
        ("X5000ABC", "X5000ABC"),
        ("X5000ABC, 5105", "X5000ABC, 5105"),
        ("5000, 5000", "5000"),
    ],
)
def test_postcodes_normalize_the_way_entrega_compares_them(crudo, esperado) -> None:
    """Se guarda EXACTAMENTE la forma con la que se va a comparar."""
    from app import entrega

    valor = limites.validar("ZONAS_ENTREGA_CP", crudo)

    assert valor == esperado
    for parte in valor.split(", "):
        assert entrega.normalizar_cp(parte) == parte


@pytest.mark.parametrize("nombre", ["ZONAS_ENTREGA_LOCALIDADES", "ZONAS_ENTREGA_CP"])
@pytest.mark.parametrize("crudo", ["", "   ", ",", " , , ", "-.-"])
def test_an_empty_zone_list_is_refused_not_stored_as_nothing(nombre, crudo) -> None:
    """«no me dijiste nada» y «ninguna» son respuestas distintas."""
    with pytest.raises(limites.LimiteError):
        limites.validar(nombre, crudo)


def test_an_ambiguous_postcode_is_refused_by_name_not_guessed() -> None:
    """"X 5000 - ABC" puede ser un CPA con espacios o tres códigos.

    Se corta por espacios a propósito, porque "5000 5105" son dos códigos. Con
    algo que no se puede leer de una sola manera, adivinar mal ensancha o
    encoge la zona de reparto sin que nadie lo decida, así que se rechaza
    diciendo qué parte no se entendió.
    """
    with pytest.raises(limites.LimiteError, match="no es un código postal"):
        limites.validar("ZONAS_ENTREGA_CP", "X 5000 - ABC")


@pytest.mark.parametrize("nombre", ["ZONAS_ENTREGA_LOCALIDADES", "ZONAS_ENTREGA_CP"])
@pytest.mark.parametrize("dicho", ["ninguno", "ninguna", "nada", "-"])
def test_the_owner_can_clear_a_zone_list_on_purpose(nombre, dicho) -> None:
    assert limites.validar(nombre, dicho) == limites.NINGUNO


@pytest.mark.parametrize(
    ("nombre", "crudo"),
    [
        ("ZONAS_ENTREGA_LOCALIDADES", "Villa Allende y Cordoba"),
        ("ZONAS_ENTREGA_CP", "5105 y x5000abc"),
    ],
)
def test_normalizing_a_zone_list_twice_gives_the_same_thing(nombre, crudo) -> None:
    """validar() corre en proponer() y otra vez en aplicar(): tiene que ser
    idempotente o el dueño confirma una cosa y se guarda otra."""
    una = limites.validar(nombre, crudo)

    assert limites.validar(nombre, una, tecleado=False) == una
    assert limites.validar(nombre, una) == una


@pytest.mark.parametrize(
    ("alias", "nombre"),
    [
        ("localidades de reparto", "ZONAS_ENTREGA_LOCALIDADES"),
        ("localidades", "ZONAS_ENTREGA_LOCALIDADES"),
        ("zonas de reparto", "ZONAS_ENTREGA_LOCALIDADES"),
        ("ciudades", "ZONAS_ENTREGA_LOCALIDADES"),
        ("códigos postales", "ZONAS_ENTREGA_CP"),
        ("codigos postales", "ZONAS_ENTREGA_CP"),
        ("cp", "ZONAS_ENTREGA_CP"),
    ],
)
def test_the_owner_can_name_a_zone_list_the_way_he_says_it(alias, nombre) -> None:
    assert limites.definicion(alias).nombre == nombre


def test_a_zone_change_needs_the_four_digit_code_like_everything_else(
    almacen: FakeRedis,
) -> None:
    """El camino completo: el agente propone, el dueño confirma, y recién ahí
    cambia lo que app/entrega.py va a leer."""
    from app import entrega

    respuesta = configuracion.proponer_limite.invoke(
        {"limite": "localidades de reparto", "valor": "Villa Allende y Cordoba"},
        config=_gerencia(),
    )

    assert "todavía sin aplicar" in respuesta
    # Nada cambió: el código no salió por la herramienta.
    assert limites.vigente("ZONAS_ENTREGA_LOCALIDADES") == limites.NINGUNO
    assert entrega.zonas_configuradas() == (frozenset(), frozenset())
    assert _codigo_enviado(almacen) == "4242"

    detalle = _confirmar()

    assert "Villa Allende, Cordoba" in detalle
    assert limites.vigente("ZONAS_ENTREGA_LOCALIDADES") == "Villa Allende, Cordoba"
    _cps, localidades = entrega.zonas_configuradas()
    assert localidades == frozenset({"villa allende", "cordoba"})


def test_a_confirmed_zone_replaces_the_env_and_takes_effect_at_once(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El .env es el valor de ARRANQUE; lo que fijó el dueño manda, sin reinicio."""
    from app import entrega

    monkeypatch.setenv("ZONAS_ENTREGA_CP", "9999")
    assert entrega.zonas_configuradas()[0] == frozenset({"9999"})

    configuracion.proponer_limite.invoke(
        {"limite": "cp", "valor": "5105, x5000abc"}, config=_gerencia()
    )
    _confirmar()

    assert entrega.zonas_configuradas()[0] == frozenset({"5105", "X5000ABC"})


def test_a_zone_change_is_audited_with_the_phone_and_both_values(
    almacen: FakeRedis,
) -> None:
    configuracion.proponer_limite.invoke(
        {"limite": "localidades", "valor": "Cordoba"}, config=_gerencia()
    )
    _confirmar()

    entradas = limites.auditoria(10)
    ultima = entradas[-1]
    assert ultima["limite"] == "ZONAS_ENTREGA_LOCALIDADES"
    assert ultima["anterior"] == limites.NINGUNO
    assert ultima["nuevo"] == "Cordoba"
    assert ultima["telefono"] == EQUIPO


def test_zones_read_as_lost_when_the_store_was_wiped(
    almacen: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un flush de Redis no puede devolver la zona al valor del .env.

    Ensanchar la zona de reparto sin que nadie lo haya decidido es vender una
    entrega que no se va a hacer. Con el almacén vacío y cambios registrados en
    ERPNext no rige ninguna zona y no se entrega nada solo.
    """
    from app import entrega

    monkeypatch.setenv("ZONAS_ENTREGA_CP", "5000")
    monkeypatch.setenv("ZONAS_ENTREGA_LOCALIDADES", "Cordoba")
    monkeypatch.setattr(limites, "_reglas_de_entrega_perdidas", lambda almacen: True)

    assert limites.zonas() == ((), ())
    assert entrega.zonas_configuradas() == (frozenset(), frozenset())


def test_an_unreadable_store_does_not_invent_a_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import entrega

    monkeypatch.setattr(
        limites, "_almacen", Mock(side_effect=limites.LimiteError("sin redis"))
    )

    assert limites.zonas() == ((), ())
    assert entrega.zonas_configuradas() == (frozenset(), frozenset())


def test_a_customer_cannot_change_a_delivery_zone(almacen: FakeRedis) -> None:
    """Es la regla que decide si su propia dirección entra: no la toca él."""
    from app import entrega

    for config in (_cliente(), _gerencia(DESCONOCIDO), {}):
        assert "no está autorizado" in configuracion.proponer_limite.invoke(
            {"limite": "localidades", "valor": "Villa Allende"}, config=config
        )

    assert limites.vigente("ZONAS_ENTREGA_LOCALIDADES") == limites.NINGUNO
    assert entrega.zonas_configuradas() == (frozenset(), frozenset())
