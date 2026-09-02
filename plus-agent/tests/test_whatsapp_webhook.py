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
    outbound_digest = hashlib.sha256("wamid.out.3".encode()).hexdigest()
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
