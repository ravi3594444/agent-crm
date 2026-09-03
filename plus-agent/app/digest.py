"""Resumen de las 18:00 para el dueño. Determinista: ningún modelo lo redacta.

Lo que el dueño necesita ver al cerrar el día, en el orden en que lo va a
resolver:

  1. pedidos CONFIRMADOS que esperan preparación o despacho,
  2. pedidos que esperan SU decisión (borradores vivos),
  3. conteos de stock vencidos o por vencer (sin conteo fresco el bot no
     promete stock, ver app/inventario.py),
  4. avisos que no llegaron y respuestas en dead-letter (app/outbound_status.py).

CÓMO SE DISPARA
- El propio agente (app/main.py) lo intenta una vez por día a partir de
  DIGEST_HORA en BUSINESS_TIMEZONE.
- `python -m app.digest` (cron o docker compose run digest) lo manda ahora.
Los dos usan la misma marca en Redis, así que un día tiene un solo resumen.

Cada sección falla por separado: si ERPNext no contesta, la sección dice
"no pude leer" y el resto sale igual. Nunca levanta.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import erpnext, inventario, locks, notificar, outbound_status, policy
from app.formato import pesos

HORA_DEFAULT = "18:00"
MAX_LINEAS = 15
MARCA_TTL_SEGUNDOS = 36 * 60 * 60
ESTADOS_ESPERANDO_DESPACHO = ("To Deliver and Bill", "To Deliver")


def _zona() -> ZoneInfo:
    nombre = os.getenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires").strip()
    try:
        return ZoneInfo(nombre)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("America/Argentina/Buenos_Aires")


def _ahora() -> datetime:
    return datetime.now(_zona())


def hora_objetivo() -> tuple[int, int]:
    """DIGEST_HORA como (hora, minuto); un valor ilegible vuelve al default."""
    crudo = os.getenv("DIGEST_HORA", HORA_DEFAULT).strip() or HORA_DEFAULT
    try:
        hh, mm = crudo.split(":")
        hora, minuto = int(hh), int(mm)
        if 0 <= hora <= 23 and 0 <= minuto <= 59:
            return hora, minuto
    except ValueError:
        pass
    print(f"[digest] DIGEST_HORA={crudo!r} inválida; uso {HORA_DEFAULT}")
    hh, mm = HORA_DEFAULT.split(":")
    return int(hh), int(mm)


def activo() -> bool:
    return os.getenv("DIGEST_ACTIVO", "true").strip().lower() in {"true", "1", "yes", "si", "sí"}


def _clave(dia: date) -> str:
    return f"plus-agent:digest:{dia.isoformat()}"


def enviado_hoy(dia: date | None = None) -> bool:
    try:
        return locks.conexion().get(_clave(dia or _ahora().date())) is not None
    except Exception as exc:
        print(f"[digest] no pude leer la marca del día ({type(exc).__name__})")
        # Sin Redis no se puede garantizar "una vez": mejor no mandar dos.
        return True


def _marcar(dia: date) -> None:
    try:
        locks.conexion().setex(_clave(dia), MARCA_TTL_SEGUNDOS, "1")
    except Exception as exc:
        print(f"[digest] no pude marcar el día ({type(exc).__name__})")


# ----------------------------------------------------------------- secciones


def _pedidos(filtros: list, orden: str) -> list[dict]:
    return erpnext.policy_get_list(
        "Sales Order",
        filters=filtros,
        fields=["name", "customer", "customer_name", "grand_total", "currency", "delivery_date", "status"],
        limit=200,
        order_by=orden,
    )


def _linea_pedido(so: dict) -> str:
    return (
        f"· {so.get('name')} — {so.get('customer_name') or so.get('customer')} — "
        f"{pesos(so.get('grand_total'))} — entrega {so.get('delivery_date') or 's/f'}"
    )


def _seccion(titulo: str, lineas: list[str], vacio: str) -> str:
    if not lineas:
        return f"{titulo}: {vacio}"
    visibles = lineas[:MAX_LINEAS]
    resto = len(lineas) - len(visibles)
    cuerpo = "\n".join(visibles)
    if resto > 0:
        cuerpo += f"\n· … y {resto} más"
    return f"{titulo} ({len(lineas)}):\n{cuerpo}"


def seccion_despacho() -> str:
    try:
        filas = _pedidos(
            [["docstatus", "=", 1], ["status", "in", list(ESTADOS_ESPERANDO_DESPACHO)]],
            "delivery_date asc",
        )
    except Exception as exc:
        print(f"[digest] despacho: {type(exc).__name__}")
        return "🚚 Confirmados para preparar/despachar: no pude leer ERPNext"
    return _seccion(
        "🚚 Confirmados para preparar/despachar",
        [_linea_pedido(f) for f in filas],
        "ninguno",
    )


def seccion_pendientes() -> str:
    try:
        filas = _pedidos(
            [["docstatus", "=", 0], ["status", "not in", list(policy.ESTADOS_SIN_RESERVA)]],
            "creation asc",
        )
    except Exception as exc:
        print(f"[digest] pendientes: {type(exc).__name__}")
        return "🟡 Esperan tu decisión: no pude leer ERPNext"
    lineas = [_linea_pedido(f) for f in filas]
    if lineas:
        lineas.append("Respondé 'confirmar <pedido>', 'rechazar <pedido>' o 'ver <pedido>'.")
    return _seccion("🟡 Esperan tu decisión", lineas, "ninguno")


def seccion_conteos() -> str:
    if not inventario.maestra_encendida():
        return "📦 Conteos: STOCK_CONFIABLE=false, el bot no promete stock de nada"
    horas = inventario.horas_de_validez()
    if horas <= 0:
        return "📦 Conteos: STOCK_CONFIABLE_HORAS inválida, nada es confiable"
    try:
        deposito = erpnext.default_warehouse()
        bins = erpnext.policy_get_list(
            "Bin", filters=[["warehouse", "=", deposito]], fields=["item_code"], limit=200
        )
        ahora = _ahora()
    except Exception as exc:
        print(f"[digest] conteos: {type(exc).__name__}")
        return "📦 Conteos: no pude leer ERPNext"
    productos = sorted({str(b.get("item_code") or "").strip() for b in bins} - {""})
    vencidos: list[str] = []
    por_vencer: list[str] = []
    for code in productos:
        try:
            momento = inventario.ultimo_conteo(code, deposito)
        except Exception as exc:
            print(f"[digest] conteo {code}: {type(exc).__name__}")
            vencidos.append(f"· {code}: no pude leer el conteo")
            continue
        if momento is None:
            vencidos.append(f"· {code}: nunca se confirmó un conteo")
            continue
        restante = timedelta(hours=horas) - (ahora - momento)
        if restante <= timedelta(0):
            vencidos.append(f"· {code}: vencido hace {(-restante).total_seconds() / 3600:.0f} h")
        elif restante <= timedelta(hours=max(3.0, horas * 0.25)):
            por_vencer.append(f"· {code}: vence en {restante.total_seconds() / 3600:.0f} h")
    lineas = vencidos + por_vencer
    return _seccion(
        "📦 Conteos vencidos o por vencer",
        lineas,
        f"todos los conteos vigentes ({len(productos)} productos)",
    )


def seccion_fallos() -> str:
    cuentas = outbound_status.contar_pendientes()

    def _n(clave: str) -> str:
        valor = cuentas.get(clave)
        return "?" if valor is None else str(valor)

    return (
        "⚠️ Comunicación: "
        f"{_n('avisos_en_dead_letter')} avisos sin entregar, "
        f"{_n('respuestas_en_dead_letter')} respuestas a clientes en dead-letter, "
        f"{_n('entregas_fallidas')} mensajes que Meta no pudo entregar"
    )


def resumen(dia: date | None = None) -> str:
    dia = dia or _ahora().date()
    return "\n\n".join(
        [
            f"📋 Resumen del {dia.isoformat()}",
            seccion_despacho(),
            seccion_pendientes(),
            seccion_conteos(),
            seccion_fallos(),
        ]
    )


# --------------------------------------------------------------------- envío


def enviar(*, forzar: bool = False) -> bool:
    """Manda el resumen al equipo. Una vez por día salvo ``forzar``.

    El día queda marcado aunque nadie lo haya recibido: si Meta lo rechazó, el
    aviso ya quedó en la lista de avisos fallidos con su ToDo, y reintentar cada
    minuto hasta medianoche no lo arreglaría.
    """
    dia = _ahora().date()
    if not forzar and enviado_hoy(dia):
        return False
    texto = resumen(dia)
    _marcar(dia)
    ok = notificar.alertar_excepcion(
        "📋 Resumen del día",
        texto,
        urgencia=notificar.URGENCIA_NORMAL,
        plantilla_env="WHATSAPP_STAFF_ALERT_TEMPLATE",
    )
    print(f"[digest] {dia.isoformat()} {'enviado' if ok else 'NO entregado'}")
    return ok


def tick() -> bool:
    """Lo llama el agente cada minuto: manda el resumen del día una sola vez."""
    if not activo():
        return False
    ahora = _ahora()
    if (ahora.hour, ahora.minute) < hora_objetivo():
        return False
    if enviado_hoy(ahora.date()):
        return False
    return enviar()


if __name__ == "__main__":
    enviar(forzar=True)
