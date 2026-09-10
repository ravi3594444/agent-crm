"""Read-only dashboard API. No agent tools, policy identity, or write operations.

An independent ASGI surface keeps its bearer auth and CORS away from the signed
WhatsApp webhook. The UI is public static content; business data never is.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import math
import os
from datetime import timedelta

LIMIT = 250


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
            rows = erpnext.get_list(doctype, limit=LIMIT + 1, **kwargs)
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
            filters=[["company", "=", company], ["transaction_date", ">=", since]],
            fields=["name", "customer", "customer_name", "transaction_date", "delivery_date",
                    "grand_total", "currency", "status", "docstatus"],
            order_by="transaction_date desc, name desc",
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
            currency = erpnext.get_doc("Company", company).get("default_currency") or ""
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

    # Only whitelisted, non-secret model identifiers are returned. Configured
    # does not mean the provider or WhatsApp is healthy: no probe is implied.
    provider = os.getenv("LLM_PROVIDER", "qwen")
    model_vars = (
        ("QWEN_SALES_MODEL", "QWEN_MANAGER_MODEL") if provider == "qwen"
        else ("GEMINI_SALES_MODEL", "GEMINI_MANAGER_MODEL")
    )
    models = [os.getenv(key) or "Provider default" for key in model_vars]
    return {
        "mode": "live", "generatedAt": now.isoformat(), "today": now.date().isoformat(),
        "since": since, "company": company, "currency": currency,
        "orders": None if orders is None else [order_row(x) for x in orders],
        "customers": None if customers is None else [{
            "id": x["name"], "name": x.get("customer_name") or x["name"],
            "group": x.get("customer_group") or "Customer", "territory": x.get("territory") or "",
        } for x in customers],
        "products": products, "policies": None,
        "agents": [
            {"id": "sales", "name": "Sales agent", "role": "Customer conversations & order drafts",
             "model": models[0], "status": "Not checked"},
            {"id": "manager", "name": "Management agent", "role": "Business reports & manager assistance",
             "model": models[1], "status": "Not checked"},
        ],
        "errors": errors, "truncated": truncated, "limit": LIMIT,
    }


class DashboardAPI:
    """Bearer-authenticated GET /snapshot, mounted only at /api/dashboard."""

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
        if path != "/snapshot":
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
            data = await asyncio.to_thread(snapshot)
        except Exception:
            await reply(502, {"error": "Could not read CRM data. Check the agent service."})
            return
        await reply(200, data)
