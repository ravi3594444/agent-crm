"""
Thin ERPNext REST client.

RULE: the agent NEVER touches MariaDB. Everything goes through this file.
RULE: this client authenticates as a dedicated ERPNext user ("Agente IA")
      whose Role grants create-draft but NOT submit. The permission boundary
      is enforced by ERPNext, not by the prompt. Prompt injection cannot
      escalate past a role.

SERIALIZACIÓN DE FILTROS
Frappe espera los filtros como JSON en el query string. Se serializan con
`json.dumps`, no con `str(...).replace("'", '"')`: ese truco rompe con
cualquier apóstrofo en el dato ("D'Angelo" -> JSON inválido -> 400) y le da
al que escribe el mensaje control sobre la estructura del filtro.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from app import log

_log = log.get("erpnext")

ERPNEXT_URL = os.environ["ERPNEXT_URL"]  # http://backend:8000 (internal docker network)
ERPNEXT_KEY = os.environ["ERPNEXT_API_KEY"]
ERPNEXT_SECRET = os.environ["ERPNEXT_API_SECRET"]

_TIMEOUT = float(os.getenv("ERPNEXT_TIMEOUT", "20"))
_REINTENTOS = int(os.getenv("ERPNEXT_RETRIES", "2"))

_HEADERS = {
    "Authorization": f"token {ERPNEXT_KEY}:{ERPNEXT_SECRET}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# Reintenta solo lo idempotente (GET). Los POST no se reintentan nunca acá:
# un POST repetido crea un segundo documento.
_transport = httpx.HTTPTransport(retries=_REINTENTOS)

_client = httpx.Client(
    base_url=ERPNEXT_URL, headers=_HEADERS, timeout=_TIMEOUT, transport=_transport
)


class ERPNextError(RuntimeError):
    """Raised so the agent can tell the customer something went wrong,
    instead of hallucinating a successful order."""


def _json_param(valor: Any) -> str:
    """Serializa un filtro/lista de campos como JSON válido.

    `ensure_ascii=False` para no romper acentos en los `like`.
    """
    return json.dumps(valor, ensure_ascii=False)


def _check(r: httpx.Response) -> Any:
    if r.status_code >= 400:
        _log.warning(
            "%s %s -> %s: %s", r.request.method, r.request.url, r.status_code, r.text[:300]
        )
        raise ERPNextError(f"ERPNext {r.status_code}: {r.text[:300]}")
    return r.json().get("data")


def get_list(
    doctype: str,
    filters: list | None = None,
    fields: list[str] | None = None,
    limit: int = 20,
    order_by: str | None = None,
) -> list[dict]:
    params: dict[str, Any] = {"limit_page_length": limit}
    if filters:
        params["filters"] = _json_param(filters)
    if fields:
        params["fields"] = _json_param(fields)
    if order_by:
        # Explícito siempre que el orden importe. El default de Frappe es
        # `modified desc`, que no es lo que uno quiere para "los últimos N".
        params["order_by"] = order_by
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
    doc = _check(_client.post(f"/api/resource/{doctype}", json=payload))
    _log.info("creado %s %s", doctype, (doc or {}).get("name"))
    return doc


def add_comment(doctype: str, name: str, text: str) -> None:
    """Audit trail. Every single AI write leaves a footprint on the record,
    so at 2am you know whether the bot or a human did it."""
    try:
        r = _client.post(
            "/api/resource/Comment",
            json={
                "comment_type": "Comment",
                "reference_doctype": doctype,
                "reference_name": name,
                "content": text,
            },
        )
        if r.status_code >= 400:
            _log.warning("comentario en %s %s falló: %s", doctype, name, r.text[:200])
    except Exception as e:
        # Nunca falla un pedido de un cliente porque la nota de auditoría
        # falló — pero queda en el log, no en el vacío.
        _log.warning("comentario en %s %s falló: %s", doctype, name, e)


def run_report(report_name: str, filters: dict | None = None) -> list:
    """Run an ERPNext Query Report and return its rows.

    This is how the management agent gets numbers. ERPNext computes them;
    the LLM only explains them. Never let the model aggregate raw rows.
    """
    r = _client.get(
        "/api/method/frappe.desk.query_report.run",
        params={"report_name": report_name, "filters": _json_param(filters or {})},
    )
    if r.status_code >= 400:
        _log.warning("reporte %s falló: %s", report_name, r.text[:300])
        raise ERPNextError(f"report {report_name} failed: {r.text[:200]}")
    return r.json().get("message", {}).get("result", [])


_company_cache: str | None = None


def default_company() -> str:
    global _company_cache
    if _company_cache is None:
        forzada = os.getenv("ERPNEXT_COMPANY", "").strip()
        if forzada:
            _company_cache = forzada
        else:
            rows = get_list("Company", fields=["name"], limit=2, order_by="name asc")
            if len(rows) > 1:
                _log.warning(
                    "hay %d compañías en ERPNext y ERPNEXT_COMPANY no está seteada; "
                    "uso '%s'. Setealo para que no dependa del orden de la base.",
                    len(rows),
                    rows[0]["name"],
                )
            _company_cache = rows[0]["name"] if rows else ""
    return _company_cache


_warehouse_cache: str | None = None


def default_warehouse() -> str:
    global _warehouse_cache
    if _warehouse_cache is None:
        forzado = os.getenv("ERPNEXT_WAREHOUSE", "").strip()
        if forzado:
            _warehouse_cache = forzado
        else:
            rows = get_list(
                "Warehouse",
                filters=[["is_group", "=", 0]],
                fields=["name"],
                limit=2,
                order_by="name asc",
            )
            if len(rows) > 1:
                _log.warning(
                    "hay %d depósitos y ERPNEXT_WAREHOUSE no está seteada; uso '%s'. "
                    "Setealo si el stock que importa está en otro.",
                    len(rows),
                    rows[0]["name"],
                )
            _warehouse_cache = rows[0]["name"] if rows else ""
    return _warehouse_cache


def reset_caches() -> None:
    """Para los tests, y para poder recargar sin reiniciar el contenedor."""
    global _company_cache, _warehouse_cache
    _company_cache = None
    _warehouse_cache = None


# ---------------------------------------------------------------------------
# SUBMIT — separate credentials, deliberately NOT importable as an agent tool.
# Only app/policy.py (via app/tools/pedidos.py) and app/aprobacion.py call
# this, and only after every deterministic rule passes, or after a human on
# the staff list taps the button. The LLM has no route to reach it.
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
            timeout=float(os.getenv("ERPNEXT_POLICY_TIMEOUT", "30")),
        )
    return _policy_client


def submit_doc(doctype: str, name: str) -> dict:
    r = _policy().put(f"/api/resource/{doctype}/{name}", json={"docstatus": 1})
    if r.status_code >= 400:
        _log.error("submit %s %s falló: %s", doctype, name, r.text[:300])
        raise ERPNextError(f"submit {doctype} {name} failed: {r.text[:300]}")
    _log.info("submit %s %s ok", doctype, name)
    return r.json().get("data")
