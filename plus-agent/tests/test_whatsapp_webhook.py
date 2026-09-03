import asyncio
import hashlib
import hmac
import importlib
import json
import sys
import threading
import types
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException, Request

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeRedis:
    """Thread-safe Redis double with TTLs, lists, and the webhook Lua scripts."""

    def __init__(self):
        self.values = {}
        self.expires = {}
        self.lists = defaultdict(deque)
        self.now = 0.0
        self.guard = threading.Lock()
        self.fail_completion = 0

    def _purge_locked(self, key):
        expiry = self.expires.get(key)
        if expiry is not None and expiry <= self.now:
            self.values.pop(key, None)
            self.expires.pop(key, None)

    def _set_locked(self, key, value, ex=None):
        self.values[key] = value
        if ex is None:
            self.expires.pop(key, None)
        else:
            self.expires[key] = self.now + float(ex)

    def set(self, key, value, nx=False, ex=None):
        with self.guard:
            self._purge_locked(key)
            if nx and key in self.values:
                return False
            self._set_locked(key, value, ex)
            return True

    def get(self, key):
        with self.guard:
            self._purge_locked(key)
            return self.values.get(key)

    def lindex(self, key, index):
        with self.guard:
            try:
                return self.lists[key][index]
            except IndexError:
                return None

    def lmove(self, source, destination, where_from, where_to):
        assert where_from == "LEFT"
        assert where_to == "RIGHT"
        with self.guard:
            if not self.lists[source]:
                return None
            item = self.lists[source].popleft()
            self.lists[destination].append(item)
            return item

    def force_delete(self, key):
        with self.guard:
            self.values.pop(key, None)
            self.expires.pop(key, None)

    def delete(self, key):
        self.force_delete(key)
        return 1

    def advance(self, seconds):
        with self.guard:
            self.now += seconds
            for key in list(self.values):
                self._purge_locked(key)

    def eval(self, script, numkeys, *args):
        keys = args[:numkeys]
        argv = args[numkeys:]
        with self.guard:
            if "dead-letter" in script:
                lock_key, processing_key, lease_key, dead_key = keys
                token, raw = argv
                self._purge_locked(lock_key)
                self._purge_locked(lease_key)
                if self.values.get(lock_key) != token:
                    return -1
                removed = 0
                for index, value in enumerate(self.lists[processing_key]):
                    if value == raw:
                        del self.lists[processing_key][index]
                        removed = 1
                        break
                if removed:
                    self.lists[dead_key].append(raw)
                if self.values.get(lease_key) == token:
                    self.values.pop(lease_key, None)
                    self.expires.pop(lease_key, None)
                return removed

            if "RPUSH" in script and "EXISTS" in script:
                seen_key, queue_key = keys
                item, ttl = argv
                self._purge_locked(seen_key)
                if seen_key in self.values:
                    return 0
                self.lists[queue_key].append(item)
                self._set_locked(seen_key, "1", ttl)
                return 1

            if "accepted_by_meta" in script:
                mapping_key, status_key, business_key, inbound_key = keys
                metadata, ttl, outbound_digest = argv
                self._set_locked(mapping_key, metadata, ttl)
                self._purge_locked(status_key)
                if status_key not in self.values:
                    self._set_locked(status_key, "accepted_by_meta", ttl)
                if business_key != mapping_key:
                    self._set_locked(business_key, "1", ttl)
                if inbound_key != mapping_key:
                    self._set_locked(inbound_key, outbound_digest, ttl)
                return self.values[status_key]

            if "LREM" in script:
                if self.fail_completion:
                    self.fail_completion -= 1
                    raise ConnectionError("completion response lost")
                lock_key, processing_key, lease_key = keys
                token, raw = argv
                self._purge_locked(lock_key)
                self._purge_locked(lease_key)
                if self.values.get(lock_key) != token:
                    return -1
                removed = 0
                for index, value in enumerate(self.lists[processing_key]):
                    if value == raw:
                        del self.lists[processing_key][index]
                        removed = 1
                        break
                if self.values.get(lease_key) == token:
                    self.values.pop(lease_key, None)
                    self.expires.pop(lease_key, None)
                return removed

            if "EXPIRE" in script:
                key = keys[0]
                token, ttl = argv
                self._purge_locked(key)
                if self.values.get(key) != token:
                    return 0
                self.expires[key] = self.now + float(ttl)
                return 1

            if "DEL" in script:
                key = keys[0]
                token = argv[0]
                self._purge_locked(key)
                if self.values.get(key) != token:
                    return 0
                self.values.pop(key, None)
                self.expires.pop(key, None)
                return 1

        raise AssertionError("unknown Lua script")


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.syspath_prepend(str(PROJECT_ROOT))
    for name, value in {
        "META_APP_SECRET": "test-app-secret",
        "META_VERIFY_TOKEN": "test-verify-token",
        "REDIS_URL": "redis://unused/0",
    }.items():
        monkeypatch.setenv(name, value)

    package = importlib.import_module("app")
    erpnext = types.ModuleType("app.erpnext")
    erpnext.get_list = lambda *args, **kwargs: []
    erpnext.add_comment = lambda *args, **kwargs: None
    graph = types.ModuleType("app.graph")
    graph.responder_cliente = lambda text, **kwargs: f"final:{text}"
    graph.responder_gerencia = lambda text, **kwargs: f"final:{text}"
    router = types.ModuleType("app.router")
    router.es_equipo = lambda phone: False
    whatsapp = types.ModuleType("app.whatsapp")
    whatsapp.enviar_mensaje = lambda phone, text: {
        "messages": [{"id": "wamid.out.default"}]
    }
    aprobacion = types.ModuleType("app.aprobacion")
    aprobacion.manejar_boton = lambda reply_id, phone: f"button:{reply_id}"

    stubs = {
        "app.erpnext": erpnext,
        "app.graph": graph,
        "app.router": router,
        "app.whatsapp": whatsapp,
        "app.aprobacion": aprobacion,
    }
    for full_name, module in stubs.items():
        monkeypatch.setitem(sys.modules, full_name, module)
        monkeypatch.setattr(package, full_name.rsplit(".", 1)[1], module, raising=False)

    monkeypatch.delitem(sys.modules, "app.main", raising=False)
    module = importlib.import_module("app.main")
    fake_redis = FakeRedis()
    monkeypatch.setattr(module, "r", fake_redis)
    outbound_status = importlib.import_module("app.outbound_status")
    monkeypatch.setattr(outbound_status, "_client", fake_redis)

    async def run_sync_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(module, "_run_sync", run_sync_inline)
    monkeypatch.setattr(module, "_WORKER_POLL_SECONDS", 0.01)
    monkeypatch.setattr(module, "_RETRY_SECONDS", 0.01)
    module._volatile_results.clear()
    module._volatile_accepted.clear()
    module._worker_wake.clear()
    return module


def _message_payload(message_id, text, phone="5491112345678"):
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": phone,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


def _status_payload(outbound_id, status):
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {
                                    "id": outbound_id,
                                    "status": status,
                                    "recipient_id": "sensitive-recipient",
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


def _call_body(module, body, run_background=True):
    digest = hmac.new(module.APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    consumed = False

    async def receive():
        nonlocal consumed
        if consumed:
            return {"type": "http.disconnect"}
        consumed = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/webhook/whatsapp",
            "raw_path": b"/webhook/whatsapp",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"x-hub-signature-256", f"sha256={digest}".encode()),
            ],
            "client": ("test", 123),
            "server": ("testserver", 443),
        },
        receive,
    )
    background = BackgroundTasks()
    try:
        result = asyncio.run(module.inbound(request, background))
    except HTTPException as error:
        return SimpleNamespace(status_code=error.status_code, body=error.detail)

    if run_background:
        for task in background.tasks:
            task.func(*task.args, **task.kwargs)
    return SimpleNamespace(status_code=200, body=result)


def _post(module, payload, run_background=True):
    body = json.dumps(payload, separators=(",", ":")).encode()
    return _call_body(module, body, run_background)


def test_ack_fifo_and_server_bound_customer_identity(webhook, monkeypatch):
    events = []
    agent_calls = []
    lookups = []
    phone = "5491112345678"

    def get_list(doctype, filters, **kwargs):
        # Contrato nuevo (app/clientes.py): UNA búsqueda por mensaje, con
        # `like` sobre los últimos 8 dígitos, y la confirmación canónica se
        # hace en Python comparando mobile_no. Por eso el fake devuelve el
        # teléfono tal como lo cargaría una persona (con +), y el servidor
        # tiene que matchearlo igual contra los dígitos pelados de Meta.
        lookups.append(tuple(filters[0]))
        return [{"name": "CUST-INTERNAL", "mobile_no": f"+{phone}"}]

    def respond(text, **kwargs):
        agent_calls.append((text, kwargs))
        events.append(("agent", text))
        return f"final:{text}"

    outbound_counter = 0

    def send(recipient, text):
        nonlocal outbound_counter
        events.append(("send", text))
        outbound_counter += 1
        return {"messages": [{"id": f"wamid.out.{outbound_counter}"}]}

    monkeypatch.setattr(webhook.erpnext, "get_list", get_list)
    monkeypatch.setattr(webhook, "responder_cliente", respond)
    monkeypatch.setattr(webhook, "enviar_mensaje", send)

    first = _post(webhook, _message_payload("wamid.in.1", "Necesito 5 kg"))
    second = _post(webhook, _message_payload("wamid.in.2", "de leche"))
    outcome = webhook._worker_cycle()

    assert first.status_code == second.status_code == 200
    assert outcome == "worked"
    assert [call[0] for call in agent_calls] == ["Necesito 5 kg", "de leche"]
    assert events == [
        ("send", webhook.ACK_TEXT),
        ("send", webhook.ACK_TEXT),
        ("agent", "Necesito 5 kg"),
        ("send", "final:Necesito 5 kg"),
        ("agent", "de leche"),
        ("send", "final:de leche"),
    ]
    # Una sola consulta por mensaje, y siempre un `like` acotado al sufijo del
    # abonado: nunca el número completo en un `=` que no matchea formatos.
    assert len(lookups) == 2
    for campo, operador, patron in lookups:
        assert campo == "mobile_no"
        assert operador == "like"
        # El patrón lleva un % entre dígito y dígito ('%1%2%3%...'): así el
        # `like` matchea '351 123-4567' aunque un guion o un espacio corte la
        # secuencia de los últimos 8 dígitos. Lo que importa: son ESOS dígitos.
        assert patron.startswith("%") and patron.endswith("%")
        assert patron.replace("%", "") == phone[-8:]
    for (_, kwargs), inbound_id in zip(
        agent_calls, ["wamid.in.1", "wamid.in.2"], strict=True
    ):
        assert kwargs["customer_code"] == "CUST-INTERNAL"
        assert kwargs["inbound_message_id"] == inbound_id
        assert kwargs["actor_phone"] == phone
        assert kwargs["thread_id"] == webhook._thread_tag(phone)
        assert phone not in kwargs["thread_id"]
        context = kwargs["contexto_cliente"]
        assert phone not in context
        assert "CUST-INTERNAL" not in context

    fake = webhook.r
    assert not fake.lists[webhook._QUEUE_KEY]
    assert not fake.lists[webhook._PROCESSING_KEY]
    seen_key = webhook._message_key("seen", "wamid.in.1")
    assert fake.expires[seen_key] - fake.now == webhook._STATE_TTL_SECONDS

    # A successful POST is recorded as API acceptance, never as delivery.
    outbound_digest = hashlib.sha256(b"wamid.out.3").hexdigest()
    assert fake.values[f"wa:{{inbound}}:status:{outbound_digest}"] == (
        "accepted_by_meta"
    )


def test_send_failure_retains_pending_and_reuses_cached_final(webhook, monkeypatch):
    agent_calls = 0
    final_attempts = 0

    def respond(text, **kwargs):
        nonlocal agent_calls
        agent_calls += 1
        return "pedido SO-42 tomado"

    def send(phone, text):
        nonlocal final_attempts
        if text == webhook.ACK_TEXT:
            return {"messages": [{"id": "wamid.ack"}]}
        final_attempts += 1
        if final_attempts == 1:
            raise RuntimeError("private Meta error body")
        return {"messages": [{"id": "wamid.final"}]}

    monkeypatch.setattr(webhook, "_contexto", lambda phone: ("C-1", "cliente"))
    monkeypatch.setattr(webhook, "responder_cliente", respond)
    monkeypatch.setattr(webhook, "enviar_mensaje", send)
    assert webhook._enqueue_message("54911", "wamid.retry", "text", "pedido")

    assert webhook._worker_cycle() == "retry"
    assert agent_calls == 1
    assert final_attempts == 1
    assert len(webhook.r.lists[webhook._PROCESSING_KEY]) == 1
    assert webhook.r.get(webhook._message_key("final", "wamid.retry")) == (
        "pedido SO-42 tomado"
    )

    assert webhook._worker_cycle() == "worked"
    assert agent_calls == 1
    assert final_attempts == 2
    assert not webhook.r.lists[webhook._PROCESSING_KEY]


def test_completion_failure_does_not_resend_meta_accepted_message(webhook, monkeypatch):
    agent_calls = 0
    final_sends = 0

    def respond(text, **kwargs):
        nonlocal agent_calls
        agent_calls += 1
        return "final estable"

    def send(phone, text):
        nonlocal final_sends
        if text != webhook.ACK_TEXT:
            final_sends += 1
        return {"messages": [{"id": "wamid.accepted"}]}

    monkeypatch.setattr(webhook, "_contexto", lambda phone: ("C-1", "cliente"))
    monkeypatch.setattr(webhook, "responder_cliente", respond)
    monkeypatch.setattr(webhook, "enviar_mensaje", send)
    webhook.r.fail_completion = 1
    assert webhook._enqueue_message("54911", "wamid.crash", "text", "pedido")

    assert webhook._worker_cycle() == "retry"
    assert len(webhook.r.lists[webhook._PROCESSING_KEY]) == 1
    assert agent_calls == final_sends == 1

    assert webhook._worker_cycle() == "worked"
    assert agent_calls == final_sends == 1
    assert not webhook.r.lists[webhook._PROCESSING_KEY]


def test_startup_supervisor_recovers_crash_with_expired_leases(webhook, monkeypatch):
    final_sent = threading.Event()
    monkeypatch.setattr(webhook, "_contexto", lambda phone: ("C-1", "cliente"))
    monkeypatch.setattr(
        webhook,
        "enviar_mensaje",
        lambda phone, text: (
            final_sent.set() or {"messages": [{"id": "wamid.recovered"}]}
            if text != webhook.ACK_TEXT
            else {"messages": [{"id": "wamid.ack"}]}
        ),
    )

    assert webhook._enqueue_message("54911", "wamid.pending", "text", "pedido")
    raw = webhook.r.lmove(webhook._QUEUE_KEY, webhook._PROCESSING_KEY, "LEFT", "RIGHT")
    assert raw is not None
    webhook.r.set(webhook._WORKER_LOCK_KEY, "dead-worker", ex=1)
    webhook.r.set(
        webhook._message_key("lease", "wamid.pending"),
        "dead-worker",
        ex=1,
    )
    webhook.r.advance(2)

    stop = threading.Event()
    supervisor = threading.Thread(target=webhook._worker_supervisor, args=(stop,))
    supervisor.start()
    assert final_sent.wait(timeout=2)
    stop.set()
    webhook._worker_wake.set()
    supervisor.join(timeout=2)

    assert not supervisor.is_alive()
    assert not webhook.r.lists[webhook._PROCESSING_KEY]


def test_lock_loss_caches_final_and_successor_sends_without_agent_rerun(
    webhook, monkeypatch
):
    agent_calls = 0
    finals = []

    def respond(text, **kwargs):
        nonlocal agent_calls
        agent_calls += 1
        webhook.r.force_delete(webhook._WORKER_LOCK_KEY)
        return "final after lost lock"

    def send(phone, text):
        if text != webhook.ACK_TEXT:
            finals.append(text)
        return {"messages": [{"id": "wamid.lock-successor"}]}

    monkeypatch.setattr(webhook, "_contexto", lambda phone: ("C-1", "cliente"))
    monkeypatch.setattr(webhook, "responder_cliente", respond)
    monkeypatch.setattr(webhook, "enviar_mensaje", send)
    assert webhook._enqueue_message("54911", "wamid.lock", "text", "pedido")

    assert webhook._worker_cycle() == "lost"
    assert agent_calls == 1
    assert finals == []
    assert len(webhook.r.lists[webhook._PROCESSING_KEY]) == 1
    assert webhook.r.get(webhook._message_key("final", "wamid.lock")) == (
        "final after lost lock"
    )

    assert webhook._worker_cycle() == "worked"
    assert agent_calls == 1
    assert finals == ["final after lost lock"]


def test_webhook_body_limit_rejects_before_redis(webhook, monkeypatch):
    monkeypatch.setattr(webhook, "MAX_WEBHOOK_BYTES", 32)

    response = _call_body(webhook, b"x" * 33)

    assert response.status_code == 413
    assert webhook.r.values == {}
    assert not webhook.r.lists


def test_meta_status_webhook_updates_hashed_status_without_queueing(webhook, capsys):
    outbound_id = "wamid.outbound.sensitive"

    response = _post(webhook, _status_payload(outbound_id, "delivered"))

    digest = hashlib.sha256(outbound_id.encode()).hexdigest()
    assert response.status_code == 200
    assert webhook.r.values[f"wa:{{inbound}}:status:{digest}"] == "delivered"
    assert not webhook.r.lists[webhook._QUEUE_KEY]
    logs = capsys.readouterr().out
    assert outbound_id not in logs
    assert "sensitive-recipient" not in logs


def test_reusable_outbound_business_correlation_and_failed_audit(webhook, capsys):
    status_module = importlib.import_module("app.outbound_status")
    audits = []
    wamid = "wamid.business.private"

    status_module.record_outbound(
        wamid,
        "customer_order_confirmation",
        order_name="SO-0042",
        inbound_message_id="wamid.in.private",
    )

    assert status_module.has_accepted("SO-0042", "customer_order_confirmation")
    status_module.update_status(
        wamid,
        "failed",
        audit_comment=lambda *args: audits.append(args),
    )
    status_module.update_status(
        wamid,
        "failed",
        audit_comment=lambda *args: audits.append(args),
    )

    assert len(audits) == 1
    assert audits[0][:2] == ("Sales Order", "SO-0042")
    logs = capsys.readouterr().out
    assert wamid not in logs
    assert "wamid.in.private" not in logs


def test_status_before_business_mapping_is_not_regressed_and_is_audited(
    webhook, monkeypatch
):
    status_module = importlib.import_module("app.outbound_status")
    audits = []
    wamid = "wamid.status-first"
    digest = hashlib.sha256(wamid.encode()).hexdigest()
    monkeypatch.setattr(
        webhook.erpnext,
        "add_comment",
        lambda *args: audits.append(args),
    )

    status_module.update_status(wamid, "failed")
    status_module.record_outbound(
        wamid,
        "customer_order_confirmation",
        order_name="SO-0043",
    )

    assert webhook.r.values[f"wa:{{inbound}}:status:{digest}"] == "failed"
    assert len(audits) == 1
    assert audits[0][:2] == ("Sales Order", "SO-0043")


def test_template_button_payload_is_processed(webhook, monkeypatch):
    handled = []
    monkeypatch.setattr(
        webhook,
        "manejar_boton",
        lambda payload, phone: handled.append((payload, phone)) or "aprobado",
    )
    monkeypatch.setattr(
        webhook,
        "enviar_mensaje",
        lambda phone, text: {"messages": [{"id": "wamid.button.reply"}]},
    )
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.button.in",
                                    "from": "54911",
                                    "type": "button",
                                    "button": {"payload": "ok:SO-42"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    assert _post(webhook, payload).status_code == 200
    assert webhook._worker_cycle() == "worked"
    assert handled == [("ok:SO-42", "54911")]


class _MetaRejection(Exception):
    """Shape of app.whatsapp.WhatsAppSendError without importing the real module."""

    def __init__(self, status_code, error_code, permanent, retry_after=None):
        super().__init__(f"HTTP {status_code} code {error_code}")
        self.status_code = status_code
        self.error_code = error_code
        self.permanent = permanent
        self.retry_after = retry_after


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, 190), (400, 131030), (400, 131047), (400, 132001)],
    ids=["token-expired-190", "recipient-not-allowed", "window-closed", "template-missing"],
)
def test_permanent_send_failure_is_dead_lettered_on_first_attempt(webhook, monkeypatch, status, code):
    """A permanent Meta rejection never retries and never blocks other customers."""
    monkeypatch.setattr(webhook, "_SEND_MAX_ATTEMPTS", 30)
    comments = []
    monkeypatch.setattr(
        webhook.erpnext,
        "add_comment",
        lambda doctype, name, text: comments.append((doctype, name, text)),
    )
    monkeypatch.setattr(webhook, "_contexto", lambda phone: ("C-1", "cliente"))
    monkeypatch.setattr(
        webhook,
        "responder_cliente",
        lambda text, **kwargs: f"PEDIDO_PENDIENTE. Número real: SAL-ORD-2026-00008. {text}",
    )
    sends = []

    def send(phone, text):
        sends.append((phone, text))
        if text == webhook.ACK_TEXT:
            return {"messages": [{"id": "wamid.ack"}]}
        if phone == "54911bad":
            raise _MetaRejection(status, code, permanent=True)
        return {"messages": [{"id": "wamid.ok"}]}

    monkeypatch.setattr(webhook, "enviar_mensaje", send)
    assert webhook._enqueue_message("54911bad", "wamid.bad", "text", "pedido")
    assert webhook._enqueue_message("54911good", "wamid.good", "text", "hola")

    # One cycle: the bad item is parked on its first rejection and the next
    # customer is served immediately, no 30-attempt loop.
    assert webhook._worker_cycle() == "worked"

    finals_bad = [t for p, t in sends if p == "54911bad" and t != webhook.ACK_TEXT]
    assert len(finals_bad) == 1
    dead = [json.loads(raw)["message_id"] for raw in webhook.r.lists[webhook._DEAD_KEY]]
    assert dead == ["wamid.bad"]
    assert not webhook.r.lists[webhook._PROCESSING_KEY]
    assert not webhook.r.lists[webhook._QUEUE_KEY]
    finals_good = [t for p, t in sends if p == "54911good" and t != webhook.ACK_TEXT]
    assert finals_good == ["PEDIDO_PENDIENTE. Número real: SAL-ORD-2026-00008. hola"]
    assert [(d, n) for d, n, _ in comments] == [("Sales Order", "SAL-ORD-2026-00008")]
    assert "NO recibió el número de pedido" in comments[0][2]
    assert f"HTTP {status}, código {code}" in comments[0][2]
    assert "54911bad" not in comments[0][2]
    # Nothing retryable happened, so no stale backoff hint survives.
    assert webhook._take_retry_hint() == webhook._RETRY_SECONDS


def test_transient_send_failure_backs_off_then_dead_letters(webhook, monkeypatch):
    """Timeouts/5xx retry with exponential backoff, bounded by the attempt cap."""
    monkeypatch.setattr(webhook, "_SEND_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(webhook, "_RETRY_SECONDS", 2.0)  # fixture shortens it to 0.01
    monkeypatch.setattr(webhook, "_RETRY_MAX_SECONDS", 60.0)
    comments = []
    monkeypatch.setattr(
        webhook.erpnext,
        "add_comment",
        lambda doctype, name, text: comments.append(text),
    )
    monkeypatch.setattr(webhook, "_contexto", lambda phone: ("C-1", "cliente"))
    monkeypatch.setattr(
        webhook, "responder_cliente", lambda text, **kwargs: "Número real: SO-0042."
    )
    attempts = []

    def send(phone, text):
        if text == webhook.ACK_TEXT:
            return {"messages": [{"id": "wamid.ack"}]}
        attempts.append(text)
        raise _MetaRejection(503, 131016, permanent=False)

    monkeypatch.setattr(webhook, "enviar_mensaje", send)
    assert webhook._enqueue_message("54911", "wamid.flaky", "text", "hola")

    assert webhook._worker_cycle() == "retry"
    assert webhook._take_retry_hint() == 2.0
    assert webhook._worker_cycle() == "retry"
    assert webhook._take_retry_hint() == 4.0
    assert len(webhook.r.lists[webhook._PROCESSING_KEY]) == 1
    assert not webhook.r.lists[webhook._DEAD_KEY]

    # Third failure exhausts the budget: parked (counts as handled), FIFO free.
    assert webhook._worker_cycle() == "worked"
    assert len(attempts) == 3
    assert [json.loads(raw)["message_id"] for raw in webhook.r.lists[webhook._DEAD_KEY]] == ["wamid.flaky"]
    assert not webhook.r.lists[webhook._PROCESSING_KEY]
    assert len(comments) == 1
    assert "3 intentos" in comments[0] and "HTTP 503, código 131016" in comments[0]


def test_retry_after_header_lengthens_but_never_exceeds_the_bound(webhook, monkeypatch):
    monkeypatch.setattr(webhook, "_RETRY_SECONDS", 2.0)  # fixture shortens it to 0.01
    monkeypatch.setattr(webhook, "_RETRY_MAX_SECONDS", 60.0)
    assert [webhook._retry_delay_seconds(RuntimeError(), n) for n in range(1, 9)] == [
        2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0,
    ]
    assert webhook._retry_delay_seconds(_MetaRejection(429, 130429, False, retry_after=45), 1) == 45.0
    assert webhook._retry_delay_seconds(_MetaRejection(429, 130429, False, retry_after=600), 1) == 60.0
    assert webhook._retry_delay_seconds(_MetaRejection(429, 130429, False, retry_after=1), 3) == 8.0
    # An unknown attempt count (Redis failure) still yields the base delay.
    assert webhook._retry_delay_seconds(RuntimeError(), 0) == 2.0


def _http_status_error(status):
    request = httpx.Request("POST", "https://graph.facebook.com/v21.0/x/messages")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(status, request=request))


@pytest.mark.parametrize(
    ("error", "permanent"),
    [
        (_MetaRejection(401, 190, permanent=True), True),
        (_MetaRejection(429, 130429, permanent=False), False),
        (_http_status_error(401), True),
        (_http_status_error(400), True),
        (_http_status_error(429), False),
        (_http_status_error(500), False),
        (_http_status_error(503), False),
        (httpx.ConnectTimeout("t"), False),
        (httpx.ReadTimeout("t"), False),
        (httpx.ConnectError("refused"), False),
        (RuntimeError("unknown failure"), False),
    ],
    ids=[
        "flagged-permanent", "flagged-transient", "401", "400", "429", "500", "503",
        "connect-timeout", "read-timeout", "connect-error", "unknown",
    ],
)
def test_send_error_classification(webhook, error, permanent):
    assert webhook._send_error_is_permanent(error) is permanent


def test_meta_rate_limit_codes_inside_http_400_are_transient(webhook):
    class Unflagged(Exception):
        status_code = 400
        error_code = 130429

    class UnflaggedPermanent(Exception):
        status_code = 400
        error_code = 131047

    assert webhook._send_error_is_permanent(Unflagged()) is False
    assert webhook._send_error_is_permanent(UnflaggedPermanent()) is True


def test_redis_failure_never_counts_towards_dead_letter(webhook, monkeypatch):
    monkeypatch.setattr(webhook, "_SEND_MAX_ATTEMPTS", 1)
    original_get = webhook.r.get

    def flaky_get(key):
        if "send-attempts" in key:
            raise ConnectionError("redis down")
        return original_get(key)

    monkeypatch.setattr(webhook.r, "get", flaky_get)
    monkeypatch.setattr(webhook, "_contexto", lambda phone: ("C-1", "cliente"))
    monkeypatch.setattr(webhook, "responder_cliente", lambda text, **kwargs: "final")

    def send(phone, text):
        if text == webhook.ACK_TEXT:
            return {"messages": [{"id": "wamid.ack"}]}
        raise RuntimeError("meta down")

    monkeypatch.setattr(webhook, "enviar_mensaje", send)
    assert webhook._enqueue_message("54911", "wamid.flaky", "text", "hola")
    # With cap 1 a single counted failure would dead-letter. A counter that
    # cannot be read must report 0 so the item stays pending instead.
    assert webhook._note_send_failure("wamid.flaky") == 0
    assert webhook._worker_cycle() == "retry"
    assert len(webhook.r.lists[webhook._PROCESSING_KEY]) == 1
    assert not webhook.r.lists[webhook._DEAD_KEY]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("confirmar SAL-ORD-2026-00008", "ok:SAL-ORD-2026-00008"),
        ("Confirmar sal-ord-2026-00008", "ok:SAL-ORD-2026-00008"),
        ("  ok SO-0042 ", "ok:SO-0042"),
        ("Apruebo SAL-ORD-2026-00008!", "ok:SAL-ORD-2026-00008"),
        ("ver SAL-ORD-2026-00008", "ver:SAL-ORD-2026-00008"),
        ("Detalle SO-0042.", "ver:SO-0042"),
        ("rechazar SAL-ORD-2026-00008", "no:SAL-ORD-2026-00008"),
        ("Rechazo sal-ord-2026-00008", "no:SAL-ORD-2026-00008"),
        ("preparar SAL-ORD-2026-00008", "preparar:SAL-ORD-2026-00008"),
        ("Despachar SAL-ORD-2026-00008!", "despachar:SAL-ORD-2026-00008"),
        ("despreparar SAL-ORD-2026-00008", "despreparar:SAL-ORD-2026-00008"),
        ("Desprepara sal-ord-2026-00008.", "despreparar:SAL-ORD-2026-00008"),
        # "despreparar" must not be swallowed by the cancellation verbs, whose
        # pattern also matches "<verb> <order> <rest>".
        ("despreparar SAL-ORD-2026-00008 porque quiero", None),
        ("hola, cuántos pedidos pendientes hay?", None),
        ("confirmar", None),
        ("confirmar todo", None),
        ("confirmar SAL-ORD-2026-00008 y SAL-ORD-2026-00009", None),
        ("cancelar SAL-ORD-2026-00008 el cliente se arrepintió", "cancelar:SAL-ORD-2026-00008:el cliente se arrepintió"),
        ("Anular sal-ord-2026-00008 no hay stock", "cancelar:SAL-ORD-2026-00008:no hay stock"),
        ("cancelar SAL-ORD-2026-00008", "cancelar:SAL-ORD-2026-00008:"),
        ("borrar SAL-ORD-2026-00008", None),
        ("", None),
    ],
)
def test_staff_command_parsing(webhook, text, expected):
    assert webhook._staff_command(text) == expected


def test_staff_text_command_confirms_without_llm(webhook, monkeypatch):
    staff = "5491100000000"
    monkeypatch.setattr(webhook, "es_equipo", lambda phone: phone == staff)
    handled = []
    monkeypatch.setattr(
        webhook,
        "manejar_boton",
        lambda payload, phone: handled.append((payload, phone)) or "✅ confirmado",
    )
    monkeypatch.setattr(
        webhook,
        "responder_gerencia",
        lambda *args, **kwargs: pytest.fail("un comando no debe invocar al LLM"),
    )
    sent = []
    monkeypatch.setattr(
        webhook,
        "enviar_mensaje",
        lambda phone, text: sent.append((phone, text)) or {"messages": [{"id": "wamid.r"}]},
    )

    payload = _message_payload("wamid.cmd", "Confirmar sal-ord-2026-00008", phone=staff)
    assert _post(webhook, payload).status_code == 200
    assert webhook._worker_cycle() == "worked"
    assert handled == [("ok:SAL-ORD-2026-00008", staff)]
    assert sent[-1] == (staff, "✅ confirmado")


def test_customer_typing_a_command_still_reaches_the_customer_agent(webhook, monkeypatch):
    monkeypatch.setattr(webhook, "es_equipo", lambda phone: False)
    monkeypatch.setattr(
        webhook,
        "manejar_boton",
        lambda payload, phone: pytest.fail("un cliente nunca llega al handler de aprobación"),
    )
    monkeypatch.setattr(webhook, "_contexto", lambda phone: ("C-1", "cliente"))
    seen = []
    monkeypatch.setattr(
        webhook,
        "responder_cliente",
        lambda text, **kwargs: seen.append(text) or "respuesta",
    )
    result = webhook._generate_response(
        {"message_id": "m", "telefono": "54911", "kind": "text", "data": "confirmar SAL-ORD-2026-00008"}
    )
    assert result == "respuesta"
    assert seen == ["confirmar SAL-ORD-2026-00008"]


def test_inbound_opens_free_form_window_for_sender(webhook):
    from app import outbound_status

    assert _post(webhook, _message_payload("wamid.win", "hola", phone="5491122223333")).status_code == 200
    assert outbound_status.window_open("5491122223333") is True
    assert outbound_status.window_open("+5491122223333") is True
    assert outbound_status.window_open("5491199999999") is False
    webhook.r.advance(outbound_status.WINDOW_TTL_SECONDS + 1)
    assert outbound_status.window_open("5491122223333") is False


def test_technical_failure_alerts_team_once_and_is_truthful(webhook, monkeypatch):
    todos = []
    monkeypatch.setattr(
        webhook.erpnext,
        "create_doc",
        lambda doctype, payload: todos.append((doctype, payload)) or {"name": "TD-1"},
        raising=False,
    )
    monkeypatch.setattr(webhook, "_contexto", lambda phone: ("C-1", "cliente"))

    def boom(*args, **kwargs):
        raise RuntimeError("RESOURCE_EXHAUSTED")

    monkeypatch.setattr(webhook, "responder_cliente", boom)
    item = {"message_id": "m1", "telefono": "54911", "kind": "text", "data": "hola"}

    assert webhook._generate_response(item) == webhook.TECHNICAL_ERROR_ALERTED
    assert webhook._generate_response({**item, "message_id": "m2"}) == webhook.TECHNICAL_ERROR_ALERTED
    assert len(todos) == 1
    assert todos[0][0] == "ToDo"
    assert "RuntimeError" in todos[0][1]["description"]
    assert "54911" not in todos[0][1]["description"]


def test_technical_failure_without_erp_task_never_claims_the_team_was_alerted(webhook, monkeypatch):
    def cannot_create(doctype, payload):
        raise RuntimeError("erpnext down")

    monkeypatch.setattr(webhook.erpnext, "create_doc", cannot_create, raising=False)
    monkeypatch.setattr(webhook, "_contexto", lambda phone: ("C-1", "cliente"))

    def boom(*args, **kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr(webhook, "responder_cliente", boom)
    item = {"message_id": "m1", "telefono": "54911", "kind": "text", "data": "hola"}
    assert webhook._generate_response(item) == webhook.TECHNICAL_ERROR
    assert "avisé" not in webhook.TECHNICAL_ERROR


def test_config_warnings_name_every_gap_that_disables_the_manager_loop(webhook, monkeypatch):
    for variable in (
        "TELEFONOS_EQUIPO",
        "WHATSAPP_STAFF_PENDING_TEMPLATE",
        "WHATSAPP_STAFF_CONFIRMED_TEMPLATE",
        "WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE",
        "AUTO_CONFIRM_PRICE_LIST",
        "AUTO_CONFIRM_CURRENCY",
    ):
        monkeypatch.delenv(variable, raising=False)
    warnings = webhook._config_warnings()
    joined = "\n".join(warnings)
    assert "TELEFONOS_EQUIPO" in joined
    assert "WHATSAPP_STAFF_PENDING_TEMPLATE" in joined
    assert "WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE" in joined
    assert "AUTO_CONFIRM_PRICE_LIST" in joined

    monkeypatch.setenv("TELEFONOS_EQUIPO", "5491100000000")
    for variable in (
        "WHATSAPP_STAFF_PENDING_TEMPLATE",
        "WHATSAPP_STAFF_CONFIRMED_TEMPLATE",
        "WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE",
    ):
        monkeypatch.setenv(variable, "plantilla")
    monkeypatch.setenv("AUTO_CONFIRM_PRICE_LIST", "Standard Selling")
    monkeypatch.setenv("AUTO_CONFIRM_CURRENCY", "ARS")
    assert webhook._config_warnings() == []
