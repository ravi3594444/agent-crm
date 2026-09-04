"""Dos herramientas de SÓLO LECTURA para que el dueño pregunte «¿cómo está esto?».

QUÉ SON Y QUÉ NO SON
Leen contadores y estados que ya existen (app/outbound_status.py, app/avisos.py,
app/solicitudes.py, app/modelos.py) y los cuentan en una respuesta corta. No
escriben, no reintentan, no borran, no marcan nada como visto y no cambian
ninguna política de pedidos, stock, entrega o confirmación. Si mañana hace
falta reintentar un aviso, eso es otra herramienta y otra decisión.

LO QUE NUNCA SALE DE ACÁ
Ninguna clave, ningún token, ningún teléfono completo, ningún log crudo y
ningún texto que haya escrito un cliente. De una credencial se informa si está
o no y cuántos caracteres tiene; de un destinatario, un tag hasheado recortado;
de un aviso caído, su primera línea —el titular que armó este sistema— y nunca
el cuerpo, porque el aviso al equipo incluye la cita del cliente.

FALLAR NO ES ESTAR EN CERO
«No pude leer el contador» y «no hay ninguno» son respuestas distintas y sólo
una significa que nadie tiene que hacer nada. Cada bloque falla por separado y
dice NO DISPONIBLE o DESCONOCIDO; ninguno reporta 0 ni OK cuando no pudo
averiguarlo.

POR QUÉ NO IMPORTA app.main
app/main.py construye el webhook y el worker al importarse. Estas herramientas
las importa app/graph.py, que a su vez lo importa app/main.py: importarlo acá
sería un ciclo. Los contadores de las colas de main viven en Redis y se leen
por sus claves públicas en app/outbound_status.py.
"""
from __future__ import annotations

import json

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app import avisos, erpnext, modelos, outbound_status, solicitudes
from app.runtime_context import RuntimeContextError, require_management

_SIN_PERMISO = (
    "Ese número no está autorizado para ver el estado del sistema. No consulté nada."
)

# Una prueba de vida tiene que contestar en segundos o no sirve como prueba de
# vida: el dueño está esperando en WhatsApp.
TIMEOUT_ERPNEXT = 4.0

NO_DISPONIBLE = "NO DISPONIBLE"
DESCONOCIDO = "DESCONOCIDO"

# Cuántos registros se muestran, y el techo duro que ni el modelo ni nadie
# puede pasar: esto va a un mensaje de WhatsApp.
REGISTROS_DEFAULT = 10
REGISTROS_MAXIMO = 20

# La cola de respuestas al cliente que no se pudieron entregar (app/main.py).
# Se informa SÓLO su tamaño: cada entrada es el mensaje del cliente con su
# teléfono, y nada de eso puede salir por acá.
CLAVE_DEAD_RESPUESTAS = "wa:{inbound}:dead"


def _cuenta(valor: object) -> str:
    """Un contador, o DESCONOCIDO. None y -1 significan «no pude leer»."""
    if valor is None:
        return DESCONOCIDO
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        return DESCONOCIDO
    return DESCONOCIDO if numero < 0 else str(numero)


def _presencia(nombre: str) -> str:
    """Si una variable está cargada y cuánto mide. NUNCA su valor."""
    import os

    valor = str(os.getenv(nombre, "") or "").strip()
    return f"cargada ({len(valor)} caracteres)" if valor else "VACÍA"


def _titular(texto: object, tope: int = 100) -> str:
    """La primera línea útil de un aviso, sin la cita del cliente.

    El aviso al equipo lleva el texto del cliente citado con «>» después de un
    salto de línea (ver solicitudes.texto_para_equipo). El titular es lo que
    armó este sistema —«Pedido X confirmado», «Decisión pendiente Y»— así que
    se toma la primera línea que no sea una cita y se recorta.
    """
    for linea in str(texto or "").splitlines():
        limpia = linea.strip()
        if limpia and not limpia.startswith(">"):
            return limpia[:tope]
    return ""


def _bloque_redis() -> str:
    try:
        outbound_status.cliente().ping()
    except Exception as exc:
        return f"· Redis: {NO_DISPONIBLE} ({type(exc).__name__})"
    return "· Redis: responde"


def _bloque_erpnext() -> str:
    """Una lectura mínima con la identidad de política: existe y contesta."""
    try:
        erpnext.policy_get_list(
            "Company", fields=["name"], limit=1, timeout=TIMEOUT_ERPNEXT
        )
    except Exception as exc:
        return f"· ERPNext: {NO_DISPONIBLE} ({type(exc).__name__})"
    return "· ERPNext: responde"


def _bloque_modelos() -> str:
    try:
        prov = modelos.proveedor()
        _, ventas = modelos.nombre_modelo("clientes", prov=prov)
        _, gerencia = modelos.nombre_modelo("gerencia", prov=prov)
        variable, clave = modelos.clave_api(prov)
    except Exception as exc:
        return f"· Modelos: {DESCONOCIDO} ({type(exc).__name__})"
    estado = f"{variable} cargada ({len(clave)} caracteres)" if clave else "clave VACÍA"
    return (
        f"· Modelos: proveedor {prov.nombre} — ventas {ventas}, gerencia {gerencia}; "
        f"{estado}"
    )


def _bloque_whatsapp(cuentas: dict) -> str:
    return (
        f"· WhatsApp: número {_presencia('WHATSAPP_PHONE_NUMBER_ID')}, "
        f"token {_presencia('WHATSAPP_TOKEN')}; "
        f"{_cuenta(cuentas.get('entregas_fallidas'))} entrega(s) que Meta rechazó, "
        f"{_cuenta(cuentas.get('respuestas_en_dead_letter'))} respuesta(s) a clientes "
        "sin entregar"
    )


def _bloque_colas(cuentas: dict) -> str:
    return (
        f"· Cola de avisos al cliente: {_cuenta(avisos.pendientes())} en espera, "
        f"{_cuenta(cuentas.get('avisos_en_dead_letter'))} caído(s)"
    )


def _bloque_decisiones() -> str:
    trabadas = solicitudes.trabadas()
    try:
        incompleta = solicitudes.reconstruccion_incompleta()
        indice = "reconstrucción PENDIENTE" if incompleta else "índice completo"
    except Exception as exc:
        indice = f"índice {DESCONOCIDO} ({type(exc).__name__})"
    return (
        f"· Decisiones: {indice}; borradores trabados "
        f"{_cuenta(trabadas)}"
        + (
            " (siguen reservando stock; hay un ToDo por cada uno)"
            if isinstance(trabadas, int) and trabadas > 0
            else ""
        )
    )


@tool
def estado_del_sistema(config: RunnableConfig) -> str:
    """Estado operativo del sistema: Redis, ERPNext, WhatsApp, modelos y colas.

    Usala cuando el dueño pregunta si el sistema está funcionando, si hay algo
    trabado, si los avisos están saliendo, o antes de una prueba en vivo.

    SÓLO LECTURA: no reintenta, no arregla y no cambia nada. Lo que no se pudo
    verificar dice NO DISPONIBLE o DESCONOCIDO — no lo interpretes como "cero"
    ni como "está bien", y no adivines el motivo.
    """
    try:
        require_management(config)
    except RuntimeContextError:
        return _SIN_PERMISO
    try:
        cuentas = outbound_status.contar_pendientes()
    except Exception as exc:
        print(f"[operaciones] contadores no disponibles ({type(exc).__name__})")
        cuentas = {}
    lineas = [
        _bloque_redis(),
        _bloque_erpnext(),
        _bloque_whatsapp(cuentas),
        _bloque_modelos(),
        _bloque_colas(cuentas),
        _bloque_decisiones(),
    ]
    return "Estado del sistema:\n" + "\n".join(lineas)


def _entradas_de_avisos_caidos(maximo: int) -> tuple[list[str], str]:
    """(líneas seguras, problema). Las más nuevas primero.

    Una entrada ilegible se cuenta y se salta: la lista es un registro de algo
    que ya falló, y no poder leer una de sus filas no puede tirar la consulta.
    """
    try:
        crudas = outbound_status.cliente().lrange(
            outbound_status.DEAD_NOTIFY_KEY, -maximo, -1
        )
    except Exception as exc:
        return [], f"la lista de avisos caídos está {NO_DISPONIBLE} ({type(exc).__name__})"

    lineas: list[str] = []
    ilegibles = 0
    for cruda in reversed(list(crudas or [])):
        try:
            texto = cruda.decode() if isinstance(cruda, bytes) else str(cruda)
            entrada = json.loads(texto)
            if not isinstance(entrada, dict):
                raise ValueError("no es un objeto")
        except Exception:
            ilegibles += 1
            continue
        pedido = str(entrada.get("order_name") or "").strip() or "sin pedido"
        proposito = str(entrada.get("purpose") or "").strip() or "sin propósito"
        # El tag ya es un hash; se recorta igual, y el teléfono nunca estuvo acá.
        tag = str(entrada.get("destinatario") or "").strip()[:8]
        resumen = _titular(entrada.get("resumen"))
        linea = f"· {pedido} — {proposito}"
        if tag:
            linea += f" — destinatario {tag}…"
        if resumen:
            linea += f"\n    {resumen}"
        lineas.append(linea)
    problema = f"{ilegibles} entrada(s) ilegible(s) omitida(s)" if ilegibles else ""
    return lineas, problema


@tool
def ver_avisos_fallidos(config: RunnableConfig, cuantos: int = REGISTROS_DEFAULT) -> str:
    """Avisos y respuestas que NO llegaron: cuántos y los últimos registros.

    Usala cuando el dueño pregunta si algún cliente quedó sin respuesta, si un
    aviso no salió, o qué quedó pendiente de contactar a mano.

    `cuantos` es cuántos registros mostrar (máximo 20). SÓLO LECTURA: no
    reintenta, no borra y no marca nada como visto; cada aviso caído ya tiene su
    tarea en ERPNext. No se muestran teléfonos ni el texto que escribió el
    cliente.
    """
    try:
        require_management(config)
    except RuntimeContextError:
        return _SIN_PERMISO
    maximo = max(1, min(int(cuantos or REGISTROS_DEFAULT), REGISTROS_MAXIMO))
    try:
        cuentas = outbound_status.contar_pendientes()
    except Exception as exc:
        print(f"[operaciones] contadores no disponibles ({type(exc).__name__})")
        cuentas = {}

    lineas = [
        "Comunicación que no llegó:",
        f"· Avisos sin entregar: {_cuenta(cuentas.get('avisos_en_dead_letter'))}",
        f"· Respuestas a clientes sin entregar: "
        f"{_cuenta(cuentas.get('respuestas_en_dead_letter'))} "
        "(no se listan: cada una lleva el mensaje del cliente)",
        f"· Entregas que Meta rechazó: {_cuenta(cuentas.get('entregas_fallidas'))}",
    ]
    registros, problema = _entradas_de_avisos_caidos(maximo)
    if problema:
        lineas.append(f"⚠️ {problema}")
    if registros:
        lineas.append(f"\nÚltimos {len(registros)} aviso(s) caído(s), del más nuevo:")
        lineas.extend(registros)
    elif not problema:
        lineas.append("\nNo hay avisos caídos registrados.")
    lineas.append(
        "\nCada uno tiene una tarea en ERPNext para contactarlo a mano. Desde acá "
        "no se reintenta nada."
    )
    return "\n".join(lineas)
