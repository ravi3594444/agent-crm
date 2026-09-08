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
- `python -m app.digest` (cron o docker compose run digest) lo intenta ahora.
Los dos pasan por el MISMO reclamo atómico del día en Redis (SET NX EX): el
primero que lo toma manda, el otro no manda nada. Antes era leer-y-después-
marcar, y el cron de las 18:00 saltaba la marca: dos procesos a la misma hora
eran dos resúmenes. Sin Redis no se reclama y no se manda (fallar cerrado).
`python -m app.digest --forzar` es la única forma de saltarse el reclamo, y es
para una persona en una terminal, no para un cron.

A QUIÉN VA
Al dueño, y sólo a él: TELEFONO_DUENO (app/notificar.py::telefono_dueno). No
sale por la ruta genérica de alertas al equipo, que elige «el primero» de la
lista ordenada alfabéticamente y podía caer en otro miembro del equipo.

Cada sección falla por separado: si ERPNext no contesta, la sección dice
"no pude leer" y el resto sale igual. Nunca levanta.
"""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import erpnext, inventario, locks, notificar, outbound_status
from app import idioma as idioma_mod
from app.formato import pesos

HORA_DEFAULT = "18:00"
MAX_LINEAS = 15
# Desde qué porcentaje del techo de borradores el resumen avisa. Antes de
# esto la fuga es normal; pasado el techo la auto-confirmación muere entera.
UMBRAL_BORRADORES_PCT = 80.0
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
    """¿Alguien ya reclamó el resumen de ese día? Sólo lectura, para no
    componer el resumen en vano cada minuto. La decisión de mandar NO se toma
    acá: la toma ``reclamar``, que es atómico."""
    try:
        return locks.conexion().get(_clave(dia or _ahora().date())) is not None
    except Exception as exc:
        print(f"[digest] no pude leer la marca del día ({type(exc).__name__})")
        # Sin Redis no se puede garantizar "una vez": mejor no mandar dos.
        return True


def reclamar(dia: date, testigo: str) -> bool:
    """Toma el día para ESTE proceso, o devuelve False si otro ya lo tomó.

    ``testigo`` es el valor que queda escrito; sólo quien lo escribió puede
    soltar el reclamo (``liberar``), y sólo si no llegó a mandar nada.

    UN solo SET NX EX: reclamar y marcar son la misma operación, así que dos
    procesos que llegan a la vez —el scheduler del agente y el cron— no pueden
    reclamar los dos. El que pierde no compone ni manda nada.

    El reclamo queda aunque el envío después falle: si Meta rechazó el
    resumen, el aviso ya quedó en la lista de avisos fallidos con su ToDo
    (notificar.registrar_aviso_fallido), y reintentar cada minuto hasta
    medianoche no lo arreglaría. Un día tiene un solo intento.

    Sin Redis no hay forma de garantizar «una vez»: no se reclama y no se
    manda. Es el mismo criterio de ``enviado_hoy``.
    """
    try:
        return bool(locks.conexion().set(_clave(dia), testigo, nx=True, ex=MARCA_TTL_SEGUNDOS))
    except Exception as exc:
        print(f"[digest] no pude reclamar el día ({type(exc).__name__}); no mando")
        return False


def liberar(dia: date, testigo: str) -> None:
    """Suelta el reclamo si todavía es el nuestro. Sólo cuando NO se mandó nada.

    Es para una falla al COMPONER el resumen: nada salió, nada quedó registrado
    como fallido, y quedarse con el día tomado sería perder el resumen de hoy
    por un error que el próximo intento quizá no tenga. Una falla al ENVIAR es
    otra cosa y conserva el reclamo (ver ``enviar``). Best effort: si Redis no
    contesta, el reclamo vence solo con su TTL.
    """
    try:
        cliente = locks.conexion()
        actual = cliente.get(_clave(dia))
        if isinstance(actual, bytes):
            actual = actual.decode()
        if actual == testigo:
            cliente.delete(_clave(dia))
    except Exception as exc:
        print(f"[digest] no pude soltar el reclamo ({type(exc).__name__})")


# ----------------------------------------------------------------- secciones


def _pedidos(filtros: list, orden: str) -> list[dict]:
    return erpnext.policy_get_list(
        "Sales Order",
        filters=filtros,
        fields=["name", "customer", "customer_name", "grand_total", "currency", "delivery_date", "status"],
        limit=200,
        order_by=orden,
    )


def _linea_pedido(
    so: dict, *, edad: float | None = None, a_mano: bool = False
) -> str:
    """Una línea de pedido. `edad` y `a_mano` son sólo para los pendientes.

    Compartida con seccion_despacho, que no pasa ninguno de los dos y sigue
    imprimiendo exactamente lo que imprimía.
    """
    linea = (
        f"· {so.get('name')} — {so.get('customer_name') or so.get('customer')} — "
        f"{pesos(so.get('grand_total'))} — entrega {so.get('delivery_date') or 's/f'}"
    )
    if edad is not None:
        linea += f" — hace {edad:.0f} h"
    if a_mano:
        linea += " — cargado a mano"
    return linea


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
    """Los borradores que esperan, con su edad y de dónde vienen.

    Muestra TODOS, no sólo los del agente: un borrador que una persona cargó a
    mano en ERPNext retiene el stock que promete exactamente igual
    (`policy._borradores_que_reservan` filtra por `docstatus` y `status`, no por
    origen), cuenta para el mismo techo de borradores, y es de los que el dueño
    tiene que ir a limpiar — el recordatorio automático no los toca y el cierre
    tampoco. Esconderlos acá los dejaría invisibles y sin dueño.

    Por eso el número va DESCOMPUESTO: «11 del bot + 3 cargados a mano». Así el
    total de esta sección nunca discute con el del recordatorio ni con los de
    autonomía, que cuentan sólo los del bot; el número se separa en vez de
    reconciliarse.
    """
    from app import pendientes

    try:
        filas = pendientes.listar_esperando(limite=200)
    except Exception as exc:
        print(f"[digest] pendientes: {type(exc).__name__}")
        return "🟡 Esperan tu decisión: no pude leer ERPNext"
    ahora = _ahora()
    del_bot = [f for f in filas if pendientes.del_agente(f)]
    a_mano = [f for f in filas if not pendientes.del_agente(f)]
    lineas = [_linea_pedido(f, edad=pendientes.edad_horas(f, ahora)) for f in del_bot]
    lineas += [
        _linea_pedido(f, edad=pendientes.edad_horas(f, ahora), a_mano=True)
        for f in a_mano
    ]
    titulo = "🟡 Esperan tu decisión"
    if a_mano:
        titulo = f"{titulo} · {len(del_bot)} del bot + {len(a_mano)} cargados a mano"
    # La instrucción va DESPUÉS de armar la sección, no dentro de `lineas`:
    # `_seccion` usa len(lineas) como el total entre paréntesis, así que
    # meterla ahí hacía que el encabezado dijera uno más que los pedidos que
    # lista — justo lo que el docstring promete que no puede pasar.
    cuerpo = _seccion(titulo, lineas, "ninguno")
    if lineas:
        cuerpo += "\nRespondé 'confirmar <pedido>', 'rechazar <pedido>' o 'ver <pedido>'."
    return cuerpo


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


def seccion_trabadas() -> str:
    """Drafts ERPNext will not close. Nothing else in this digest shows them."""
    from app import solicitudes

    cuantas = solicitudes.trabadas()
    if cuantas is None:
        return "🔒 Borradores trabados: no pude leer el contador"
    if not cuantas:
        return "🔒 Borradores trabados: ninguno"
    return (
        f"🔒 Borradores trabados ({cuantas}): ERPNext no los deja cerrar y "
        "siguen reservando stock que no se puede vender. Hay un ToDo por cada "
        "uno; cerralos o confirmalos a mano en ERPNext."
    )


# Cada sección tiene su propio «no pude leer», pero el resumen no puede
# depender de que cada función lo haya previsto todo: una excepción que se
# escape de una sección no puede tirar el resumen entero, porque para entonces
# el día ya está reclamado. La sección que falla dice que falló; las demás
# salen igual.
_SECCIONES: tuple[tuple[str, str], ...] = (
    ("seccion_despacho", "🚚 Confirmados para preparar/despachar: no pude armar esta sección"),
    ("seccion_pendientes", "🟡 Esperan tu decisión: no pude armar esta sección"),
    ("seccion_conteos", "📦 Conteos: no pude armar esta sección"),
    ("seccion_trabadas", "🔒 Borradores trabados: no pude armar esta sección"),
    ("seccion_fallos", "⚠️ Comunicación: no pude armar esta sección"),
    ("seccion_autonomia", "📈 Autonomía: no pude armar esta sección"),
)


def seccion_autonomia() -> str:
    """Cuánto se confirma solo, y qué lo frena. Y el techo de borradores.

    El techo va acá porque es la falla más grande que este sistema puede tener
    y hoy no se ve en ninguna parte: pasados policy.MAX_BORRADORES borradores
    vivos, `_borradores_que_reservan` levanta y no se auto-confirma NADA, de
    ningún producto, con un motivo que se lee igual que una caída de ERPNext.
    """
    from app import autonomia

    try:
        datos = autonomia.resumen()
        cuerpo = autonomia.texto(datos, idioma_mod.gerencia())
    except Exception as exc:
        print(f"[digest] autonomia: {type(exc).__name__}")
        return "📈 Autonomía: no pude armar esta sección"
    borradores = datos.get("borradores")
    if borradores and borradores.get("pasado"):
        cuerpo += (
            f"\n🚨 PASASTE EL TECHO de {borradores['tope']} borradores: hasta que "
            "bajen, NINGÚN pedido se confirma solo y el motivo se parece a una "
            "caída de ERPNext. Cerrá o confirmá los que sobran."
        )
    elif borradores and borradores.get("pct", 0) >= UMBRAL_BORRADORES_PCT:
        cuerpo += (
            f"\n⚠️ {borradores['vivos']} de {borradores['tope']} borradores "
            f"({borradores['pct']:.0f} %). Pasado el techo no se confirma solo "
            "nada, de ningún producto."
        )
    return cuerpo


def _seccion_segura(nombre: str, respaldo: str) -> str:
    funcion = globals()[nombre]
    try:
        return str(funcion())
    except Exception as exc:
        print(f"[digest] {nombre}: {type(exc).__name__}; la sección sale con su respaldo")
        return respaldo


def resumen(dia: date | None = None) -> str:
    dia = dia or _ahora().date()
    return "\n\n".join(
        [
            f"📋 Resumen del {dia.isoformat()}",
            *(_seccion_segura(nombre, respaldo) for nombre, respaldo in _SECCIONES),
        ]
    )


# --------------------------------------------------------------------- envío


def enviar(*, forzar: bool = False) -> bool:
    """Manda el resumen del día AL DUEÑO, una vez por día.

    Primero se reclama el día (``reclamar``, atómico); sólo el proceso que lo
    consigue compone y manda. El reclamo queda aunque nadie lo haya recibido:
    si Meta lo rechazó, el aviso ya está en la lista de avisos fallidos con su
    ToDo, y un segundo intento el mismo día sería el mismo rechazo.

    ``forzar`` se salta el reclamo y NO lo toma: es para una persona que quiere
    el resumen ahora aunque ya haya salido. Ningún camino automático lo usa.
    """
    dia = _ahora().date()
    testigo = f"{_ahora().isoformat()}:{uuid.uuid4().hex[:8]}"
    if forzar:
        print(f"[digest] {dia.isoformat()} envío forzado: no reclamo el día")
    elif not reclamar(dia, testigo):
        return False
    try:
        texto = resumen(dia)
    except Exception as exc:
        # Nada salió y nada quedó registrado: el día tiene que seguir
        # disponible para el próximo intento. Distinto de una falla al enviar.
        print(f"[digest] {dia.isoformat()} no pude componer el resumen ({type(exc).__name__})")
        if not forzar:
            liberar(dia, testigo)
        return False
    ok = notificar.avisar_dueno(
        "📋 Resumen del día",
        texto,
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
    # enviar() reclama el día en forma atómica: la lectura de arriba sólo
    # ahorra componer el resumen cada minuto cuando ya salió.
    return enviar()


def main(argv: list[str] | None = None) -> bool:
    """`python -m app.digest`: el camino del cron. Mismo reclamo que el agente.

    Sin argumentos pasa por ``reclamar`` igual que el scheduler: si el agente
    ya lo mandó hoy, el cron no manda nada. ``--forzar`` se lo salta, para una
    persona en una terminal; el cron no debe llevarlo.
    """
    import sys

    argumentos = sys.argv[1:] if argv is None else list(argv)
    return enviar(forzar="--forzar" in argumentos)


if __name__ == "__main__":
    main()
