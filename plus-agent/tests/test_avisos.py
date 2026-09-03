"""The customer's confirmation is data, not something the model must remember.

Before this, app/tools/pedidos.py returned a PEDIDO_CONFIRMADO token and the
prompt asked the model to "decí confirmado". If the model paraphrased it away,
or the turn failed after ERPNext had already submitted the order, the customer
was never told — and the "already informed" marker was set anyway, so no later
path sent one either. These tests pin the replacement: an authoritative notice
built from the document, queued once per (event, order), retried, and parked
with a follow-up task when it truly cannot be delivered.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avisos, erpnext, notificar, outbound_status, whatsapp
from tests.fakes import FakeMarcas, entrada_de_cola

SO = "SAL-ORD-2026-00011"
CUSTOMER_PHONE = "5493512222222"

PEDIDO = {
    "name": SO,
    "customer": "CUST-001",
    "customer_name": "Kiosco La Esquina",
    "docstatus": 1,
    "currency": "ARS",
    "grand_total": 4800,
    "delivery_date": "2026-09-05",
    "items": [
        {"item_code": "LECHE-1L", "item_name": "Leche entera 1 L", "qty": 5, "uom": "Litro"}
    ],
}


@pytest.fixture
def canal(monkeypatch: pytest.MonkeyPatch):
    marcas = FakeMarcas()
    monkeypatch.setattr(outbound_status, "_client", marcas)
    monkeypatch.setattr(
        erpnext, "policy_get_doc", lambda dt, name: {"name": name, "mobile_no": CUSTOMER_PHONE}
    )
    monkeypatch.setattr(notificar, "_direccion_de_entrega", lambda so: "Av. Colón 1234")
    comentarios: list[str] = []
    monkeypatch.setattr(erpnext, "add_comment", lambda dt, name, text: comentarios.append(text))
    todos: list[dict] = []
    monkeypatch.setattr(
        erpnext,
        "create_doc",
        lambda dt, payload: todos.append(payload) or {"name": f"TD-{len(todos)}"},
    )
    enviados: list[tuple[str, str]] = []
    monkeypatch.setattr(
        whatsapp,
        "enviar_mensaje",
        lambda tel, texto: enviados.append((tel, texto))
        or {"messages": [{"id": f"wamid.{len(enviados)}"}]},
    )
    plantillas: list[tuple] = []
    monkeypatch.setattr(
        whatsapp,
        "enviar_plantilla",
        lambda *args: plantillas.append(args) or {"messages": [{"id": "wamid.tpl"}]},
    )
    monkeypatch.setattr(avisos, "window_open", lambda tel: tel == CUSTOMER_PHONE)
    monkeypatch.delenv("WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE", raising=False)
    monkeypatch.delenv("AVISOS_MAX_INTENTOS", raising=False)
    monkeypatch.delenv("AVISOS_REINTENTO_SEGUNDOS", raising=False)
    return {
        "marcas": marcas,
        "enviados": enviados,
        "plantillas": plantillas,
        "comentarios": comentarios,
        "todos": todos,
    }


def _en_cola(canal) -> list[dict]:
    return [json.loads(e) for e in entrada_de_cola(canal["marcas"], avisos.COLA)]


# ---------------------------------------------------------------- the content


def test_the_confirmation_carries_order_items_total_and_fulfilment(canal) -> None:
    texto = avisos.texto_confirmacion_cliente(PEDIDO)

    assert SO in texto
    assert "5 Litro" in texto and "Leche entera 1 L" in texto
    assert "4.800,00 ARS" in texto
    assert "Av. Colón 1234" in texto and "2026-09-05" in texto
    # Bilingual, because outside a model turn the customer's language is unknown.
    assert "confirmado" in texto and "confirmed" in texto


def test_the_text_does_not_depend_on_any_model_output(canal) -> None:
    """Same document in, same words out — no prompt, no paraphrase."""
    assert avisos.texto_confirmacion_cliente(PEDIDO) == avisos.texto_confirmacion_cliente(
        dict(PEDIDO)
    )


# --------------------------------------------------------------- the enqueue


def test_confirming_queues_exactly_one_notice_for_the_customer(canal) -> None:
    assert avisos.confirmacion_cliente(PEDIDO) is True

    (entrada,) = _en_cola(canal)
    assert entrada["pedido"] == SO
    assert entrada["evento"] == avisos.EVENTO_CONFIRMACION
    assert entrada["telefono"] == CUSTOMER_PHONE
    assert SO in entrada["texto"]


def test_a_second_call_for_the_same_order_queues_nothing(canal) -> None:
    """The idempotency key is (event, order): the automatic path and a manager
    tapping Confirmar cannot produce two confirmations."""
    assert avisos.confirmacion_cliente(PEDIDO) is True
    assert avisos.confirmacion_cliente(PEDIDO) is False
    assert avisos.confirmacion_cliente({**PEDIDO, "grand_total": 9999}) is False

    assert len(_en_cola(canal)) == 1


def test_the_idempotency_key_is_written_with_the_entry_not_before_it(canal) -> None:
    """A marker written first, with the enqueue then failing, would mean a
    customer marked as informed who never hears anything. So the Lua script
    writes both or neither."""
    canal["marcas"].caido = True

    with pytest.raises(RuntimeError):
        avisos.confirmacion_cliente(PEDIDO)

    canal["marcas"].caido = False
    assert _en_cola(canal) == []
    assert avisos.confirmacion_cliente(PEDIDO) is True
    assert len(_en_cola(canal)) == 1


def test_an_order_whose_customer_has_no_phone_is_flagged_not_queued(
    canal, monkeypatch
) -> None:
    monkeypatch.setattr(erpnext, "policy_get_doc", lambda dt, name: {"name": name})

    assert avisos.confirmacion_cliente(PEDIDO) is False
    assert _en_cola(canal) == []
    assert any("no tiene teléfono cargado" in c for c in canal["comentarios"])


def test_a_notice_meta_already_accepted_is_not_queued_again(canal, monkeypatch) -> None:
    monkeypatch.setattr(avisos, "has_accepted", lambda pedido, evento: True)

    assert avisos.confirmacion_cliente(PEDIDO) is False
    assert _en_cola(canal) == []


# --------------------------------------------------------------- the delivery


def test_the_worker_sends_free_text_inside_the_customer_window(canal) -> None:
    avisos.confirmacion_cliente(PEDIDO)

    assert avisos.procesar() == 1

    ((tel, texto),) = canal["enviados"]
    assert tel == CUSTOMER_PHONE and SO in texto
    assert canal["plantillas"] == []
    assert _en_cola(canal) == []


def test_a_delivered_notice_is_recorded_and_never_sent_twice(canal) -> None:
    avisos.confirmacion_cliente(PEDIDO)
    avisos.procesar()

    assert avisos.procesar() == 0
    assert len(canal["enviados"]) == 1


def test_outside_the_window_a_configured_template_is_used(canal, monkeypatch) -> None:
    monkeypatch.setattr(avisos, "window_open", lambda tel: False)
    monkeypatch.setenv("WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE", "pedido_confirmado_cliente")
    avisos.confirmacion_cliente(PEDIDO)

    avisos.procesar()

    (llamada,) = canal["plantillas"]
    assert llamada[0] == CUSTOMER_PHONE
    assert llamada[1] == "pedido_confirmado_cliente"
    assert llamada[3] == [SO, "2026-09-05"]
    assert canal["enviados"] == []


def test_outside_the_window_with_no_template_the_notice_waits(canal, monkeypatch) -> None:
    """Templates are optional in this pilot, so "no channel right now" is a
    retry: the customer usually writes again and reopens their own window."""
    monkeypatch.setattr(avisos, "window_open", lambda tel: False)
    avisos.confirmacion_cliente(PEDIDO)

    avisos.procesar()

    assert canal["enviados"] == [] and canal["plantillas"] == []
    assert len(_en_cola(canal)) == 1
    assert canal["todos"] == []


def test_a_notice_that_waited_goes_out_when_the_window_reopens(canal, monkeypatch) -> None:
    ventana = {"abierta": False}
    monkeypatch.setattr(avisos, "window_open", lambda tel: ventana["abierta"])
    avisos.confirmacion_cliente(PEDIDO)
    avisos.procesar()
    assert canal["enviados"] == []

    ventana["abierta"] = True
    _adelantar(canal)

    assert avisos.procesar() == 1
    assert len(canal["enviados"]) == 1


def _adelantar(canal) -> None:
    """Make every queued notice due now, the way waiting would."""
    cola = canal["marcas"].zsets.get(avisos.COLA, {})
    for miembro in list(cola):
        cola[miembro] = time.time() - 1


# ----------------------------------------------------------- retry and giving up


def test_a_transient_failure_is_retried_with_backoff(canal, monkeypatch) -> None:
    fallo = whatsapp.WhatsAppSendError("timeout", permanent=False)
    monkeypatch.setattr(whatsapp, "enviar_mensaje", Mock(side_effect=fallo))
    avisos.confirmacion_cliente(PEDIDO)

    avisos.procesar()

    ((_, cuando),) = list(canal["marcas"].zsets[avisos.COLA].items())
    assert cuando > time.time() + 10
    assert canal["todos"] == []
    assert avisos.procesar() == 0  # not due yet


def test_a_permanent_rejection_is_parked_immediately_with_one_todo(
    canal, monkeypatch
) -> None:
    fallo = whatsapp.WhatsAppSendError("token vencido", permanent=True)
    monkeypatch.setattr(whatsapp, "enviar_mensaje", Mock(side_effect=fallo))
    avisos.confirmacion_cliente(PEDIDO)

    avisos.procesar()

    assert _en_cola(canal) == []
    assert canal["marcas"].llen(outbound_status.DEAD_NOTIFY_KEY) == 1
    assert len(canal["todos"]) == 1 and SO in canal["todos"][0]["description"]
    assert any("NO entregado" in c for c in canal["comentarios"])


def test_retries_are_bounded_and_end_in_a_dead_letter(canal, monkeypatch) -> None:
    monkeypatch.setenv("AVISOS_MAX_INTENTOS", "3")
    monkeypatch.setattr(
        whatsapp,
        "enviar_mensaje",
        Mock(side_effect=whatsapp.WhatsAppSendError("timeout", permanent=False)),
    )
    avisos.confirmacion_cliente(PEDIDO)

    for _ in range(3):
        _adelantar(canal)
        avisos.procesar()

    assert _en_cola(canal) == []
    assert canal["marcas"].llen(outbound_status.DEAD_NOTIFY_KEY) == 1
    assert len(canal["todos"]) == 1


def test_the_parked_entry_carries_no_phone_number(canal, monkeypatch) -> None:
    monkeypatch.setattr(
        whatsapp,
        "enviar_mensaje",
        Mock(side_effect=whatsapp.WhatsAppSendError("token", permanent=True)),
    )
    avisos.confirmacion_cliente(PEDIDO)
    avisos.procesar()

    (parked,) = canal["marcas"].lists[outbound_status.DEAD_NOTIFY_KEY]

    assert CUSTOMER_PHONE not in parked
    assert json.loads(parked)["order_name"] == SO


def test_a_second_failure_for_the_same_order_does_not_open_a_second_todo(
    canal, monkeypatch
) -> None:
    monkeypatch.setattr(
        whatsapp,
        "enviar_mensaje",
        Mock(side_effect=whatsapp.WhatsAppSendError("token", permanent=True)),
    )
    avisos.confirmacion_cliente(PEDIDO)
    avisos.procesar()
    # The order is confirmed again elsewhere and the notice is re-queued.
    canal["marcas"].values.pop(
        avisos._clave_encolado(avisos.EVENTO_CONFIRMACION, SO), None
    )
    avisos.confirmacion_cliente(PEDIDO)
    avisos.procesar()

    assert len(canal["todos"]) == 1


# ------------------------------------------------------------------ durability


def test_a_queued_notice_survives_a_worker_that_dies_mid_send(canal) -> None:
    """Claiming leases the entry instead of removing it, so a crash between the
    claim and the send loses nothing."""
    avisos.confirmacion_cliente(PEDIDO)

    reclamado = avisos._reclamar(time.time())

    assert reclamado is not None
    assert len(_en_cola(canal)) == 1  # still there, leased into the future
    assert avisos._reclamar(time.time()) is None  # no second worker takes it
    _adelantar(canal)
    assert avisos.procesar() == 1
    assert len(canal["enviados"]) == 1


def test_an_unreadable_entry_is_discarded_without_stopping_the_queue(canal) -> None:
    canal["marcas"].zadd(avisos.COLA, {"no es json": time.time() - 1})
    avisos.confirmacion_cliente(PEDIDO)

    assert avisos.procesar() == 2
    assert len(canal["enviados"]) == 1


def test_a_redis_that_is_down_does_not_raise_out_of_the_worker(canal) -> None:
    avisos.confirmacion_cliente(PEDIDO)
    canal["marcas"].caido = True

    assert avisos.procesar() == 0

    canal["marcas"].caido = False
    assert avisos.procesar() == 1


def test_the_queue_length_is_reportable(canal) -> None:
    assert avisos.pendientes() == 0
    avisos.confirmacion_cliente(PEDIDO)
    assert avisos.pendientes() == 1
    canal["marcas"].caido = True
    assert avisos.pendientes() == -1


# ------------------------------------------------------- both confirmation paths


def test_the_automatic_path_queues_the_confirmation_and_the_durable_record(
    canal, monkeypatch
) -> None:
    from app import confirmacion
    from app.tools import pedidos

    registrado: list[tuple] = []
    monkeypatch.setattr(
        confirmacion, "registrar", lambda pedido, fuente: registrado.append((pedido, fuente))
    )
    monkeypatch.setattr(pedidos, "notificar_confirmacion", lambda so, fuente: True)

    pedidos._notificar_confirmada(PEDIDO)

    assert registrado == [(SO, "automática (política)")]
    assert len(_en_cola(canal)) == 1


def test_the_model_can_still_talk_but_owns_nothing(canal, monkeypatch) -> None:
    """The tool result keeps its token for the conversation; the fact does not
    depend on the model repeating it."""
    from app.tools import pedidos

    resultado = pedidos._order_result(PEDIDO, [], "2026-09-05")

    assert resultado.startswith("PEDIDO_CONFIRMADO")
    # ...and the authoritative notice is queued independently of that string.
    avisos.confirmacion_cliente(PEDIDO)
    assert len(_en_cola(canal)) == 1
