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
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field

from app import avisos, erpnext, idioma, modelos, outbound_status, solicitudes
from app.runtime_context import RuntimeContextError, require_management


def _sin_permiso(lengua: str | None = None) -> str:
    """La rama de «no autorizado» de los dos informes de este módulo.

    ERA UNA CONSTANTE DE MÓDULO, y ése era todo el bug: se evalúa al importar,
    o sea antes de que exista un idioma que consultar, así que el único string
    del archivo sin clave de catálogo era justamente el que no podía tenerla.
    Como función se resuelve cuando se contesta, que es cuando se sabe a quién.
    """
    return idioma.t(
        "sistema.sin_permiso", lengua if lengua is not None else idioma.gerencia()
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


def _cuenta(valor: object, lengua: str | None = None) -> str:
    """Un contador, o el centinela de «no pude leer», en el idioma de quien lee.

    `DESCONOCIDO` es la palabra que significa «no leas esto como cero», o sea
    justo la que hay que entender — y salía en español adentro de un informe en
    inglés porque esta función no tomaba idioma. El catálogo ya tenía la fila
    (`sistema.desconocido`, EN «UNKNOWN») y el mismo archivo la usaba bien dos
    líneas más arriba; lo que faltaba era el parámetro.

    None y -1 significan «no pude leer». El número, cuando hay, se interpola
    tal cual: un contador no tiene idioma.
    """
    if valor is None:
        return idioma.t("sistema.desconocido", lengua)
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        return idioma.t("sistema.desconocido", lengua)
    return idioma.t("sistema.desconocido", lengua) if numero < 0 else str(numero)


def _presencia(nombre: str, lengua: str) -> str:
    """Si una variable está cargada y cuánto mide. NUNCA su valor."""
    import os

    valor = str(os.getenv(nombre, "") or "").strip()
    return (
        idioma.t("sistema.cargada", lengua, n=len(valor))
        if valor
        else idioma.t("sistema.vacia", lengua)
    )


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


def _bloque_redis(lengua: str) -> str:
    try:
        outbound_status.cliente().ping()
    except Exception as exc:
        return idioma.t(
            "sistema.componente_caido", lengua, componente="Redis",
            estado=idioma.t("sistema.no_disponible", lengua),
            error=type(exc).__name__,
        )
    return idioma.t(
        "sistema.componente_ok", lengua, componente="Redis",
        estado=idioma.t("sistema.responde", lengua),
    )


def _bloque_erpnext(lengua: str) -> str:
    """Una lectura mínima con la identidad de política: existe y contesta."""
    try:
        erpnext.policy_get_list(
            "Company", fields=["name"], limit=1, timeout=TIMEOUT_ERPNEXT
        )
    except Exception as exc:
        return idioma.t(
            "sistema.componente_caido", lengua, componente="ERPNext",
            estado=idioma.t("sistema.no_disponible", lengua),
            error=type(exc).__name__,
        )
    return idioma.t(
        "sistema.componente_ok", lengua, componente="ERPNext",
        estado=idioma.t("sistema.responde", lengua),
    )


def _bloque_modelos(lengua: str) -> str:
    try:
        prov = modelos.proveedor()
        _, ventas = modelos.nombre_modelo("clientes", prov=prov)
        _, gerencia = modelos.nombre_modelo("gerencia", prov=prov)
        variable, clave = modelos.clave_api(prov)
    except Exception as exc:
        return idioma.t(
            "sistema.componente_caido", lengua, componente="Modelos",
            estado=idioma.t("sistema.desconocido", lengua),
            error=type(exc).__name__,
        )
    estado = (
        f"{variable} " + idioma.t("sistema.cargada", lengua, n=len(clave))
        if clave
        else idioma.t("sistema.clave_vacia", lengua)
    )
    return idioma.t(
        "sistema.modelos", lengua, proveedor=prov.nombre, ventas=ventas,
        gerencia=gerencia, estado=estado,
    )


def _bloque_whatsapp(cuentas: dict, lengua: str) -> str:
    return idioma.t(
        "sistema.whatsapp", lengua,
        numero=_presencia("WHATSAPP_PHONE_NUMBER_ID", lengua),
        token=_presencia("WHATSAPP_TOKEN", lengua),
        rechazadas=_cuenta(cuentas.get("entregas_fallidas"), lengua),
        sin_entregar=_cuenta(cuentas.get("respuestas_en_dead_letter"), lengua),
    )


def _bloque_colas(cuentas: dict, lengua: str) -> str:
    return idioma.t(
        "sistema.colas", lengua, espera=_cuenta(avisos.pendientes(), lengua),
        caidos=_cuenta(cuentas.get("avisos_en_dead_letter"), lengua),
    )


def _bloque_decisiones(lengua: str) -> str:
    trabadas = solicitudes.trabadas()
    try:
        incompleta = solicitudes.reconstruccion_incompleta()
        indice = idioma.t(
            "sistema.indice_pendiente" if incompleta else "sistema.indice_completo",
            lengua,
        )
    except Exception as exc:
        indice = idioma.t(
            "sistema.indice_desconocido", lengua,
            estado=idioma.t("sistema.desconocido", lengua),
            error=type(exc).__name__,
        )
    reservan = (
        idioma.t("sistema.decisiones_reservan", lengua)
        if isinstance(trabadas, int) and trabadas > 0
        else ""
    )
    return idioma.t(
        "sistema.decisiones", lengua, indice=indice,
        trabadas=_cuenta(trabadas, lengua), extra=reservan,
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
        return _sin_permiso()
    try:
        cuentas = outbound_status.contar_pendientes()
    except Exception as exc:
        print(f"[operaciones] contadores no disponibles ({type(exc).__name__})")
        cuentas = {}
    lengua = idioma.gerencia()
    lineas = [
        _bloque_redis(lengua),
        _bloque_erpnext(lengua),
        _bloque_whatsapp(cuentas, lengua),
        _bloque_modelos(lengua),
        _bloque_colas(cuentas, lengua),
        _bloque_decisiones(lengua),
    ]
    return idioma.t("sistema.titulo", lengua) + "\n" + "\n".join(lineas)


def _entradas_de_avisos_caidos(
    maximo: int, lengua: str | None = None
) -> tuple[list[str], str]:
    """(líneas seguras, problema). Las más nuevas primero.

    Una entrada ilegible se cuenta y se salta: la lista es un registro de algo
    que ya falló, y no poder leer una de sus filas no puede tirar la consulta.
    """
    try:
        crudas = outbound_status.cliente().lrange(
            outbound_status.DEAD_NOTIFY_KEY, -maximo, -1
        )
    except Exception as exc:
        return [], idioma.t(
            "sistema.lista_no_disponible",
            lengua,
            estado=idioma.t("sistema.no_disponible", lengua),
            error=type(exc).__name__,
        )

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
        # Los dos fallbacks NO son decorado: se alcanzan en la forma más
        # normal de una respuesta fallida a un cliente, que no tiene pedido.
        pedido = str(entrada.get("order_name") or "").strip() or idioma.t(
            "sistema.sin_pedido", lengua
        )
        proposito = str(entrada.get("purpose") or "").strip() or idioma.t(
            "sistema.sin_proposito", lengua
        )
        # El tag ya es un hash; se recorta igual, y el teléfono nunca estuvo acá.
        tag = str(entrada.get("destinatario") or "").strip()[:8]
        resumen = _titular(entrada.get("resumen"))
        linea = f"· {pedido} — {proposito}"
        if tag:
            linea += idioma.t("sistema.destinatario", lengua, tag=tag)
        if resumen:
            linea += f"\n    {resumen}"
        lineas.append(linea)
    problema = idioma.t("sistema.ilegibles", lengua, n=ilegibles) if ilegibles else ""
    return lineas, problema


@tool
def ver_avisos_fallidos(
    config: RunnableConfig,
    cuantos: Annotated[
        int,
        Field(description="Cuántos registros mostrar, de los más nuevos. Alcanza con el "
                          "default salvo que el dueño pida más."),
    ] = REGISTROS_DEFAULT,
) -> str:
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
        return _sin_permiso()
    maximo = max(1, min(int(cuantos or REGISTROS_DEFAULT), REGISTROS_MAXIMO))
    try:
        cuentas = outbound_status.contar_pendientes()
    except Exception as exc:
        print(f"[operaciones] contadores no disponibles ({type(exc).__name__})")
        cuentas = {}

    lengua = idioma.gerencia()
    lineas = [
        idioma.t("sistema.fallidos_titulo", lengua),
        idioma.t(
            "sistema.fallidos_avisos", lengua,
            n=_cuenta(cuentas.get("avisos_en_dead_letter"), lengua),
        ),
        idioma.t(
            "sistema.fallidos_respuestas", lengua,
            n=_cuenta(cuentas.get("respuestas_en_dead_letter"), lengua),
        ),
        idioma.t(
            "sistema.fallidos_rechazadas", lengua,
            n=_cuenta(cuentas.get("entregas_fallidas"), lengua),
        ),
    ]
    registros, problema = _entradas_de_avisos_caidos(maximo, lengua)
    if problema:
        lineas.append(f"⚠️ {problema}")
    if registros:
        lineas.append(idioma.t("sistema.fallidos_ultimos", lengua, n=len(registros)))
        lineas.extend(registros)
    elif not problema:
        lineas.append(idioma.t("sistema.fallidos_ninguno", lengua))
    lineas.append(idioma.t("sistema.fallidos_pie", lengua))
    return "\n".join(lineas)
