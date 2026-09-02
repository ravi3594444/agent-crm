"""Deterministic, fail-closed Sales Order auto-confirmation policy."""
from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import erpnext
from app.formato import pesos
from app.locks import distributed_lock

MAX_AUTO = float(os.getenv("AUTO_CONFIRM_MAX", "0"))
MAX_MULT = float(os.getenv("AUTO_CONFIRM_MULT", "2.0"))
MIN_PEDIDOS = int(os.getenv("AUTO_CONFIRM_MIN_ORDERS", "3"))
MAX_DEUDA = float(os.getenv("AUTO_CONFIRM_MAX_DEBT", "0"))
STOCK_CONFIABLE = os.getenv("STOCK_CONFIABLE", "false").strip().lower() == "true"
PRICE_LIST = os.getenv("AUTO_CONFIRM_PRICE_LIST", "").strip()
CURRENCY = os.getenv("AUTO_CONFIRM_CURRENCY", "").strip()


@dataclass
class Decision:
    auto: bool
    motivos: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return "auto-confirmado" if self.auto else "; ".join(self.motivos)


def _hoy_del_negocio() -> date:
    zone_name = os.getenv(
        "BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires"
    ).strip()
    try:
        return datetime.now(ZoneInfo(zone_name)).date()
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise erpnext.ERPNextError("BUSINESS_TIMEZONE inválida") from exc


def _float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError) as exc:
        raise erpnext.ERPNextError("ERPNext devolvió un importe inválido") from exc


def _zero(value: object) -> bool:
    return abs(_float(value)) < 0.000001


def evaluar(sales_order: dict) -> Decision:
    """Return auto=True only when every independently verified rule passes."""
    if MAX_AUTO <= 0:
        return Decision(False, ["auto-confirmación desactivada"])

    motivos: list[str] = []
    if not STOCK_CONFIABLE:
        motivos.append("inventario no marcado como confiable")
    if not PRICE_LIST:
        motivos.append("lista estándar de auto-confirmación no configurada")
    if not CURRENCY:
        motivos.append("moneda de auto-confirmación no configurada")

    cliente = str(sales_order.get("customer") or "").strip()
    if not cliente:
        motivos.append("cliente ausente")

    try:
        total = _float(sales_order.get("grand_total"))
    except erpnext.ERPNextError:
        total = 0
        motivos.append("total inválido")
    if total <= 0:
        motivos.append("total no positivo")
    elif total > MAX_AUTO:
        motivos.append(f"monto {pesos(total)} supera el tope de {pesos(MAX_AUTO)}")

    if PRICE_LIST and str(sales_order.get("selling_price_list") or "") != PRICE_LIST:
        motivos.append("lista de precios distinta de la autorizada")
    if CURRENCY and str(sales_order.get("currency") or "") != CURRENCY:
        motivos.append("moneda distinta de la autorizada")
    for field_name in (
        "discount_amount",
        "base_discount_amount",
        "additional_discount_percentage",
    ):
        try:
            if not _zero(sales_order.get(field_name)):
                motivos.append("descuento general no autorizado")
                break
        except erpnext.ERPNextError:
            motivos.append("descuento general inválido")
            break

    if cliente:
        try:
            historial = erpnext.get_list(
                "Sales Order",
                filters=[["customer", "=", cliente], ["docstatus", "=", 1]],
                fields=["grand_total"],
                limit=50,
            )
            importes = [_float(row.get("grand_total")) for row in historial]
            if len(importes) < MIN_PEDIDOS:
                motivos.append(
                    f"cliente con solo {len(importes)} pedidos confirmados"
                )
            else:
                promedio = sum(importes) / len(importes)
                if promedio <= 0:
                    motivos.append("historial sin un promedio positivo")
                elif total > promedio * MAX_MULT:
                    motivos.append(
                        f"pedido {pesos(total)} supera {MAX_MULT:g}x su promedio"
                    )
        except (erpnext.ERPNextError, KeyError):
            motivos.append("no se pudo verificar el historial")

        deuda = _saldo_vencido(cliente)
        if deuda is None:
            motivos.append("no se pudo verificar la deuda vencida")
        elif deuda > MAX_DEUDA:
            motivos.append(f"tiene {pesos(deuda)} vencidos")

    items = sales_order.get("items") or []
    if not isinstance(items, list) or not items:
        motivos.append("pedido sin productos")
        items = []

    # Duplicate item rows must consume their combined quantity, not each pass
    # independently against the same available Bin quantity.
    cantidades: dict[tuple[str, str], float] = {}
    for item in items:
        code = str(item.get("item_code") or "").strip()
        warehouse = str(item.get("warehouse") or "").strip()
        if not code or not warehouse:
            motivos.append("producto o depósito ausente")
            continue
        try:
            qty = _float(item.get("qty"))
        except erpnext.ERPNextError:
            motivos.append(f"cantidad inválida para {code}")
            continue
        if qty <= 0:
            motivos.append(f"cantidad no positiva para {code}")
            continue
        key = (code, warehouse)
        cantidades[key] = cantidades.get(key, 0) + qty

    if STOCK_CONFIABLE:
        for (code, warehouse), qty in cantidades.items():
            try:
                if not _hay_stock(code, qty, warehouse):
                    motivos.append(f"stock insuficiente de {code}")
            except erpnext.ERPNextError:
                motivos.append(f"no se pudo verificar stock de {code}")

    order_day = _order_day(sales_order, motivos)
    if PRICE_LIST and CURRENCY and order_day is not None:
        for item in items:
            code = str(item.get("item_code") or "").strip() or "producto"
            try:
                if not _precio_estandar(item, order_day):
                    motivos.append(f"precio fuera de lista en {code}")
            except erpnext.ERPNextError:
                motivos.append(f"no se pudo verificar precio de {code}")

    entrega = sales_order.get("delivery_date")
    try:
        today = _hoy_del_negocio()
        delivery_day = date.fromisoformat(str(entrega))
        if delivery_day < today:
            motivos.append("fecha de entrega vencida")
        elif delivery_day > today + timedelta(days=30):
            motivos.append("fecha de entrega muy lejana")
    except (ValueError, TypeError, erpnext.ERPNextError):
        motivos.append("fecha de entrega inválida")

    return Decision(not motivos, motivos)


def _order_day(sales_order: dict, motivos: list[str]) -> date | None:
    raw = sales_order.get("transaction_date")
    if not raw:
        try:
            return _hoy_del_negocio()
        except erpnext.ERPNextError:
            motivos.append("fecha del negocio no disponible")
            return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        motivos.append("fecha del pedido inválida")
        return None


def _saldo_vencido(cliente: str) -> float | None:
    """Return overdue balance, or None when privileged verification fails."""
    try:
        today = _hoy_del_negocio()
        rows = erpnext.policy_run_report(
            "Accounts Receivable",
            {
                "company": erpnext.default_company(),
                "customer": [cliente],
                "based_on": "Due Date",
                "report_date": today.isoformat(),
            },
        )
        overdue = 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            amount = _float(row.get("outstanding_amount"))
            if amount <= 0:
                continue
            raw_due = row.get("due_date")
            if not raw_due:
                raise erpnext.ERPNextError("reporte sin fecha de vencimiento")
            if date.fromisoformat(str(raw_due)) < today:
                overdue += amount
        return overdue
    except (erpnext.ERPNextError, ValueError, TypeError):
        return None


def _hay_stock(item_code: str, qty: float, warehouse: str) -> bool:
    if not warehouse or qty <= 0:
        return False
    bins = erpnext.get_list(
        "Bin",
        filters=[
            ["item_code", "=", item_code],
            ["warehouse", "=", warehouse],
        ],
        fields=["actual_qty", "reserved_qty"],
        limit=10,
    )
    disponible = sum(
        _float(row.get("actual_qty")) - _float(row.get("reserved_qty"))
        for row in bins
    )
    buffer = float(os.getenv("STOCK_BUFFER_PCT", "20")) / 100.0
    if buffer < 0 or buffer >= 1:
        raise erpnext.ERPNextError("STOCK_BUFFER_PCT fuera de rango")
    return disponible * (1 - buffer) >= qty


def _precio_estandar(item: dict, order_day: date) -> bool:
    """Verify exact unscoped price, currency, UOM, validity, and no discounts."""
    code = str(item.get("item_code") or "").strip()
    uom = str(item.get("uom") or "").strip()
    stock_uom = str(item.get("stock_uom") or "").strip()
    if not code or not uom or uom != stock_uom:
        return False
    if abs(_float(item.get("conversion_factor")) - 1.0) > 0.000001:
        return False
    for field_name in (
        "discount_percentage",
        "discount_amount",
        "distributed_discount_amount",
    ):
        if not _zero(item.get(field_name)):
            return False

    rate = _float(item.get("rate"))
    list_rate = _float(item.get("price_list_rate"))
    if rate <= 0 or list_rate <= 0 or abs(rate - list_rate) >= 0.01:
        return False

    prices = erpnext.get_list(
        "Item Price",
        filters=[
            ["item_code", "=", code],
            ["selling", "=", 1],
            ["price_list", "=", PRICE_LIST],
            ["currency", "=", CURRENCY],
        ],
        fields=[
            "price_list_rate",
            "price_list",
            "currency",
            "uom",
            "valid_from",
            "valid_upto",
            "customer",
            "batch_no",
        ],
        limit=100,
    )
    for price in prices:
        if str(price.get("price_list") or "") != PRICE_LIST:
            continue
        if str(price.get("currency") or "") != CURRENCY:
            continue
        if str(price.get("uom") or "") != uom:
            continue
        if price.get("customer") or price.get("batch_no"):
            continue
        try:
            valid_from = (
                date.fromisoformat(str(price["valid_from"]))
                if price.get("valid_from")
                else date.min
            )
            valid_upto = (
                date.fromisoformat(str(price["valid_upto"]))
                if price.get("valid_upto")
                else date.max
            )
            configured_rate = _float(price.get("price_list_rate"))
        except (ValueError, erpnext.ERPNextError):
            continue
        if valid_from <= order_day <= valid_upto and abs(configured_rate - rate) < 0.01:
            return True
    return False


@contextmanager
def auto_submit_lock() -> Iterator[None]:
    """Serialize the final stock recheck and submit across all app workers."""
    with distributed_lock("auto-submit-global", lease_seconds=300, wait_seconds=5):
        yield
