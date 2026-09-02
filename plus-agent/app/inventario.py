"""¿Se le puede prometer stock de este producto a un cliente?

LO QUE ESTO REEMPLAZA
`STOCK_CONFIABLE=true` era una promesa escrita una vez en el .env: decía
"confiá en el inventario" y el sistema confiaba para siempre, aunque nadie
hubiera contado nada en tres semanas. En una lechería el stock del sistema se
despega de la realidad en horas —ventas en el mostrador, ventas del camión,
roturas—, así que esa promesa envejecía y nadie se enteraba.

Ahora la confianza se GANA y se VENCE:

  un producto es confiable  ⇔  alguien contó ESE producto en ESE depósito
                               y confirmó el ajuste hace menos de
                               STOCK_CONFIABLE_HORAS

La confianza es por PAR (item_code, warehouse), y las dos mitades importan:
contar la leche no dice nada del queso, y contar la leche de un depósito no
dice nada de la leche del otro. Un "confiable" global dejaría que un conteo de
un producto avalara todos los productos de todos los depósitos.

El interruptor maestro sigue existiendo. Con STOCK_CONFIABLE=false nada es
confiable, se haya contado o no: es la forma de apagar todo de una.

FALLA CERRADA
Cualquier duda —no se pudo leer ERPNext, no hay conteos, la fecha no se
entiende, el conteo dice ser del futuro— es "no confiable". Nunca se estima.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import erpnext

# Cuántos conteos confirmados de un producto se miran para encontrar el último.
# Son los más recientes primero, así que el más nuevo está en el primer puñado.
MAX_CONTEOS = 20


def maestra_encendida() -> bool:
    """El interruptor de despliegue, leído en cada llamada (no al importar)."""
    return os.getenv("STOCK_CONFIABLE", "false").strip().lower() == "true"


def horas_de_validez() -> float:
    """Cuánto vale un conteo. Un valor ilegible o <= 0 no habilita nada."""
    try:
        horas = float(os.getenv("STOCK_CONFIABLE_HORAS", "24"))
    except (TypeError, ValueError):
        return 0.0
    return horas if horas > 0 else 0.0


def _ahora() -> datetime:
    zona = os.getenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires").strip()
    try:
        return datetime.now(ZoneInfo(zona))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise erpnext.ERPNextError("BUSINESS_TIMEZONE inválida") from exc


def _momento(fila: dict) -> datetime | None:
    """Cuándo dice ERPNext que se hizo el conteo, en hora del negocio."""
    fecha = str(fila.get("posting_date") or "").strip()
    hora = str(fila.get("posting_time") or "00:00:00").strip()
    if not fecha:
        return None
    zona = os.getenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires").strip()
    try:
        crudo = datetime.fromisoformat(f"{fecha} {hora}")
        return crudo.replace(tzinfo=ZoneInfo(zona))
    except (ValueError, ZoneInfoNotFoundError):
        return None


def ultimo_conteo(item_code: str, warehouse: str) -> datetime | None:
    """El conteo CONFIRMADO más reciente de ese producto, o None.

    Sólo `docstatus = 1`: un borrador de Stock Reconciliation es alguien que
    escribió un número por WhatsApp, no un inventario que el dueño validó.
    """
    if not item_code or not warehouse:
        return None
    filas = erpnext.policy_get_list(
        "Stock Reconciliation Item",
        filters=[
            ["item_code", "=", item_code],
            ["warehouse", "=", warehouse],
            ["docstatus", "=", 1],
        ],
        fields=["parent", "item_code", "warehouse", "docstatus"],
        limit=MAX_CONTEOS,
        parent="Stock Reconciliation",
        order_by="modified desc",
    )
    nombres = []
    for fila in filas:
        if str(fila.get("item_code") or item_code).strip() != item_code:
            continue
        if str(fila.get("warehouse") or warehouse).strip() != warehouse:
            continue
        nombre = str(fila.get("parent") or "").strip()
        if nombre and nombre not in nombres:
            nombres.append(nombre)
    if not nombres:
        return None

    conteos = erpnext.policy_get_list(
        "Stock Reconciliation",
        filters=[["name", "in", nombres], ["docstatus", "=", 1]],
        fields=["name", "docstatus", "posting_date", "posting_time"],
        limit=len(nombres),
    )
    momentos = []
    for conteo in conteos:
        if int(float(conteo.get("docstatus") or 0)) != 1:
            continue
        momento = _momento(conteo)
        if momento is not None:
            momentos.append(momento)
    return max(momentos) if momentos else None


def confiable(item_code: str, warehouse: str) -> tuple[bool, str]:
    """(confiable, motivo). El motivo explica en criollo por qué no.

    Nunca levanta: cualquier duda vuelve como "no confiable", que es lo que
    tanto la política de auto-confirmación como el nivel que se le dice al
    cliente necesitan para no prometer nada.
    """
    if not maestra_encendida():
        return False, "el inventario está marcado como no confiable"
    horas = horas_de_validez()
    if horas <= 0:
        return False, "STOCK_CONFIABLE_HORAS no es un número de horas válido"
    try:
        momento = ultimo_conteo(item_code, warehouse)
        ahora = _ahora()
    except erpnext.ERPNextError as exc:
        print(f"[inventario] no pude leer conteos de {item_code}: {exc}")
        return False, f"no pude verificar el último conteo de {item_code}"
    if momento is None:
        return False, f"nadie confirmó un conteo de {item_code}"
    if momento > ahora:
        return False, f"el último conteo de {item_code} dice ser del futuro"
    if ahora - momento > timedelta(hours=horas):
        antiguedad = (ahora - momento).total_seconds() / 3600.0
        return False, (
            f"el último conteo de {item_code} es de hace {antiguedad:.0f} h "
            f"(vale {horas:g} h)"
        )
    return True, ""
