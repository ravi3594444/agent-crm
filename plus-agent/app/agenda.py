"""La agenda: UNA lista durable de cosas que vencen más tarde.

Por qué existe
--------------
`solicitudes.py::tick`, `pendientes.py::tick` y `digest.py::tick` re-implementan
los mismos cinco conceptos: un registro durable de «esto vence en T», un barrido
que encuentra lo vencido, exactamente-una-vez, horas de silencio, re-leer antes
de actuar y fallar cerrado con un valor ilegible. Plazos habría sido el cuarto.
Escrito una vez, «avisarle al cliente antes de la entrega», «cerrar un borrador
abandonado», «perseguir al dueño» y lo que no está pensado todavía son FILAS, no
módulos. Ver el hallazgo 5 de `docs/AUDITORIA.md` y `docs/prompt-agenda.md`.

No es arquitectura nueva: es el patrón de `solicitudes.py` extraído. Registro
durable como comentario append-only en el documento, índice en Redis que se
reconstruye desde ERPNext cuando está vacío, y por la misma razón que ese módulo
lo documenta: Redis es un caché y no puede ser la fuente de verdad de algo que
se le prometió a un cliente.

La fila
-------
`id` · `sobre` (el pedido) · `tipo` (de una LISTA BLANCA) · `vence` (epoch UTC) ·
`estado` (pendiente/hecho/cancelado) · `params` (JSON chico: qué clave de texto,
qué monto; nada interpretativo). Cada evento lleva la foto ENTERA, así que leer
es «el evento más nuevo que parsea gana», nunca un fold sobre la historia.

`sobre` es SIEMPRE el nombre de un Sales Order, y no a veces un teléfono
-----------------------------------------------------------------------
Una fila del registro de `app/marcas.py` tiene UN doctype: `escribir`/`filas`
usan `m.doctype` y no hay override por llamada, así que una fila colgada de un
teléfono no tendría documento donde colgar el comentario. Y `avisos.encolar` le
pasa `pedido` a ERPNext COMO nombre de Sales Order (`avisos.py:319`), así que un
`sobre` que no lo fuera dejaría un comentario contra un documento inexistente y
un ToDo apuntando a la nada. Si algún día hace falta una fila por cliente, es
una segunda fila del registro con su propio doctype, no un `if` acá.

Los cinco conceptos compartidos viven ACÁ y sólo acá
----------------------------------------------------
* exactamente-una-vez: `avisos.encolar` con clave `(tipo, sobre)` —el id de la
  fila entra en el evento cuando puede haber más de una fila del mismo tipo
  sobre el mismo pedido, porque `encolar` deduplica por `(evento, pedido)`
  durante 30 días y la segunda se perdería en silencio.
* horas de silencio 22:00–07:00: se POSTERGA, nunca se saltea.
* re-leer antes de actuar: un pedido confirmado hace treinta segundos no se
  toca.
* fallar cerrado: una fila, un documento o un límite ilegible es NO HACER NADA y
  dejarlo anotado; nunca adivinar.
* un lote acotado por tick, para que un atraso no pueda trabar el barrido.

Un handler es entonces ~10 líneas: arma un mensaje con datos y vuelve. NINGÚN
handler toca Redis, ni el reloj, ni la idempotencia.

Las horas de silencio se DELEGAN, no se copian
-----------------------------------------------
`pendientes.en_silencio` ya es la única implementación de la ventana que cruza
la medianoche, con los límites del dueño (`PENDIENTE_NOCHE_DESDE`/`HASTA`)
detrás. Copiarla acá sería escribirla por SEXTA vez, que es exactamente lo que
este módulo existe para terminar. Así que el concepto «lo que le habla a una
persona espera a la mañana» vive acá —es este barrido el que lo aplica a todas
las filas— y el cálculo de si son las horas de silencio sigue viviendo donde ya
estaba, en un solo lugar.

Lo que una fila NO puede hacer
------------------------------
Una fila causa un MENSAJE. Nunca una confirmación, un submit, una cancelación ni
un movimiento de plata. `cierre_borrador` es la excepción heredada y sigue
estando acotada por lo que ya hacía: cierra un borrador que el dueño mandó
cerrar con su límite, y sólo después de que `solicitudes.soltar_reserva` PRUEBE
que soltó el stock.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app import erpnext, marcas, reloj
from app.outbound_status import cliente as _redis
from app.outbound_status import digest_recipiente

# El texto durable sale del registro, nunca de un literal acá: una fila con
# `texto="[a]"` y un parser armado sobre `"[b]"` es el bug que `app/marcas.py`
# existe para hacer imposible.
MARCA = marcas.texto("agenda")
MAX_EVENTOS = marcas.marca("agenda").techo
VERSION = 1

# --------------------------------------------------------------- los tipos
#
# LISTA BLANCA. El modelo propone y Python valida: `recordar` fuerza
# `SEGUIMIENTO` y no puede elegir ninguno de los otros. Agregar un
# comportamiento es agregar una constante acá, un handler en `_HANDLERS` y dos
# tests — no un módulo.
CIERRE_BORRADOR = "cierre_borrador"
AVISO_ANTES_DE_ENTREGA = "aviso_antes_de_entrega"
RECORDATORIO_PLAZO_DUENO = "recordatorio_plazo_dueno"
SEGUIMIENTO = "seguimiento"

TIPOS = (CIERRE_BORRADOR, AVISO_ANTES_DE_ENTREGA, RECORDATORIO_PLAZO_DUENO, SEGUIMIENTO)

# El único tipo que el modelo puede pedir. Todo lo demás lo decide Python.
TIPO_DEL_MODELO = SEGUIMIENTO

PENDIENTE = "pendiente"
HECHO = "hecho"
CANCELADO = "cancelado"

# Las filas que el barrido tiene que mirar. Igual que `solicitudes.CON_PLAZO`:
# un estado terminal es, por definición, una fila sin plazo.
CON_PLAZO = frozenset({PENDIENTE})

# El lock bajo el que corre cada tipo. `cierre_borrador` conserva el nombre que
# ya usaba `pendientes._cerrar` —`pendiente:{pedido}`— a propósito: el lock es
# lo que impide que el barrido viejo y el nuevo cierren el mismo borrador dos
# veces si alguna vez conviven, y además es lo que sus tests afirman.
_LOCKS = {
    CIERRE_BORRADOR: "pendiente:{sobre}",
    AVISO_ANTES_DE_ENTREGA: "agenda:{sobre}",
    RECORDATORIO_PLAZO_DUENO: "agenda:{sobre}",
    SEGUIMIENTO: "agenda:{sobre}",
}

# Los tipos que le hablan a una PERSONA y por lo tanto esperan a la mañana.
# `cierre_borrador` está adentro porque le dice al cliente que su pedido no se
# confirmó, y eso no son las 3 de la mañana.
#
# `recordatorio_plazo_dueno` también, y la decisión no es obvia: el aviso existe
# para que el dueño llegue a contestar ANTES del plazo, así que postergarlo
# puede hacerlo llegar tarde. Entra igual, por dos razones. Una entrega a las
# 08:00 con tres horas de aviso lo despertaría a las 04:00, y un WhatsApp a esa
# hora no se contesta: se apaga. Y el aviso al CLIENTE de ese mismo plazo ya
# está postergado hasta las 07:00, así que un aviso al dueño de madrugada no
# compra el plazo — sólo lo despierta. El que sale a las 07:00 todavía sirve
# para una entrega del día.
_HABLAN_CON_ALGUIEN = frozenset(
    {CIERRE_BORRADOR, AVISO_ANTES_DE_ENTREGA, RECORDATORIO_PLAZO_DUENO, SEGUIMIENTO}
)

# Cuántas filas se despachan por tick. Acotado para que un atraso no trabe el
# barrido: lo que no entra en esta ronda entra en la siguiente, sesenta segundos
# después, y el índice está ordenado por vencimiento así que lo más vencido sale
# primero.
POR_RONDA = 25

# Reconstrucción: las mismas dos variables separadas que `solicitudes`, por el
# mismo motivo escrito allá — una sola conflaba «quedó incompleta» con «cuándo
# volver a intentar» y salían 25 lecturas de página por minuto para siempre.
MAX_RECONSTRUCCION = 200
MAX_PAGINAS_RECONSTRUCCION = 25
REINTENTO_RECONSTRUCCION_SEGUNDOS = 600.0

CLAVE_INDICE = "wa:{inbound}:agenda"
CACHE_TTL_SEGUNDOS = 30 * 24 * 60 * 60


# ----------------------------------------------------------------- el reloj


def _zona() -> ZoneInfo:
    """Con respaldo y log: un barrido que se cae por una zona mal escrita deja
    de avisarle al cliente PARA SIEMPRE, que es peor que el default."""
    return reloj.zona_con_respaldo("agenda")


def _ahora() -> float:
    return time.time()


def _reloj() -> float:
    """Monotónico, y a propósito distinto de `_ahora`: el enfriamiento de la
    reconstrucción no es una fecha del negocio, es un «no vuelvas a preguntar
    en diez minutos», y un salto del reloj de pared no tiene que moverlo."""
    return time.monotonic()


def _momento(ahora: float) -> datetime:
    return datetime.fromtimestamp(ahora, tz=_zona())


# ------------------------------------------------------------------ la fila


@dataclass(frozen=True)
class Fila:
    """Una cosa que vence más tarde. Chica y aburrida, a propósito."""

    id: str
    sobre: str
    tipo: str
    vence: float
    estado: str = PENDIENTE
    params: dict = field(default_factory=dict)
    evento: str = "creada"
    sello: float = 0.0

    @property
    def con_plazo(self) -> bool:
        return self.estado in CON_PLAZO

    def vencida(self, ahora: float | None = None) -> bool:
        return self.vence <= (ahora if ahora is not None else _ahora())

    def como_dict(self) -> dict:
        """La forma durable EXACTA. `v` primero, como el resto del repo."""
        return {
            "v": VERSION,
            "id": self.id,
            "sobre": self.sobre,
            "tipo": self.tipo,
            "vence": round(float(self.vence), 3),
            "estado": self.estado,
            "params": dict(self.params or {}),
            "evento": self.evento,
            "sello": round(float(self.sello or 0.0), 3),
        }


def _desde_dict(datos: object) -> Fila | None:
    """Coerción total. Una fila ilegible se SALTEA, nunca se adivina."""
    if not isinstance(datos, dict):
        return None
    try:
        identificador = str(datos["id"]).strip()
        sobre = str(datos["sobre"]).strip()
        tipo = str(datos["tipo"]).strip()
        if not identificador or not sobre or tipo not in TIPOS:
            # Un tipo que no está en la lista blanca es una fila que este código
            # no sabe despachar. Se descarta acá y no en el handler: así una
            # versión vieja (o un comentario escrito a mano) no puede hacer que
            # el barrido llame a cualquier cosa.
            return None
        estado = str(datos.get("estado") or PENDIENTE).strip()
        if estado not in (PENDIENTE, HECHO, CANCELADO):
            return None
        parametros = datos.get("params") or {}
        if not isinstance(parametros, dict):
            return None
        return Fila(
            id=identificador,
            sobre=sobre,
            tipo=tipo,
            vence=float(datos.get("vence") or 0.0),
            estado=estado,
            params=dict(parametros),
            evento=str(datos.get("evento") or ""),
            sello=float(datos.get("sello") or 0.0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parsear(contenido: object) -> object:
    """El ÚNICO camino de parseo, derivado de la fila del registro."""
    parser = marcas.marca("agenda").parser
    if parser is None:  # pragma: no cover - la fila declara parseo=JSON
        raise ValueError("la marca 'agenda' no declara parseo JSON")
    return parser(contenido)


def _nuevo_id(sobre: str, tipo: str, vence: float) -> str:
    """Determinístico y corto: el mismo (sobre, tipo, vence) da el mismo id.

    Sin reloj y sin azar, así que un reintento después de una caída no crea una
    fila gemela — y un test puede nombrarlo sin adivinar.
    """
    return digest_recipiente(f"{sobre}\0{tipo}\0{vence:.3f}")[:16]


# ------------------------------------------------------- lo durable primero


def _escribir(fila: Fila) -> bool:
    """Anexa un evento de forma durable y después lo cachea. True sólo si quedó.

    `exigir=True` levanta si ERPNext no lo tomó, y el llamador tiene que tratar
    un False como «esto NO pasó»: una fila que existe sólo en Redis es
    exactamente lo que este módulo se niega a construir.
    """
    cuerpo = json.dumps(fila.como_dict(), ensure_ascii=False, separators=(",", ":"))
    try:
        marcas.escribir("agenda", fila.sobre, cuerpo, exigir=True)
    except Exception as exc:
        print(
            f"[agenda] {fila.sobre}: fila {fila.tipo} evento {fila.evento} "
            f"NO durable ({type(exc).__name__})"
        )
        return False
    _cachear(fila)
    return True


def _miembro(fila: Fila) -> str:
    return f"{fila.sobre}|{fila.id}"


def _partir(miembro: object) -> tuple[str, str]:
    crudo = miembro.decode() if isinstance(miembro, bytes) else str(miembro)
    sobre, _, identificador = crudo.partition("|")
    return sobre, identificador


def _clave_cache(sobre: str, identificador: str) -> str:
    return f"wa:{{inbound}}:agenda:{digest_recipiente(sobre)}:{identificador}"


def _cachear(fila: Fila) -> None:
    """El ÚNICO lugar donde se mantiene la pertenencia al índice.

    Toda transición pasa por acá, así que es esto lo que tiene que saber si la
    fila sigue teniendo plazo — no cada uno de los llamadores que pueden
    terminarla.

    La pertenencia al índice va PRIMERO y el blob después, y no al revés: el
    índice es lo único que hace que la fila se despache, y el blob es sólo un
    atajo que `leer` sabe suplir yendo a ERPNext. Si sólo una de las dos
    escrituras entra, que sea la que no se puede reconstruir barato.

    Y si algo falla se anota la DEUDA: el evento durable ya quedó escrito, así
    que una fila puede existir sin estar en el índice. `_toca_reconstruir` sólo
    mira si el índice está VACÍO, y con otra fila adentro no lo está — sin esta
    marca la fila quedaba escrita y no se despachaba nunca.
    """
    global _cache_incompleta

    cuerpo = json.dumps(fila.como_dict(), ensure_ascii=False, separators=(",", ":"))
    try:
        cliente = _redis()
        if fila.con_plazo:
            cliente.zadd(CLAVE_INDICE, {_miembro(fila): fila.vence})
        else:
            cliente.zrem(CLAVE_INDICE, _miembro(fila))
        cliente.set(_clave_cache(fila.sobre, fila.id), cuerpo, ex=CACHE_TTL_SEGUNDOS)
    except Exception as exc:
        print(f"[agenda] {fila.sobre}: caché no guardada ({type(exc).__name__})")
        _cache_incompleta = True


def _desde_cache(sobre: str, identificador: str) -> Fila | None:
    try:
        crudo = _redis().get(_clave_cache(sobre, identificador))
    except Exception as exc:
        print(f"[agenda] {sobre}: caché no legible ({type(exc).__name__})")
        return None
    if not crudo:
        return None
    if isinstance(crudo, bytes):
        crudo = crudo.decode()
    try:
        return _desde_dict(json.loads(crudo))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _eventos(sobre: str) -> list[dict] | None:
    """Los comentarios `[agenda]` del pedido, del más nuevo al más viejo.

    None es «no pude leer», que NO es lo mismo que «no hay ninguno»: lo primero
    deja la fila donde está, lo segundo la da por inexistente.
    """
    try:
        return marcas.filas("agenda", sobre)
    except Exception as exc:
        print(f"[agenda] {sobre}: no pude leer las filas ({type(exc).__name__})")
        return None


def _desde_erpnext(sobre: str, identificador: str) -> Fila | None:
    """El evento más nuevo de ESA fila. El primero que parsea es el estado."""
    eventos = _eventos(sobre)
    if eventos is None:
        return None
    for evento in eventos:
        if not isinstance(evento, dict):
            continue
        datos = _parsear(evento.get("content"))
        candidata = _desde_dict(datos)
        if candidata is not None and candidata.id == identificador:
            # Más nueva primero: la primera que parsea ES el estado actual.
            return candidata
    return None


def leer(sobre: str, identificador: str) -> Fila | None:
    """La fila, del caché si está y de ERPNext si no."""
    sobre = str(sobre or "").strip()
    identificador = str(identificador or "").strip()
    if not sobre or not identificador:
        return None
    desde_cache = _desde_cache(sobre, identificador)
    if desde_cache is not None:
        return desde_cache
    durable = _desde_erpnext(sobre, identificador)
    if durable is not None:
        _cachear(durable)
    return durable


def vivas(sobre: str, tipo: str = "") -> list[Fila] | None:
    """Las filas con plazo de un pedido, opcionalmente de un tipo.

    Lee ERPNext, no el índice: la usa `recordar` para hacer valer «un
    seguimiento vivo por pedido», y esa regla no puede depender de un caché.

    None es «NO PUDE LEER» y es distinto de `[]`, que es «no hay ninguna».
    Aplastar los dos en una lista vacía dejaba a `recordar` creando un
    seguimiento nuevo mientras el viejo seguía vivo, justo cuando ERPNext no
    contestaba — o sea, la regla de «uno por pedido» se caía sola en el único
    momento en que hacía falta.
    """
    eventos = _eventos(sobre)
    if eventos is None:
        return None
    if not eventos:
        return []
    ultimas: dict[str, Fila] = {}
    for evento in eventos:
        if not isinstance(evento, dict):
            continue
        candidata = _desde_dict(_parsear(evento.get("content")))
        if candidata is not None:
            ultimas.setdefault(candidata.id, candidata)
    return [
        f for f in ultimas.values()
        if f.con_plazo and (not tipo or f.tipo == tipo)
    ]


# ------------------------------------------------------------- crear y mover


def crear(
    sobre: str,
    tipo: str,
    vence: float,
    *,
    params: dict | None = None,
    ahora: float | None = None,
) -> Fila | None:
    """Escribe una fila nueva. None si no quedó durable — o sea, si no pasó."""
    sobre = str(sobre or "").strip()
    if not sobre:
        return None
    if tipo not in TIPOS:
        # Acá y no en el handler: la lista blanca es la garantía de que una fila
        # sólo puede causar lo que este módulo sabe causar.
        raise ValueError(f"tipo de fila desconocido: {tipo!r}; hay {sorted(TIPOS)}")
    momento = _ahora() if ahora is None else ahora
    fila = Fila(
        id=_nuevo_id(sobre, tipo, vence),
        sobre=sobre,
        tipo=tipo,
        vence=float(vence),
        estado=PENDIENTE,
        params=dict(params or {}),
        evento="creada",
        sello=momento,
    )
    return fila if _escribir(fila) else None


def registrar(fila: Fila, evento: str, ahora: float | None = None, **cambios) -> Fila | None:
    """Anexa un evento nuevo con la foto entera. None = no quedó durable."""
    momento = _ahora() if ahora is None else ahora
    movida = replace(fila, evento=evento, sello=momento, **cambios)
    return movida if _escribir(movida) else None


def cancelar(fila: Fila, motivo: str, ahora: float | None = None) -> Fila | None:
    parametros = {**dict(fila.params or {}), "motivo": motivo}
    return registrar(
        fila, "cancelada", ahora=ahora, estado=CANCELADO, params=parametros
    )


# ------------------------------------------------------------ el índice


def _indice_vencidas(ahora: float) -> list[tuple[str, str]]:
    try:
        crudos = _redis().zrangebyscore(CLAVE_INDICE, "-inf", f"{ahora:.3f}")
    except Exception as exc:
        print(f"[agenda] índice no legible ({type(exc).__name__})")
        return []
    return [_partir(m) for m in crudos or []]


def _indice_vacio() -> bool:
    """Falla hacia RECONSTRUIR: un índice que no se puede leer se trata como
    vacío, porque la alternativa es un barrido que no encuentra nada y no lo
    dice."""
    try:
        return not _redis().zcard(CLAVE_INDICE)
    except Exception:
        return True


_reconstruccion_truncada: bool = False
_reconstruccion_reintento_desde: float = 0.0
# Una escritura de caché que no entró entera. El evento durable SÍ quedó, así
# que hay una fila que puede no estar en el índice: hay que reconstruir aunque
# el índice tenga otras cosas adentro y por lo tanto no esté vacío.
_cache_incompleta: bool = False


def reconstruccion_incompleta() -> bool:
    """Si la última reconstrucción se quedó sin páginas y todavía debe otra."""
    return _reconstruccion_truncada


def _toca_reconstruir() -> bool:
    """La deuda no vence: lo que vence es la ESPERA.

    Un índice vacío pasa por el mismo enfriamiento, porque una reconstrucción
    truncada que no recuperó ni una fila deja el índice vacío igual y si no
    fuera por esto machacaría ERPNext lo mismo.

    `_cache_incompleta` es la tercera puerta y no sobra: un índice NO vacío al
    que le falta una fila es invisible para las otras dos.
    """
    if _reloj() < _reconstruccion_reintento_desde:
        return False
    return _indice_vacio() or _reconstruccion_truncada or _cache_incompleta


def reconstruir_indice() -> int:
    """Rearma el índice desde ERPNext. Devuelve cuántas filas con plazo entraron.

    ERPNext es la verdad y Redis el caché: un FLUSHALL no puede perder una fila
    que se le prometió a un cliente. Se pagina desde el extremo MÁS NUEVO
    (`creation desc`) y gana la primera aparición de cada id, que es su estado
    actual.
    """
    global _reconstruccion_truncada, _reconstruccion_reintento_desde
    global _cache_incompleta

    # Se limpia ANTES de leer, no después: si una escritura de caché vuelve a
    # fallar durante esta misma reconstrucción, la deuda tiene que quedar
    # armada de nuevo y no borrada por el final de esta función.
    _cache_incompleta = False
    ultimas: dict[tuple[str, str], Fila] = {}
    truncada = False
    for pagina in range(MAX_PAGINAS_RECONSTRUCCION):
        try:
            eventos = marcas.filas(
                "agenda",
                techo=MAX_RECONSTRUCCION,
                campos=["content", "reference_name", "creation"],
                start=pagina * MAX_RECONSTRUCCION,
            )
        except Exception as exc:
            print(
                f"[agenda] no pude reconstruir el índice "
                f"({type(exc).__name__}); página {pagina + 1}"
            )
            # Lo leído hasta acá igual sirve —es el extremo más nuevo— pero la
            # reconstrucción no terminó.
            truncada = True
            break
        for evento in eventos:
            if not isinstance(evento, dict):
                continue
            candidata = _desde_dict(_parsear(evento.get("content")))
            if candidata is not None:
                ultimas.setdefault((candidata.sobre, candidata.id), candidata)
        if len(eventos) < MAX_RECONSTRUCCION:
            break
    else:
        truncada = True

    recuperadas = 0
    for candidata in ultimas.values():
        _cachear(candidata)
        if candidata.con_plazo:
            recuperadas += 1
    if truncada:
        _reconstruccion_truncada = True
        _reconstruccion_reintento_desde = _reloj() + REINTENTO_RECONSTRUCCION_SEGUNDOS
        print(
            f"[agenda] reconstrucción INCOMPLETA: leí "
            f"{MAX_PAGINAS_RECONSTRUCCION * MAX_RECONSTRUCCION} eventos y hay "
            f"más. {recuperadas} fila(s) con plazo de {len(ultimas)}; reintento "
            f"en {REINTENTO_RECONSTRUCCION_SEGUNDOS / 60:g} min y hasta entonces "
            f"no vuelvo a preguntar."
        )
    else:
        _reconstruccion_truncada = False
        _reconstruccion_reintento_desde = 0.0
    return recuperadas


# --------------------------------------------------------------- el barrido


def _docstatus(sobre: str) -> int | None:
    """0 borrador, 1 confirmado, 2 cancelado — o None para «no pude saber».

    None NUNCA es 0. Aplastar una respuesta ilegible contra «sigue en borrador»
    es cómo se le manda a un cliente un aviso sobre un pedido que ya le
    confirmaron.
    """
    try:
        doc = erpnext.policy_get_doc("Sales Order", sobre)
    except Exception as exc:
        print(f"[agenda] {sobre}: relectura falló ({type(exc).__name__})")
        return None
    if not isinstance(doc, dict):
        return None
    try:
        return int(doc.get("docstatus") or 0)
    except (TypeError, ValueError):
        return None


def _documento(sobre: str) -> dict | None:
    try:
        doc = erpnext.policy_get_doc("Sales Order", sobre)
    except Exception as exc:
        print(f"[agenda] {sobre}: relectura falló ({type(exc).__name__})")
        return None
    return doc if isinstance(doc, dict) else None


def en_silencio(momento: datetime) -> bool:
    """¿Son las horas en que no se le escribe a una persona?

    Se delega en `pendientes.en_silencio` a propósito: es la única
    implementación de la ventana que cruza la medianoche y de los límites del
    dueño que la fijan. Copiarla acá la escribiría por sexta vez, que es lo que
    este módulo existe para terminar.
    """
    from app import pendientes

    return pendientes.en_silencio(momento)


def tick(ahora: float | None = None) -> int:
    """Despacha lo que venció. Devuelve cuántas filas se hicieron.

    NUNCA levanta: corre en un hilo de fondo y un hipo de Redis o de ERPNext no
    puede frenar el barrido que va a volver a intentar en sesenta segundos.
    """
    momento = _ahora() if ahora is None else ahora
    # Un índice meramente NO VACÍO no prueba que la reconstrucción haya
    # terminado. Con enfriamiento, eso sí: ver `_toca_reconstruir`.
    if _toca_reconstruir():
        reconstruir_indice()
    hechas = 0
    for sobre, identificador in _indice_vencidas(momento)[:POR_RONDA]:
        try:
            if _despachar(sobre, identificador, momento):
                hechas += 1
        except Exception as exc:
            print(f"[agenda] {sobre}: despacho falló ({type(exc).__name__}: {exc})")
    return hechas


def _despachar(sobre: str, identificador: str, ahora: float) -> bool:
    """Los cinco conceptos, para UNA fila. El handler no ve nada de esto."""
    from app.locks import CoordinationError, distributed_lock

    fila = leer(sobre, identificador)
    if fila is None or not fila.con_plazo:
        # O se terminó, o no se pudo leer. En los dos casos se saca del índice
        # sólo si se pudo leer y ya no corresponde; si no se pudo leer, el
        # `leer` de arriba ya devolvió None y la próxima ronda vuelve a
        # preguntar — sacarla acá sería perderla por un hipo de ERPNext.
        if fila is not None:
            _olvidar(sobre, identificador)
        return False
    if not fila.vencida(ahora):
        return False

    # Horas de silencio: se POSTERGA la decisión, no se encola un mensaje ya
    # armado. Un pedido que el dueño confirma a las 23:00 no puede hacer que a
    # las 07:00 le llegue al cliente un aviso que decía otra cosa.
    if fila.tipo in _HABLAN_CON_ALGUIEN and en_silencio(_momento(ahora)):
        return False

    handler = _HANDLERS.get(fila.tipo)
    if handler is None:  # pragma: no cover - TIPOS y _HANDLERS se prueban juntos
        print(f"[agenda] {sobre}: tipo {fila.tipo!r} sin handler")
        return False

    nombre_lock = _LOCKS[fila.tipo].format(sobre=sobre)
    try:
        with distributed_lock(nombre_lock, lease_seconds=60, wait_seconds=5):
            # RE-LEER ADENTRO DEL LOCK. Entre el índice y acá pudo pasar
            # cualquier cosa, y la que importa es que otro worker ya la hizo.
            fila = leer(sobre, identificador)
            if fila is None or not fila.con_plazo or not fila.vencida(ahora):
                return False
            resultado = handler(fila, ahora)
    except CoordinationError:
        # Ronda salteada, nunca un cambio a medias: el otro que tiene el lock
        # converge, y si no, esta fila sigue vencida el minuto que viene.
        return False

    if resultado is None:
        # El handler no pudo y lo dijo. La fila queda viva y se vuelve a
        # intentar: fallar cerrado es NO HACER NADA, no marcarla hecha.
        return False

    if resultado.reprogramar is not None:
        # Los params van CON el `vence`, no después: la hora que el mensaje va a
        # nombrar sale del mismo plazo que decide cuándo dispara. Moverle uno
        # solo es cómo se manda un aviso puntual que dice la hora de ayer.
        cambios = {"vence": float(resultado.reprogramar)}
        if resultado.params is not None:
            cambios["params"] = dict(resultado.params)
        movida = registrar(fila, "reprogramada", ahora=ahora, **cambios)
        return movida is not None

    # FUERA del lock: el teléfono de una persona no puede estar en el camino
    # crítico de un cambio de estado.
    encolados = _encolar(fila, resultado.avisos)
    if not encolados and not resultado.ya_paso:
        # Nadie se enteró y nada irreversible pasó: la fila sigue viva y la
        # próxima ronda reintenta. Marcarla hecha acá perdería el aviso.
        return False
    return _terminar(fila, resultado.detalle, ahora)


def _encolar(fila: Fila, salientes: tuple) -> bool:
    """Encola los avisos del handler. False si alguno no llegó a la cola.

    Las tres respuestas de `avisos.encolar` son tres cosas distintas y se tratan
    distinto: True = lo encolé; False = ya lo cubrió otro (sigue contando como
    hecho); levantó = NADIE lo hizo, y eso es lo único que deja la fila viva.
    """
    from app import avisos as cola

    todo_bien = True
    for saliente in salientes:
        # El id de la fila entra en el evento: `encolar` deduplica por
        # `(evento, pedido)` treinta días, así que sin esto la SEGUNDA fila del
        # mismo tipo sobre el mismo pedido se perdería en silencio.
        evento = f"{saliente.evento}:{fila.id}"
        try:
            if saliente.equipo:
                cola.encolar_equipo(evento, fila.sobre, saliente.texto)
            else:
                cola.encolar(
                    evento,
                    fila.sobre,
                    saliente.telefono,
                    saliente.texto,
                    plantilla_env=saliente.plantilla_env,
                    parametros=list(saliente.parametros),
                )
        except Exception as exc:
            print(
                f"[agenda] {fila.sobre}: aviso {saliente.evento} no encolado "
                f"({type(exc).__name__})"
            )
            todo_bien = False
    return todo_bien


def _olvidar(sobre: str, identificador: str) -> None:
    try:
        _redis().zrem(CLAVE_INDICE, f"{sobre}|{identificador}")
    except Exception:
        pass


def _terminar(fila: Fila, detalle: str, ahora: float) -> bool:
    """Marca la fila hecha, de forma durable. False si no quedó."""
    parametros = {**dict(fila.params or {}), "detalle": detalle}
    hecha = registrar(
        fila, "hecha", ahora=ahora, estado=HECHO, params=parametros
    )
    # No durable = no pasó: se vuelve a intentar. Y como el aviso ya salió por
    # la cola idempotente, repetirlo no le manda dos veces nada al cliente —
    # `avisos.encolar` deduplica por (evento, pedido) durante 30 días.
    return hecha is not None


# Se llena abajo, después de definir los handlers: un dict declarado acá y
# poblado al final deja el orden de lectura (mecanismo primero, comportamientos
# después) sin pagar un import circular.
_HANDLERS: dict = {}


# =========================================================================
# LOS COMPORTAMIENTOS
#
# De acá para abajo, cada bloque es UN comportamiento: unas líneas que arman un
# mensaje con datos y vuelven. Ninguno toca Redis, ni el reloj, ni la
# idempotencia, ni el lock — todo eso ya pasó arriba. Agregar uno es una
# constante en TIPOS, un handler acá, una línea en _HANDLERS y dos tests.
# =========================================================================

PLANTILLA_AVISO_ENTREGA = "WHATSAPP_CUSTOMER_DELIVERY_LEAD_TEMPLATE"


@dataclass(frozen=True)
class Aviso:
    """Un mensaje que el mecanismo encola DESPUÉS de soltar el lock.

    El handler lo DESCRIBE; no lo manda. El teléfono de una persona no puede
    estar en el camino crítico de un cambio de estado.
    """

    evento: str
    texto: str
    telefono: str = ""
    plantilla_env: str = ""
    parametros: tuple[str, ...] = ()
    equipo: bool = False


@dataclass(frozen=True)
class Resultado:
    """Lo que un handler contesta. None en vez de esto = «no pude»."""

    detalle: str
    avisos: tuple[Aviso, ...] = ()
    # El efecto ya ocurrió y no se puede deshacer (el borrador YA está cerrado),
    # así que la fila se cierra aunque un aviso no se haya podido encolar:
    # callarse sería el peor final, y la próxima ronda no vuelve a mirar.
    ya_paso: bool = False
    # La fecha de entrega se movió: correr la fila en vez de dispararla.
    reprogramar: float | None = None
    # Los params que van con ese `vence` nuevo. None = dejarlos como estaban.
    params: dict | None = None


def _sello(ahora: float) -> str:
    return _momento(ahora).isoformat(timespec="seconds")


# ------------------------------------------------------------------ textos
#
# Constructores PUROS: sin reloj y sin contar nada por su cuenta, así que dos
# llamadas con los mismos datos dan el mismo texto. Es lo que exige
# tests/test_idioma_cobertura.py, que los llama una vez por idioma y compara.


def aviso_antes_de_entrega(pedido: str, hora: str, lengua: str | None = None) -> str:
    """Al cliente: todavía no se lo pude confirmar. Sin día, sin precio.

    La hora es la que YA estaba prometida —la del reparto configurado—, no una
    nueva: el mensaje repite el plazo que el cliente ya conocía y dice que no
    llegó a confirmarse. No promete nada.
    """
    from app import idioma

    return idioma.t("pedido.aviso_antes_de_entrega", lengua, pedido=pedido, hora=hora)


def recordatorio_plazo(
    pedido: str, hora: str, lengua: str | None = None
) -> tuple[str, str]:
    """(asunto, cuerpo) al dueño: esto necesita respuesta ANTES de tal hora."""
    from app import idioma

    return (
        idioma.t("gerencia.plazo_asunto", lengua, pedido=pedido),
        idioma.t("gerencia.plazo_cuerpo", lengua, pedido=pedido, hora=hora),
    )


def seguimiento_equipo(pedido: str, motivo: str, lengua: str | None = None) -> str:
    """Al equipo: alguien pidió volver sobre este pedido, y por qué.

    `motivo` es DATO y se muestra tal cual a una persona. No se vuelve a
    interpretar como una instrucción, y ya vino sin citas (`formato.sin_citas`)
    desde la herramienta, así que un cliente no puede plantar un seguimiento.
    """
    from app import idioma

    return idioma.t("gerencia.seguimiento", lengua, pedido=pedido, motivo=motivo)


# ---------------------------------------------------- cierre_borrador (port)


def _cerrar_borrador(fila: Fila, ahora: float) -> Resultado | None:
    """El cierre que vivía en `pendientes._cerrar`, ahora como fila.

    El COMPORTAMIENTO no cambió —mismo lock, misma marca durable, mismos dos
    avisos, misma regla dura de no escribir nada terminal sin prueba de que
    soltó el stock—; lo que cambió es que los cinco conceptos compartidos los
    pone el mecanismo de arriba y no este código. Ésa es la prueba de que el
    mecanismo es equivalente: `tests/test_pendientes.py` pasa sin tocarse.

    Usa tres ayudantes de `pendientes` a propósito: la relectura de adentro del
    lock y la marca de idempotencia SON el comportamiento del cierre, no del
    mecanismo, así que se quedan donde estaban.
    """
    from app import decisiones, idioma, pendientes, solicitudes

    sobre = fila.sobre
    horas = float(fila.params.get("horas") or 0.0)

    # Las TRES respuestas de `_tiene_marca` son tres cosas distintas y cada una
    # termina distinto. Aplastar True contra None dejaba la fila viva para
    # siempre: el borrador ya estaba cerrado, así que el handler contestaba
    # «no pude» en cada barrido y la fila no llegaba nunca a un estado terminal.
    marca = pendientes._tiene_marca(sobre, "pendiente_cierre")
    if marca is None:
        return None  # no pude saber: la próxima ronda vuelve a preguntar
    if marca:
        # Ya estaba cerrado. No hay nada que hacer y no se le habla a nadie,
        # pero la fila SÍ se cierra: es la que converge.
        return Resultado(detalle="el borrador ya estaba cerrado")
    if pendientes._sigue_esperando(sobre) is None:
        # Una persona lo decidió entre el listado y acá. No se le dice nada al
        # cliente, y la fila se cierra: no hay nada que hacer con ella.
        return Resultado(detalle="lo decidió una persona")

    liberado, detalle = solicitudes.soltar_reserva(sobre)
    if not liberado:
        # Sin prueba de que soltó el stock no se escribe un estado terminal: la
        # próxima ronda vuelve a preguntar.
        print(f"[agenda] {sobre}: no pude cerrarlo ({detalle})")
        return None

    try:
        erpnext.add_comment(
            "Sales Order",
            sobre,
            f"{pendientes.MARCA_CIERRE} {_sello(ahora)} sin decisión en "
            f"{horas:g} h; {detalle}",
        )
    except Exception as exc:
        # Su propio try: el cierre YA pasó y la próxima ronda no lo vuelve a
        # mirar, así que si este fallo se comiera los avisos nadie se enteraría
        # nunca de que el borrador se cerró.
        print(f"[agenda] {sobre}: cerrado, pero la marca no quedó ({type(exc).__name__})")

    salientes: list[Aviso] = []
    try:
        telefono = str(decisiones.telefono_del_cliente(sobre) or "")
    except Exception as exc:
        print(f"[agenda] {sobre}: no pude resolver el teléfono ({type(exc).__name__})")
        telefono = ""
    if telefono:
        salientes.append(
            Aviso(
                evento="pendiente_cerrado",
                texto=pendientes.pendiente_cerrado(
                    sobre, idioma.para_destinatario(telefono)
                ),
                telefono=telefono,
                plantilla_env=pendientes.PLANTILLA_CERRADO,
                parametros=(sobre,),
            )
        )
    salientes.append(
        Aviso(
            evento="pendiente_cerrado_equipo",
            texto=pendientes.pendiente_cerrado_equipo(sobre, horas, idioma.gerencia()),
            equipo=True,
        )
    )
    return Resultado(detalle=detalle, avisos=tuple(salientes), ya_paso=True)


# ------------------------------------------- aviso_antes_de_entrega (plazos)


def _avisar_antes_de_entrega(fila: Fila, ahora: float) -> Resultado | None:
    """Al cliente, antes de la entrega: todavía no está confirmado.

    Re-lee el documento porque la fecha de entrega NO es final cuando se crea
    el pedido: `policy_aplicar_terminos` la reescribe cada vez que un cliente
    acepta una contraoferta. Una fila cuyo `vence` se calculó en la creación
    dispararía contra un día que nadie acordó.
    """
    from app import decisiones, idioma

    doc = _documento(fila.sobre)
    if doc is None:
        return None  # ilegible: no adivinar, la próxima ronda pregunta

    if int(doc.get("docstatus") or 0) != 0:
        # Se confirmó (o se canceló) entre que venció y ahora. No hay nada que
        # avisar, y la fila se cierra en vez de quedar viva para siempre.
        return Resultado(detalle="el pedido ya no es un borrador")

    nuevo = vence_antes_de_entrega(doc, fila.params.get("horas"))
    if nuevo is None:
        # El pedido ya no tiene un plazo legible —le sacaron la fecha de
        # entrega, o quedó mal escrita, o no hay hora de reparto configurada—.
        # NO se manda el aviso con la hora que se guardó al crear la fila: ésa
        # es exactamente la hora que puede haber dejado de ser cierta. Se cierra
        # la fila: el pedido sin fecha ya está cubierto por el plazo plano de
        # `PENDIENTE_AVISO_HORAS`.
        return Resultado(detalle="el pedido ya no tiene un plazo legible")

    hora = texto_de_la_hora(doc)
    if nuevo > ahora + 60:
        # La entrega se movió: se corre la fila Y se corrige la hora que el
        # mensaje va a nombrar. Las dos salen del mismo plazo recalculado.
        return Resultado(
            detalle="la entrega se movió",
            reprogramar=nuevo,
            params={**dict(fila.params or {}), "hora": hora},
        )

    try:
        telefono = str(decisiones.telefono_del_cliente(fila.sobre) or "")
    except Exception as exc:
        print(f"[agenda] {fila.sobre}: no pude resolver el teléfono ({type(exc).__name__})")
        return None
    if not telefono:
        return Resultado(detalle="el cliente no tiene teléfono")

    return Resultado(
        detalle="avisado antes de la entrega",
        avisos=(
            Aviso(
                evento="agenda_entrega",
                texto=aviso_antes_de_entrega(
                    fila.sobre, hora, idioma.para_destinatario(telefono)
                ),
                telefono=telefono,
                plantilla_env=PLANTILLA_AVISO_ENTREGA,
                parametros=(fila.sobre,),
            ),
        ),
    )


# ------------------------------------------- el re-ping al dueño con la hora


def _recordar_al_dueno(fila: Fila, ahora: float) -> Resultado | None:
    """Al dueño: esto necesita respuesta antes de tal hora o no llega.

    `avisar_dueno` avisa un fallo con un RETORNO falso, no con una excepción, y
    False significa «al dueño NO se le dijo»: la fila se queda viva.
    """
    from app import idioma, notificar

    doc = _documento(fila.sobre)
    if doc is None:
        return None
    if int(doc.get("docstatus") or 0) != 0:
        return Resultado(detalle="ya lo decidieron")

    # El plazo se RECALCULA, no se lee de la fila: una excepción de entrega
    # aceptada reescribe `delivery_date` después de que la fila se creó, y
    # entonces la hora guardada es la de un plazo que ya no existe. Decirle al
    # dueño «contestá antes de las 14» cuando el plazo pasó a ser las 17 es
    # peor que no decirle nada: contesta tarde creyendo que llegó.
    plazo = vence_antes_de_entrega(doc)
    if plazo is None:
        return Resultado(detalle="el pedido ya no tiene un plazo legible")
    if plazo > ahora + 60:
        # El plazo se corrió: este aviso se corre con él, una hora antes.
        nuevo = plazo - RE_PING_DUENO_HORAS * 3600.0
        return Resultado(
            detalle="el plazo se movió",
            reprogramar=nuevo,
            params={**dict(fila.params or {}), "hora": texto_del_plazo(plazo)},
        )

    asunto, cuerpo = recordatorio_plazo(
        fila.sobre, texto_del_plazo(plazo), idioma.gerencia()
    )
    try:
        avisado = bool(
            notificar.avisar_dueno(
                asunto, cuerpo, plantilla_env="WHATSAPP_STAFF_ALERT_TEMPLATE"
            )
        )
    except Exception as exc:
        print(f"[agenda] {fila.sobre}: re-aviso al dueño falló ({type(exc).__name__})")
        return None
    return Resultado(detalle="dueño avisado") if avisado else None


# ------------------------------------------------ seguimiento (el del modelo)


def _seguimiento(fila: Fila, ahora: float) -> Resultado | None:
    """Lo que pidió `recordar`: volver sobre este pedido, por este motivo.

    Va al EQUIPO y no al cliente. El peor final posible de una fila que propuso
    un modelo es un mensaje que no hacía falta; que ese mensaje lo lea una
    persona del equipo y no un cliente es lo que lo mantiene barato.
    """
    from app import idioma

    doc = _documento(fila.sobre)
    if doc is None:
        return None

    # UN seguimiento por pedido, hecho valer acá y no sólo al crearlo. `recordar`
    # crea el nuevo antes de cancelar el viejo —para que un fallo de escritura
    # no deje CERO recordatorios, que es el error caro— y el precio de ese orden
    # es que una cancelación fallida deje dos vivos. Éste es el lugar donde eso
    # se paga sin que se note: si hay uno MÁS NUEVO para el mismo pedido, éste
    # se termina sin hablar. Dos filas pueden convivir un rato; dos mensajes al
    # equipo por el mismo pedido, no.
    hermanas = vivas(fila.sobre, SEGUIMIENTO)
    if hermanas is None:
        return None  # no pude saber: mejor no hablar que hablar de más
    if any(otra.sello > fila.sello for otra in hermanas):
        return Resultado(detalle="lo reemplazó un seguimiento más nuevo")

    motivo = str(fila.params.get("por_que") or "").strip()
    return Resultado(
        detalle="seguimiento avisado",
        avisos=(
            Aviso(
                evento="agenda_seguimiento",
                texto=seguimiento_equipo(fila.sobre, motivo, idioma.gerencia()),
                equipo=True,
            ),
        ),
    )


_HANDLERS.update(
    {
        CIERRE_BORRADOR: _cerrar_borrador,
        AVISO_ANTES_DE_ENTREGA: _avisar_antes_de_entrega,
        RECORDATORIO_PLAZO_DUENO: _recordar_al_dueno,
        SEGUIMIENTO: _seguimiento,
    }
)


# =========================================================================
# CUÁNDO — de dónde sale el `vence` de cada fila
# =========================================================================

LIMITE_AVISO_ENTREGA = "AVISO_ANTES_DE_ENTREGA_HORAS"

# Cuánto antes del vencimiento se le vuelve a tocar el hombro al dueño. UNA
# vez: el objetivo es que no se le pase, no perseguirlo.
RE_PING_DUENO_HORAS = 1.0

_RE_HORA_RELOJ = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def horas_de_aviso() -> float | None:
    """El límite del dueño, en horas. None = NINGUNO (apagado) o ilegible.

    Ilegible cae en el mismo None que apagado, y eso es deliberado: un límite
    que no se puede leer no puede ser motivo para escribirle a un cliente.
    `limites.vigente` LEVANTA cuando Redis no contesta, así que esto no es una
    lectura segura y el `except` no es decorativo.
    """
    from app import limites

    try:
        crudo = limites.vigente(LIMITE_AVISO_ENTREGA).strip()
    except Exception as exc:
        print(f"[agenda] {LIMITE_AVISO_ENTREGA} ilegible ({type(exc).__name__})")
        return None
    if not crudo or crudo == limites.NINGUNO:
        return None
    try:
        valor = float(crudo)
    except (TypeError, ValueError):
        print(f"[agenda] {LIMITE_AVISO_ENTREGA}={crudo!r} no es un número de horas")
        return None
    return valor if valor > 0 else None


def hora_de_entrega() -> tuple[int, int] | None:
    """La hora del reparto configurada, o None si no hay una legible.

    El Sales Order NO la tiene: `delivery_date` es un Date de ERPNext, un
    «YYYY-MM-DD» pelado, sin hora y sin zona. La hora es configuración del
    dueño (`ENTREGA_HORA`), y por eso una fila que la necesita se la guarda.
    """
    from app import excepciones

    try:
        crudo = str(excepciones.hora_reparto() or "").strip()
    except Exception as exc:
        print(f"[agenda] no pude leer la hora de reparto ({type(exc).__name__})")
        return None
    encontrada = _RE_HORA_RELOJ.match(crudo)
    if not encontrada:
        return None
    return int(encontrada.group(1)), int(encontrada.group(2))


def vence_antes_de_entrega(doc: dict, horas: object = None) -> float | None:
    """Cuándo avisar: la entrega prometida MENOS las horas del dueño.

    None cuando falta cualquiera de las tres piezas —la fecha, la hora o el
    límite—, y el llamador tiene que caer entonces en el plazo plano de
    `PENDIENTE_AVISO_HORAS` en vez de perder el vencimiento.
    """
    if horas is None:
        horas = horas_de_aviso()
    try:
        adelanto = float(horas)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if adelanto <= 0:
        return None

    crudo = str(doc.get("delivery_date") or "").strip()
    if not crudo:
        return None
    try:
        dia = date.fromisoformat(crudo[:10])
    except ValueError:
        return None

    hora = hora_de_entrega()
    if hora is None:
        return None

    entrega = datetime(dia.year, dia.month, dia.day, hora[0], hora[1], tzinfo=_zona())
    return (entrega - timedelta(hours=adelanto)).timestamp()


def texto_de_la_hora(doc: dict) -> str:
    """«17» o «17:30»: la hora de ENTREGA prometida, para el mensaje al cliente.

    Sale de la MISMA configuración que `vence_antes_de_entrega`, así que el
    mensaje no puede nombrar una hora distinta de la que usó el plazo.
    """
    hora = hora_de_entrega()
    if hora is None:
        return ""
    return f"{hora[0]:02d}" if hora[1] == 0 else f"{hora[0]:02d}:{hora[1]:02d}"


def texto_del_plazo(vence: float) -> str:
    """«14:00»: la hora en que se vence el plazo, para el aviso al DUEÑO.

    NO es la misma hora que `texto_de_la_hora`, y confundirlas es el error que
    tener dos funciones evita: al cliente se le nombra la ENTREGA (las 17) y al
    dueño el PLAZO para decidir (las 14). Son el mismo `vence` leído por dos
    consumidores distintos, así que cada uno se prueba por separado.
    """
    return _momento(vence).strftime("%H:%M")


def plazo_del_pedido(doc: dict) -> str:
    """La hora límite de este pedido para el dueño, o "" si no hay plazo.

    "" cuando el límite está apagado, cuando el pedido no tiene fecha de
    entrega o cuando no hay hora de reparto configurada — o sea, exactamente
    cuando no hay un plazo que nombrar. El aviso sin plazo no cambia ni una
    palabra respecto de hoy.
    """
    vence = vence_antes_de_entrega(doc)
    return "" if vence is None else texto_del_plazo(vence)


def programar_para_entrega(doc: dict, ahora: float | None = None) -> list[Fila]:
    """Las filas de un pedido recién creado que tiene fecha de entrega.

    Dos: el aviso al cliente antes de la entrega, y UN re-ping al dueño cuando
    el plazo se le viene encima. Devuelve las que quedaron durables — una lista
    vacía es «no hay nada programado», que es lo que corresponde con el límite
    apagado, sin fecha de entrega o sin hora de reparto configurada.

    Es idempotente por construcción: el id sale de `(sobre, tipo, vence)`, así
    que volver a llamarla con los mismos datos reescribe la MISMA fila en vez de
    crear una gemela. Hace falta que lo sea: `crear_pedido` tiene cuatro salidas
    tempranas que no llegan a este punto, y la de recuperación después de una
    caída vuelve a pasar por acá con el pedido ya existente.
    """
    sobre = str(doc.get("name") or "").strip()
    if not sobre:
        return []
    # Sólo un BORRADOR. Un pedido ya confirmado no necesita que le avisen que no
    # está confirmado, y un cancelado menos. Va acá y no en cada llamador para
    # que llamarla de más sea inofensivo: es lo que la vuelve segura de reponer
    # en los caminos de reintento, donde el pedido ya existe.
    try:
        if int(doc.get("docstatus") or 0) != 0:
            return []
    except (TypeError, ValueError):
        return []
    horas = horas_de_aviso()
    if horas is None:
        return []  # apagado: desplegar esto no cambia nada de lo que ve nadie
    vence = vence_antes_de_entrega(doc, horas)
    if vence is None:
        return []
    momento = _ahora() if ahora is None else ahora
    if vence <= momento:
        # La entrega es hoy y ya pasó el punto de aviso. Avisar «antes» de algo
        # que ya ocurrió no es un aviso: se deja al plazo plano de
        # `PENDIENTE_AVISO_HORAS`, que es el que sabe hablar de un pedido viejo.
        return []

    hora = texto_de_la_hora(doc)
    creadas: list[Fila] = []
    fila = crear(
        sobre,
        AVISO_ANTES_DE_ENTREGA,
        vence,
        params={"horas": horas, "hora": hora},
        ahora=momento,
    )
    if fila is not None:
        creadas.append(fila)

    # El re-ping al dueño: una sola vez, antes del aviso al cliente, para que
    # todavía pueda decidirlo él en vez de enterarse por el aviso.
    #
    # Su `hora` es la del PLAZO (las 14), no la de la entrega (las 17): al dueño
    # se le dice hasta cuándo puede contestar, al cliente para cuándo era. Mismo
    # `vence`, dos lecturas, y por eso son dos funciones y no una.
    aviso_dueno = vence - RE_PING_DUENO_HORAS * 3600.0
    if aviso_dueno > momento:
        fila_dueno = crear(
            sobre,
            RECORDATORIO_PLAZO_DUENO,
            aviso_dueno,
            params={"hora": texto_del_plazo(vence)},
            ahora=momento,
        )
        if fila_dueno is not None:
            creadas.append(fila_dueno)
    return creadas


def reconciliar_entrega(sobre: str, ahora: float | None = None) -> list[Fila]:
    """Rehace las filas de entrega de un pedido cuya fecha cambió.

    `programar_para_entrega` corre una sola vez, al crear el pedido, pero
    `policy_aplicar_terminos` reescribe `delivery_date` cada vez que un cliente
    acepta una contraoferta. Las filas viejas se re-leen al dispararse y saben
    correrse hacia ADELANTE — pero una entrega que se adelanta las deja
    despertando tarde, y a esa altura el aviso ya no llega antes de nada.

    Cancelar primero y crear después es seguro acá, al revés que en `recordar`:
    lo que se cancela es una fila cuyo plazo ya no existe, así que perderla no
    pierde nada. Y es idempotente: con la fecha sin cambios, el id vuelve a ser
    el mismo y la fila se reescribe en lugar de duplicarse.
    """
    sobre = str(sobre or "").strip()
    if not sobre:
        return []
    momento = _ahora() if ahora is None else ahora

    doc = _documento(sobre)
    if doc is None:
        return []  # ilegible: no se toca nada
    nuevo = vence_antes_de_entrega(doc)

    abiertas = vivas(sobre)
    if abiertas is None:
        return []  # no pude leer: no se toca nada
    for fila in abiertas:
        if fila.tipo not in (AVISO_ANTES_DE_ENTREGA, RECORDATORIO_PLAZO_DUENO):
            continue
        if nuevo is not None and fila.vence == nuevo:
            continue  # ya apunta al plazo vigente
        cancelar(fila, "la fecha de entrega cambió", ahora=momento)

    return programar_para_entrega(doc, ahora=momento)


def ejecutar_ahora(
    tipo: str, sobre: str, ahora: float, params: dict | None = None
) -> bool:
    """Crear una fila que vence YA y despacharla en el acto.

    Es cómo entra un comportamiento que decide sus propios candidatos —el
    cierre de borradores, que `pendientes.tick` encuentra barriendo ERPNext— sin
    dejar de pasar por los cinco conceptos de este módulo. La fila queda igual
    de durable: es el registro de que el cierre se intentó y cómo terminó.
    """
    fila = crear(sobre, tipo, ahora, params=params, ahora=ahora)
    if fila is None:
        # No quedó durable = no pasó. Nada se cerró y nadie fue avisado.
        return False
    return _despachar(sobre, fila.id, ahora)


# =========================================================================
# `recordar`: lo que el MODELO puede pedir, y todo lo que Python le exige
# =========================================================================

# El mismo horizonte que las excepciones de entrega, y por el mismo motivo: una
# fila para dentro de dos meses no es un recordatorio, es un olvido con fecha.
# Se importa en vez de escribir un 7 acá — `docs/AUDITORIA.md` anota justamente
# que ese número estaba por duplicarse.
def horizonte_dias() -> int:
    from app import excepciones

    return excepciones.HORIZONTE_DIAS


class PropuestaInvalida(ValueError):
    """Lo que el modelo propuso no pasa una guarda. Nunca llega a ser fila."""


def validar_recordatorio(
    sobre: str, cuando: float, por_que: str, ahora: float
) -> tuple[str, float, str]:
    """Las guardas de `recordar`, todas en Python. Levanta si algo no pasa.

    El modelo propone; esto valida y recién después se guarda. Es la regla de
    las cuatro capas intacta: percepción y propuesta más ricas, autoridad
    ninguna.
    """
    from app import formato

    pedido = str(sobre or "").strip()
    if not pedido:
        raise PropuestaInvalida("no dijiste sobre qué pedido")

    if cuando <= ahora:
        raise PropuestaInvalida("esa fecha ya pasó")
    tope = ahora + horizonte_dias() * 86400.0
    if cuando > tope:
        raise PropuestaInvalida(
            f"no puedo agendar más allá de {horizonte_dias()} días"
        )

    # El motivo entra como DATO. `sin_citas` saca lo que el cliente haya citado,
    # así que un mensaje del cliente no puede plantar un seguimiento con texto
    # elegido por él.
    motivo = str(formato.sin_citas(por_que or "")).strip()[:300]
    return pedido, float(cuando), motivo


def recordar(
    sobre: str, cuando: float, por_que: str, ahora: float | None = None
) -> Fila | None:
    """«Volvé sobre este pedido a esta hora, por este motivo.»

    El `tipo` se FUERZA a `SEGUIMIENTO`: no es un parámetro y el modelo no lo
    ve, así que no puede pedir una fila privilegiada. Y hay UN seguimiento vivo
    por pedido: el segundo reemplaza al primero, porque dos recordatorios sobre
    el mismo pedido son ruido, no dos avisos.
    """
    momento = _ahora() if ahora is None else ahora
    pedido, vence, motivo = validar_recordatorio(sobre, cuando, por_que, momento)

    anteriores = vivas(pedido, SEGUIMIENTO)
    if anteriores is None:
        # No se pudo leer qué había. Crear igual sería crear un SEGUNDO
        # seguimiento vivo sin saberlo, que es justo lo que la regla prohíbe, y
        # además pasaría en el peor momento: con ERPNext sin contestar.
        raise PropuestaInvalida("no pude leer los recordatorios que ya tiene")

    # Se crea PRIMERO y se cancela después. Al revés, una escritura fallida
    # después de una cancelación exitosa dejaba el pedido sin ningún
    # recordatorio: el viejo ya terminal y el nuevo nunca escrito. De este lado,
    # lo peor que puede pasar es que queden dos —dos mensajes al equipo, que es
    # ruido— y nunca cero, que es un olvido.
    nueva = crear(
        pedido,
        # Forzado, no elegido: ver el docstring.
        SEGUIMIENTO,
        vence,
        params={"por_que": motivo},
        ahora=momento,
    )
    if nueva is None:
        # No quedó durable = no pasó. El anterior sigue vivo, que es correcto.
        return None

    for anterior in anteriores:
        if anterior.id == nueva.id:
            continue  # el mismo (pedido, tipo, vence): se reescribió, no se duplica
        if cancelar(anterior, "reemplazado por un seguimiento nuevo", ahora=momento) is None:
            print(
                f"[agenda] {pedido}: no pude cancelar el seguimiento anterior "
                f"{anterior.id}; quedan dos vivos"
            )
    return nueva
