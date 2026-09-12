"""Read-only dashboard API. No agent tools or business write operations.

An independent ASGI surface keeps its bearer auth and CORS away from the signed
WhatsApp webhook. The UI is public static content; business data never is.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import math
import os
import re
from datetime import timedelta
from html import unescape
from urllib.parse import quote, unquote, urlsplit

LIMIT = 250
READ_TIMEOUT = 3.0
ORDER_FIELDS = [
    "name", "customer", "customer_name", "transaction_date", "delivery_date",
    "grand_total", "currency", "status", "docstatus",
]


class RecordNotFound(Exception):
    """A document outside the configured company is not a dashboard record."""


def public_erp_origin() -> str:
    """Only an explicitly configured browser origin, never the internal API URL."""
    value = os.getenv("ERPNEXT_PUBLIC_URL", "").strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not parsed.netloc or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment
            or not (parsed.scheme == "https" or (parsed.scheme == "http" and local))
        ):
            return ""
    except ValueError:
        return ""
    return value


def plain_text(value: object) -> str:
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", str(value or ""))).split())[:2000]


def model_status() -> list[dict]:
    """Reuse the repo's provider selection, aliases, and defaults without a model call."""
    from app import modelos

    try:
        provider = modelos.proveedor()
        configured = bool(modelos.clave_api(provider)[1])
        names = [modelos.nombre_modelo(role, prov=provider)[1] for role in ("clientes", "gerencia")]
        status = "Configured" if configured else "Missing credentials"
    except Exception:
        names, status = ["Unavailable", "Unavailable"], "Configuration error"
    return [
        {"id": "sales", "name": "Sales agent", "role": "Customer conversations & order drafts",
         "model": names[0], "status": status},
        {"id": "manager", "name": "Management agent", "role": "Business reports & manager assistance",
         "model": names[1], "status": status},
    ]


def controls() -> dict:
    """Canonical settings reader preserves the repo's lost-state guards.

    This existing reader may use policy-scoped READS to verify durable Company
    audit markers. It does not propose, apply, or bypass an owner setting.
    """
    from app import inventario, limites

    labels = {
        "AUTO_CONFIRM_MAX": "Order ceiling",
        "AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO": "Quantity per product",
        "STOCK_BUFFER_PCT": "Stock buffer",
        "AUTO_CONFIRM_MAX_CLIENTE_NUEVO": "New customer ceiling",
    }
    rows = limites.resumen()
    policies = [{
        "id": row["nombre"], "name": labels.get(row["nombre"], row["alias"]),
        "value": limites.mostrar(row["nombre"], row["valor"], en_idioma="en")
        if not row["problema"] else "Unavailable",
        "note": row["problema"] or row["significado"],
        "source": row["origen"], "unit": row["unidad"], "valid": not bool(row["problema"]),
    } for row in rows]
    policies.append({
        "id": "STOCK_CONFIABLE_HORAS", "name": "Stock trust window",
        "value": f"{inventario.horas_de_validez():g} hours",
        "note": "A submitted stock count must be recent enough before auto-confirmation.",
        "source": "Environment", "valid": True,
    })
    return {"policies": policies}


def operations() -> dict:
    from app import avisos, outbound_status

    client = outbound_status.cliente()
    client.ping()
    notice_count = avisos.pendientes()
    return {
        "redis": "Connected",
        # The worker takes a short lease while handling a turn; idle is not failure.
        "worker": "Active lease" if client.exists("wa:{inbound}:worker-lock") else "No active lease",
        "queuedMessages": int(client.llen("wa:{inbound}:queue")),
        "failedReplies": int(client.llen("wa:{inbound}:dead")),
        "failedNotices": int(client.llen(outbound_status.DEAD_NOTIFY_KEY)),
        "queuedNotices": notice_count if notice_count >= 0 else None,
        "providerHealth": "Not probed",
    }


def order_detail(order_id: str) -> dict:
    from app import erpnext

    with erpnext.manager_scope():
        try:
            doc = erpnext.get_doc("Sales Order", order_id, timeout=READ_TIMEOUT)
        except erpnext.ERPNextError as exc:
            if exc.status_code in {403, 404}:
                raise RecordNotFound from exc
            raise
        if doc.get("company") != erpnext.default_company():
            raise RecordNotFound
        result = order_row(doc)
        result["items"] = [{
            "code": str(item.get("item_code") or ""),
            "name": str(item.get("item_name") or item.get("item_code") or "Item"),
            "qty": number(item.get("qty")), "rate": number(item.get("rate")),
            "amount": number(item.get("amount")), "unit": str(item.get("uom") or ""),
        } for item in doc.get("items", [])]
        result["address"] = plain_text(doc.get("shipping_address") or doc.get("address_display"))
        result["erpUrl"] = (
            public_erp_origin() + "/app/sales-order/" + quote(order_id, safe="")
            if public_erp_origin() else ""
        )
        return result


def authorized(header: str) -> bool:
    expected = os.getenv("DASHBOARD_API_TOKEN", "")
    supplied = header.removeprefix("Bearer ")
    return (
        len(expected) >= 32
        and header.startswith("Bearer ")
        and hmac.compare_digest(supplied.encode(), expected.encode())
    )


def allowed_origin(origin: str) -> bool:
    # Exact origins only. A wildcard must never expose manager data.
    configured = os.getenv("DASHBOARD_ALLOWED_ORIGINS", "").split(",")
    return bool(origin) and origin in {x.strip().rstrip("/") for x in configured if x.strip() != "*"}


def number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def order_row(row: dict) -> dict:
    status = str(row.get("status") or "Unknown")
    if row.get("docstatus") == 2:
        state = "cancelled"
    elif status in {"Closed", "On Hold"}:
        state = "closed" if status == "Closed" else "on-hold"
    elif row.get("docstatus") == 0:
        state = "pending"
    elif row.get("docstatus") == 1:
        state = "completed" if status == "Completed" else "confirmed"
    else:
        state = "unknown"
    return {
        "id": str(row.get("name", "")),
        "customerId": str(row.get("customer", "")),
        "customer": str(row.get("customer_name") or row.get("customer") or "Unknown customer"),
        "date": str(row.get("transaction_date") or ""),
        "deliveryDate": str(row.get("delivery_date") or ""),
        "total": number(row.get("grand_total")),
        "currency": str(row.get("currency") or ""),
        "status": state,
        "erpStatus": status,
        "channel": "ERPNext",
    }


def snapshot() -> dict:
    from app import erpnext, reloj

    now = reloj.ahora()
    since = (now.date() - timedelta(days=29)).isoformat()
    company = erpnext.default_company()
    errors: list[str] = []
    truncated: list[str] = []

    def read(label: str, doctype: str, **kwargs) -> list[dict] | None:
        try:
            rows = erpnext.get_list(doctype, limit=LIMIT + 1, timeout=READ_TIMEOUT, **kwargs)
        except Exception:
            # Never put upstream response bodies or credential-bearing exceptions in JSON.
            errors.append(label)
            return None
        if len(rows) > LIMIT:
            truncated.append(label)
        return rows[:LIMIT]

    with erpnext.manager_scope():
        orders = read(
            "orders", "Sales Order",
            filters=[["company", "=", company], ["transaction_date", ">=", since],
                     ["transaction_date", "<=", now.date().isoformat()]],
            fields=ORDER_FIELDS,
            order_by="transaction_date desc, name desc",
        )
        pending = read(
            "pending orders", "Sales Order",
            filters=[["company", "=", company], ["docstatus", "=", 0],
                     ["status", "not in", ["Closed", "Cancelled", "On Hold"]]],
            fields=ORDER_FIELDS, order_by="creation asc, name asc",
        )
        customers = read(
            "customers", "Customer", filters=[["disabled", "=", 0]],
            fields=["name", "customer_name", "customer_group", "territory"],
            order_by="modified desc, name desc",
        )
        warehouse = erpnext.default_warehouse()
        bins = read(
            "inventory", "Bin", filters=[["warehouse", "=", warehouse]],
            fields=["name", "item_code", "warehouse", "actual_qty", "reserved_qty", "stock_uom"],
            order_by="item_code asc, name asc",
        )
        items = []
        if bins:
            items = read(
                "product names", "Item",
                filters=[["name", "in", list({b["item_code"] for b in bins})]],
                fields=["name", "item_name", "stock_uom"], order_by="name asc",
            ) or []
        labels = {x["name"]: x for x in items}
        try:
            currency = erpnext.get_doc("Company", company, timeout=READ_TIMEOUT).get("default_currency") or ""
        except Exception:
            currency = ""
            errors.append("currency")

    products = None
    if bins is not None:
        products = []
        for b in bins:
            actual, reserved = number(b.get("actual_qty")), number(b.get("reserved_qty"))
            item = labels.get(b["item_code"], {})
            products.append({
                "id": b["item_code"], "name": item.get("item_name") or b["item_code"],
                "unit": item.get("stock_uom") or b.get("stock_uom") or "units",
                "stock": actual, "reserved": reserved,
                "available": None if actual is None or reserved is None else actual - reserved,
                "warehouse": b.get("warehouse", ""),
            })

    return {
        "mode": "live", "generatedAt": now.isoformat(), "today": now.date().isoformat(),
        "since": since, "company": company, "currency": currency,
        "orders": None if orders is None else [order_row(x) for x in orders],
        "pendingOrders": None if pending is None else [order_row(x) for x in pending],
        "customers": None if customers is None else [{
            "id": x["name"], "name": x.get("customer_name") or x["name"],
            "group": x.get("customer_group") or "Customer", "territory": x.get("territory") or "",
        } for x in customers],
        "products": products, "policies": None,
        "agents": model_status(), "erpOrigin": public_erp_origin(),
        "errors": errors, "truncated": truncated, "limit": LIMIT,
    }


class DashboardAPI:
    """Authenticated read endpoints, mounted only at /api/dashboard."""

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        origin = headers.get("origin", "")
        cors = allowed_origin(origin)
        response_headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
            (b"vary", b"Origin"),
        ]
        if cors:
            response_headers.extend([
                (b"access-control-allow-origin", origin.encode()),
                (b"access-control-allow-methods", b"GET, OPTIONS"),
                (b"access-control-allow-headers", b"Authorization"),
            ])

        async def reply(code, payload):
            await send({"type": "http.response.start", "status": code, "headers": response_headers})
            await send({"type": "http.response.body", "body": json.dumps(payload, allow_nan=False).encode()})

        path = scope.get("path", "").removeprefix(scope.get("root_path", ""))
        detail_match = re.fullmatch(r"/orders/([^/]{1,140})", path)
        readers = {"/snapshot": snapshot, "/controls": controls, "/operations": operations}
        if path == "/config" and scope["method"] == "GET":
            # No company, model, origin, or business data is returned before auth.
            await reply(200, {"service": "plus-agent", "apiVersion": 1,
                              "configured": len(os.getenv("DASHBOARD_API_TOKEN", "")) >= 32})
            return
        if path not in readers and not detail_match:
            await reply(404, {"error": "Not found"})
            return
        if scope["method"] == "OPTIONS":
            await reply(200 if cors else 403, {"ok": cors})
            return
        if scope["method"] != "GET":
            await reply(405, {"error": "This dashboard is read-only"})
            return
        if len(os.getenv("DASHBOARD_API_TOKEN", "")) < 32:
            await reply(503, {"error": "Live dashboard access is not configured"})
            return
        if not authorized(headers.get("authorization", "")):
            response_headers.append((b"www-authenticate", b"Bearer"))
            await reply(401, {"error": "A valid dashboard access token is required"})
            return
        # Same-origin requests need no CORS. Cross-origin access is explicit.
        host = headers.get("host", "")
        same_origin = origin == f"{scope.get('scheme', 'http')}://{host}"
        if origin and not same_origin and not cors:
            await reply(403, {"error": "This dashboard origin is not allowed"})
            return
        try:
            if detail_match:
                order_id = unquote(detail_match[1])
                if "/" in order_id or "\\" in order_id or order_id in {".", ".."}:
                    raise RecordNotFound
                data = await asyncio.to_thread(order_detail, order_id)
            else:
                data = await asyncio.to_thread(readers[path])
        except RecordNotFound:
            await reply(404, {"error": "Order not found in this workspace"})
            return
        except Exception:
            await reply(502, {"error": "Could not read CRM data. Check the agent service."})
            return
        await reply(200, data)


def install_dashboard(application) -> None:
    """Mount exactly the same application in production and HTTP integration tests."""
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    application.mount("/api/dashboard", DashboardAPI())
    application.mount(
        "/dashboard", StaticFiles(directory=Path(__file__).parent / "dashboard_ui", html=True),
        name="dashboard",
    )
