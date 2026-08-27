"""Auto-confirmation policy. The thing that removes the wait.

THE INSIGHT
Most orders are boring: a known customer, ordering what they always order,
at list price, in stock. Those should confirm INSTANTLY. Only the unusual
ones need a human.

THE SAFETY
Notice what this file is: deterministic Python. It never sees the customer's
words. The LLM cannot call it, cannot argue with it, cannot be talked into
widening it. The agent still has NO submit tool — the policy engine submits,
and only when every rule passes.

That is how you get instant confirmation without giving an LLM the keys.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta

from app import erpnext

MAX_AUTO = float(os.getenv("AUTO_CONFIRM_MAX", "0"))          # 0 = feature off
MAX_MULT = float(os.getenv("AUTO_CONFIRM_MULT", "2.0"))       # x customer average
MIN_PEDIDOS = int(os.getenv("AUTO_CONFIRM_MIN_ORDERS", "3"))  # order history required
MAX_DEUDA = float(os.getenv("AUTO_CONFIRM_MAX_DEBT", "0"))    # overdue tolerated


@dataclass
class Decision:
    auto: bool
    motivos: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return "auto-confirmado" if self.auto else "; ".join(self.motivos)


def evaluar(sales_order: dict) -> Decision:
    """Every rule must pass. Any single failure sends it to a human."""
    motivos: list[str] = []

    if MAX_AUTO <= 0:
        return Decision(False, ["auto-confirmación desactivada"])

    total = float(sales_order.get("grand_total") or 0)
    cliente = sales_order.get("customer")

    # 1. Hard ceiling. Nothing large auto-confirms, ever.
    if total > MAX_AUTO:
        motivos.append(f"monto ${total:,.0f} supera el tope de ${MAX_AUTO:,.0f}")

    # 2. Known customer with real history.
    historial = erpnext.get_list(
        "Sales Order",
        filters=[["customer", "=", cliente], ["docstatus", "=", 1]],
        fields=["grand_total"],
        limit=50,
    )
    if len(historial) < MIN_PEDIDOS:
        motivos.append(f"cliente con solo {len(historial)} pedidos confirmados")
    else:
        promedio = sum(h["grand_total"] for h in historial) / len(historial)
        if total > promedio * MAX_MULT:
            motivos.append(
                f"pedido ${total:,.0f} es {total / promedio:.1f}x su promedio de ${promedio:,.0f}"
            )

    # 3. No overdue balance.
    deuda = _saldo_vencido(cliente)
    if deuda > MAX_DEUDA:
        motivos.append(f"tiene ${deuda:,.0f} vencidos")

    # 4. Everything actually in stock, above the safety buffer.
    for item in sales_order.get("items", []):
        if not _hay_stock(item["item_code"], item["qty"]):
            motivos.append(f"stock insuficiente de {item['item_code']}")

    # 5. Standard prices only — no negotiated rates slipping through.
    for item in sales_order.get("items", []):
        if not _precio_de_lista(item["item_code"], float(item.get("rate") or 0)):
            motivos.append(f"precio fuera de lista en {item['item_code']}")

    # 6. Reasonable delivery date.
    entrega = sales_order.get("delivery_date")
    if entrega and date.fromisoformat(str(entrega)) > date.today() + timedelta(days=30):
        motivos.append("fecha de entrega muy lejana")

    return Decision(not motivos, motivos)


def _saldo_vencido(cliente: str) -> float:
    try:
        rows = erpnext.run_report(
            "Accounts Receivable",
            {"company": erpnext.default_company(), "customer": cliente},
        )
        return sum(
            float(r.get("outstanding_amount") or 0)
            for r in rows
            if isinstance(r, dict) and (r.get("age") or 0) > 0
        )
    except Exception:
        return float("inf")  # can't verify -> treat as risky -> human reviews


def _hay_stock(item_code: str, qty: float) -> bool:
    bins = erpnext.get_list(
        "Bin",
        filters=[["item_code", "=", item_code]],
        fields=["actual_qty", "reserved_qty"],
        limit=10,
    )
    disponible = sum(b["actual_qty"] - b.get("reserved_qty", 0) for b in bins)
    buffer = float(os.getenv("STOCK_BUFFER_PCT", "20")) / 100.0
    return disponible * (1 - buffer) >= qty


def _precio_de_lista(item_code: str, rate: float) -> bool:
    precios = erpnext.get_list(
        "Item Price",
        filters=[["item_code", "=", item_code], ["selling", "=", 1]],
        fields=["price_list_rate"],
        limit=1,
    )
    if not precios:
        return False
    return abs(float(precios[0]["price_list_rate"]) - rate) < 0.01
