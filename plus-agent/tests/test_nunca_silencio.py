"""Never silence: every accepted inbound message produces SOME outbound reply.

A bot that goes quiet is worse than no bot: the customer thinks the order was
taken. These tests drive ``app/main.py`` with the exact harness that
``test_whatsapp_webhook.py`` uses (FakeRedis, stubbed graph/whatsapp modules,
direct ``inbound`` + ``_worker_cycle`` calls) and count outbound messages.
"""
from __future__ import annotations

import sys

import pytest

# Same harness, same module: pytest's default import mode puts tests/ on
# sys.path, and importing only the fixture and helpers does not re-collect
# the other file's tests.
from test_whatsapp_webhook import (  # noqa: F401  (webhook is a fixture)
    FakeRedis,
    _message_payload,
    _post,
    webhook,
)

CUSTOMER = "5491112345678"
STAFF = "5493519999999"


def _payload(message_id: str, tipo: str, phone: str = CUSTOMER, **extra) -> dict:
    message = {"id": message_id, "from": phone, "type": tipo, **extra}
    return {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}


@pytest.fixture
def outbox(webhook, monkeypatch):
    """Record every outbound (free-form or template) as (recipient, text)."""
    sent: list[tuple[str, str]] = []
    counter = {"n": 0}

    def send(recipient, text):
        counter["n"] += 1
        sent.append((recipient, text))
        return {"messages": [{"id": f"wamid.out.{counter['n']}"}]}

    def send_template(recipient, name, language, params, actions=None):
        counter["n"] += 1
        sent.append((recipient, f"template:{name}:{params}"))
        return {"messages": [{"id": f"wamid.out.{counter['n']}"}]}

    monkeypatch.setattr(webhook, "enviar_mensaje", send)
    whatsapp_stub = sys.modules["app.whatsapp"]
    monkeypatch.setattr(whatsapp_stub, "enviar_mensaje", send, raising=False)
    monkeypatch.setattr(whatsapp_stub, "enviar_plantilla", send_template, raising=False)
    monkeypatch.setattr(webhook, "_contexto", lambda phone: ("CUST-001", "cliente"))
    return sent


@pytest.mark.parametrize(
    ("tipo", "extra"),
    [
        ("audio", {"audio": {"id": "media-1", "mime_type": "audio/ogg"}}),
        ("image", {"image": {"id": "media-2", "caption": "esto quiero"}}),
        ("sticker", {"sticker": {"id": "media-3"}}),
        ("document", {"document": {"id": "media-4", "filename": "pedido.pdf"}}),
        ("location", {"location": {"latitude": -31.4, "longitude": -64.2}}),
        ("video", {"video": {"id": "media-5"}}),
        ("contacts", {"contacts": [{"name": {"formatted_name": "X"}}]}),
        ("unknown", {}),
    ],
    ids=lambda v: v if isinstance(v, str) else "payload",
)
def test_non_text_message_always_gets_a_reply_instead_of_silence(
    webhook, outbox, monkeypatch, tipo, extra
) -> None:
    agent = []
    monkeypatch.setattr(
        webhook, "responder_cliente", lambda text, **kw: agent.append(text) or "final"
    )

    response = _post(webhook, _payload(f"wamid.{tipo}", tipo, **extra))
    outcome = webhook._worker_cycle()

    assert response.status_code == 200
    assert outcome == "worked"
    replies = [text for recipient, text in outbox if recipient == CUSTOMER]
    assert len(replies) >= 1, "zero outbound messages: the customer was left in silence"
    assert replies == [webhook.texto_solo_texto("es")]
    # Media never reaches the LLM (nothing to transcribe/see) and the queue drains.
    assert agent == []
    assert not webhook.r.lists[webhook._PROCESSING_KEY]
    assert not webhook.r.lists[webhook._QUEUE_KEY]


def test_text_message_with_empty_body_still_gets_a_reply(webhook, outbox, monkeypatch) -> None:
    monkeypatch.setattr(webhook, "responder_cliente", lambda text, **kw: "final")

    _post(webhook, _payload("wamid.empty", "text", text={"body": ""}))
    webhook._worker_cycle()

    assert [t for r, t in outbox if r == CUSTOMER] == [webhook.texto_solo_texto("es")]


def test_non_text_from_a_staff_phone_also_gets_a_reply(webhook, outbox, monkeypatch) -> None:
    monkeypatch.setattr(webhook, "es_equipo", lambda phone: True)
    monkeypatch.setattr(
        webhook, "responder_gerencia", lambda *a, **kw: pytest.fail("no debe llamar al agente")
    )

    _post(webhook, _payload("wamid.staff-audio", "audio", phone=STAFF, audio={"id": "m"}))
    webhook._worker_cycle()

    assert [t for r, t in outbox if r == STAFF] == [webhook.texto_solo_texto("es")]


# --------------------------------------------------------------------------
# The agent blows up.
# --------------------------------------------------------------------------


def _agent_that_raises(*args, **kwargs):
    raise RuntimeError("secret stack trace with customer data")


def test_agent_crash_still_sends_the_customer_an_apology(
    webhook, outbox, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(webhook, "responder_cliente", _agent_that_raises)

    _post(webhook, _message_payload("wamid.crash", "quiero 5 kg de queso"))
    outcome = webhook._worker_cycle()

    assert outcome == "worked"
    replies = [t for r, t in outbox if r == CUSTOMER]
    assert replies == [webhook.texto_ack("es"), webhook.texto_error_tecnico("es")]
    # The exception body never reaches the customer or the logs.
    assert "secret stack trace" not in " ".join(replies)
    assert "secret stack trace" not in capsys.readouterr().out
    assert not webhook.r.lists[webhook._PROCESSING_KEY]


def test_erpnext_outage_during_customer_lookup_still_sends_a_reply(
    webhook, outbox, monkeypatch
) -> None:
    def down(phone):
        raise ConnectionError("erpnext unreachable")

    monkeypatch.setattr(webhook, "_contexto", down)
    monkeypatch.setattr(
        webhook, "responder_cliente", lambda *a, **kw: pytest.fail("no debe correr")
    )

    _post(webhook, _message_payload("wamid.erp-down", "hola"))
    webhook._worker_cycle()

    assert [t for r, t in outbox if r == CUSTOMER] == [webhook.texto_ack("es"), webhook.texto_error_tecnico("es")]


def test_agent_crash_does_not_wedge_the_fifo_for_the_next_customer(
    webhook, outbox, monkeypatch
) -> None:
    calls = []

    def flaky(text, **kwargs):
        calls.append(text)
        if text == "explota":
            raise RuntimeError("boom")
        return f"final:{text}"

    monkeypatch.setattr(webhook, "responder_cliente", flaky)
    other = "5491199999999"

    _post(webhook, _message_payload("wamid.a", "explota"))
    _post(webhook, _message_payload("wamid.b", "hola", phone=other))
    assert webhook._worker_cycle() == "worked"

    assert calls == ["explota", "hola"]
    assert [t for r, t in outbox if r == other] == [webhook.texto_ack("es"), "final:hola"]
    assert not webhook.r.lists[webhook._PROCESSING_KEY]


def test_agent_crash_alerts_the_staff_phone_as_the_apology_promises(
    webhook, outbox, monkeypatch
) -> None:
    monkeypatch.setenv("TELEFONOS_EQUIPO", STAFF)
    monkeypatch.setattr(sys.modules["app.router"], "STAFF", {STAFF}, raising=False)
    monkeypatch.setattr(webhook, "responder_cliente", _agent_that_raises)

    _post(webhook, _message_payload("wamid.crash-alert", "quiero 5 kg de queso"))
    webhook._worker_cycle()

    staff_alerts = [t for r, t in outbox if r == STAFF]
    assert staff_alerts, "the customer was told the team was alerted, but nobody was"


def test_empty_agent_reply_is_replaced_by_a_fallback_not_sent_as_empty(
    webhook, outbox, monkeypatch
) -> None:
    monkeypatch.setattr(webhook, "responder_cliente", lambda text, **kw: "")

    _post(webhook, _message_payload("wamid.empty-reply", "hola"))
    webhook._worker_cycle()

    finals = [t for r, t in outbox if r == CUSTOMER and t != webhook.texto_ack("es")]
    assert finals, "no final message at all"
    assert all(t.strip() for t in finals), "an empty body was handed to Meta"


# --------------------------------------------------------------------------
# Meta retries and status-only payloads.
# --------------------------------------------------------------------------


def test_meta_retry_of_the_same_message_id_is_processed_once(
    webhook, outbox, monkeypatch
) -> None:
    calls = []
    monkeypatch.setattr(
        webhook, "responder_cliente", lambda text, **kw: calls.append(text) or "final"
    )
    payload = _message_payload("wamid.retry", "quiero leche")

    first = _post(webhook, payload)
    second = _post(webhook, payload)
    third = _post(webhook, payload)
    webhook._worker_cycle()

    assert first.status_code == second.status_code == third.status_code == 200
    assert calls == ["quiero leche"]
    finals = [t for r, t in outbox if r == CUSTOMER and t != webhook.texto_ack("es")]
    assert finals == ["final"]
    assert len(webhook.r.lists[webhook._QUEUE_KEY]) == 0


def test_meta_retry_after_processing_finished_does_not_rerun_or_resend(
    webhook, outbox, monkeypatch
) -> None:
    calls = []
    monkeypatch.setattr(
        webhook, "responder_cliente", lambda text, **kw: calls.append(text) or "final"
    )
    payload = _message_payload("wamid.late-retry", "quiero leche")

    _post(webhook, payload)
    assert webhook._worker_cycle() == "worked"
    before = list(outbox)

    _post(webhook, payload)
    assert webhook._worker_cycle() == "idle"

    assert calls == ["quiero leche"]
    assert outbox == before


def test_ack_is_sent_once_even_when_webhook_and_worker_both_try(
    webhook, outbox, monkeypatch
) -> None:
    monkeypatch.setattr(webhook, "responder_cliente", lambda text, **kw: "final")

    _post(webhook, _message_payload("wamid.ack-once", "hola"))
    webhook._worker_cycle()

    acks = [t for r, t in outbox if t == webhook.texto_ack("es")]
    assert acks == [webhook.texto_ack("es")]


def test_status_only_payload_returns_200_without_queueing_or_replying(
    webhook, outbox
) -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "statuses": [
                                {"id": "wamid.out.x", "status": "read", "recipient_id": CUSTOMER}
                            ],
                        }
                    }
                ]
            }
        ]
    }

    response = _post(webhook, payload)
    outcome = webhook._worker_cycle()

    assert response.status_code == 200
    assert response.body == {"status": "ok"}
    assert outcome == "idle"
    assert outbox == []
    assert not webhook.r.lists[webhook._QUEUE_KEY]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"entry": []},
        {"entry": [{"changes": []}]},
        {"entry": [{"changes": [{"value": {}}]}]},
        {"entry": [{"changes": [{"value": {"messages": []}}]}]},
        {"entry": [{"changes": [{"value": {"messages": [{"type": "text"}]}}]}]},
    ],
    ids=["empty", "no-entries", "no-changes", "no-value", "no-messages", "no-id-no-from"],
)
def test_payload_without_processable_messages_is_acknowledged_with_200(
    webhook, outbox, payload
) -> None:
    """Meta retries anything that is not 2xx; a 4xx/5xx here would replay the
    same empty event forever."""
    response = _post(webhook, payload)

    assert response.status_code == 200
    assert webhook._worker_cycle() == "idle"
    assert outbox == []
