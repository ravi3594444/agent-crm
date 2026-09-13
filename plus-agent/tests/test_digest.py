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
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import time_machine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import FakeRedis, RelojDePrueba

from app import digest, locks, notificar, reloj, router, whatsapp

# Captured before any fixture swaps it: the tests about composition need the
# real resumen() with only its ERPNext-facing sections stubbed.
_RESUMEN_REAL = digest.resumen

# Igual que el de arriba, y por el mismo motivo: el fixture `mundo` reemplaza
# `_ahora`, así que el test que prueba de qué reloj sale el DÍA necesita el real.
# Capturarlo adentro del test capturaría el lambda del fixture.
_AHORA_REAL = digest._ahora

DUENO = "5493519999999"  # sorts AFTER the employee: the sorted-first rule picks the wrong one
EMPLEADO = "5493511111111"

# EL DÍA QUE ESTE ARCHIVO NOMBRA, y ahora una sola vez. `HOY` era `date(2026, 9,
# 6)` escrito acá y el fixture fijaba `digest._ahora` en las 18:00 **UTC** del
# mismo día: dos relojes que TIENEN que dar la misma fecha —`enviar()` reclama el
# día con `_ahora().date()` y los tests abren ese reclamo con `_clave(HOY)`— y
# nada los obligaba. Coincidían por el offset de Buenos Aires y por nada más; con
# el negocio bastante al este, las 18:00 UTC ya son el día siguiente y la mitad
# del archivo probaría otra cosa en silencio. Ahora `HOY` sale del mismo reloj que
# el `_ahora` del fixture, así que no pueden separarse.
RELOJ = RelojDePrueba("2026-09-06")
HOY = RELOJ.hoy


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
    # Las 18:00 DEL NEGOCIO, que es contra lo que `tick()` compara DIGEST_HORA.
    monkeypatch.setattr(digest, "_ahora", lambda: RELOJ.a_las(18))
    # El doble FECHA lo que le pasan, no lo que el archivo supone. Con
    # `HOY.isoformat()` escrito acá, el cuerpo salía con la misma fecha siempre y
    # `enviar()` podía componer el resumen de un día y reclamar otro sin que
    # nadie lo viera: medido, componer con `dia - 1` dejaba los 2481 tests en
    # verde. Es el mismo defecto que este archivo fue a buscar —dos relojes que
    # tienen que coincidir y nada los obliga—, una capa más abajo. Lo encontró
    # Qodo revisando #31.
    monkeypatch.setattr(
        digest,
        "resumen",
        lambda dia=None: f"📋 Resumen del {(dia or HOY).isoformat()}\n(prueba)",
    )
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


def test_el_dia_que_se_reclama_es_el_del_negocio_y_no_el_del_servidor(
    mundo, monkeypatch
) -> None:
    """UN instante, DOS zonas, DOS días del negocio — y por lo tanto dos resúmenes.

    EL TEST QUE ESTE ARCHIVO NO PODÍA ESCRIBIR. `enviar()` toma `dia =
    _ahora().date()` y `_clave(dia)` es la marca «este día ya salió», con un solo
    intento por día. Si ese día fuera el del SERVIDOR, dos días del negocio que
    caen dentro de la misma fecha UTC comparten la clave: el segundo encuentra el
    día tomado, se calla, y el dueño pierde un resumen entero sin que nada lo
    diga. `enviar()` devolvería False, que es lo mismo que devuelve cuando el
    resumen ya salió de verdad.

    No se podía escribir porque `HOY` estaba escrito a mano y el fixture fijaba
    `_ahora` en las 18:00 de ese mismo día: un instante en el que la fecha del
    negocio y la del servidor COINCIDEN. Todos los tests del reclamo se
    ejercitaban con la única fecha en la que las dos lecturas dan lo mismo.

    Acá el reloj NO se fija: se congela el mundo con `time_machine` y se deja que
    `_ahora()` resuelva la zona, que es el camino real. A las 02:30 UTC del 7 son
    las 23:30 del **6** en Buenos Aires y las 08:00 del **7** en Kolkata.
    """
    monkeypatch.setattr(digest, "_ahora", _AHORA_REAL)
    cruce = datetime(2026, 9, 7, 2, 30, tzinfo=UTC)

    with time_machine.travel(cruce, tick=False):
        monkeypatch.setenv(reloj.VARIABLE, "America/Argentina/Buenos_Aires")
        assert digest.enviar() is True
        assert mundo["redis"].get(digest._clave(date(2026, 9, 6))) is not None

        # El MISMO instante, con el negocio al este: es otro día del negocio, así
        # que es otro reclamo y sale otro resumen. Con el día del servidor los
        # dos caerían en el 7 y éste se perdería en silencio.
        monkeypatch.setenv(reloj.VARIABLE, "Asia/Kolkata")
        assert digest.enviar() is True
        assert mundo["redis"].get(digest._clave(date(2026, 9, 7))) is not None

    # Y el CUERPO lleva el mismo día que el reclamo, que es la otra mitad y no
    # se seguía de la primera: `enviar()` saca `dia` una vez y se lo pasa al
    # reclamo Y a `resumen(dia)`. Afirmar sólo las claves dejaba pasar un resumen
    # fechado un día y reclamado otro — el dueño recibe el resumen de ayer y el
    # de hoy queda reclamado y sin salir nunca.
    cuerpos = [texto for _, texto in mundo["enviados"]]
    assert len(cuerpos) == 2
    assert "2026-09-06" in cuerpos[0]
    assert "2026-09-07" in cuerpos[1]


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


# --------------------------------- el total de la sección no cuenta la orden


def test_the_pending_section_header_counts_orders_not_the_instruction_line(
    mundo, monkeypatch
):
    """El docstring promete que el total nunca discute con el desglose.

    `_seccion` usa len(lineas) como el número entre paréntesis, así que meter
    la línea de instrucciones dentro de `lineas` hacía que el encabezado dijera
    uno más que los pedidos listados: «2 del bot + 1 cargados a mano (4)».
    """
    from app import pendientes

    filas = [
        {"name": "SO-1", "po_no": "WA-" + "a" * 40, "creation": "2026-09-06 09:00:00"},
        {"name": "SO-2", "po_no": "WA-" + "b" * 40, "creation": "2026-09-06 09:00:00"},
        {"name": "SO-3", "po_no": "OC-4471", "creation": "2026-09-06 09:00:00"},
    ]
    monkeypatch.setattr(pendientes, "listar_esperando", lambda **k: filas)
    monkeypatch.setattr(pendientes, "edad_horas", lambda f, ahora=None: 3.0)

    salida = digest.seccion_pendientes()

    assert "2 del bot + 1 cargados a mano (3):" in salida
    assert "(4)" not in salida
    # Y la instrucción sigue estando, sólo que fuera de la cuenta.
    assert "Respondé 'confirmar <pedido>'" in salida


# --------------------------------------------- el techo de lectura que no avisaba


def _fila_pendiente(nombre: str, po_no: str = "WA-" + "a" * 40) -> dict:
    return {"name": nombre, "po_no": po_no, "creation": "2026-09-06 09:00:00"}


def test_a_pending_list_that_filled_the_read_is_not_reported_as_the_total(
    mundo, monkeypatch
):
    """El encabezado decía «(200)» con 900 esperando, y el docstring promete TODOS.

    Un conteo que llenó el techo de lectura es un PISO. Presentarlo como exacto
    le dice al dueño que ya vio todo lo que tiene para limpiar cuando le
    faltan 700 — y cada borrador que queda retiene el stock que promete.
    """
    from app import pendientes

    filas = [_fila_pendiente(f"SO-{i}") for i in range(digest.TECHO_PENDIENTES + 1)]
    pedidos: list[int] = []

    def listar(**kw):
        pedidos.append(int(kw["limite"]))
        return filas[: int(kw["limite"])]

    monkeypatch.setattr(pendientes, "listar_esperando", listar)
    monkeypatch.setattr(pendientes, "edad_horas", lambda f, ahora=None: 3.0)

    salida = digest.seccion_pendientes()

    # Se pide UNA fila más que el techo: es lo único que distingue «hay
    # exactamente 200» de «hay al menos 200».
    assert pedidos == [digest.TECHO_PENDIENTES + 1]
    assert f"({digest.TECHO_PENDIENTES}+):" in salida
    assert f"({digest.TECHO_PENDIENTES}):" not in salida
    assert "son un piso y no el total" in salida
    # El «y N más» de las líneas también es un mínimo, por lo mismo.
    assert f"· … y {digest.TECHO_PENDIENTES - digest.MAX_LINEAS}+ más" in salida
    # Y la fila extra no se muestra: se leyó para saber, no para listar.
    assert f"SO-{digest.TECHO_PENDIENTES}" not in salida


def test_a_pending_list_that_fits_is_still_reported_as_exact(mundo, monkeypatch):
    """El caso que separa los dos: el techo justo lleno todavía es exacto."""
    from app import pendientes

    filas = [_fila_pendiente(f"SO-{i}") for i in range(digest.TECHO_PENDIENTES)]
    monkeypatch.setattr(pendientes, "listar_esperando", lambda **kw: list(filas))
    monkeypatch.setattr(pendientes, "edad_horas", lambda f, ahora=None: 3.0)

    salida = digest.seccion_pendientes()

    assert f"({digest.TECHO_PENDIENTES}):" in salida
    assert "piso" not in salida
