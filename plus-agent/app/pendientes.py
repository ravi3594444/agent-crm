"""El pedido que espera a una persona: plazo, recordatorio y cierre.

QUÉ PROBLEMA RESUELVE
Un pedido que no se auto-confirma queda como borrador con un comentario
«Requiere revisión humana: …» y nada más. Hasta acá eso significaba dos cosas,
las dos malas:

  * al cliente se le dijo «el equipo te confirma en un rato» y podía esperar
    para siempre. Una solicitud de decisión (`app/solicitudes.py`) tiene plazo,
    aviso y oferta de respaldo; un borrador común no tenía ninguna de las tres,
    y el único recordatorio era el resumen de las 18:00;
  * el borrador retiene el stock que prometió, y NADA lo suelta nunca.
    `policy._sin_reservas_vencidas` sólo descarta los borradores cuyo plazo
    venció, y ese plazo lo lee de los comentarios `[solicitud]`. Un borrador
    común no aparece ahí, así que compite con los pedidos vivos para siempre.
    Peor: pasado el techo de borradores que `policy` puede revisar, la
    verificación de stock LEVANTA y no se auto-confirma nada, de ningún
    producto.

DOS CONSUMIDORES, UNA CONSULTA
`borradores_esperando(solo_del_agente=...)`. El resumen del dueño quiere ver
TODOS los borradores que esperan —los que cargó una persona a mano en ERPNext
retienen stock exactamente igual, porque `policy._borradores_que_reservan`
filtra por `docstatus` y `status` y no por origen— y son justamente los que él
tiene que ir a limpiar. El recordatorio y el cierre, en cambio, sólo tocan los
del agente: el sistema no le escribe a un cliente por un pedido que no tomó, y
cerrar el borrador de una persona no es decisión suya.

El origen se decide por la FORMA de `po_no`, no por «tiene algo escrito»:
`tools/pedidos.py::_message_key` escribe `WA-` + 40 hex. Un número de orden de
compra real («OC-4471») no puede confundirse con un pedido del agente, y sin
esa forma exacta el pedido queda del lado de «lo cargó una persona», que es la
dirección en la que el cierre no toca nada.

LAS HORAS DE SILENCIO FRENAN ESTO, NO LA COLA
El recordatorio no se encola de noche: no se pospone un mensaje ya armado, se
posterga la DECISIÓN de mandarlo. La diferencia importa — si el dueño confirma
el pedido a las 23:00, un aviso que ya estuviera en la cola le diría al cliente
a las 07:00 que su pedido sigue sin confirmar. Acá el pedido simplemente sigue
siendo candidato y la ronda de las 07:00 vuelve a leer ERPNext y lo saltea. La
cola (`app/avisos.py`) entrega datos y no vuelve a decidir nada, y así tiene
que seguir: la confirmación de un pedido sale a las 22:10 si el pedido se
confirmó a las 22:10, porque el cliente la está esperando.

CÓMO CORRE
Desde el barrido que ya existe (`main._solicitudes_scheduler`, cada 60 s), en
su propio try/except al lado de `solicitudes.tick()`: un fallo acá no puede
saltear el vencimiento de una solicitud, y al revés.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import erpnext, idioma

# `tools/pedidos.py::_message_key` = "WA-" + sha256(message_id).hexdigest()[:40].
# La forma EXACTA, porque el cierre es destructivo: un `po_no` que una persona
# tecleó no puede parecerse a esto ni por accidente.
_RE_ORIGEN_AGENTE = re.compile(r"^WA-[0-9a-f]{40}$")

# Cuántos borradores se miran por ronda. ERPNext pagina y el barrido vuelve en
# 60 s; pedir "todos" es cómo una cola vieja hace un request de 414.
MAX_CANDIDATOS = 100
# Cuántos avisos y cuántos cierres como máximo por ronda. Cada uno son varias
# llamadas a ERPNext, y el barrido comparte su hilo con los vencimientos.
POR_RONDA = 10

MARCA_AVISO = "[pendiente-aviso]"
MARCA_CIERRE = "[pendiente-cerrado]"

_ZONA_DEFAULT = "America/Argentina/Buenos_Aires"
_NOCHE_DESDE_DEFAULT = "22:00"
_NOCHE_HASTA_DEFAULT = "07:00"

_RE_HORA = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


# --------------------------------------------------------------- el reloj


def _zona() -> ZoneInfo:
    nombre = os.getenv("BUSINESS_TIMEZONE", _ZONA_DEFAULT).strip() or _ZONA_DEFAULT
    try:
        return ZoneInfo(nombre)
    except (ZoneInfoNotFoundError, ValueError):
        print(f"[pendientes] BUSINESS_TIMEZONE={nombre!r} inválida; uso {_ZONA_DEFAULT}")
        return ZoneInfo(_ZONA_DEFAULT)


def _ahora() -> datetime:
    return datetime.now(_zona())


def edad_horas(fila: dict, ahora: datetime | None = None) -> float | None:
    """Horas que lleva esperando el borrador, o None si no se puede saber.

    None no es 0: un `creation` ilegible tiene que dejar el pedido en paz, no
    tratarlo como recién creado ni como vencido hace un mes.
    """
    crudo = str(fila.get("creation") or "").strip()
    if not crudo:
        return None
    try:
        creado = datetime.fromisoformat(crudo)
    except (TypeError, ValueError):
        return None
    zona = _zona()
    if creado.tzinfo is None:
        # ERPNext guarda sin zona, en la hora de su propio sistema; la del
        # negocio es la que usa todo el resto del código para decidir.
        creado = creado.replace(tzinfo=zona)
    momento = ahora or _ahora()
    return (momento - creado).total_seconds() / 3600.0


def _hora_limite(nombre: str, respaldo: str) -> tuple[int, int]:
    """(hora, minuto) de un límite HORA del dueño, con respaldo si no se lee.

    Ni silencio ni un mensaje a las 3 de la mañana: si los límites no se pueden
    leer se usa la ventana de fábrica y se dice en el log. Un límite ilegible ya
    frena toda la auto-confirmación (`policy.evaluar`), así que el sistema tiene
    un problema más grande que la hora de este recordatorio.
    """
    from app import limites

    try:
        crudo = limites.vigente(nombre).strip()
    except Exception as exc:
        print(f"[pendientes] {nombre} ilegible ({type(exc).__name__}); uso {respaldo}")
        crudo = respaldo
    encontrado = _RE_HORA.match(crudo or respaldo)
    if not encontrado:
        print(f"[pendientes] {nombre}={crudo!r} inválida; uso {respaldo}")
        encontrado = _RE_HORA.match(respaldo)
    return int(encontrado.group(1)), int(encontrado.group(2))


def en_silencio(ahora: datetime | None = None) -> bool:
    """¿Estamos en las horas en que no se le escribe a un cliente?

    La ventana cruza la medianoche, así que no alcanza una comparación como la
    de `digest.tick`: 22:00–07:00 es "después de las 22" O "antes de las 7".
    Una ventana con las dos horas iguales no silencia nada.
    """
    desde = _hora_limite("PENDIENTE_NOCHE_DESDE", _NOCHE_DESDE_DEFAULT)
    hasta = _hora_limite("PENDIENTE_NOCHE_HASTA", _NOCHE_HASTA_DEFAULT)
    if desde == hasta:
        return False
    momento = ahora or _ahora()
    actual = (momento.hour, momento.minute)
    if desde < hasta:
        return desde <= actual < hasta
    return actual >= desde or actual < hasta


def _horas(nombre: str, respaldo: float | None) -> float | None:
    """Un límite en horas del dueño. None = NINGUNO (apagado) o ilegible."""
    from app import limites

    try:
        crudo = limites.vigente(nombre).strip()
    except Exception as exc:
        print(f"[pendientes] {nombre} ilegible ({type(exc).__name__})")
        return respaldo
    if not crudo or crudo == limites.NINGUNO:
        return None
    try:
        valor = float(crudo)
    except (TypeError, ValueError):
        print(f"[pendientes] {nombre}={crudo!r} no es un número de horas")
        return respaldo
    return valor if valor > 0 else None


# ----------------------------------------------------------- los candidatos


def del_agente(fila: dict) -> bool:
    """¿Este borrador lo creó el agente? Falla cerrado: sin la forma, no."""
    return bool(_RE_ORIGEN_AGENTE.match(str(fila.get("po_no") or "").strip()))


def listar_esperando(
    *, limite: int = MAX_CANDIDATOS, solo_del_agente: bool = False
) -> list[dict]:
    """Borradores que todavía espera decidir una persona. LEVANTA si no se puede.

    `solo_del_agente=True` deja únicamente los que creó el agente. Sin eso
    vienen también los que cargó una persona a mano, que es lo que el resumen
    del dueño tiene que mostrarle: retienen stock igual —
    `policy._borradores_que_reservan` no filtra por origen— y son suyos para
    limpiar.

    No se filtra por `[confirmado-por-agente]` ni por `[solicitud]` en ERPNext
    —no se puede pedir "un comentario que NO existe" en un filtro de Frappe—.
    `docstatus=0` ya descarta lo confirmado, el filtro de `status` descarta lo
    que alguien rechazó o cerró, y las que tienen una solicitud abierta se
    excluyen con `_sin_solicitud_abierta`, que lo pregunta en UNA lectura.

    Levanta a propósito: el resumen del dueño tiene que poder decir «no pude
    leer ERPNext» en vez de «ninguno», y una lista vacía no distingue las dos.
    El barrido usa `borradores_esperando`, que se lo traga.
    """
    from app import policy

    filas = erpnext.policy_get_list(
        "Sales Order",
        filters=[
            ["docstatus", "=", 0],
            ["status", "not in", list(policy.ESTADOS_SIN_RESERVA)],
        ],
        fields=[
            "name",
            "customer",
            "customer_name",
            "grand_total",
            "currency",
            "delivery_date",
            "creation",
            "po_no",
            "status",
        ],
        limit=max(1, int(limite or MAX_CANDIDATOS)),
        order_by="creation asc",
    )
    vivos = [f for f in filas if str(f.get("name") or "").strip()]
    if solo_del_agente:
        return [f for f in vivos if del_agente(f)]
    return vivos


def borradores_esperando(
    *, limite: int = MAX_CANDIDATOS, solo_del_agente: bool = False
) -> list[dict]:
    """Lo mismo, pero [] cuando ERPNext no contesta — para el barrido.

    Ese [] NO es «no hay ninguno»: es «no sé», y lo único que el barrido puede
    hacer con eso es «esta ronda no hice nada». Nadie puede convertirlo en un
    número para el dueño; para eso está `listar_esperando`, que levanta.
    """
    try:
        return listar_esperando(limite=limite, solo_del_agente=solo_del_agente)
    except Exception as exc:  # el barrido nunca muere por una ronda
        print(f"[pendientes] no pude listar los borradores: {type(exc).__name__}: {exc}")
        return []


def _sin_solicitud_abierta(pedidos: list[str]) -> list[str]:
    """Los que NO tienen una solicitud de decisión con plazo vivo.

    Los que la tienen ya están cubiertos: `app/solicitudes.py` les puso plazo,
    les avisa al cliente y les ofrece un respaldo cuando vencen. Anotarlos,
    empujarlos o cerrarlos desde acá sería el segundo aviso por el mismo pedido.

    Si la lectura falla se devuelven TODOS para la sombra —un registro de más no
    le hace nada a nadie— pero el aviso y el cierre vuelven a preguntar antes de
    tocar algo, porque ahí un error sí se le nota al cliente.
    """
    if not pedidos:
        return []
    from app import solicitudes

    try:
        con_plazo = solicitudes.vencimientos(pedidos)
    except Exception as exc:
        print(f"[pendientes] no pude ver qué borradores ya tienen plazo: {type(exc).__name__}")
        return list(pedidos)
    return [p for p in pedidos if p not in con_plazo]


def _tiene_marca(pedido: str, marca: str) -> bool | None:
    """True/False si se pudo averiguar, None si ERPNext no contestó.

    Los tres estados son distintos: aplastar el None en False manda el segundo
    recordatorio al cliente cada vez que ERPNext tose.
    """
    try:
        filas = erpnext.policy_get_list(
            "Comment",
            filters=[
                ["reference_doctype", "=", "Sales Order"],
                ["reference_name", "=", pedido],
                ["content", "like", f"%{marca}%"],
            ],
            fields=["name"],
            limit=1,
        )
    except Exception as exc:
        print(f"[pendientes] {pedido}: no pude ver la marca {marca}: {type(exc).__name__}")
        return None
    return bool(filas)


def _sigue_esperando(pedido: str) -> dict | None:
    """El documento, sólo si SIGUE siendo un borrador que espera. None si no.

    Se relee justo antes de tocar algo: entre el listado y ahora el dueño pudo
    confirmarlo, rechazarlo o cerrarlo, y decirle al cliente que su pedido no
    está confirmado cuando acaba de confirmarse es peor que el silencio.
    """
    from app import policy

    try:
        doc = erpnext.policy_get_doc("Sales Order", pedido)
    except Exception as exc:
        print(f"[pendientes] {pedido}: relectura falló ({type(exc).__name__})")
        return None
    if not isinstance(doc, dict):
        return None
    try:
        if int(doc.get("docstatus") or 0) != 0:
            return None
    except (TypeError, ValueError):
        return None
    if policy.sin_reserva(doc.get("status")):
        return None
    return doc


# --------------------------------------------------------------- la sombra


def _anotar_sombras(filas: list[dict]) -> int:
    """W1: el registro de lo que las reglas habrían dicho. No le habla a nadie."""
    from app import sombra

    if not sombra.encendido():
        return 0
    anotados = 0
    for fila in filas:
        nombre = str(fila["name"])
        if anotados >= sombra.POR_RONDA:
            break
        try:
            if sombra.ya_anotado(nombre) is not False:
                # True = ya tiene registro. None = ERPNext no contestó, y
                # anotar a ciegas duplicaría el registro del pedido.
                continue
            completo = _sigue_esperando(nombre)
            if completo is None:
                continue
            if sombra.anotar(nombre, completo):
                anotados += 1
        except Exception as exc:
            print(f"[pendientes] {nombre}: la sombra falló: {type(exc).__name__}: {exc}")
    return anotados


# ---------------------------------------------------------- el recordatorio


def _avisar(filas: list[dict], momento: datetime) -> int:
    """Un recordatorio por pedido, una vez, y un resumen al dueño por día."""
    from app import avisos, decisiones

    # None = NINGUNO (nadie lo armó) o ilegible. Las dos veces, silencio: un
    # recordatorio que el dueño no encendió no puede salirle a un cliente.
    horas = _horas("PENDIENTE_AVISO_HORAS", None)
    if horas is None:
        return 0

    vencidos: list[tuple[dict, float]] = []
    for fila in filas:
        edad = edad_horas(fila, momento)
        if edad is not None and edad >= horas:
            vencidos.append((fila, edad))
    if not vencidos:
        return 0

    avisados = 0
    for fila, _edad in vencidos:
        pedido = str(fila["name"])
        if avisados >= POR_RONDA:
            break
        try:
            if _tiene_marca(pedido, MARCA_AVISO) is not False:
                continue
            if _sigue_esperando(pedido) is None:
                continue
            telefono = decisiones.telefono_del_cliente(pedido)
            if not telefono:
                erpnext.add_comment(
                    "Sales Order",
                    pedido,
                    "Recordatorio de pendiente no enviado: el cliente no tiene "
                    "teléfono cargado. Requiere contacto manual.",
                )
                continue
            texto = recordatorio_pendiente(pedido, idioma.para_destinatario(telefono))
            # La cola garantiza (evento, pedido) una sola vez por 30 días, pero
            # un FLUSHALL borra esa clave: la marca durable en ERPNext es la que
            # de verdad impide el segundo aviso. Se encola primero (es atómico)
            # y se marca después; una caída entre las dos, MÁS un flush, puede
            # costar un aviso repetido, que es el error barato de los dos.
            #
            # Un False no es un fallo: es "ya está encolado" o "Meta ya lo
            # aceptó", y las dos cosas significan que el aviso existe, así que
            # la marca va igual. Si no fuera así el barrido volvería a preguntar
            # por este pedido cada 60 s para siempre. Sólo un encolar que LEVANTA
            # deja el pedido sin marca, y ahí sí hay que reintentar.
            nuevo = avisos.encolar("pendiente_aviso", pedido, telefono, texto)
            erpnext.add_comment(
                "Sales Order", pedido, f"{MARCA_AVISO} {_sello(momento)}"
            )
            if nuevo:
                avisados += 1
        except Exception as exc:
            print(f"[pendientes] {pedido}: recordatorio falló: {type(exc).__name__}: {exc}")

    _recordar_al_dueno(vencidos, momento)
    return avisados


def _recordar_al_dueno(vencidos: list[tuple[dict, float]], momento: datetime) -> None:
    """Un resumen al dueño, una vez por día, no una vez por ronda de 60 s."""
    from app import notificar
    from app.outbound_status import claim_once, release_claim

    dia = momento.date().isoformat()
    clave = f"pendientes-dueno:{dia}"
    try:
        if not claim_once(clave, 24 * 60 * 60):
            return
    except Exception as exc:
        print(f"[pendientes] no pude reclamar el recordatorio del día ({type(exc).__name__})")
        return
    lengua = idioma.gerencia()
    lineas = "\n".join(
        f"· {f.get('name')} — {f.get('customer_name') or f.get('customer')} "
        f"— {edad:.0f} h"
        for f, edad in vencidos[:15]
    )
    asunto, cuerpo = recordatorio_dueno(len(vencidos), lineas, lengua)
    # La reclamación se DEVUELVE si el aviso no salió. Quedársela igual gastaba
    # el día entero por un fallo transitorio: `avisar_dueno` no levanta, avisa
    # con un False (sin destinatario, o Meta que rechaza sin plantilla), y el
    # dueño se quedaba sin el recordatorio hasta mañana con borradores vivos.
    entregado = False
    try:
        entregado = bool(
            notificar.avisar_dueno(
                asunto, cuerpo, plantilla_env="WHATSAPP_STAFF_ALERT_TEMPLATE"
            )
        )
    except Exception as exc:
        print(f"[pendientes] recordatorio al dueño falló ({type(exc).__name__})")
    if not entregado:
        try:
            release_claim(clave)
        except Exception as exc:
            print(f"[pendientes] no pude devolver la reclamación ({type(exc).__name__})")


# --------------------------------------------------------------- el cierre


def _cerrar(filas: list[dict], momento: datetime) -> int:
    """Cerrar el borrador que nadie miró, para que suelte el stock.

    Es lo único de este módulo que cambia el documento, y por eso es lo único
    que corre bajo el lock del pedido, relee adentro, y no escribe nada
    terminal que no haya podido probar.
    """
    from app import avisos, decisiones, solicitudes
    from app.locks import CoordinationError, distributed_lock

    horas = _horas("PENDIENTE_CIERRE_HORAS", None)
    if horas is None:
        return 0  # NINGUNO: no se cierra nada, como hoy

    cerrados = 0
    for fila in filas:
        pedido = str(fila["name"])
        if cerrados >= POR_RONDA:
            break
        edad = edad_horas(fila, momento)
        if edad is None or edad < horas:
            continue
        try:
            if _tiene_marca(pedido, MARCA_CIERRE) is not False:
                continue
            with distributed_lock(f"pendiente:{pedido}", lease_seconds=60, wait_seconds=5):
                if _sigue_esperando(pedido) is None:
                    # Una persona lo decidió. No se le dice nada al cliente.
                    continue
                liberado, detalle = solicitudes.soltar_reserva(pedido)
                if not liberado:
                    # Sin prueba de que soltó el stock no se escribe un estado
                    # terminal: la próxima ronda vuelve a preguntar.
                    print(f"[pendientes] {pedido}: no pude cerrarlo ({detalle})")
                    continue
                try:
                    erpnext.add_comment(
                        "Sales Order",
                        pedido,
                        f"{MARCA_CIERRE} {_sello(momento)} sin decisión en "
                        f"{horas:g} h; {detalle}",
                    )
                except Exception as exc:
                    # Su propio try: el cierre YA pasó y la próxima ronda no lo
                    # vuelve a mirar (`_sigue_esperando` lo ve cerrado), así que
                    # si este fallo cayera en el except de afuera se saltearían
                    # los dos avisos y nadie se enteraría nunca del cierre.
                    print(
                        f"[pendientes] {pedido}: cerrado, pero la marca no quedó "
                        f"({type(exc).__name__})"
                    )
        except CoordinationError:
            continue  # ronda salteada, nunca un cambio a medias
        except Exception as exc:
            print(f"[pendientes] {pedido}: cierre falló: {type(exc).__name__}: {exc}")
            continue

        cerrados += 1
        # Los avisos salen FUERA del lock, por la cola durable: el teléfono de
        # una persona no puede estar en el camino crítico de un cambio de estado.
        try:
            telefono = decisiones.telefono_del_cliente(pedido)
            if telefono:
                avisos.encolar(
                    "pendiente_cerrado",
                    pedido,
                    telefono,
                    pendiente_cerrado(pedido, idioma.para_destinatario(telefono)),
                )
        except Exception as exc:
            print(f"[pendientes] {pedido}: aviso de cierre no encolado ({type(exc).__name__})")
        try:
            avisos.encolar_equipo(
                "pendiente_cerrado_equipo",
                pedido,
                pendiente_cerrado_equipo(pedido, horas, idioma.gerencia()),
            )
        except Exception as exc:
            print(f"[pendientes] {pedido}: aviso de cierre al equipo falló ({type(exc).__name__})")
    return cerrados


def _sello(momento: datetime) -> str:
    return momento.isoformat(timespec="seconds")


# ------------------------------------------------------------------ textos
# Constructores puros: sin reloj y sin contar nada por su cuenta, así que dos
# llamadas con los mismos datos dan el mismo texto. Es lo que exige la
# auditoría de idiomas (tests/test_idioma_cobertura.py los llama una vez por
# idioma y compara), y por eso la EDAD del pedido no entra en el mensaje al
# cliente: no la necesita, y la haría no determinista.


def recordatorio_pendiente(pedido: str, lengua: str | None = None) -> str:
    """Al cliente: todavía no se pudo confirmar. Sin prometer día ni hora."""
    return idioma.t("pedido.recordatorio", lengua, pedido=pedido)


def pendiente_cerrado(pedido: str, lengua: str | None = None) -> str:
    """Al cliente: no se confirmó y no queda nada a su nombre."""
    return idioma.t("pedido.cerrado_sin_confirmar", lengua, pedido=pedido)


def recordatorio_dueno(
    cuantos: int, lineas: str, lengua: str | None = None
) -> tuple[str, str]:
    """(asunto, cuerpo) del resumen al dueño. El asunto es la clave del ToDo."""
    return (
        idioma.t("gerencia.pendientes_asunto", lengua, cuantos=cuantos),
        idioma.t("gerencia.pendientes_cuerpo", lengua, cuantos=cuantos, lineas=lineas),
    )


def pendiente_cerrado_equipo(
    pedido: str, horas: float, lengua: str | None = None
) -> str:
    """Al equipo: se cerró este pedido y por qué."""
    return idioma.t(
        "gerencia.pendiente_cerrado", lengua, pedido=pedido, horas=f"{horas:g}"
    )


# ---------------------------------------------------------------- el barrido


def tick(ahora: float | None = None) -> int:
    """Una ronda. Devuelve cuántas cosas se hicieron (sombras + avisos + cierres).

    Nunca levanta: corre en un hilo de fondo y un hipo de Redis o de ERPNext no
    puede parar el bucle que va a volver a intentar en un minuto.
    """
    momento = (
        datetime.fromtimestamp(ahora, tz=_zona()) if ahora is not None else _ahora()
    )

    filas = borradores_esperando(solo_del_agente=True)
    if not filas:
        return 0
    elegibles = set(_sin_solicitud_abierta([str(f["name"]) for f in filas]))
    candidatos = [f for f in filas if str(f["name"]) in elegibles]
    if not candidatos:
        return 0

    hechos = 0
    # La sombra es sólo un registro: corre siempre, también de noche.
    try:
        hechos += _anotar_sombras(candidatos)
    except Exception as exc:
        print(f"[pendientes] la ronda de sombra falló: {type(exc).__name__}: {exc}")

    # Lo que le habla a una persona respeta las horas de silencio. El cierre
    # también: suelta stock, pero le dice al cliente que su pedido no se
    # confirmó, y eso no son las 3 de la mañana. De noche no entran pedidos,
    # así que el stock retenido nueve horas más no le cuesta una venta a nadie.
    if en_silencio(momento):
        return hechos

    try:
        hechos += _cerrar(candidatos, momento)
    except Exception as exc:
        print(f"[pendientes] la ronda de cierre falló: {type(exc).__name__}: {exc}")
    try:
        hechos += _avisar(candidatos, momento)
    except Exception as exc:
        print(f"[pendientes] la ronda de avisos falló: {type(exc).__name__}: {exc}")
    return hechos

