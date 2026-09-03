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
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import FakeRedis, entrega_autorizada, inventario_confiable

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
    configuracion.confirmar_limite.invoke({"codigo": "4242"}, config=_gerencia())

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
    return configuracion.confirmar_limite.invoke(
        {"codigo": codigo}, config=_gerencia(telefono)
    )


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
    configuracion.confirmar_limite.invoke({"codigo": "4242"}, config=config)

    assert almacen.hashes == {}
    assert limites.entrega().dias_reparto == ()


def test_a_manager_cannot_confirm_a_delivery_change_somebody_else_proposed(
    almacen: FakeRedis,
) -> None:
    _proponer("días de reparto", "martes y viernes", EQUIPO)

    assert "No apliqué nada" in _confirmar(telefono=OTRO_DEL_EQUIPO)
    assert limites.entrega().dias_reparto == ()


# --------------------------------------------------- the confirmation code
def test_nothing_about_delivery_changes_until_the_owner_writes_the_code(
    almacen: FakeRedis,
) -> None:
    propuesta = _proponer("días de reparto", "martes y viernes")

    assert "4242" in propuesta
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
    nombres = {t.name for t in [
        configuracion.ver_limites,
        configuracion.ver_reglas_de_entrega,
        configuracion.proponer_limite,
        configuracion.confirmar_limite,
        configuracion.historial_limites,
    ]}
    assert nombres == {
        "ver_limites",
        "ver_reglas_de_entrega",
        "proponer_limite",
        "confirmar_limite",
        "historial_limites",
    }

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
    assert limites.MARCA_DURABLE in texto
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
    assert "No apliqué nada" in _confirmar()


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
def test_a_percentage_or_an_hour_count_is_never_regrouped(
    almacen: FakeRedis, dicho: str, esperado: float
) -> None:
    """The thousands rule is for money only: 1.5% is one and a half percent."""
    _proponer("colchón de stock", dicho)
    _confirmar()

    assert limites.configuracion().buffer == pytest.approx(esperado / 100.0)


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
