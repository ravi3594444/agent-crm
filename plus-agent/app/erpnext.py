"""
Thin ERPNext REST client.

RULE: the agent NEVER touches MariaDB. Everything goes through this file.
RULE: this client authenticates as a dedicated ERPNext user ("Agente IA")
      whose Role grants create-draft but NOT submit. The permission boundary
      is enforced by ERPNext, not by the prompt. Prompt injection cannot
      escalate past a role.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

ERPNEXT_URL = os.environ["ERPNEXT_URL"]          # http://backend:8000 (internal docker network)
ERPNEXT_KEY = os.environ["ERPNEXT_API_KEY"]
ERPNEXT_SECRET = os.environ["ERPNEXT_API_SECRET"]

_HEADERS = {
    "Authorization": f"token {ERPNEXT_KEY}:{ERPNEXT_SECRET}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

_client = httpx.Client(base_url=ERPNEXT_URL, headers=_HEADERS, timeout=20.0)


class ERPNextError(RuntimeError):
    """Raised so the agent can tell the customer something went wrong,
    instead of hallucinating a successful order."""


def _check(r: httpx.Response) -> Any:
    if r.status_code >= 400:
        raise ERPNextError(f"ERPNext {r.status_code}: {r.text[:300]}")
    return r.json().get("data")


def get_list(
    doctype: str,
    filters: list | None = None,
    fields: list[str] | None = None,
    limit: int = 20,
) -> list[dict]:
    params: dict[str, Any] = {"limit_page_length": limit}
    if filters:
        params["filters"] = str(filters).replace("'", '"')
    if fields:
        params["fields"] = str(fields).replace("'", '"')
    return _check(_client.get(f"/api/resource/{doctype}", params=params)) or []


def get_doc(doctype: str, name: str) -> dict:
    return _check(_client.get(f"/api/resource/{doctype}/{name}"))


def create_doc(doctype: str, payload: dict) -> dict:
    """Creates a DRAFT document (docstatus 0).

    We never send docstatus=1. Submitting is a human action in the ERPNext UI.
    That single fact is the entire money/stock guardrail — and ERPNext gives
    it to us for free via its built-in draft -> submitted -> cancelled model.
    """
    payload = {**payload, "docstatus": 0}
    return _check(_client.post(f"/api/resource/{doctype}", json=payload))


def add_comment(doctype: str, name: str, text: str) -> None:
    """Audit trail. Every single AI write leaves a footprint on the record,
    so at 2am you know whether the bot or a human did it."""
    try:
        _client.post(
            "/api/resource/Comment",
            json={
                "comment_type": "Comment",
                "reference_doctype": doctype,
                "reference_name": name,
                "content": text,
            },
        )
    except Exception:
        pass  # never fail a customer order because the audit note failed


def run_report(report_name: str, filters: dict | None = None) -> list:
    """Run an ERPNext Query Report and return its rows.

    This is how the management agent gets numbers. ERPNext computes them;
    the LLM only explains them. Never let the model aggregate raw rows.
    """
    r = _client.get(
        "/api/method/frappe.desk.query_report.run",
        params={"report_name": report_name, "filters": str(filters or {}).replace("'", '"')},
    )
    if r.status_code >= 400:
        raise ERPNextError(f"report {report_name} failed: {r.text[:200]}")
    return r.json().get("message", {}).get("result", [])


_company_cache: str | None = None


def default_company() -> str:
    global _company_cache
    if _company_cache is None:
        rows = get_list("Company", fields=["name"], limit=1)
        _company_cache = rows[0]["name"] if rows else ""
    return _company_cache


_warehouse_cache: str | None = None


def default_warehouse() -> str:
    global _warehouse_cache
    if _warehouse_cache is None:
        rows = get_list(
            "Warehouse", filters=[["is_group", "=", 0]], fields=["name"], limit=1
        )
        _warehouse_cache = rows[0]["name"] if rows else ""
    return _warehouse_cache


# ---------------------------------------------------------------------------
# SUBMIT — separate credentials, deliberately NOT importable as an agent tool.
# Only app/policy.py calls this, and only after every deterministic rule
# passes. The LLM has no route to reach it.
# ---------------------------------------------------------------------------
_policy_client: httpx.Client | None = None


def _policy() -> httpx.Client:
    global _policy_client
    if _policy_client is None:
        key = os.environ["ERPNEXT_POLICY_API_KEY"]
        secret = os.environ["ERPNEXT_POLICY_API_SECRET"]
        _policy_client = httpx.Client(
            base_url=ERPNEXT_URL,
            headers={
                "Authorization": f"token {key}:{secret}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
    return _policy_client


def submit_doc(doctype: str, name: str) -> dict:
    r = _policy().put(f"/api/resource/{doctype}/{name}", json={"docstatus": 1})
    if r.status_code >= 400:
        raise ERPNextError(f"submit {doctype} {name} failed: {r.text[:300]}")
    return r.json().get("data")
