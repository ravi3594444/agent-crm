"""El único reloj del negocio: qué hora es, qué día es, y qué dice un sello.

POR QUÉ EXISTE
`os.getenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires")` + `ZoneInfo`
estaba escrito **13 veces en 11 archivos**, cada uno con su envoltorio:
`_zona()` dos veces, `_ahora()` siete, `_hoy()` cinco. `policy._hoy_del_negocio`
y `tools/pedidos._hoy_del_negocio` eran la MISMA función copiada, hasta el
mensaje de error.

Y no encarecía el cambio — lo medimos: cambiar el concepto en una copia rompe
UN archivo de test, no diez, porque cada archivo de test ejercita una sola
copia. Lo que hacía era **invisible la divergencia**: si el concepto cambia en
una copia, las otras diez siguen con el comportamiento viejo y ningún test
puede notar que discrepan, porque ninguno mira dos copias a la vez. Así
pasaron #12, #14 y #16 — tres bugs del mismo concepto en tres semanas.

LO QUE LA CONSOLIDACIÓN ENCONTRÓ, Y ES EL MOTIVO POR EL QUE VALÍA
Las once copias no estaban de acuerdo sobre **qué pasa con una zona inválida**.
Cuatro comportamientos distintos, ningún test afirmando ninguno:

  1. levantar `erpnext.ERPNextError`  — inventario, policy, tools/pedidos
  2. levantar `RuntimeError`          — conversacion
  3. usar el default configurado      — pendientes (con log), digest (sin log)
  4. usar el reloj DEL SERVIDOR       — notificar, limites, acciones

La (4) es la peligrosa y es la única que este módulo no conserva. `datetime.now()`
sin zona, en un contenedor que casi siempre es UTC, sellando un registro
DURABLE (`[limite]`, `[accion]`) con un reloj distinto del que decide todo lo
demás y sin decirlo en ninguna parte. Esas tres pasan a (3): el default
configurado, con log. Nadie gana una excepción que antes no tenía —las tres
eran respaldos y siguen siendo respaldos— y el respaldo pasa a ser el reloj del
negocio en vez del de la máquina.

LAS DOS FORMAS, Y POR QUÉ SON DOS
`zona()` levanta y `zona_con_respaldo()` no, porque la diferencia es real y
depende de quién pregunta:

  * Lo que DECIDE tiene que fallar cerrado. `policy` con una fecha adivinada
    auto-confirma pedidos contra el día equivocado, así que una zona ilegible
    ahí no es «menos útil», es hacer algo mal: levanta.
  * Lo que INFORMA o SELLA no puede morirse por eso. El barrido de
    `pendientes` que se cae deja de recordarle al cliente para siempre, y un
    sello de auditoría que no se escribe es peor que uno escrito con el default
    y un log al lado.

Un solo comportamiento habría cambiado producción en once lugares a la vez. Los
call sites que levantan traducen la excepción ellos —`ZonaInvalida` es un
`RuntimeError`, y 46 lugares atrapan `erpnext.ERPNextError` en particular— así
que la política de error queda VISIBLE en cada módulo en vez de escondida acá.

EL DEFAULT VIVE ACÁ Y EN NINGÚN OTRO LADO
`ZONA_DEFAULT` era un literal en 11 archivos de `app/` y 16 de `tests/`. Un
cliente que no está en Buenos Aires se configuraba buscando 27 sitios.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# El único lugar donde vive. `readiness.chequear_zona_erpnext` lo compara contra
# lo que declara ERPNext, así que también es el valor que ese chequeo espera.
ZONA_DEFAULT = "America/Argentina/Buenos_Aires"

VARIABLE = "BUSINESS_TIMEZONE"


class ZonaInvalida(RuntimeError):
    """BUSINESS_TIMEZONE no nombra una zona que exista.

    Hereda de `RuntimeError` a propósito: `conversacion.business_today`
    levantaba exactamente eso, así que quien atrapaba `RuntimeError` ahí sigue
    atrapando esto. Los tres módulos que levantaban `erpnext.ERPNextError`
    —que también es un `RuntimeError`, pero se atrapa por su tipo en 46
    lugares— la traducen ellos.
    """


def _nombre() -> str:
    return os.getenv(VARIABLE, ZONA_DEFAULT).strip() or ZONA_DEFAULT


def zona() -> ZoneInfo:
    """La zona del negocio. LEVANTA `ZonaInvalida` si no se puede resolver.

    Para lo que decide: una zona ilegible tiene que frenar la decisión, no
    tomarla contra un día adivinado.
    """
    nombre = _nombre()
    try:
        return ZoneInfo(nombre)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ZonaInvalida(f"{VARIABLE} inválida") from exc


def zona_con_respaldo(quien: str) -> ZoneInfo:
    """La zona del negocio, o `ZONA_DEFAULT` con un log. NUNCA levanta.

    Para lo que informa, avisa o sella: un barrido que se cae deja de avisarle
    al cliente para siempre, y eso es peor que el default. `quien` es el
    prefijo del log, para que se sepa qué módulo se conformó con el respaldo.

    El respaldo es el default CONFIGURADO, no `datetime.now()` sin zona: tres
    de las copias hacían eso y sellaban registros durables con el reloj del
    servidor sin decirlo.
    """
    nombre = _nombre()
    try:
        return ZoneInfo(nombre)
    except (ZoneInfoNotFoundError, ValueError):
        print(f"[{quien}] {VARIABLE}={nombre!r} inválida; uso {ZONA_DEFAULT}")
        return ZoneInfo(ZONA_DEFAULT)


def ahora() -> datetime:
    """Ahora, con zona. Levanta `ZonaInvalida`."""
    return datetime.now(zona())


def ahora_con_respaldo(quien: str) -> datetime:
    """Ahora, con zona. Nunca levanta; ver `zona_con_respaldo`."""
    return datetime.now(zona_con_respaldo(quien))


def hoy() -> date:
    """El día del negocio. Levanta `ZonaInvalida`.

    No es `date.today()`: a las 22:30 de Buenos Aires el servidor en UTC ya
    está en el día siguiente, y la fecha de un pedido saldría corrida.
    """
    return ahora().date()


def de_erpnext(sello: object, *, en: ZoneInfo | None = None) -> datetime | None:
    """Un sello de ERPNext como momento con zona, o None si no se entiende.

    ERPNext guarda `creation`, `posting_date` y `posting_time` **sin zona**, en
    la hora de su propio sistema (`System Settings.time_zone`, que el asistente
    de instalación pone según el país). Acá se interpretan en la zona del
    negocio, que es el único reloj con el que decide el resto del código.

    Son DOS zonas configuradas por separado y supuestas iguales.
    `readiness.chequear_zona_erpnext` las compara y BLOQUEA el despliegue si
    difieren, porque desde acá no hay forma de notarlo: toda edad saldría
    corrida por el offset, en silencio, y ningún test lo vería porque los tests
    comparten la suposición del código.

    None NO es 0 y no es «ahora»: un sello ilegible tiene que dejar el
    documento en paz, no tratarlo como recién creado ni como vencido hace un
    mes. Los tres llamadores ya dependían de eso.

    `en` es para el llamador que ya tiene la zona resuelta y no quiere volver a
    leer el entorno por fila; sin él usa el respaldo, porque un sello que no se
    puede fechar ya devuelve None y morirse acá no agrega nada.
    """
    crudo = str(sello or "").strip()
    if not crudo:
        return None
    try:
        momento = datetime.fromisoformat(crudo)
    except (TypeError, ValueError):
        return None
    if momento.tzinfo is not None:
        # Ya viene con zona: se respeta. Un ERPNext configurado para devolver
        # sellos con offset no se re-interpreta.
        return momento
    return momento.replace(tzinfo=en or zona_con_respaldo("reloj"))
