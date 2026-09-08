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
from datetime import date

import pytest

from app import erpnext, pendientes, policy, sombra

PEDIDO = "SAL-ORD-2026-00042"


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
        if "add_comment" in caidas:
            raise erpnext.ERPNextError("ERPNext no acepta comentarios")
        escritos.append((doctype, name, texto))
        comentarios.setdefault(name, []).append(
            {"content": texto, "reference_name": name, "creation": f"2026-09-08 10:0{len(escritos)}:00", "name": f"c{len(escritos)}"}
        )
        return {}

    def policy_get_list(doctype, filters=None, fields=None, limit=20, **kwargs):
        if doctype == "Sales Order":
            if "listar" in caidas:
                raise erpnext.ERPNextError("ERPNext no contesta")
            return list(borradores)[:limit]
        if doctype == "Comment":
            if "comentarios" in caidas:
                raise erpnext.ERPNextError("ERPNext no contesta")
            pedidos = None
            for campo, operador, valor in filters or []:
                if campo == "reference_name":
                    pedidos = [valor] if operador == "=" else list(valor)
            filas = []
            for nombre, lista in comentarios.items():
                if pedidos is not None and nombre not in pedidos:
                    continue
                filas.extend(lista)
            filas.sort(key=lambda f: f["creation"], reverse=True)
            return filas[:limit]
        return []

    def policy_get_doc(doctype, name):
        if name not in docs:
            raise erpnext.ERPNextError(f"{name} no existe")
        return dict(docs[name])

    monkeypatch.setattr(erpnext, "add_comment", add_comment)
    monkeypatch.setattr(erpnext, "policy_get_list", policy_get_list)
    monkeypatch.setattr(erpnext, "policy_get_doc", policy_get_doc)
    monkeypatch.setattr(policy, "evaluar_sombra", lambda so: _sombra_verde())
    monkeypatch.setattr("app.solicitudes.vencimientos", lambda pedidos: {})
    monkeypatch.setenv("AUTO_CONFIRM_SOMBRA", "true")
    return {
        "escritos": escritos,
        "comentarios": comentarios,
        "borradores": borradores,
        "docs": docs,
        "caidas": caidas,
    }


def _borrador(nombre: str = PEDIDO, **extra) -> dict:
    base = {
        "name": nombre,
        "customer": "CUST-0007",
        "customer_name": "Panadería López",
        "grand_total": 8450.0,
        "creation": "2026-09-08 09:00:00",
        "docstatus": 0,
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

    assert pendientes.tick() == 0
    assert mundo["escritos"] == []


def test_the_sweep_annotates_a_waiting_draft(mundo) -> None:
    mundo["borradores"].append(_borrador())
    mundo["docs"][PEDIDO] = _borrador()

    assert pendientes.tick() == 1
    assert sombra.leer(PEDIDO)["pasa_reglas"] is True


def test_the_sweep_is_idempotent_across_rounds(mundo) -> None:
    """El comentario ES la marca: mil rondas, un registro."""
    mundo["borradores"].append(_borrador())
    mundo["docs"][PEDIDO] = _borrador()

    assert pendientes.tick() == 1
    assert pendientes.tick() == 0
    assert pendientes.tick() == 0
    assert len(mundo["escritos"]) == 1


def test_the_sweep_does_not_annotate_blind_when_erpnext_will_not_say(mundo) -> None:
    """ya_anotado None: anotar a ciegas le deja dos registros al pedido."""
    mundo["borradores"].append(_borrador())
    mundo["docs"][PEDIDO] = _borrador()
    mundo["caidas"].add("comentarios")

    assert pendientes.tick() == 0
    assert mundo["escritos"] == []


def test_an_order_decided_between_the_listing_and_the_write_is_left_alone(mundo) -> None:
    """Confirmado por una persona mientras el barrido iba por la mitad."""
    mundo["borradores"].append(_borrador())
    mundo["docs"][PEDIDO] = _borrador(docstatus=1)

    assert pendientes.tick() == 0
    assert mundo["escritos"] == []


def test_a_draft_with_a_live_decision_request_is_skipped(mundo, monkeypatch) -> None:
    """Ya tiene plazo, aviso al cliente y respaldo: app/solicitudes.py lo cubre."""
    mundo["borradores"].append(_borrador())
    mundo["docs"][PEDIDO] = _borrador()
    monkeypatch.setattr("app.solicitudes.vencimientos", lambda pedidos: {PEDIDO: 1.0})

    assert pendientes.tick() == 0
    assert mundo["escritos"] == []


def test_one_bad_order_does_not_lose_the_round(mundo) -> None:
    """El que explota se loguea; los demás se anotan igual."""
    mundo["borradores"].extend([_borrador("SO-ROTO"), _borrador(PEDIDO)])
    mundo["docs"][PEDIDO] = _borrador()
    # SO-ROTO no está en docs: policy_get_doc levanta.

    assert pendientes.tick() == 1
    assert [n for _, n, _ in mundo["escritos"]] == [PEDIDO]


def test_the_round_is_capped(mundo, monkeypatch) -> None:
    """Cada anotación es una evaluación completa: sin techo el barrido se cuelga."""
    monkeypatch.setattr(sombra, "POR_RONDA", 3)
    for i in range(10):
        nombre = f"SO-{i:04d}"
        mundo["borradores"].append(_borrador(nombre))
        mundo["docs"][nombre] = _borrador(nombre)

    assert pendientes.tick() == 3
    assert len(mundo["escritos"]) == 3


def test_an_unreadable_listing_is_not_an_empty_queue(mundo) -> None:
    mundo["caidas"].add("listar")

    assert pendientes.borradores_esperando() == []
    assert pendientes.tick() == 0


def test_only_orders_the_agent_created_are_candidates(
    mundo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un borrador que alguien cargó a mano en ERPNext no es nuestro.

    El filtro viaja en la consulta, así que lo que se prueba es que se pide —
    no que ERPNext lo aplique, que es su trabajo.
    """
    visto: dict = {}

    def espia(doctype, filters=None, **kwargs):
        visto["doctype"] = doctype
        visto["filters"] = filters
        return []

    monkeypatch.setattr(erpnext, "policy_get_list", espia)
    pendientes.borradores_esperando()

    assert visto["doctype"] == "Sales Order"
    assert ["docstatus", "=", 0] in visto["filters"]
    assert ["po_no", "like", f"{pendientes.PREFIJO_AGENTE}%"] in visto["filters"]


def test_when_the_deadline_read_fails_everything_is_still_eligible(mundo, monkeypatch) -> None:
    """De los dos errores posibles, anotar uno de más no le hace nada a nadie."""
    def explota(pedidos):
        raise erpnext.ERPNextError("no contesta")

    monkeypatch.setattr("app.solicitudes.vencimientos", explota)

    assert pendientes._sin_solicitud_abierta([PEDIDO]) == [PEDIDO]
