"""The daily digest: one atomic claim per day, and it goes to the OWNER.

TWO BUGS THIS PINS DOWN
1. The day marker was read-then-write (GET, compose, SETEX). The agent's
   scheduler and the cron entry point run at the same minute on purpose, and
   `python -m app.digest` skipped the marker altogether: two processes, two
   digests. Now the day is CLAIMED with one SET NX EX before anything is
   composed; whoever loses the claim sends nothing.
2. The digest is a message for the owner but went through the generic staff
   funnel, which picks the first number of the ALPHABETICALLY sorted staff
   list. With two team phones that first number can be an employee. It now
   goes to TELEFONO_DUENO, which must be one of TELEFONOS_EQUIPO.
"""
from __future__ import annotations

import sys
import threading
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import FakeRedis

from app import digest, locks, notificar, router, whatsapp

# Captured before any fixture swaps it: the tests about composition need the
# real resumen() with only its ERPNext-facing sections stubbed.
_RESUMEN_REAL = digest.resumen

DUENO = "5493519999999"  # sorts AFTER the employee: the sorted-first rule picks the wrong one
EMPLEADO = "5493511111111"
HOY = date(2026, 9, 6)


class RedisAtomico(FakeRedis):
    """FakeRedis with a real lock around SET NX, so two threads see one winner."""

    def __init__(self):
        super().__init__()
        self.candado = threading.Lock()
        self.sets_nx = 0

    def set(self, key, value, nx=False, ex=None):
        with self.candado:
            if nx:
                self.sets_nx += 1
            return super().set(key, value, nx=nx, ex=ex)


@pytest.fixture
def mundo(monkeypatch):
    falso = RedisAtomico()
    monkeypatch.setattr(locks, "conexion", lambda: falso)
    monkeypatch.setattr(digest, "_ahora", lambda: datetime(2026, 9, 6, 18, 0, tzinfo=ZoneInfo("UTC")))
    monkeypatch.setattr(digest, "resumen", lambda dia=None: f"📋 Resumen del {HOY.isoformat()}\n(prueba)")
    monkeypatch.setenv("DIGEST_ACTIVO", "true")
    monkeypatch.setenv("DIGEST_HORA", "18:00")
    # Pinned, not inherited: alertar_excepcion's routing IS this flag, and a
    # deployment that legitimately notifies the whole team (false) turned
    # test_exception_alerts_keep_their_own_routing red from a .env alone.
    monkeypatch.setenv("NOTIFICAR_SOLO_PRIMERO", "true")
    monkeypatch.delenv("WHATSAPP_STAFF_ALERT_TEMPLATE", raising=False)
    monkeypatch.setattr(router, "STAFF", [DUENO, EMPLEADO])
    monkeypatch.setenv("TELEFONO_DUENO", DUENO)
    enviados: list[tuple[str, str]] = []
    fallidos: list[str] = []

    def send(phone, text):
        enviados.append((phone, text))
        return {"messages": [{"id": f"wamid.{len(enviados)}"}]}

    # _alertar imports app.whatsapp at call time, so patch the module itself.
    monkeypatch.setattr(whatsapp, "enviar_mensaje", send)
    monkeypatch.setattr(
        notificar, "registrar_aviso_fallido", lambda nombre, pedido, texto: fallidos.append(nombre) or True
    )
    return {"redis": falso, "enviados": enviados, "fallidos": fallidos}


# ------------------------------------------------------------ one atomic claim


def test_the_scheduler_and_the_cron_path_share_one_claim(mundo):
    assert digest.tick() is True  # the agent's scheduler
    assert digest.main([]) is False  # the cron entry point, same day
    assert digest.tick() is False
    assert [p for p, _ in mundo["enviados"]] == [DUENO]
    assert mundo["redis"].sets_nx == 2  # claim attempted twice, won once


def test_the_cron_path_claims_too_when_it_runs_first(mundo):
    assert digest.main([]) is True
    assert digest.tick() is False
    assert len(mundo["enviados"]) == 1


def test_two_concurrent_senders_produce_one_digest(mundo):
    """Both processes reach enviar() at the same instant; SET NX decides."""
    largada = threading.Barrier(2)
    resultados: list[bool] = []

    def correr():
        largada.wait(2)
        resultados.append(digest.enviar())

    hilos = [threading.Thread(target=correr) for _ in range(2)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(5)
    assert sorted(resultados) == [False, True]
    assert len(mundo["enviados"]) == 1


def test_the_claim_is_taken_before_anything_is_composed(mundo, monkeypatch):
    """A process that loses the claim never even builds the summary."""
    llamadas = []
    monkeypatch.setattr(digest, "resumen", lambda dia=None: llamadas.append(dia) or "x")
    mundo["redis"].set(digest._clave(HOY), "otro proceso", nx=True, ex=10)
    assert digest.enviar() is False
    assert llamadas == []
    assert mundo["enviados"] == []


def test_without_redis_nothing_is_claimed_and_nothing_is_sent(mundo):
    mundo["redis"].caido = True
    assert digest.enviar() is False
    assert digest.tick() is False
    assert mundo["enviados"] == []


def test_a_failed_delivery_keeps_the_claim_so_the_day_is_not_retried(mundo, monkeypatch):
    def rechaza(phone, text):
        raise RuntimeError("Meta 131047")

    monkeypatch.setattr(whatsapp, "enviar_mensaje", rechaza)
    assert digest.enviar() is False
    assert mundo["fallidos"], "the failure must be recorded, not swallowed"
    # Same day, the agent's scheduler and the cron both stay quiet.
    assert digest.tick() is False
    assert digest.main([]) is False
    assert mundo["redis"].get(digest._clave(HOY)) is not None


RESPALDO_TRABADAS = "🔒 Borradores trabados: no pude armar esta sección"
RESPALDO_FALLOS = "⚠️ Comunicación: no pude armar esta sección"


@pytest.mark.parametrize(
    ("fallan", "esperados", "intactos"),
    [
        (("contar_pendientes",), [RESPALDO_FALLOS], [RESPALDO_TRABADAS]),
        (("trabadas",), [RESPALDO_TRABADAS], [RESPALDO_FALLOS]),
        (("contar_pendientes", "trabadas"), [RESPALDO_FALLOS, RESPALDO_TRABADAS], []),
    ],
    ids=["comunicacion", "trabadas", "ambas"],
)
def test_a_section_whose_dependency_raises_still_lets_the_digest_go_out(
    mundo, monkeypatch, fallan, esperados, intactos
):
    """The claim is taken before composing, so composition must never abort:
    the section that blows up says so, and every other section still reaches
    the owner. Each dependency alone, and both at once."""
    from app import outbound_status, solicitudes

    monkeypatch.setattr(digest, "resumen", _RESUMEN_REAL)
    for nombre in ("seccion_despacho", "seccion_pendientes", "seccion_conteos"):
        monkeypatch.setattr(digest, nombre, lambda n=nombre: f"{n}: ok")

    def explota(*a, **k):
        raise RuntimeError("Redis se fue")

    monkeypatch.setattr(
        outbound_status, "contar_pendientes",
        explota if "contar_pendientes" in fallan else lambda: {},
    )
    monkeypatch.setattr(solicitudes, "trabadas", explota if "trabadas" in fallan else lambda: 0)

    assert digest.enviar() is True
    texto = mundo["enviados"][0][1]
    assert "seccion_despacho: ok" in texto
    for respaldo in esperados:
        assert respaldo in texto
    for respaldo in intactos:
        assert respaldo not in texto  # the healthy section is not replaced
    # Sent, so the day stays claimed like any delivered digest.
    assert digest.enviar() is False


def test_a_composition_failure_releases_the_claim_so_the_next_attempt_can_send(mundo, monkeypatch):
    """Nothing was sent and nothing was recorded: the day must not be burnt."""
    def explota(dia=None):
        raise RuntimeError("bug nuevo en el resumen")

    monkeypatch.setattr(digest, "resumen", explota)
    assert digest.enviar() is False
    assert mundo["enviados"] == []
    assert mundo["redis"].get(digest._clave(HOY)) is None  # released

    monkeypatch.setattr(digest, "resumen", lambda dia=None: "📋 recuperado")
    assert digest.enviar() is True  # the next tick gets the day
    assert [t for _, t in mundo["enviados"]] == ["📋 Resumen del día\n📋 recuperado"]


def test_only_the_process_holding_the_claim_can_release_it(mundo):
    mundo["redis"].set(digest._clave(HOY), "otro proceso", nx=True, ex=10)
    digest.liberar(HOY, "yo")
    assert mundo["redis"].get(digest._clave(HOY)) == "otro proceso"


def test_forzar_is_the_only_way_past_the_claim_and_only_by_hand(mundo):
    assert digest.enviar() is True
    assert digest.enviar() is False
    assert digest.main(["--forzar"]) is True  # a person at a terminal
    assert digest.main([]) is False  # the cron never passes --forzar
    assert len(mundo["enviados"]) == 2


# ---------------------------------------------------------------- the owner


def test_the_digest_goes_to_the_owner_not_to_the_alphabetically_first_staff(mundo):
    assert sorted([DUENO, EMPLEADO])[0] == EMPLEADO  # the old rule would pick the employee
    assert digest.enviar() is True
    assert [p for p, _ in mundo["enviados"]] == [DUENO]
    assert "📋 Resumen del día" in mundo["enviados"][0][1]


def test_a_single_team_phone_is_the_owner_without_extra_configuration(mundo, monkeypatch):
    monkeypatch.delenv("TELEFONO_DUENO")
    monkeypatch.setattr(router, "STAFF", [EMPLEADO])
    assert notificar.telefono_dueno() == EMPLEADO
    assert digest.enviar() is True
    assert [p for p, _ in mundo["enviados"]] == [EMPLEADO]


def test_with_several_team_phones_and_no_owner_nothing_is_guessed(mundo, monkeypatch):
    monkeypatch.delenv("TELEFONO_DUENO")
    assert notificar.telefono_dueno() == ""
    assert digest.enviar() is False
    assert mundo["enviados"] == []
    assert mundo["fallidos"] == ["owner:📋 Resumen del día"]


def test_an_owner_outside_the_team_list_gets_nothing(mundo, monkeypatch):
    monkeypatch.setenv("TELEFONO_DUENO", "5490000000000")
    assert notificar.telefono_dueno() == ""
    assert digest.enviar() is False
    assert mundo["enviados"] == []


def test_the_owner_number_is_normalised_like_every_other_phone(mundo, monkeypatch):
    monkeypatch.setenv("TELEFONO_DUENO", "+54 9 351 999-9999")
    assert notificar.telefono_dueno() == DUENO


def test_exception_alerts_keep_their_own_routing(mundo):
    """Only the digest changed recipient; alertar_excepcion is untouched."""
    assert notificar.alertar_excepcion("⚠️ prueba", "cuerpo") is True
    assert [p for p, _ in mundo["enviados"]] == [sorted([DUENO, EMPLEADO])[0]]
