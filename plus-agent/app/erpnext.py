"""Thin ERPNext REST client.

The customer agent never touches MariaDB and authenticates as a restricted
ERPNext user. A separate policy identity is used only for privileged policy
reads and the deterministic submit transition.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import quote

import httpx

ERPNEXT_URL = os.environ["ERPNEXT_URL"]
ERPNEXT_KEY = os.environ["ERPNEXT_API_KEY"]
ERPNEXT_SECRET = os.environ["ERPNEXT_API_SECRET"]

_HEADERS = {
    "Authorization": f"token {ERPNEXT_KEY}:{ERPNEXT_SECRET}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

_client = httpx.Client(base_url=ERPNEXT_URL, headers=_HEADERS, timeout=20.0)
_manager_client: httpx.Client | None = None
_credential_scope: ContextVar[str] = ContextVar(
    "erpnext_credential_scope", default="customer"
)


class ERPNextError(RuntimeError):
    """A sanitized ERPNext failure safe to pass through internal tool logic."""


def _manager() -> httpx.Client:
    """Build the broad management client lazily and fail closed if absent."""
    global _manager_client
    if _manager_client is None:
        try:
            key = os.environ["ERPNEXT_MANAGER_API_KEY"].strip()
            secret = os.environ["ERPNEXT_MANAGER_API_SECRET"].strip()
        except KeyError as exc:
            raise ERPNextError("Credenciales de gerencia ERPNext no configuradas") from exc
        if not key or not secret:
            raise ERPNextError("Credenciales de gerencia ERPNext no configuradas")
        _manager_client = httpx.Client(
            base_url=ERPNEXT_URL,
            headers={
                "Authorization": f"token {key}:{secret}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
    return _manager_client


def _active_client() -> httpx.Client:
    return _manager() if _credential_scope.get() == "management" else _client


@contextmanager
def customer_scope() -> Iterator[None]:
    """Force restricted credentials for an entire customer-agent turn."""
    token = _credential_scope.set("customer")
    try:
        yield
    finally:
        _credential_scope.reset(token)


@contextmanager
def manager_scope() -> Iterator[None]:
    """Use manager credentials for a turn, refusing to run if not configured."""
    _manager()  # validate before any model/tool work starts
    token = _credential_scope.set("management")
    try:
        yield
    finally:
        _credential_scope.reset(token)


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    operation: str,
    **kwargs: Any,
) -> dict:
    """Run one request without exposing ERPNext response bodies to the LLM."""
    try:
        response = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise ERPNextError(f"ERPNext no disponible durante {operation}") from exc
    if response.status_code >= 400:
        raise ERPNextError(
            f"ERPNext rechazó {operation} (estado {response.status_code})"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise ERPNextError(
            f"ERPNext devolvió una respuesta inválida durante {operation}"
        ) from exc
    if not isinstance(body, dict):
        raise ERPNextError(
            f"ERPNext devolvió una respuesta inválida durante {operation}"
        )
    return body


def _resource_path(doctype: str, name: str | None = None) -> str:
    path = f"/api/resource/{quote(doctype, safe='')}"
    return f"{path}/{quote(name, safe='')}" if name is not None else path


def _list(
    client: httpx.Client,
    doctype: str,
    filters: list | None,
    fields: list[str] | None,
    limit: int,
    parent: str | None,
) -> list[dict]:
    params: dict[str, Any] = {"limit_page_length": limit}
    if filters:
        params["filters"] = json.dumps(filters, ensure_ascii=False)
    if fields:
        params["fields"] = json.dumps(fields, ensure_ascii=False)
    if parent:
        # Frappe refuses to list a child doctype (a table row such as "Sales
        # Order Item") without being told which parent doctype it belongs to.
        params["parent"] = parent
    body = _request(
        client,
        "GET",
        _resource_path(doctype),
        operation=f"la consulta de {doctype}",
        params=params,
    )
    data = body.get("data")
    # An answer with no list in it is NOT "zero rows". This used to coalesce
    # with `or []`, so a 200 carrying {"data": null}, {} or someone else's
    # envelope read as "nothing found" — and app/policy.py reads "nothing
    # found" as "nothing is promised", which is how the last units get sold
    # twice. Absence of an answer has to be an error.
    if not isinstance(data, list):
        raise ERPNextError(f"ERPNext devolvió datos inválidos para {doctype}")
    return data


def get_list(
    doctype: str,
    filters: list | None = None,
    fields: list[str] | None = None,
    limit: int = 20,
    parent: str | None = None,
) -> list[dict]:
    return _list(_active_client(), doctype, filters, fields, limit, parent)


def get_doc(doctype: str, name: str) -> dict:
    body = _request(
        _active_client(),
        "GET",
        _resource_path(doctype, name),
        operation=f"la lectura de {doctype}",
    )
    data = body.get("data")
    if not isinstance(data, dict):
        raise ERPNextError(f"ERPNext devolvió datos inválidos para {doctype}")
    return data


def create_doc(doctype: str, payload: dict) -> dict:
    """Create a draft; the restricted identity has no Submit permission."""
    body = _request(
        _active_client(),
        "POST",
        _resource_path(doctype),
        operation=f"la creación de {doctype}",
        json={**payload, "docstatus": 0},
    )
    data = body.get("data")
    if not isinstance(data, dict):
        raise ERPNextError(f"ERPNext devolvió datos inválidos al crear {doctype}")
    return data


def add_comment(doctype: str, name: str, text: str) -> None:
    """Best-effort audit note; it never changes the known order outcome."""
    try:
        _request(
            _active_client(),
            "POST",
            _resource_path("Comment"),
            operation="la creación del comentario de auditoría",
            json={
                "comment_type": "Comment",
                "reference_doctype": doctype,
                "reference_name": name,
                "content": text,
            },
        )
    except ERPNextError as exc:
        print(f"[erpnext] comentario de auditoría no creado: {exc}")


def _run_report(client: httpx.Client, report_name: str, filters: dict | None) -> list:
    body = _request(
        client,
        "GET",
        "/api/method/frappe.desk.query_report.run",
        operation=f"el reporte {report_name}",
        params={
            "report_name": report_name,
            "filters": json.dumps(filters or {}, ensure_ascii=False),
        },
    )
    message = body.get("message") or {}
    result = message.get("result") if isinstance(message, dict) else None
    if not isinstance(result, list):
        raise ERPNextError(
            f"ERPNext devolvió datos inválidos para el reporte {report_name}"
        )
    return result


def run_report(report_name: str, filters: dict | None = None) -> list:
    """Run an ERPNext report with the active customer/management identity."""
    return _run_report(_active_client(), report_name, filters)


def default_context() -> tuple[str, str]:
    """Return the explicitly configured company and fulfilment warehouse.

    A fallback warehouse is unsafe on multi-company sites. Missing or partial
    configuration therefore fails closed before an order can be written.
    """
    company = os.getenv("ERPNEXT_COMPANY", "").strip()
    warehouse = os.getenv("ERPNEXT_WAREHOUSE", "").strip()
    if not company or not warehouse:
        raise ERPNextError(
            "ERPNEXT_COMPANY y ERPNEXT_WAREHOUSE deben configurarse explícitamente"
        )
    return company, warehouse


def default_company() -> str:
    return default_context()[0]


def default_warehouse() -> str:
    return default_context()[1]


_policy_client: httpx.Client | None = None


def _policy() -> httpx.Client:
    global _policy_client
    if _policy_client is None:
        try:
            key = os.environ["ERPNEXT_POLICY_API_KEY"]
            secret = os.environ["ERPNEXT_POLICY_API_SECRET"]
        except KeyError as exc:
            raise ERPNextError("Credenciales de política ERPNext no configuradas") from exc
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


def policy_run_report(report_name: str, filters: dict | None = None) -> list:
    """Run a policy-only report using the privileged non-LLM identity."""
    return _run_report(_policy(), report_name, filters)


def policy_get_doc(doctype: str, name: str) -> dict:
    """Read a document with the policy identity after privileged transitions."""
    body = _request(
        _policy(),
        "GET",
        _resource_path(doctype, name),
        operation=f"la lectura de política de {doctype}",
    )
    data = body.get("data")
    if not isinstance(data, dict):
        raise ERPNextError(f"ERPNext devolvió datos inválidos para {doctype}")
    return data


def policy_get_list(
    doctype: str,
    filters: list | None = None,
    fields: list[str] | None = None,
    limit: int = 20,
    parent: str | None = None,
) -> list[dict]:
    """List documents with the policy identity, for policy checks only.

    The restricted customer-agent user must not be able to enumerate other
    customers' orders. app/policy.py needs exactly that to know how much stock
    is already promised, so the read runs under the non-LLM policy identity.
    """
    return _list(_policy(), doctype, filters, fields, limit, parent)


def policy_update_status(doctype: str, name: str, status: str) -> dict:
    """Set only the workflow ``status`` field, with the policy identity.

    Used by the manual rejection path so a draft nobody will fulfil stops
    holding stock. It writes one Select field: it cannot submit, cancel, or
    change quantities, prices or amounts. Submitting is still submit_doc alone.

    A Frappe PUT is a save, not a field write: the doctype's own validate() can
    recompute the field and still answer 200 carrying the OLD value. Reporting
    that as success would be a lie the caller acts on, so the saved value is
    checked and a silent reset is raised as an error.
    """
    body = _request(
        _policy(),
        "PUT",
        _resource_path(doctype, name),
        operation=f"la actualización de estado de {doctype}",
        json={"status": status},
    )
    data = body.get("data")
    if not isinstance(data, dict):
        raise ERPNextError(
            f"ERPNext devolvió datos inválidos al actualizar {doctype}"
        )
    guardado = str(data.get("status") or "").strip()
    if guardado != status:
        raise ERPNextError(
            f"ERPNext no dejó {doctype} {name} en estado {status}: "
            f"quedó en {guardado or 'un estado desconocido'}"
        )
    return data


def submit_doc(doctype: str, name: str) -> dict:
    body = _request(
        _policy(),
        "PUT",
        _resource_path(doctype, name),
        operation=f"la confirmación de {doctype}",
        json={"docstatus": 1},
    )
    data = body.get("data")
    if not isinstance(data, dict):
        raise ERPNextError(
            f"ERPNext devolvió datos inválidos al confirmar {doctype}"
        )
    return data
