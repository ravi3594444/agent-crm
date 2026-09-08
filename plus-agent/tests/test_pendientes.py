"""El registro de sombra y el barrido que lo escribe.

``test_sombra.py`` prueba que la EVALUACIÓN dice lo mismo que la política. Acá
se prueba el resto: que el registro sobreviva a cómo ERPNext guarda un
comentario, que un pedido no termine con dos, que el barrido no anote a ciegas
cuando ERPNext no contesta, y que nada de esto pueda tocar un pedido que
alguien decidió mientras el barrido estaba en la mitad.

Ninguna de estas pruebas llama a ERPNext ni a Redis de verdad: el fixture
autouse de conftest ya puso un FakeRedis, y acá se reemplazan las cuatro
funciones de ERPNext que se usan.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app import avisos, erpnext, outbound_status, pendientes, policy, router, sombra
from tests.fakes import entrada_de_cola

PEDIDO = "SAL-ORD-2026-00042"
TELEFONO = "5493511234567"
ZONA = ZoneInfo("America/Argentina/Buenos_Aires")


def epoch(hora: int, minuto: int = 0, dia: int = 8) -> float:
    """Un momento de septiembre 2026 en hora del negocio, como epoch para tick().

    `dia` existe para las pruebas de la ventana nocturna: las 07:30 del MISMO
    día son ANTES de que el borrador se creara (09:00), así que la ronda de la
    mañana es la del día siguiente.
    """
    return datetime(2026, 9, dia, hora, minuto, tzinfo=ZONA).timestamp()


@pytest.fixture(autouse=True)
def _el_reloj_de_verdad_no_entra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ningún test de este archivo lee la hora real, y es a propósito.

    Los borradores del fixture se crean a una hora FIJA —`_borrador` dice las
    09:00 del 8/9/2026— y `tick()` compara esa fecha contra el reloj del
    negocio. Con el reloj de verdad, el borrador envejece solo con el día: las
    patas de aviso y de cierre se prenden solas y entran en el conteo que
    devuelve `tick()`, así que los mismos tests pasan a la mañana y fallan a
    la tarde sin que nadie toque una línea.

    No es hipotético: cinco tests de este archivo se pusieron rojos en `main`
    a las 11 de la mañana del 8/9/2026, con la suite verde a las 10:41. Y el
    conteo dependía de DOS cosas del reloj a la vez —la edad del borrador y la
    ventana nocturna— así que también pasaban de madrugada y fallaban de día.

    El resto del archivo ya hacía lo correcto: pasar el momento con
    `epoch(...)`. Esto lo vuelve obligatorio, y hace que leer el reloj real
    explote acá con el motivo escrito en vez de seis horas más tarde en el CI
    de otra persona.
    """

    def _prohibido() -> None:
        raise AssertionError(
            "un test de test_pendientes.py leyó la hora REAL. Pasale el momento: "
            "`pendientes.tick(ahora=epoch(9))`. Ver el docstring de "
            "_el_reloj_de_verdad_no_entra."
        )

    monkeypatch.setattr(pendientes, "_ahora", _prohibido)


def _sombra_verde() -> policy.Sombra:
    return policy.Sombra(
        pasa_reglas=True,
        motivos_reglas=[],
        motivos_postura=[policy.POSTURA_STOCK, policy.POSTURA_TOPE],
        total=8450.0,
        habitual=None,
        tope_vigente=0.0,
    )


@pytest.fixture
def mundo(monkeypatch: pytest.MonkeyPatch) -> dict:
    """ERPNext de mentira: comentarios escritos, borradores listados, docs leídos."""
    escritos: list[tuple[str, str, str]] = []
    comentarios: dict[str, list[dict]] = {}
    borradores: list[dict] = []
    docs: dict[str, dict] = {}
    caidas: set[str] = set()

    def add_comment(doctype, name, texto):
        # Se ata a los DOS primitivos de comentario, porque el código usa los
        # dos a propósito: `add_comment` para lo que es best-effort y
        # `registrar_comentario` para el registro durable del que depende un
        # contador (la sombra). La clave "add_comment" en `caidas` significa
        # "ERPNext rechaza las escrituras de comentario", sin importar por
        # cuál de los dos entró.
        if "add_comment" in caidas:
            raise erpnext.ERPNextError("ERPNext no acepta comentarios")
        escritos.append((doctype, name, texto))
        comentarios.setdefault(name, []).append(
            {"content": texto, "reference_name": name, "creation": f"2026-09-08 10:0{len(escritos)}:00", "name": f"c{len(escritos)}"}
        )
        return {}

    def policy_get_list(doctype, filters=None, fields=None, limit=20, **kwargs):
        """Honra los filtros que el código realmente manda.

        El `content like` importa: con dos marcadores en el mismo pedido
        ([sombra] y [pendiente-aviso]), un doble que ignore el filtro contesta
        "ya está avisado" mirando el registro de sombra. Y el `status not in`
        importa porque es lo que saca de la cola lo que alguien ya rechazó.
        """
        if doctype == "Sales Order":
            if "listar" in caidas:
                raise erpnext.ERPNextError("ERPNext no contesta")
            fuera = []
            for campo, operador, valor in filters or []:
                if campo == "status" and operador == "not in":
                    fuera = [str(v) for v in valor]
            return [
                f for f in borradores if str(f.get("status") or "") not in fuera
            ][:limit]
        if doctype == "Comment":
            if "comentarios" in caidas:
                raise erpnext.ERPNextError("ERPNext no contesta")
            pedidos = None
            marca = ""
            for campo, operador, valor in filters or []:
                if campo == "reference_name":
                    pedidos = [valor] if operador == "=" else list(valor)
                if campo == "content" and operador == "like":
                    marca = str(valor).strip("%")
            filas = []
            for nombre, lista in comentarios.items():
                if pedidos is not None and nombre not in pedidos:
                    continue
                filas.extend(c for c in lista if marca in str(c.get("content") or ""))
            filas.sort(key=lambda f: f["creation"], reverse=True)
            return filas[:limit]
        return []

    def policy_get_doc(doctype, name):
        if name not in docs:
            raise erpnext.ERPNextError(f"{name} no existe")
        return dict(docs[name])

    estados: list[tuple[str, str]] = []
    enviados: list[tuple[str, str]] = []
    al_dueno: list[tuple[str, str]] = []
    locks_tomados: list[str] = []

    def policy_update_status(doctype, name, status):
        if "cerrar" in caidas:
            raise erpnext.ERPNextError("ERPNext no deja cerrar")
        estados.append((name, status))
        if name in docs:
            docs[name] = {**docs[name], "status": status}
        return {}

    def enviar_mensaje(telefono, texto):
        enviados.append((telefono, texto))
        return {"messages": [{"id": f"wamid.{len(enviados)}"}]}

    @contextmanager
    def lock(nombre, **kwargs):
        if "lock" in caidas:
            from app.locks import CoordinationError

            raise CoordinationError("ocupado")
        locks_tomados.append(nombre)
        yield

    monkeypatch.setattr(erpnext, "add_comment", add_comment)
    monkeypatch.setattr(erpnext, "registrar_comentario", add_comment)
    monkeypatch.setattr(erpnext, "policy_get_list", policy_get_list)
    monkeypatch.setattr(erpnext, "policy_get_doc", policy_get_doc)
    monkeypatch.setattr(erpnext, "policy_update_status", policy_update_status)
    monkeypatch.setattr(policy, "evaluar_sombra", lambda so: _sombra_verde())
    monkeypatch.setattr("app.solicitudes.vencimientos", lambda pedidos: {})
    monkeypatch.setattr("app.locks.distributed_lock", lock)
    monkeypatch.setattr("app.decisiones.telefono_del_cliente", lambda so: TELEFONO)
    monkeypatch.setattr("app.whatsapp.enviar_mensaje", enviar_mensaje)
    monkeypatch.setattr(avisos, "window_open", lambda tel: True)
    monkeypatch.setattr(
        "app.notificar.avisar_dueno",
        lambda asunto, cuerpo, **kw: bool(al_dueno.append((asunto, cuerpo))) or True,
    )
    monkeypatch.setenv("AUTO_CONFIRM_SOMBRA", "true")
    monkeypatch.setenv("PENDIENTE_AVISO_HORAS", "2")
    return {
        "escritos": escritos,
        "comentarios": comentarios,
        "borradores": borradores,
        "docs": docs,
        "caidas": caidas,
        "estados": estados,
        "enviados": enviados,
        "al_dueno": al_dueno,
        "locks": locks_tomados,
    }


def _en_cola() -> list[dict]:
    return [
        json.loads(e)
        for e in entrada_de_cola(outbound_status.cliente(), avisos.COLA)
    ]


def _al_cliente(mundo) -> list[str]:
    avisos.procesar()
    return [t for tel, t in mundo["enviados"] if tel == TELEFONO]


def _listo(mundo, nombre: str = PEDIDO, **extra) -> dict:
    """Un borrador esperando, listado y legible."""
    fila = _borrador(nombre, **extra)
    mundo["borradores"].append(fila)
    mundo["docs"][nombre] = dict(fila)
    return fila


# Un po_no con la forma que escribe tools/pedidos.py::_message_key: "WA-" y
# 40 hex. Sin esta forma exacta el pedido cuenta como cargado a mano, y ni el
# recordatorio ni el cierre lo tocan.
PO_AGENTE = "WA-" + "0123456789abcdef" * 2 + "01234567"


def _borrador(nombre: str = PEDIDO, **extra) -> dict:
    base = {
        "name": nombre,
        "customer": "CUST-0007",
        "customer_name": "Panadería López",
        "grand_total": 8450.0,
        "creation": "2026-09-08 09:00:00",
        "docstatus": 0,
        "status": "Draft",
        "po_no": PO_AGENTE,
    }
    base.update(extra)
    return base


# ------------------------------------------------------------- el registro


def test_the_record_is_written_once_and_parses_back(mundo) -> None:
    assert sombra.anotar(PEDIDO, _borrador()) is True

    doctype, nombre, texto = mundo["escritos"][0]
    assert (doctype, nombre) == ("Sales Order", PEDIDO)
    assert texto.startswith(sombra.MARCA)

    leido = sombra.leer(PEDIDO)
    assert leido["pasa_reglas"] is True
    assert leido["motivos_reglas"] == []
    assert leido["motivos_postura"] == [policy.POSTURA_STOCK, policy.POSTURA_TOPE]
    assert leido["total"] == 8450.0
    assert leido["habitual"] is None
    assert "ts" in leido


def test_the_record_survives_how_erpnext_mangles_a_comment(mundo) -> None:
    """ERPNext envuelve el contenido en HTML y escapa las entidades.

    Parsear el crudo es el bug que este test existe para prevenir: sin
    html.unescape y sin sacar las etiquetas, json.loads no ve nada.
    """
    carga = {"pasa_reglas": True, "motivos_reglas": [], "motivos_postura": ["tope"], "total": 1.0}
    crudo = json.dumps(carga, ensure_ascii=False)
    escapado = crudo.replace('"', "&quot;")
    mangled = (
        '<div class="ql-editor read-mode"><p>'
        f"{sombra.MARCA} {escapado}"
        "</p></div>"
    )

    assert sombra._parsear(mangled) == carga


def test_a_comment_without_the_marker_is_not_a_record(mundo) -> None:
    assert sombra._parsear("Requiere revisión humana: monto supera el tope") is None
    assert sombra._parsear(f"{sombra.MARCA} esto no es json") is None
    assert sombra._parsear("") is None


def test_the_newest_record_wins_when_an_order_somehow_has_two(mundo) -> None:
    sombra.anotar(PEDIDO, _borrador())
    mundo["comentarios"][PEDIDO].append(
        {
            "content": f'{sombra.MARCA} {{"pasa_reglas": false, "total": 99.0}}',
            "reference_name": PEDIDO,
            "creation": "2026-09-08 23:59:00",
            "name": "cX",
        }
    )

    assert sombra.leer(PEDIDO)["total"] == 99.0


def test_ya_anotado_has_three_answers_and_none_is_not_false(mundo) -> None:
    assert sombra.ya_anotado(PEDIDO) is False

    sombra.anotar(PEDIDO, _borrador())
    assert sombra.ya_anotado(PEDIDO) is True

    mundo["caidas"].add("comentarios")
    assert sombra.ya_anotado(PEDIDO) is None  # no «no», sino «no sé»


def test_a_failed_write_is_reported_and_not_counted(mundo) -> None:
    mundo["caidas"].add("add_comment")

    assert sombra.anotar(PEDIDO, _borrador()) is False
    assert sombra.contar() is None  # nada que contar


def test_the_daily_counter_separates_passes_from_blocks(mundo, monkeypatch) -> None:
    sombra.anotar(PEDIDO, _borrador())
    monkeypatch.setattr(
        policy,
        "evaluar_sombra",
        lambda so: policy.Sombra(pasa_reglas=False, motivos_reglas=["stock insuficiente de X"]),
    )
    sombra.anotar("SAL-ORD-2026-00043", _borrador("SAL-ORD-2026-00043"))

    assert sombra.contar() == {"pasan": 1, "frenados": 1}


def test_an_absent_counter_is_unknown_not_zero(mundo) -> None:
    """El resumen tiene que poder decir «no pude leer», no una autonomía de cero."""
    assert sombra.contar(date(2020, 1, 1)) is None


def test_registros_reads_a_batch_in_one_call(mundo) -> None:
    sombra.anotar(PEDIDO, _borrador())
    sombra.anotar("SAL-ORD-2026-00043", _borrador("SAL-ORD-2026-00043"))

    encontrados = sombra.registros([PEDIDO, "SAL-ORD-2026-00043", "SAL-ORD-2026-99999"])

    assert set(encontrados) == {PEDIDO, "SAL-ORD-2026-00043"}
    assert encontrados[PEDIDO]["pasa_reglas"] is True


# --------------------------------------------------------------- el barrido


def test_the_sweep_does_nothing_while_shadow_is_off(mundo, monkeypatch) -> None:
    """Por defecto apagado: un .env que no lo nombra se comporta como hoy."""
    monkeypatch.delenv("AUTO_CONFIRM_SOMBRA")
    mundo["borradores"].append(_borrador())
    mundo["docs"][PEDIDO] = _borrador()

    assert pendientes.tick(ahora=epoch(9)) == 0
    assert mundo["escritos"] == []


def test_the_sweep_annotates_a_waiting_draft(mundo) -> None:
    mundo["borradores"].append(_borrador())
    mundo["docs"][PEDIDO] = _borrador()

    assert pendientes.tick(ahora=epoch(9)) == 1
    assert sombra.leer(PEDIDO)["pasa_reglas"] is True


def test_the_sweep_is_idempotent_across_rounds(mundo) -> None:
    """El comentario ES la marca: mil rondas, un registro."""
    mundo["borradores"].append(_borrador())
    mundo["docs"][PEDIDO] = _borrador()

    assert pendientes.tick(ahora=epoch(9)) == 1
    assert pendientes.tick(ahora=epoch(9)) == 0
    assert pendientes.tick(ahora=epoch(9)) == 0
    assert len(mundo["escritos"]) == 1


def test_the_sweep_does_not_annotate_blind_when_erpnext_will_not_say(mundo) -> None:
    """ya_anotado None: anotar a ciegas le deja dos registros al pedido."""
    mundo["borradores"].append(_borrador())
    mundo["docs"][PEDIDO] = _borrador()
    mundo["caidas"].add("comentarios")

    assert pendientes.tick(ahora=epoch(9)) == 0
    assert mundo["escritos"] == []


def test_an_order_decided_between_the_listing_and_the_write_is_left_alone(mundo) -> None:
    """Confirmado por una persona mientras el barrido iba por la mitad."""
    mundo["borradores"].append(_borrador())
    mundo["docs"][PEDIDO] = _borrador(docstatus=1)

    assert pendientes.tick(ahora=epoch(9)) == 0
    assert mundo["escritos"] == []


def test_a_draft_with_a_live_decision_request_is_skipped(mundo, monkeypatch) -> None:
    """Ya tiene plazo, aviso al cliente y respaldo: app/solicitudes.py lo cubre."""
    mundo["borradores"].append(_borrador())
    mundo["docs"][PEDIDO] = _borrador()
    monkeypatch.setattr("app.solicitudes.vencimientos", lambda pedidos: {PEDIDO: 1.0})

    assert pendientes.tick(ahora=epoch(9)) == 0
    assert mundo["escritos"] == []


def test_one_bad_order_does_not_lose_the_round(mundo) -> None:
    """El que explota se loguea; los demás se anotan igual."""
    mundo["borradores"].extend([_borrador("SO-ROTO"), _borrador(PEDIDO)])
    mundo["docs"][PEDIDO] = _borrador()
    # SO-ROTO no está en docs: policy_get_doc levanta.

    assert pendientes.tick(ahora=epoch(9)) == 1
    assert [n for _, n, _ in mundo["escritos"]] == [PEDIDO]


def test_the_round_is_capped(mundo, monkeypatch) -> None:
    """Cada anotación es una evaluación completa: sin techo el barrido se cuelga."""
    monkeypatch.setattr(sombra, "POR_RONDA", 3)
    for i in range(10):
        nombre = f"SO-{i:04d}"
        mundo["borradores"].append(_borrador(nombre))
        mundo["docs"][nombre] = _borrador(nombre)

    assert pendientes.tick(ahora=epoch(9)) == 3
    assert len(mundo["escritos"]) == 3


def test_only_orders_the_agent_created_are_candidates(
    mundo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El origen se decide por la FORMA de po_no, en Python, no en el filtro.

    Se saca de la consulta a propósito: el resumen del dueño tiene que ver
    también los que cargó una persona, porque retienen stock igual y son suyos
    para limpiar. Lo que NO puede pasar es que el cierre —que es destructivo—
    confunda un número de orden de compra real con un pedido del agente.
    """
    mundo["borradores"].extend(
        [
            _borrador("SO-DEL-BOT"),
            _borrador("SO-A-MANO", po_no="OC-4471"),
            _borrador("SO-SIN-PO", po_no=""),
            _borrador("SO-MAYUSCULAS", po_no="WA-" + "A" * 40),
            _borrador("SO-CORTO", po_no="WA-" + "a" * 39),
        ]
    )

    todos = {f["name"] for f in pendientes.listar_esperando()}
    del_agente = {f["name"] for f in pendientes.listar_esperando(solo_del_agente=True)}

    # El dueño los ve todos.
    assert todos == {"SO-DEL-BOT", "SO-A-MANO", "SO-SIN-PO", "SO-MAYUSCULAS", "SO-CORTO"}
    # El recordatorio y el cierre, sólo el que tiene la forma exacta.
    assert del_agente == {"SO-DEL-BOT"}


def test_the_query_does_not_filter_by_origin_but_does_drop_closed_drafts(
    mundo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lo rechazado o cerrado ya no es un pendiente: lo descarta ERPNext."""
    visto: dict = {}

    def espia(doctype, filters=None, **kwargs):
        visto["doctype"] = doctype
        visto["filters"] = filters
        return []

    monkeypatch.setattr(erpnext, "policy_get_list", espia)
    pendientes.listar_esperando()

    from app import policy

    assert visto["doctype"] == "Sales Order"
    assert ["docstatus", "=", 0] in visto["filters"]
    assert ["status", "not in", list(policy.ESTADOS_SIN_RESERVA)] in visto["filters"]
    assert not any("po_no" in str(f) for f in visto["filters"])


def test_an_unreadable_listing_raises_for_the_digest_and_is_swallowed_for_the_sweep(
    mundo,
) -> None:
    """«No sé» no es «no hay ninguno», y el resumen tiene que poder decirlo."""
    mundo["caidas"].add("listar")

    with pytest.raises(erpnext.ERPNextError):
        pendientes.listar_esperando()

    assert pendientes.borradores_esperando() == []
    assert pendientes.tick(ahora=epoch(9)) == 0


def test_when_the_deadline_read_fails_nothing_is_eligible(mundo, monkeypatch) -> None:
    """Falla cerrado: una lectura ilegible no habilita NINGÚN borrador.

    Antes devolvía todos, y eso dejaba que el cierre le soltara el stock y le
    dijera «no se confirmó» a un cliente que TIENE una oferta viva con plazo:
    `_sigue_esperando` mira `docstatus` y `status`, no si hay una solicitud
    abierta, así que era la única puerta y estaba abierta.

    «No sé si hay un cliente esperando por este pedido» no es «ninguno tiene
    plazo». Una ronda salteada no le cuesta nada a nadie; ese mensaje sí.
    """
    def explota(pedidos):
        raise erpnext.ERPNextError("no contesta")

    monkeypatch.setattr("app.solicitudes.vencimientos", explota)

    assert pendientes._sin_solicitud_abierta([PEDIDO]) == []


def test_an_unreadable_deadline_read_stops_the_whole_round(mundo, monkeypatch) -> None:
    """Y de punta a punta: la ronda no anota, no avisa y no cierra."""
    monkeypatch.setenv("PENDIENTE_CIERRE_HORAS", "4")
    _listo(mundo)
    monkeypatch.setattr(
        "app.solicitudes.vencimientos",
        lambda pedidos: (_ for _ in ()).throw(erpnext.ERPNextError("no contesta")),
    )

    assert pendientes.tick(ahora=epoch(15)) == 0

    assert mundo["estados"] == []
    assert _al_cliente(mundo) == []
    assert not any(t.startswith(pendientes.MARCA_CIERRE) for _, _, t in mundo["escritos"])


# ------------------------------------------------------- el recordatorio (W2)


def test_a_draft_past_the_deadline_gets_exactly_one_reminder(mundo) -> None:
    _listo(mundo)  # creado 09:00, el plazo es 2 h

    assert pendientes.tick(ahora=epoch(15)) >= 1

    dichos = _al_cliente(mundo)
    assert len(dichos) == 1
    assert PEDIDO in dichos[0]
    # Y la marca durable quedó en el pedido.
    assert any(t.startswith(pendientes.MARCA_AVISO) for _, _, t in mundo["escritos"])


def test_a_draft_inside_the_deadline_is_left_alone(mundo) -> None:
    _listo(mundo)  # creado 09:00

    pendientes.tick(ahora=epoch(10))  # una hora: todavía no

    assert _al_cliente(mundo) == []


def test_the_reminder_does_not_repeat_across_rounds(mundo) -> None:
    _listo(mundo)

    pendientes.tick(ahora=epoch(15))
    pendientes.tick(ahora=epoch(16))
    pendientes.tick(ahora=epoch(17))

    assert len(_al_cliente(mundo)) == 1
    marcas = [t for _, _, t in mundo["escritos"] if t.startswith(pendientes.MARCA_AVISO)]
    assert len(marcas) == 1


def test_the_durable_mark_and_not_redis_is_what_stops_the_second_reminder(mundo) -> None:
    """El idioma de la casa: un FLUSHALL no puede volver a avisarle al cliente.

    La clave de idempotencia de la cola vive en Redis y se borra con el flush;
    la marca en ERPNext no. Si el guard fuera sólo la cola, este test manda dos.
    """
    _listo(mundo)
    pendientes.tick(ahora=epoch(15))
    assert len(_al_cliente(mundo)) == 1

    outbound_status.cliente().values.clear()
    outbound_status.cliente().zsets.clear()

    pendientes.tick(ahora=epoch(16))

    assert len(_al_cliente(mundo)) == 1  # sigue siendo uno


def test_nothing_is_sent_during_the_quiet_hours_and_it_goes_out_in_the_morning(
    mundo,
) -> None:
    """No se posterga un mensaje ya armado: se posterga la decisión de mandarlo."""
    _listo(mundo)

    # La sombra SÍ corre de noche: es un registro, no un mensaje. Lo que no
    # sale es lo que le habla a una persona.
    pendientes.tick(ahora=epoch(23, 30))
    assert _al_cliente(mundo) == []
    assert not any(t.startswith(pendientes.MARCA_AVISO) for _, _, t in mundo["escritos"])
    assert mundo["al_dueno"] == []

    pendientes.tick(ahora=epoch(7, 30, dia=9))

    assert len(_al_cliente(mundo)) == 1


def test_an_order_decided_overnight_is_never_told_it_is_unconfirmed(mundo) -> None:
    """Por qué la puerta va acá y no en la cola.

    Si el aviso se hubiera encolado a las 23:30, a las 07:00 saldría igual y le
    diría al cliente que su pedido sigue sin confirmar. Acá el pedido sigue
    siendo candidato y la ronda de la mañana vuelve a leer ERPNext.
    """
    _listo(mundo)
    pendientes.tick(ahora=epoch(23, 30))  # noche: no se encola nada

    # El dueño lo confirma a mano a las 23:40.
    mundo["docs"][PEDIDO] = {**mundo["docs"][PEDIDO], "docstatus": 1}

    pendientes.tick(ahora=epoch(7, 30, dia=9))

    assert _al_cliente(mundo) == []


def test_an_order_confirmed_between_the_listing_and_the_send_gets_nothing(mundo) -> None:
    _listo(mundo)
    mundo["docs"][PEDIDO] = {**mundo["docs"][PEDIDO], "docstatus": 1}

    pendientes.tick(ahora=epoch(15))

    assert _al_cliente(mundo) == []


def test_a_rejected_draft_is_not_a_pending_one(mundo) -> None:
    """Closed sale del filtro de ERPNext, y además la relectura lo descarta."""
    _listo(mundo, status="Closed")

    assert pendientes.listar_esperando() == [] or all(
        f.get("status") != "Closed" for f in pendientes.listar_esperando()
    )


def test_a_customer_with_no_phone_is_recorded_not_silently_dropped(
    mundo, monkeypatch
) -> None:
    _listo(mundo)
    monkeypatch.setattr("app.decisiones.telefono_del_cliente", lambda so: "")

    pendientes.tick(ahora=epoch(15))

    assert _al_cliente(mundo) == []
    assert any("no tiene" in t and "teléfono" in t for _, _, t in mundo["escritos"])


def test_a_hand_entered_draft_is_never_messaged(mundo) -> None:
    """El sistema no le escribe a un cliente por un pedido que no tomó."""
    _listo(mundo, "SO-A-MANO", po_no="OC-4471")

    pendientes.tick(ahora=epoch(15))

    assert _al_cliente(mundo) == []


def test_the_owner_is_reminded_once_a_day_not_once_a_minute(mundo) -> None:
    _listo(mundo)

    pendientes.tick(ahora=epoch(15))
    pendientes.tick(ahora=epoch(15, 1))
    pendientes.tick(ahora=epoch(15, 2))

    assert len(mundo["al_dueno"]) == 1
    asunto, cuerpo = mundo["al_dueno"][0]
    assert PEDIDO in cuerpo
    assert "1" in asunto


def test_the_reminder_uses_no_internal_jargon(mundo) -> None:
    """La misma lista que el banco de pruebas hace cumplir, en los dos idiomas."""
    for lengua in ("es", "en"):
        texto = pendientes.recordatorio_pendiente(PEDIDO, lengua).lower()
        for prohibido in (
            "borrador", "draft", "pendiente de revisión", "pending review",
            "el sistema", "the system", "quedó recibido", "was received",
        ):
            assert prohibido not in texto, f"{lengua}: {prohibido!r}"


# ------------------------------------------------------------ el cierre (W2)


def test_nothing_is_closed_while_the_closer_is_off(mundo) -> None:
    """El default es NINGUNO: nada cambia respecto de hoy."""
    _listo(mundo)

    pendientes.tick(ahora=epoch(15))

    assert mundo["estados"] == []


def test_a_draft_past_the_closing_deadline_is_closed_and_both_sides_told(
    mundo, monkeypatch
) -> None:
    monkeypatch.setenv("PENDIENTE_CIERRE_HORAS", "4")
    # Dos cosas que el `or True` que estaba acá tapaba:
    #   1. conftest fija TELEFONOS_EQUIPO="" a propósito, y `router.STAFF` se
    #      arma al importar, así que hay que recargarlo o `encolar_equipo` no
    #      le manda a nadie.
    #   2. `_al_cliente` llama a `avisos.procesar()`, que VACÍA la cola, así
    #      que la cola se lee ANTES de drenarla.
    # setattr y no setenv+recargar(): `recargar()` deja el global STAFF pisado
    # para toda la sesión, y esa es justo la fuga que conftest evita fijando
    # TELEFONOS_EQUIPO="". monkeypatch lo restaura al terminar el test.
    monkeypatch.setattr(router, "STAFF", ["5493510000001"])
    _listo(mundo)  # creado 09:00

    assert pendientes.tick(ahora=epoch(15)) >= 1

    assert mundo["estados"] == [(PEDIDO, "Closed")]
    assert f"pendiente:{PEDIDO}" in mundo["locks"]
    assert any(t.startswith(pendientes.MARCA_CIERRE) for _, _, t in mundo["escritos"])
    eventos = {e["evento"] for e in _en_cola()}
    assert any(ev.startswith("pendiente_cerrado_equipo") for ev in eventos), eventos
    dichos = _al_cliente(mundo)
    assert len(dichos) == 1 and PEDIDO in dichos[0]


def test_the_closer_writes_nothing_terminal_when_it_cannot_prove_the_release(
    mundo, monkeypatch
) -> None:
    """La regla dura: sin prueba de que soltó el stock, no se escribe el cierre."""
    monkeypatch.setenv("PENDIENTE_CIERRE_HORAS", "4")
    _listo(mundo)
    mundo["caidas"].add("cerrar")

    pendientes.tick(ahora=epoch(15))

    assert mundo["estados"] == []
    assert not any(t.startswith(pendientes.MARCA_CIERRE) for _, _, t in mundo["escritos"])
    # Al cliente NO se le dijo que su pedido no se confirmó. El recordatorio
    # de que sigue esperando es otra cosa, y sigue siendo verdad.
    cerrado = pendientes.pendiente_cerrado(PEDIDO, "es")
    assert cerrado not in _al_cliente(mundo)


def test_the_closer_leaves_alone_an_order_a_person_just_decided(
    mundo, monkeypatch
) -> None:
    monkeypatch.setenv("PENDIENTE_CIERRE_HORAS", "4")
    _listo(mundo)
    mundo["docs"][PEDIDO] = {**mundo["docs"][PEDIDO], "docstatus": 1}

    pendientes.tick(ahora=epoch(15))

    assert mundo["estados"] == []
    assert _al_cliente(mundo) == []


def test_a_contended_lock_is_a_skipped_round_not_a_partial_change(
    mundo, monkeypatch
) -> None:
    monkeypatch.setenv("PENDIENTE_CIERRE_HORAS", "4")
    _listo(mundo)
    mundo["caidas"].add("lock")

    pendientes.tick(ahora=epoch(15))

    assert mundo["estados"] == []
    assert not any(t.startswith(pendientes.MARCA_CIERRE) for _, _, t in mundo["escritos"])


def test_the_closer_never_touches_a_hand_entered_draft(mundo, monkeypatch) -> None:
    """Cerrar el borrador de una persona no es decisión del sistema."""
    monkeypatch.setenv("PENDIENTE_CIERRE_HORAS", "4")
    _listo(mundo, "SO-A-MANO", po_no="OC-4471")

    pendientes.tick(ahora=epoch(15))

    assert mundo["estados"] == []


def test_the_closer_does_not_close_twice(mundo, monkeypatch) -> None:
    monkeypatch.setenv("PENDIENTE_CIERRE_HORAS", "4")
    _listo(mundo)

    pendientes.tick(ahora=epoch(15))
    pendientes.tick(ahora=epoch(16))

    assert mundo["estados"] == [(PEDIDO, "Closed")]


# --------------------------------------------- el resumen de las 18:00 (W2)


def test_the_digest_shows_every_waiting_draft_broken_out_by_origin(mundo) -> None:
    """El número va DESCOMPUESTO, no reconciliado.

    Un borrador que una persona cargó a mano retiene stock igual y cuenta para
    el mismo techo, así que el dueño tiene que verlo — el recordatorio no lo
    toca y el cierre tampoco. Y el total de esta sección no puede discutir con
    el del recordatorio, que cuenta sólo los del bot: por eso se separan en vez
    de sumarse en un número solo.
    """
    from app import digest

    _listo(mundo, "SO-BOT-1")
    _listo(mundo, "SO-BOT-2")
    _listo(mundo, "SO-A-MANO", po_no="OC-4471")

    texto = digest.seccion_pendientes()

    assert "2 del bot + 1 cargados a mano" in texto
    assert "SO-BOT-1" in texto and "SO-A-MANO" in texto
    assert "cargado a mano" in texto
    # Y la edad, que es el dato con el que decide a cuál atender primero.
    assert " h" in texto


def test_the_digest_says_it_could_not_read_instead_of_none(mundo) -> None:
    """«No sé» no puede leerse como «no hay ninguno esperando»."""
    from app import digest

    mundo["caidas"].add("listar")

    assert "no pude leer" in digest.seccion_pendientes()


def test_the_digest_omits_the_breakdown_when_everything_is_the_bots(mundo) -> None:
    from app import digest

    _listo(mundo, "SO-BOT-1")

    texto = digest.seccion_pendientes()

    assert "cargados a mano" not in texto
    assert "SO-BOT-1" in texto


def test_a_duplicate_enqueue_still_marks_so_the_sweep_stops_asking(
    mundo, monkeypatch
) -> None:
    """Un False de la cola es «ya existe», no un fallo: la marca va igual.

    Sin esto el barrido vuelve a preguntar por el mismo pedido cada 60 s para
    siempre, gastando dos lecturas de ERPNext por ronda y por pedido.
    """
    _listo(mundo)
    monkeypatch.setattr(avisos, "encolar", lambda *a, **k: False)

    pendientes.tick(ahora=epoch(15))

    assert any(t.startswith(pendientes.MARCA_AVISO) for _, _, t in mundo["escritos"])


def test_an_enqueue_that_raises_leaves_no_mark_so_it_is_retried(
    mundo, monkeypatch
) -> None:
    """Lo contrario: si la cola no aceptó nada, el pedido queda sin marca."""
    _listo(mundo)

    def explota(*a, **k):
        raise RuntimeError("redis lo rechazó")

    monkeypatch.setattr(avisos, "encolar", explota)

    pendientes.tick(ahora=epoch(15))

    assert not any(t.startswith(pendientes.MARCA_AVISO) for _, _, t in mundo["escritos"])


def test_nothing_is_sent_while_the_reminder_is_off(mundo, monkeypatch) -> None:
    """El default es NINGUNO: desplegar W2 no le habla a ningún cliente.

    Las dos mitades —recordatorio y cierre— arrancan apagadas y las enciende el
    dueño con su código. Un default de 2 h significaba que entre el deploy y el
    mensaje que lo apagara había clientes recibiendo avisos de una función que
    nadie armó, y que quedaba encendida si nadie se acordaba de mandarlo.
    """
    monkeypatch.delenv("PENDIENTE_AVISO_HORAS")  # el fixture lo arma; acá no
    _listo(mundo)

    pendientes.tick(ahora=epoch(15))

    assert _al_cliente(mundo) == []
    assert mundo["al_dueno"] == []
    assert not any(t.startswith(pendientes.MARCA_AVISO) for _, _, t in mundo["escritos"])


def test_both_halves_are_off_by_default(mundo, monkeypatch) -> None:
    """Ni aviso ni cierre sin que el dueño los haya fijado."""
    monkeypatch.delenv("PENDIENTE_AVISO_HORAS")
    _listo(mundo)

    pendientes.tick(ahora=epoch(15))

    assert _al_cliente(mundo) == []
    assert mundo["estados"] == []


# ------------------- la reclamación del día se devuelve si el aviso no salió


def test_a_failed_owner_reminder_does_not_burn_the_whole_day(mundo, monkeypatch):
    """`avisar_dueno` no levanta: avisa con un False, y hay que escucharlo.

    Quedándose la reclamación de 24 h igual, un fallo transitorio (sin
    TELEFONO_DUENO, o Meta que rechaza sin plantilla) dejaba al dueño sin el
    recordatorio hasta el día siguiente, con borradores vivos retenidos.
    """
    intentos: list[str] = []

    def falla(asunto, cuerpo, **kw):
        intentos.append(asunto)
        return False

    monkeypatch.setattr("app.notificar.avisar_dueno", falla)
    _listo(mundo)  # creado 09:00, con PENDIENTE_AVISO_HORAS=2

    pendientes.tick(ahora=epoch(15))
    assert len(intentos) == 1

    # La siguiente ronda del MISMO día vuelve a intentarlo, porque la
    # reclamación se devolvió.
    pendientes.tick(ahora=epoch(16))
    assert len(intentos) == 2


def test_a_delivered_owner_reminder_still_goes_out_once_a_day(mundo):
    """El otro lado de lo mismo: entregado, la reclamación se queda."""
    _listo(mundo)

    pendientes.tick(ahora=epoch(15))
    pendientes.tick(ahora=epoch(16))

    assert len(mundo["al_dueno"]) == 1


# ------------- el cierre avisa aunque la marca de auditoría no haya quedado


def test_the_closure_notices_go_out_even_if_the_marker_write_fails(
    mundo, monkeypatch
):
    """El cierre ya pasó, así que callarse es el peor final posible.

    La próxima ronda no vuelve a mirar este pedido (`_sigue_esperando` lo ve
    cerrado), así que si el fallo de la marca se comiera los avisos, el cliente
    y el equipo no se enterarían NUNCA de que el borrador se cerró.
    """
    monkeypatch.setenv("PENDIENTE_CIERRE_HORAS", "4")
    monkeypatch.setattr(router, "STAFF", ["5493510000001"])
    _listo(mundo)

    real = erpnext.registrar_comentario

    def falla_solo_la_marca(doctype, name, texto):
        if texto.startswith(pendientes.MARCA_CIERRE):
            raise erpnext.ERPNextError("ERPNext no acepta este comentario")
        return real(doctype, name, texto)

    monkeypatch.setattr(erpnext, "add_comment", falla_solo_la_marca)

    pendientes.tick(ahora=epoch(15))

    assert mundo["estados"] == [(PEDIDO, "Closed")]
    eventos = {e["evento"] for e in _en_cola()}
    assert any(ev.startswith("pendiente_cerrado_equipo") for ev in eventos), eventos
    assert any(ev.startswith("pendiente_cerrado:") or ev == "pendiente_cerrado" for ev in eventos), eventos


# ------------- los dos avisos al cliente salen SIEMPRE fuera de la ventana


def test_the_reminder_carries_a_template_because_it_always_leaves_the_window(
    mundo,
) -> None:
    """Las horas se cuentan desde que se creó el borrador.

    O sea desde el último mensaje del cliente: con el `aviso de pendiente 48`
    recomendado, esto sale un día entero después de que la ventana de 24 h se
    cerró. Sin plantilla no falla a veces, falla SIEMPRE — y el dueño recibe un
    «no se pudo entregar» en vez de que el cliente reciba el aviso.
    """
    _listo(mundo)  # PENDIENTE_AVISO_HORAS=2

    pendientes.tick(ahora=epoch(15))

    avisados = [e for e in _en_cola() if e["evento"].startswith("pendiente_aviso")]
    assert len(avisados) == 1
    assert avisados[0]["plantilla_env"] == pendientes.PLANTILLA_RECORDATORIO
    assert avisados[0]["parametros"] == [PEDIDO]


def test_the_closure_notice_carries_its_template_too(mundo, monkeypatch) -> None:
    """Y este es peor: hasta 168 h después, y el stock ya se soltó."""
    monkeypatch.setenv("PENDIENTE_CIERRE_HORAS", "4")
    _listo(mundo)

    pendientes.tick(ahora=epoch(15))

    cerrados = [
        e for e in _en_cola() if e["evento"].startswith("pendiente_cerrado")
        and not e["evento"].startswith("pendiente_cerrado_equipo")
    ]
    assert len(cerrados) == 1
    assert cerrados[0]["plantilla_env"] == pendientes.PLANTILLA_CERRADO
    assert cerrados[0]["parametros"] == [PEDIDO]
