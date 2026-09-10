"""HTTP dashboard -> production ERPNext client -> repository's Frappe test double."""
from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import avisos, dashboard, erpnext, limites, locks, outbound_status, reloj
from demo import datos
from demo.falso_erpnext import DEPOSITO, EMPRESA, Almacen, manejar

TOKEN = "integration-dashboard-token-with-enough-entropy"
TODAY = date(2026, 9, 10)


@pytest.fixture
def connected(monkeypatch):
    monkeypatch.setenv("ERPNEXT_COMPANY", EMPRESA)
    monkeypatch.setenv("ERPNEXT_WAREHOUSE", DEPOSITO)
    monkeypatch.setenv("DASHBOARD_API_TOKEN", TOKEN)
    monkeypatch.setenv("ERPNEXT_PUBLIC_URL", "https://crm.example")
    monkeypatch.setattr(reloj, "ahora", lambda: datetime(2026, 9, 10, 12, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires")))
    almacen = Almacen()
    datos.sembrar(almacen, hoy=TODAY)
    calls = []

    def transport(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "token demo-gerencia-key:demo-gerencia-secret"
        code, body = manejar(
            almacen, request.method, request.url.path,
            parse_qs(request.url.query.decode()), request.content,
            request.headers["authorization"],
        )
        return httpx.Response(code, json=body)

    manager = httpx.Client(
        base_url="https://erpnext.test",
        headers={"Authorization": "token demo-gerencia-key:demo-gerencia-secret"},
        transport=httpx.MockTransport(transport),
    )
    monkeypatch.setattr(erpnext, "_manager_client", manager)

    def denied(*args, **kwargs):
        raise AssertionError("Dashboard record reads must not use customer/policy identity")

    monkeypatch.setattr(erpnext, "_client", type("Denied", (), {"request": denied})())
    monkeypatch.setattr(erpnext, "_policy", denied)
    app = FastAPI()
    dashboard.install_dashboard(app)
    with TestClient(app, base_url="https://agent.example") as client:
        client.headers["Authorization"] = "Bearer " + TOKEN
        yield client, almacen, calls
    manager.close()


def create_order(almacen, *, days=0, company=EMPRESA, docstatus=0):
    return almacen.crear("Sales Order", {
        "company": company, "customer": datos.CLIENTE_HABITUAL,
        "customer_name": datos.CLIENTE_HABITUAL,
        "transaction_date": (TODAY - timedelta(days=days)).isoformat(),
        "delivery_date": (TODAY + timedelta(days=1)).isoformat(),
        "docstatus": docstatus, "status": "Draft" if not docstatus else "To Deliver and Bill",
        "currency": "ARS", "selling_price_list": "Standard Selling",
        "shipping_address": "<p>Belgrano 1200</p><p>Córdoba</p>",
        "items": [{"item_code": "LECHE-ENT-1L", "qty": 10, "rate": 1250, "uom": "Unidad"}],
    }, puede_confirmar=True)


def test_installed_dashboard_and_api_enforce_the_http_boundary(connected):
    client, _, calls = connected
    client.headers.pop("Authorization")
    page = client.get("/dashboard/")
    assert page.status_code == 200
    assert 'src="./app.js"' in page.text
    script = client.get("/dashboard/app.js")
    assert script.status_code == 200
    assert "disconnectedData()" in script.text
    config = client.get("/api/dashboard/config")
    assert config.json() == {"service": "plus-agent", "apiVersion": 1, "configured": True}
    for route in ("snapshot", "controls", "operations", "orders/SAL-ORD-2026-00001"):
        assert client.get("/api/dashboard/" + route).status_code == 401
    assert calls == []


def test_live_snapshot_reads_real_repository_contract_and_old_pending(connected):
    client, almacen, calls = connected
    recent = create_order(almacen, docstatus=1)
    old = create_order(almacen, days=60)
    other = create_order(almacen, company="Other Company")
    response = client.get("/api/dashboard/snapshot")
    assert response.status_code == 200
    result = response.json()
    assert result["mode"] == "live"
    assert result["company"] == EMPRESA
    assert result["currency"] == "ARS"
    assert result["errors"] == []
    assert recent["name"] in {o["id"] for o in result["orders"]}
    assert old["name"] not in {o["id"] for o in result["orders"]}
    assert old["name"] in {o["id"] for o in result["pendingOrders"]}
    assert other["name"] not in {o["id"] for o in result["pendingOrders"]}
    milk = next(p for p in result["products"] if p["id"] == "LECHE-ENT-1L")
    assert milk["stock"] == 400 and milk["available"] == 400
    assert milk["name"] == "Leche entera sachet 1 L"
    assert len(result["customers"]) == 3
    assert all(r.extensions["timeout"]["read"] == 3 for r in calls)
    assert response.headers["cache-control"] == "no-store"


def test_order_details_include_erp_items_and_enforce_company(connected):
    client, almacen, _ = connected
    own = create_order(almacen, docstatus=1)
    other = create_order(almacen, company="Other Company")
    result = client.get("/api/dashboard/orders/" + own["name"]).json()
    assert result["total"] == 12500
    assert result["items"][0]["qty"] == 10
    assert result["items"][0]["rate"] == 1250
    assert result["address"] == "Belgrano 1200 Córdoba"
    assert result["erpUrl"] == "https://crm.example/app/sales-order/" + own["name"]
    assert client.get("/api/dashboard/orders/" + other["name"]).status_code == 404
    assert client.get("/api/dashboard/orders/does-not-exist").status_code == 404
    assert client.post("/api/dashboard/orders/" + own["name"]).status_code == 405


def test_order_outage_is_retryable_and_not_reported_as_missing(connected, monkeypatch):
    client, _, _ = connected

    def outage(*args, **kwargs):
        raise erpnext.ERPNextError("private upstream error", status_code=503)

    monkeypatch.setattr(erpnext, "get_doc", outage)
    response = client.get("/api/dashboard/orders/SO-1")
    assert response.status_code == 502
    assert "private" not in response.text


def test_operations_reads_real_queue_keys_and_reports_outages(connected, monkeypatch):
    client, _, calls = connected
    counts = {
        "wa:{inbound}:queue": 3, "wa:{inbound}:dead": 2,
        outbound_status.DEAD_NOTIFY_KEY: 1,
    }
    redis = SimpleNamespace(
        ping=lambda: True,
        exists=lambda key: key == "wa:{inbound}:worker-lock",
        llen=lambda key: counts[key],
    )
    monkeypatch.setattr(outbound_status, "cliente", lambda: redis)
    monkeypatch.setattr(avisos, "pendientes", lambda: 4)
    response = client.get("/api/dashboard/operations")
    assert response.status_code == 200
    assert response.json() == {
        "redis": "Connected", "worker": "Active lease", "queuedMessages": 3,
        "failedReplies": 2, "failedNotices": 1, "queuedNotices": 4,
        "providerHealth": "Not probed",
    }
    monkeypatch.setattr(avisos, "pendientes", lambda: -1)
    assert client.get("/api/dashboard/operations").json()["queuedNotices"] is None

    def offline():
        raise ConnectionError("private Redis address")

    redis.ping = offline
    response = client.get("/api/dashboard/operations")
    assert response.status_code == 502
    assert "private" not in response.text
    assert calls == []


def test_record_limit_is_reported_and_does_not_hide_truncation(connected, monkeypatch):
    client, almacen, _ = connected
    for _ in range(3):
        create_order(almacen)
    monkeypatch.setattr(dashboard, "LIMIT", 2)
    result = client.get("/api/dashboard/snapshot").json()
    assert len(result["orders"]) == 2
    assert len(result["pendingOrders"]) == 2
    assert "orders" in result["truncated"]
    assert "pending orders" in result["truncated"]


def test_failed_dataset_is_unknown_and_not_empty(connected, monkeypatch):
    client, _, _ = connected
    original = erpnext.get_list

    def fail_inventory(doctype, **kwargs):
        if doctype == "Bin":
            raise erpnext.ERPNextError("private upstream text")
        return original(doctype, **kwargs)

    monkeypatch.setattr(erpnext, "get_list", fail_inventory)
    result = client.get("/api/dashboard/snapshot").json()
    assert result["products"] is None
    assert "inventory" in result["errors"]
    assert "private" not in str(result)


def test_controls_use_owner_values_and_preserve_lost_state_guard(connected, monkeypatch):
    client, _, _ = connected
    store = locks.conexion()
    store.hset(limites.CLAVE_VALORES, "AUTO_CONFIRM_MAX", "75000")
    response = client.get("/api/dashboard/controls")
    assert response.status_code == 200
    ceiling = next(p for p in response.json()["policies"] if p["id"] == "AUTO_CONFIRM_MAX")
    assert ceiling["value"] == "75000"
    assert ceiling["source"] == "dueño"
    store.hashes.clear()
    monkeypatch.setattr(limites, "_hubo_cambios_durables", lambda: True)
    response = client.get("/api/dashboard/controls")
    assert response.status_code == 502
    assert "75000" not in response.text


def test_snapshot_resolves_actual_model_alias(connected, monkeypatch):
    client, _, _ = connected
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    monkeypatch.delenv("QWEN_SALES_MODEL", raising=False)
    monkeypatch.setenv("LLM_MODEL_CLIENTES", "custom-sales-model")
    result = client.get("/api/dashboard/snapshot").json()
    assert result["agents"][0]["model"] == "custom-sales-model"
    assert result["agents"][0]["status"] == "Configured"


def test_setup_preserves_agent_configuration_and_does_not_rotate_token(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "dashboard_setup", Path(__file__).parents[1] / "deploy" / "configurar_dashboard.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / ".env"
    original = "ERPNEXT_COMPANY=Existing Company\nWHATSAPP_TOKEN=untouched\nDASHBOARD_API_TOKEN= # enable access with a dedicated random token\n"
    path.write_text(original)
    token = module.configure(path)
    assert len(token) >= 32
    assert "WHATSAPP_TOKEN=untouched\n" in path.read_text()
    assert "ERPNEXT_COMPANY=Existing Company\n" in path.read_text()
    assert module.configure(path) is None
    assert token in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text(f'DASHBOARD_API_TOKEN="{token}" # preserve this existing token\n')
    assert module.configure(path) is None
