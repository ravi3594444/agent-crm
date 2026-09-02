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
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import FakeRedis

from app import limites, locks, policy
from app.tools import configuracion

# Captured before the autouse fixture in conftest replaces it: the two tests
# below are about the real ERPNext cross-check, not about a stub of it.
_CONSULTA_DURABLE_REAL = limites._hubo_cambios_durables

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
    """A store that keeps what is written to it, and a staff list of two."""
    from app import router

    falso = FakeRedis()
    monkeypatch.setattr(locks, "conexion", lambda: falso)
    monkeypatch.setattr(router, "STAFF", [EQUIPO, OTRO_DEL_EQUIPO])
    monkeypatch.setattr(limites, "_codigo", lambda: "4242")
    return falso


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
    assert "no está autorizado" in configuracion.confirmar_limite.invoke(
        {"codigo": "4242"}, config=config
    )
    assert "no está autorizado" in configuracion.historial_limites.invoke(
        {}, config=config
    )
    assert almacen.hashes == {}


def test_a_manager_cannot_confirm_a_change_somebody_else_proposed(
    almacen: FakeRedis,
) -> None:
    """The pending change belongs to the phone that asked for it."""
    configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "5000"}, config=_gerencia(EQUIPO)
    )

    respuesta = configuracion.confirmar_limite.invoke(
        {"codigo": "4242"}, config=_gerencia(OTRO_DEL_EQUIPO)
    )

    assert "No apliqué nada" in respuesta
    assert limites.vigente("AUTO_CONFIRM_MAX") == "0"


# ------------------------------------------------------ confirmation and audit
def test_nothing_changes_until_the_owner_writes_the_code(almacen: FakeRedis) -> None:
    propuesta = configuracion.proponer_limite.invoke(
        {"limite": "monto máximo", "valor": "30000"}, config=_gerencia()
    )

    assert "4242" in propuesta
    assert "0 → 30000" in propuesta
    # Proposed only: the limit in force is still the old one.
    assert limites.vigente("AUTO_CONFIRM_MAX") == "0"
    assert limites.configuracion().tope == 0.0

    aplicado = configuracion.confirmar_limite.invoke(
        {"codigo": "4242"}, config=_gerencia()
    )

    assert "0 a 30000" in aplicado
    assert limites.configuracion().tope == 30_000.0


def test_a_wrong_code_changes_nothing(almacen: FakeRedis) -> None:
    configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "30000"}, config=_gerencia()
    )

    respuesta = configuracion.confirmar_limite.invoke(
        {"codigo": "1111"}, config=_gerencia()
    )

    assert "No apliqué nada" in respuesta
    assert limites.configuracion().tope == 0.0


def test_confirming_with_nothing_pending_changes_nothing(almacen: FakeRedis) -> None:
    respuesta = configuracion.confirmar_limite.invoke(
        {"codigo": "4242"}, config=_gerencia()
    )

    assert "No apliqué nada" in respuesta
    assert almacen.hashes == {}


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
    configuracion.confirmar_limite.invoke({"codigo": "4242"}, config=_gerencia())

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
    configuracion.confirmar_limite.invoke({"codigo": "4242"}, config=_gerencia())

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
    configuracion.confirmar_limite.invoke({"codigo": "4242"}, config=_gerencia())

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
    configuracion.confirmar_limite.invoke({"codigo": "4242"}, config=_gerencia())

    assert limites.configuracion().tope == 7_500.0
    fila = next(f for f in limites.resumen() if f["nombre"] == "AUTO_CONFIRM_MAX")
    assert fila["origen"] == "dueño"


@pytest.mark.skipif(
    not os.getenv("REDIS_URL", "").strip(), reason="sin REDIS_URL configurado"
)
def test_a_change_really_survives_in_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one test that talks to a real Redis: writes a limit, drops every
    connection and object, and reads it back. Skipped when Redis is absent."""
    import redis

    from app import router

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
        pytest.skip("Redis no responde")
    cliente_directo = redis.Redis.from_url(os.environ["REDIS_URL"])
    try:
        configuracion.proponer_limite.invoke(
            {"limite": "tope", "valor": "4321"}, config=_gerencia()
        )
        configuracion.confirmar_limite.invoke({"codigo": "4242"}, config=_gerencia())

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

    monkeypatch.setattr(policy, "STOCK_CONFIABLE", True)
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
    configuracion.confirmar_limite.invoke({"codigo": "4242"}, config=_gerencia())

    decision = policy.evaluar(_pedido_verde())
    assert decision.auto is False
    assert any("supera el tope" in m for m in decision.motivos)

    # And back up again, in the same process.
    monkeypatch.setattr(limites, "_codigo", lambda: "9999")
    configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "2000"}, config=_gerencia()
    )
    configuracion.confirmar_limite.invoke({"codigo": "9999"}, config=_gerencia())

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
    configuracion.confirmar_limite.invoke({"codigo": "4242"}, config=_gerencia())

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

    respuesta = configuracion.confirmar_limite.invoke(
        {"codigo": "4242"}, config=_gerencia()
    )

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


def test_the_owner_is_told_the_new_customer_ceiling_is_parked(
    almacen: FakeRedis,
) -> None:
    """He can set it, and it is stored and audited, but it decides nothing
    until the delivery address and area are verified. Saying so is the
    difference between a parked setting and a broken one."""
    from app import policy

    assert policy.CLIENTE_NUEVO_HABILITADO is False

    vista = configuracion.ver_limites.invoke({}, config=_gerencia())

    assert "todavía sin efecto" in vista
    assert "dirección y la zona de entrega" in vista


def test_one_setting_at_a_time(almacen: FakeRedis) -> None:
    """A proposal replaces any earlier one, so a confirmation can only ever
    apply the single change the owner just read back."""
    configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "5000"}, config=_gerencia()
    )
    configuracion.proponer_limite.invoke(
        {"limite": "deuda", "valor": "250"}, config=_gerencia()
    )
    configuracion.confirmar_limite.invoke({"codigo": "4242"}, config=_gerencia())

    almacenados = almacen.hashes[limites.CLAVE_VALORES]
    assert almacenados == {"AUTO_CONFIRM_MAX_DEBT": "250"}


def test_a_dead_store_cannot_be_talked_into_a_change(almacen: FakeRedis) -> None:
    almacen.caido = True

    propuesta = configuracion.proponer_limite.invoke(
        {"limite": "tope", "valor": "5000"}, config=_gerencia()
    )

    assert "No cambié nada" in propuesta
