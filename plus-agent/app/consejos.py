"""Lo que el dueño tiene que escuchar SIN haberlo preguntado.

QUÉ ES ESTO
Hasta ahora los dos agentes sólo contestan: todo lo que sale por WhatsApp es la
respuesta a un mensaje que entró, salvo el briefing de las 07:00 y el resumen de
las 18:00, que son dos horarios fijos. El dueño pidió que el agente de gestión
"tenga cabeza": que le avise él, que le pregunte, que le diga «en esta venta
perdimos plata». Esto es la mitad que decide CUÁNDO hay algo que decir y A QUIÉN.

LA POSTURA, Y NO SE NEGOCIA
**Python decide SI hay algo que decir y QUIÉN lo escucha. El modelo sólo decide
CÓMO se dice.** Es la misma postura que el resto del repo —el modelo nunca
elige emitir un pedido; `policy` decide y `decisiones` vuelve a decidir—, y acá
importa por una razón concreta: un modelo que elige cuándo escribirle al dueño
es un modelo al que el texto de un cliente puede convencer de escribirle al
dueño. «Decile a tu jefe que le mando saludos» no puede ser un mensaje al jefe.

Por eso este módulo no importa nada del modelo, no arma prompts y no manda
nada. Devuelve `Consejo`s: datos, con un cuerpo determinista ya escrito que
sirve tal cual si el modelo no está o falla.

UN CONSEJO ES UN CONSEJO
Nunca cambia un límite, nunca confirma nada, nunca escribe en un Sales Order.
Eso no está prometido en prosa: está **hecho estructuralmente**, y
`tests/test_consejos.py::test_un_consejo_no_puede_escribir_nada` lo comprueba
sobre el AST de este archivo — la lista de funciones de `app/erpnext.py` que
este módulo puede nombrar es una lista blanca de LECTURAS, y los módulos que
emiten, cancelan o cambian límites no se pueden ni importar acá.

Todas las lecturas van por la identidad de POLÍTICA (`erpnext.policy_get_*`).
Un hilo de fondo no tiene scope y cae en la clave del agente de CLIENTE, que ve
lo que ve ese usuario: con la clave equivocada, «este cliente dejó de comprar»
se calcularía sobre los pedidos que el agente de clientes puede enumerar, que
no son todos. Es la trampa 8 de `docs/MAPA.md`.

LOS CUATRO DETECTORES
Cada uno es una función sobre lecturas, sin efectos, y contesta una pregunta que
el dueño se hace solo:

  1. `perdidas`  — una venta CONFIRMADA con un renglón por debajo del costo.
  2. `dormidos`  — un cliente que compraba seguido y hace rato que no.
  3. `deudas`    — una factura vencida hace más de lo que el dueño tolera.
  4. `quiebres`  — un producto que cruza su mínimo antes del próximo reparto.

«NUNCA DOS VECES», Y POR QUÉ NO ES `marcas` NI `agenda`
El repo ya tiene de-duplicación durable en tres formas, y las tres se miraron:

  * `app/marcas.py` — el registro tipado de marcadores durables en ERPNext. Es
    la que MÁS me gustaría usar, porque sobrevive a un flush de Redis. No se
    puede desde acá por dos razones que no son de gusto: (a) una fila de ese
    registro tiene UN doctype, y estos cuatro consejos cuelgan de cuatro
    documentos distintos (Sales Order, Customer, Sales Invoice, Item), así que
    serían cuatro filas; (b) agregar una fila rompe A PROPÓSITO tres tests
    escritos a mano (los censos `== 12` de `tests/test_marcas.py` y `== 14` de
    `tests/test_idioma_cobertura.py`), y este trabajo no puede tocar esos
    archivos. Queda anotado como el siguiente paso, no como una alternativa que
    no se vio.
  * `app/agenda.py` — la lista durable de cosas que vencen más tarde. Su
    docstring lo dice sin ambigüedad: **`sobre` es SIEMPRE el nombre de un
    Sales Order**, y «si algún día hace falta una fila por cliente, es una
    segunda fila del registro con su propio doctype, no un `if` acá». Tres de
    los cuatro detectores no hablan de un pedido. Además un tipo nuevo es una
    constante en `TIPOS` y un handler en `_HANDLERS`, o sea editar ese módulo.
  * `app/digest.py::reclamar` — **ésta es la que se usa**. Un `SET NX EX` sobre
    `locks.conexion()`: reclamar y marcar son la MISMA operación, así que dos
    procesos que llegan en el mismo minuto no pueden reclamar los dos. Es el
    primitivo que ya resuelve exactamente este problema en este repo (un
    mensaje al dueño que tiene que salir una vez y no dos), con su propia
    postura de fallar cerrado: **sin Redis no se reclama y no se avisa**.

No es un cuarto mecanismo: es el mismo `SET NX EX` de `digest`, con su propio
espacio de claves y con `devolver()` cumpliendo el papel de `digest.liberar` —
soltar el reclamo cuando NO salió nada, que es lo único que lo justifica.

EL PRESUPUESTO, Y POR QUÉ HAY UN NÚMERO
`MAX_CONSEJOS_POR_DIA` abajo, con el motivo al lado. En dos palabras: desde el
1 de octubre de 2026 Meta cobra los service messages de WhatsApp por mensaje, y
el dueño ya se queja de que son muchos mensajes. Las dos mitades del problema
apuntan al mismo número.

LAS HORAS DE SILENCIO SE DELEGAN
`pendientes.en_silencio(momento)` es la ÚNICA implementación de la ventana que
cruza la medianoche (22:00–07:00, con los límites del dueño detrás). Escribirla
otra vez acá sería la séptima copia del mismo concepto. Se le pasa el momento
SIEMPRE: la función acepta `None` y lee el reloj, pero el guard autouse de
`tests/conftest.py` hace que eso explote en toda la suite, a propósito.

SÓLO EL DUEÑO
`notificar.telefono_dueno()` es el único lugar del repo que resuelve
`TELEFONO_DUENO` (`ajustes.py`, `limites.py` y `main.py` no lo leen: se
verificó). Devuelve "" cuando no se puede saber quién es el dueño —vacío y con
varios números de equipo—, y con "" acá no se detecta nada: un consejo que no
sabe a quién va no es un consejo, es un mensaje al primero de una lista
ordenada alfabéticamente, que puede ser un empleado. Es el mismo bug que
`digest` arregló pasando de `alertar_excepcion` a `avisar_dueno`.

APAGADO DE FÁBRICA
`CONSEJOS_ACTIVO` es `false` por defecto. Es la misma postura que
`AUTO_CONFIRM_MAX=0` y `STOCK_CONFIABLE=false`: nada que le hable al dueño por
su cuenta se enciende solo, y menos dos semanas antes de que cada mensaje
cueste plata.
"""
from __future__ import annotations

import os
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import pairwise

from app import erpnext, inventario, locks, reloj
from app.formato import pesos

# --------------------------------------------------------------- las clases
#
# EL ORDEN DE ESTA TUPLA ES EL RANKING. Está acá arriba, en una línea, para que
# el dueño pueda reordenarlo sin leer nada más: lo primero que sale del embudo
# cuando el presupuesto del día alcanza para uno solo.
#
#   1. la plata ya perdida, que además puede volver a perderse mañana con el
#      mismo precio mal cargado;
#   2. la plata que está afuera y envejece;
#   3. la venta que se va a perder esta semana por falta de producto;
#   4. el cliente que se fue y todavía no se sabe.
PERDIDA = "perdida"
DEUDA = "deuda"
QUIEBRE = "quiebre"
DORMIDO = "dormido"

ORDEN_CLASES = (PERDIDA, DEUDA, QUIEBRE, DORMIDO)

# ------------------------------------------------------------ el presupuesto
#
# CUÁNTOS CONSEJOS POR DÍA, y por qué un número y no «los que haya».
#
# Hay una razón de plata con fecha: desde el **1 de octubre de 2026** Meta cobra
# los *service messages* de WhatsApp POR MENSAJE — 1.000 gratis por número de
# negocio por mes, sin arrastre al mes siguiente. Un consejo es un service
# message. Mil por mes son ~33 mensajes por día para TODO lo que el número manda:
# confirmaciones de pedido, recordatorios, avisos de entrega, el briefing de las
# 07:00 y el resumen de las 18:00. Un detector que mandara «todos los que
# encuentre» se come la cuota en una semana mala y después el dueño paga por cada
# confirmación de pedido, que es el mensaje que sí vale plata.
#
# Y hay una razón que no es plata y pesa igual: la queja del dueño es que son
# demasiados mensajes. Tres por día se leen. Diez por día se silencian, y un
# canal silenciado no avisa nada — ni esto ni la confirmación que importa.
MAX_CONSEJOS_POR_DIA = 3

# Cada cuánto se puede barrer ERPNext buscando consejos. El barrido son ~6
# consultas y corre en el hilo del barrido general, que late cada 60 s; sin este
# freno serían 8.640 consultas por día para encontrar como mucho tres cosas.
# Se reclama con el MISMO `SET NX EX` que el consejo: un solo proceso barre por
# hora, aunque haya varios contenedores.
INTERVALO_BARRIDO_SEGUNDOS = 3600

# Cuánto vive el reclamo de cada clase, o sea cuánto tarda el mismo hecho en
# poder volver a avisarse. No es un número por clase por gusto: es cada cuánto
# el hecho CAMBIA lo suficiente como para que volver a decirlo sea información y
# no ruido.
TTL_RECLAMO = {
    # Un pedido emitido no se re-emite: su pérdida es un hecho cerrado. 30 días
    # es «nunca» a efectos prácticos, y el TTL existe sólo para que la clave no
    # viva para siempre en Redis.
    PERDIDA: 30 * 24 * 3600,
    # La deuda envejece, pero la clave lleva el TRAMO (ver `_tramo`), así que
    # dentro del mismo tramo esto es lo que evita el recordatorio semanal.
    DEUDA: 14 * 24 * 3600,
    # El stock se mueve en horas. Tres días es aproximadamente un ciclo de
    # reposición; menos que eso vuelve a avisar por lo mismo que sigue igual.
    QUIEBRE: 3 * 24 * 3600,
    # Un cliente dormido sigue dormido mañana. Insistir cada semana sobre el
    # mismo cliente es exactamente el ruido que este módulo tiene que evitar.
    DORMIDO: 30 * 24 * 3600,
}

# El contador del día vive 36 h por el mismo motivo que `digest.MARCA_TTL_SEGUNDOS`:
# cubre el día entero más el corrimiento de cualquier zona, y se va solo.
TTL_PRESUPUESTO_SEGUNDOS = 36 * 3600

_PREFIJO = "plus-agent:consejo"

# LA FRASE EXACTA cuando el costo no se pudo verificar. Es una constante y no un
# literal suelto porque aparece en dos lados —el motivo por renglón y el cuerpo
# del consejo— y porque el punto entero del detector de pérdidas es que ésta sea
# la salida cuando no hay certeza, en vez de una cuenta hecha con un costo
# adivinado.
#
# TODO(idioma): clave `consejo.sin_costo`. `app/idioma.py` lo está editando otra
# sesión en paralelo, así que va como literal y con la clave anotada.
SIN_COSTO = "no pude verificar el costo"


# --------------------------------------------------------------- el consejo


@dataclass(frozen=True)
class Consejo:
    """Una cosa que decirle al dueño, con todo lo que hace falta para decirla.

    `cuerpo` es prosa DETERMINISTA, ya usable: si el modelo no está, falla o
    tarda, esto sale tal cual y sigue siendo cierto. El modelo recibe `datos` y
    `cuerpo` y sólo cambia la forma.

    `peso` ordena DENTRO de su clase y no entre clases: para una pérdida son
    pesos, para una deuda son pesos, para un quiebre son unidades que van a
    faltar y para un dormido son pesos estimados. Compararlo entre clases sería
    comparar unidades con plata; el ranking entre clases lo da `ORDEN_CLASES`.

    `supuesto` es lo que este consejo está dando por cierto sin poder probarlo
    —hoy sólo el stock, con `STOCK_CONFIABLE=false`—. Vacío significa que no
    hay nada supuesto, no que no se haya mirado.
    """

    clase: str
    clave: str
    sobre: str
    titulo: str
    cuerpo: str
    peso: float = 0.0
    supuesto: str = ""
    datos: dict = field(default_factory=dict)


def activo() -> bool:
    """El interruptor de despliegue, leído en cada llamada (no al importar).

    Apagado de fábrica: ver el docstring del módulo.
    """
    return os.getenv("CONSEJOS_ACTIVO", "false").strip().lower() in {
        "true",
        "1",
        "yes",
        "si",
        "sí",
    }


def destinatario() -> str:
    """El número del dueño, o "" si no se puede saber. Nunca levanta.

    Se importa acá adentro y no arriba porque `app/notificar.py` arrastra el
    router y el cliente de WhatsApp, y este módulo tiene que poder importarse
    (y testearse) sin nada de eso.
    """
    from app import notificar

    try:
        return notificar.telefono_dueno()
    except Exception as exc:  # pragma: no cover - telefono_dueno ya no levanta
        print(f"[consejos] no pude resolver el dueño ({type(exc).__name__})")
        return ""


def _ahora() -> datetime:
    """El reloj del negocio, CON respaldo: esto informa, no decide.

    Es el mismo criterio que `pendientes._zona` y `digest._zona`: un barrido que
    se muere por una zona mal escrita deja de avisar para siempre, que es peor
    que el default con un log al lado.
    """
    return reloj.ahora_con_respaldo("consejos")


def _float(valor: object) -> float:
    try:
        return float(valor)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _dia(valor: object) -> date | None:
    """Un `Date` de ERPNext (`YYYY-MM-DD` pelado) como fecha, o None.

    None NO es hoy y no es el año cero: una fecha ilegible tiene que dejar la
    fila afuera, no ponerla en un extremo.
    """
    crudo = str(valor or "").strip()[:10]
    if not crudo:
        return None
    try:
        return date.fromisoformat(crudo)
    except ValueError:
        return None


# ------------------------------------------------- el reclamo y el presupuesto
#
# El mismo `SET NX EX` que `digest.reclamar`, con su propio espacio de claves.
# Ver el docstring del módulo para por qué éste y no `marcas` ni `agenda`.

NUEVO = "nuevo"
REPETIDO = "repetido"
SIN_PRESUPUESTO = "sin_presupuesto"
SIN_REDIS = "sin_redis"

# Los dos que cortan el recorrido. `REPETIDO` no corta: el consejo siguiente
# puede ser nuevo. `SIN_PRESUPUESTO` y `SIN_REDIS` sí, y son cosas distintas —
# uno es «ya hablé bastante hoy» y el otro es «no puedo garantizar una sola
# vez»— pero las dos significan «no sigas mirando».
CORTAN = frozenset({SIN_PRESUPUESTO, SIN_REDIS})


def _clave_consejo(clave: str) -> str:
    return f"{_PREFIJO}:{clave}"


def _clave_presupuesto(dia: date) -> str:
    return f"{_PREFIJO}:presupuesto:{dia.isoformat()}"


def _clave_barrido() -> str:
    return f"{_PREFIJO}:barrido"


def restante(dia: date) -> int:
    """Cuántos consejos quedan hoy. **-1 cuando Redis no contesta.**

    -1 y no 0: son cosas distintas y el que llama tiene que poder distinguirlas.
    0 es «ya hablé tres veces hoy», -1 es «no sé cuántas veces hablé», y con lo
    segundo no se puede prometer «una sola vez». Las dos frenan, pero sólo una
    es normal.
    """
    try:
        crudo = locks.conexion().get(_clave_presupuesto(dia))
    except Exception as exc:
        print(f"[consejos] no pude leer el presupuesto del día ({type(exc).__name__})")
        return -1
    if isinstance(crudo, bytes):
        crudo = crudo.decode()
    try:
        usados = int(crudo or 0)
    except (TypeError, ValueError):
        print(f"[consejos] presupuesto del día ilegible ({crudo!r}); no aviso nada")
        return -1
    return max(0, MAX_CONSEJOS_POR_DIA - usados)


def reclamar(consejo: Consejo, dia: date) -> str:
    """Tomar este consejo para ESTE proceso. Devuelve por qué sí o por qué no.

    EL ORDEN DE LAS DOS OPERACIONES ES EL DISEÑO, no una preferencia:

      1. primero el `SET NX EX` del consejo. Es atómico, así que dos procesos no
         pueden mandar el mismo consejo aunque lleguen en el mismo milisegundo;
      2. y **sólo si ese SET ganó** se incrementa el contador del día. Al revés,
         un consejo repetido —que no se va a mandar— gastaría presupuesto, y
         tres repetidos dejarían al dueño sin enterarse de lo que sí es nuevo.

    El contador se toca con `INCR`, que también es atómico: con un `GET` + `SET`
    dos procesos leerían 2, escribirían 3 y mandarían cuatro consejos. Por eso
    el tope se comprueba DESPUÉS de incrementar, sobre el valor que devolvió el
    propio `INCR`: es la única lectura que no puede estar desactualizada.

    Si el incremento pasó el tope, el reclamo del consejo se SUELTA: ese consejo
    no se dijo, así que tiene que poder decirse mañana. El contador queda pasado
    de rosca y no se baja — ya está en el tope, y se va solo con su TTL.
    """
    try:
        cliente = locks.conexion()
        testigo = f"{dia.isoformat()}:{consejo.clase}"
        ttl = TTL_RECLAMO.get(consejo.clase, 24 * 3600)
        if not cliente.set(_clave_consejo(consejo.clave), testigo, nx=True, ex=ttl):
            return REPETIDO
        try:
            usados = int(cliente.incr(_clave_presupuesto(dia)))
            cliente.expire(_clave_presupuesto(dia), TTL_PRESUPUESTO_SEGUNDOS)
        except Exception:
            # El `SET NX` de arriba YA GANÓ: el consejo está reclamado. Si el
            # presupuesto falla acá y se vuelve con SIN_REDIS, `tick` no lo
            # devuelve —no es uno de los estados que mira— y el reclamo se queda
            # puesto hasta su TTL: el consejo figura como dicho sin que nadie lo
            # haya oído, y no vuelve a intentarse en 24 horas.
            #
            # Se suelta SÓLO el reclamo. El contador NO se baja: `INCR` puede
            # haber pasado y ser `expire` el que falló, y en ese caso un
            # decremento le regalaría presupuesto a un consejo que sí lo gastó.
            # De más en el contador se corrige solo con su TTL; de menos, no.
            devolver(consejo)
            raise
    except Exception as exc:
        print(f"[consejos] no pude reclamar {consejo.clave} ({type(exc).__name__})")
        return SIN_REDIS
    if usados > MAX_CONSEJOS_POR_DIA:
        devolver(consejo)
        return SIN_PRESUPUESTO
    return NUEVO


def devolver(consejo: Consejo) -> None:
    """Soltar el reclamo. **Sólo cuando NO salió nada.**

    Mismo papel que `digest.liberar`: un consejo que no llegó a decirse no puede
    quedar marcado como dicho. Best effort — si Redis no contesta, la clave vence
    sola con su TTL y el consejo vuelve más tarde.
    """
    try:
        locks.conexion().delete(_clave_consejo(consejo.clave))
    except Exception as exc:
        print(f"[consejos] no pude soltar {consejo.clave} ({type(exc).__name__})")


def _puedo_barrer() -> bool:
    """¿Le toca a este proceso barrer ERPNext? Un barrido por hora, a lo sumo.

    El mismo `SET NX EX`, acá como freno de consultas y no como
    exactamente-una-vez. Falla cerrado: sin Redis no se barre, que es coherente
    con que sin Redis tampoco se pueda reclamar nada.
    """
    try:
        return bool(
            locks.conexion().set(
                _clave_barrido(), "1", nx=True, ex=INTERVALO_BARRIDO_SEGUNDOS
            )
        )
    except Exception as exc:
        print(f"[consejos] no pude tomar el turno de barrido ({type(exc).__name__})")
        return False


# ----------------------------------------------------------------- 1. pérdidas
#
# Una venta CONFIRMADA con un renglón por debajo del costo.

VENTANA_PERDIDA_DIAS = 7
MAX_PEDIDOS_PERDIDA = 100
MAX_RENGLONES_POR_PEDIDO = 20
LOTE_PEDIDOS = 50
# Menos que esto es redondeo, no una pérdida. Un consejo por $0,40 es ruido.
UMBRAL_PERDIDA = 1.0


def lista_de_costo() -> str:
    """Contra QUÉ lista de precios se compara el costo. Vacío = no se compara.

    Se nombra explícitamente y no se adivina. `Item.valuation_rate` estaba a
    mano y es justo lo que no hay que usar acá: es un promedio móvil de
    valuación contable, cambia con cada recepción, está en la unidad de STOCK y
    no en la del renglón, y nadie lo cargó pensando «éste es mi costo». Un
    consejo que dice «perdiste plata» tiene que poder nombrar el número contra
    el que comparó, y el dueño tiene que poder mirarlo en ERPNext.

    Sin `CONSEJOS_LISTA_COSTO` este detector no dice nada — que es lo correcto:
    la alternativa es inventar un costo.
    """
    return os.getenv("CONSEJOS_LISTA_COSTO", "").strip()


def _costos(codigos: list[str], lista: str, moneda: str) -> list[dict]:
    """Los Item Price de compra de esos productos, crudos. Levanta si no lee."""
    if not codigos:
        return []
    return erpnext.policy_get_list(
        "Item Price",
        filters=[
            ["item_code", "in", codigos],
            ["buying", "=", 1],
            ["price_list", "=", lista],
            ["currency", "=", moneda],
        ],
        fields=[
            "item_code",
            "price_list_rate",
            "price_list",
            "currency",
            "uom",
            "valid_from",
            "valid_upto",
        ],
        limit=max(100, len(codigos) * 10),
    )


def _costo_de(
    precios: list[dict], code: str, uom: str, lista: str, moneda: str, dia: date
) -> float | None:
    """El costo de UNA unidad de `uom`, o None si no se puede verificar.

    LOS TRES CAMPOS SE VUELVEN A COMPROBAR ACÁ, uno por uno, y un precio al que
    le falte cualquiera se SALTEA. Es exactamente lo que hace
    `policy._precio_estandar`, y no es paranoia: un Item Price cargado sin
    `price_list`, sin `currency` o sin `uom` ya produjo dos veces en este
    proyecto un agente que no cotiza nada y no confirma nada, sin un error en
    ninguna parte. Acá el mismo agujero sería peor, porque no callaría: diría
    «perdiste $3.000» comparando el precio de un litro contra el costo de un
    cajón.

    **`uom` es la razón por la que la resta significa algo.** El renglón cobra
    `rate` por `uom`; sólo un costo en LA MISMA `uom` se le puede restar. Un
    costo por cajón contra un precio por litro no es un margen chico: es un
    número inventado con la forma de un margen.

    Empate entre dos precios vigentes: gana el MÁS BAJO. Es la dirección que no
    inventa una pérdida — con dos costos posibles, el consejo se calla antes de
    acusar.
    """
    candidatos: list[tuple[date, float]] = []
    for precio in precios:
        if str(precio.get("item_code") or "").strip() != code:
            continue
        if str(precio.get("price_list") or "").strip() != lista:
            continue
        if str(precio.get("currency") or "").strip() != moneda:
            continue
        if str(precio.get("uom") or "").strip() != uom:
            continue
        desde = _dia(precio.get("valid_from")) or date.min
        hasta = _dia(precio.get("valid_upto")) or date.max
        if not (desde <= dia <= hasta):
            continue
        tarifa = _float(precio.get("price_list_rate"))
        if tarifa <= 0:
            continue
        candidatos.append((desde, tarifa))
    if not candidatos:
        return None
    ultimo = max(desde for desde, _ in candidatos)
    return min(tarifa for desde, tarifa in candidatos if desde == ultimo)


def _renglones(pedidos: list[str]) -> list[dict]:
    """Los renglones emitidos de esos pedidos. Levanta si el techo se llena.

    Un techo lleno acá no se puede informar como «éstos son todos»: faltarían
    renglones, y un renglón que falta es una pérdida que no se vio. Se levanta y
    el barrido de la hora siguiente lo vuelve a intentar.
    """
    salida: list[dict] = []
    for inicio in range(0, len(pedidos), LOTE_PEDIDOS):
        lote = pedidos[inicio : inicio + LOTE_PEDIDOS]
        tope = len(lote) * MAX_RENGLONES_POR_PEDIDO
        filas = erpnext.policy_get_list(
            "Sales Order Item",
            filters=[["parent", "in", lote], ["docstatus", "=", 1]],
            fields=[
                "parent",
                "item_code",
                "item_name",
                "qty",
                "uom",
                "rate",
            ],
            limit=tope + 1,
            parent="Sales Order",
            order_by="parent asc",
        )
        if len(filas) > tope:
            raise erpnext.ERPNextError(
                "demasiados renglones para revisar el margen de un lote de pedidos"
            )
        salida.extend(filas)
    return salida


def perdidas(dia: date) -> list[Consejo]:
    """Ventas confirmadas de los últimos días con algún renglón bajo el costo.

    Nunca levanta: un error de lectura es «hoy no hay consejo de pérdidas», no
    un barrido caído.
    """
    lista = lista_de_costo()
    if not lista:
        print("[consejos] CONSEJOS_LISTA_COSTO vacío: no puedo verificar ningún costo")
        return []
    try:
        empresa = erpnext.default_company()
        desde = (dia - timedelta(days=VENTANA_PERDIDA_DIAS)).isoformat()
        pedidos = erpnext.policy_get_list(
            "Sales Order",
            filters=[
                ["company", "=", empresa],
                ["docstatus", "=", 1],
                ["transaction_date", ">=", desde],
            ],
            fields=[
                "name",
                "customer",
                "customer_name",
                "currency",
                "transaction_date",
            ],
            limit=MAX_PEDIDOS_PERDIDA + 1,
            # `desc` es lo que hace SEGURO truncar: lo que se pierde al cortar
            # es lo más viejo de la ventana, que el dueño ya facturó y ya vio.
            order_by="transaction_date desc",
        )
    except Exception as exc:
        print(f"[consejos] no pude leer las ventas confirmadas ({type(exc).__name__})")
        return []
    if len(pedidos) > MAX_PEDIDOS_PERDIDA:
        print(
            f"[consejos] más de {MAX_PEDIDOS_PERDIDA} ventas en la ventana: "
            "reviso las más nuevas"
        )
        pedidos = pedidos[:MAX_PEDIDOS_PERDIDA]
    por_nombre = {
        str(p.get("name") or "").strip(): p
        for p in pedidos
        if str(p.get("name") or "").strip()
    }
    if not por_nombre:
        return []

    try:
        filas = _renglones(sorted(por_nombre))
    except Exception as exc:
        print(f"[consejos] no pude leer los renglones ({type(exc).__name__})")
        return []

    codigos = sorted(
        {str(f.get("item_code") or "").strip() for f in filas} - {""}
    )
    monedas = sorted(
        {str(p.get("currency") or "").strip() for p in por_nombre.values()} - {""}
    )
    precios: list[dict] = []
    try:
        for moneda in monedas:
            precios.extend(_costos(codigos, lista, moneda))
    except Exception as exc:
        print(f"[consejos] no pude leer la lista de costo ({type(exc).__name__})")
        return []

    acumulado: dict[str, dict] = {}
    for f in filas:
        pedido = str(f.get("parent") or "").strip()
        cabecera = por_nombre.get(pedido)
        if cabecera is None:
            continue
        code = str(f.get("item_code") or "").strip()
        uom = str(f.get("uom") or "").strip()
        moneda = str(cabecera.get("currency") or "").strip()
        dia_pedido = _dia(cabecera.get("transaction_date")) or dia
        entrada = acumulado.setdefault(
            pedido,
            {
                "cabecera": cabecera,
                "renglones": [],
                "sin_costo": [],
                "perdida": 0.0,
            },
        )
        if not code or not uom or not moneda:
            entrada["sin_costo"].append(code or "(renglón sin producto)")
            continue
        costo = _costo_de(precios, code, uom, lista, moneda, dia_pedido)
        if costo is None:
            entrada["sin_costo"].append(code)
            continue
        rate = _float(f.get("rate"))
        qty = _float(f.get("qty"))
        if qty <= 0 or rate <= 0:
            continue
        diferencia = (costo - rate) * qty
        if diferencia < UMBRAL_PERDIDA:
            continue
        entrada["perdida"] += diferencia
        entrada["renglones"].append(
            {
                "item_code": code,
                "item_name": str(f.get("item_name") or code),
                "uom": uom,
                "qty": qty,
                "rate": rate,
                "costo": costo,
                "perdida": diferencia,
            }
        )

    salida: list[Consejo] = []
    for pedido, entrada in sorted(acumulado.items()):
        if not entrada["renglones"]:
            # Sin ningún renglón verificado por debajo del costo no hay consejo.
            # Ni siquiera cuando NO se pudo verificar nada: «no pude mirar» no
            # es una noticia, es ruido, y el que tiene que enterarse de que
            # falta cargar la lista de costo es el que despliega, por el log.
            continue
        cabecera = entrada["cabecera"]
        cliente = str(cabecera.get("customer_name") or cabecera.get("customer") or "")
        detalle = "\n".join(
            f"· {r['item_name']} — {r['qty']:g} {r['uom']} a {pesos(r['rate'], 2)} "
            f"y cuesta {pesos(r['costo'], 2)} — {pesos(r['perdida'], 2)}"
            for r in entrada["renglones"]
        )
        cuerpo = (
            # TODO(idioma): clave `consejo.perdida.cuerpo`.
            f"El pedido {pedido} de {cliente} salió por debajo del costo: "
            f"{pesos(entrada['perdida'], 2)} contra la lista «{lista}».\n{detalle}"
        )
        if entrada["sin_costo"]:
            faltantes = ", ".join(sorted(set(entrada["sin_costo"])))
            cuerpo += f"\n({SIN_COSTO} de: {faltantes}, así que la pérdida es un piso)"
        salida.append(
            Consejo(
                clase=PERDIDA,
                clave=f"{PERDIDA}:{pedido}",
                sobre=pedido,
                # TODO(idioma): clave `consejo.perdida.titulo`.
                titulo="Una venta por debajo del costo",
                cuerpo=cuerpo,
                peso=float(entrada["perdida"]),
                datos={
                    "pedido": pedido,
                    "cliente": cliente,
                    "lista_costo": lista,
                    "moneda": str(cabecera.get("currency") or ""),
                    "perdida": float(entrada["perdida"]),
                    "renglones": entrada["renglones"],
                    "sin_costo": sorted(set(entrada["sin_costo"])),
                },
            )
        )
    return salida


# ------------------------------------------------------------- 2. dormidos
#
# Un cliente que compraba seguido y hace rato que no.

VENTANA_HISTORIA_DIAS = 180
MAX_PEDIDOS_HISTORIA = 500
# Cuatro pedidos son TRES intervalos, que es el mínimo con el que una mediana
# dice algo. Con dos pedidos hay un solo intervalo y «su ritmo» sería ese
# intervalo: cualquiera que compró dos veces quedaría «dormido» a la tercera
# semana. Un cliente con menos que esto no tiene ritmo propio y no se juzga.
MIN_PEDIDOS_PARA_RITMO = 4
# El silencio tiene que ser DE ÉL: 2,5 veces su propio intervalo típico. Un
# panadero que compra todos los días y uno que compra cada quince no se miden
# con el mismo número, que es lo que pidió el brief y lo que una constante
# global haría imposible.
FACTOR_DORMIDO = 2.5
# Y nunca por menos de una semana, por chico que sea su intervalo: un cliente
# diario que faltó dos días está de vacaciones, no perdido.
MIN_DIAS_DORMIDO = 7


def _ritmo(fechas: list[date]) -> float | None:
    """La MEDIANA de los días entre pedidos, o None si no alcanza la historia.

    Mediana y no promedio: un cliente que compró todos los días de marzo y una
    vez en agosto tiene un promedio que no describe a nadie. La mediana ignora
    ese salto, que es justo el dato que después se mide contra ella.
    """
    unicas = sorted(set(fechas))
    if len(unicas) < MIN_PEDIDOS_PARA_RITMO:
        return None
    huecos = [(b - a).days for a, b in pairwise(unicas) if (b - a).days > 0]
    if len(huecos) < MIN_PEDIDOS_PARA_RITMO - 1:
        return None
    mediana = float(statistics.median(huecos))
    return mediana if mediana > 0 else None


def dormidos(dia: date) -> list[Consejo]:
    """Clientes con ritmo propio que dejaron de comprar. Nunca levanta."""
    try:
        empresa = erpnext.default_company()
        desde = (dia - timedelta(days=VENTANA_HISTORIA_DIAS)).isoformat()
        pedidos = erpnext.policy_get_list(
            "Sales Order",
            filters=[
                ["company", "=", empresa],
                ["docstatus", "=", 1],
                ["transaction_date", ">=", desde],
            ],
            fields=[
                "name",
                "customer",
                "customer_name",
                "transaction_date",
                "grand_total",
            ],
            limit=MAX_PEDIDOS_HISTORIA + 1,
            # `desc` otra vez, y acá el argumento es más fuerte: truncar pierde
            # lo VIEJO, así que el pedido MÁS NUEVO de cada cliente siempre está
            # en la página. Con `asc`, truncar podría esconder la compra de ayer
            # y este detector diría «dejó de comprar» de un cliente que compró
            # ayer — el único error que este consejo no puede cometer.
            order_by="transaction_date desc",
        )
    except Exception as exc:
        print(f"[consejos] no pude leer la historia de pedidos ({type(exc).__name__})")
        return []
    if len(pedidos) > MAX_PEDIDOS_HISTORIA:
        print(
            f"[consejos] más de {MAX_PEDIDOS_HISTORIA} pedidos en la ventana: "
            "el ritmo de los clientes viejos sale de menos historia"
        )
        pedidos = pedidos[:MAX_PEDIDOS_HISTORIA]

    historia: dict[str, dict] = {}
    for p in pedidos:
        cliente = str(p.get("customer") or "").strip()
        fecha = _dia(p.get("transaction_date"))
        if not cliente or fecha is None or fecha > dia:
            continue
        entrada = historia.setdefault(
            cliente,
            {"nombre": str(p.get("customer_name") or cliente), "fechas": [], "totales": []},
        )
        entrada["fechas"].append(fecha)
        entrada["totales"].append(_float(p.get("grand_total")))

    salida: list[Consejo] = []
    for cliente, entrada in sorted(historia.items()):
        ritmo = _ritmo(entrada["fechas"])
        if ritmo is None:
            continue
        ultimo = max(entrada["fechas"])
        silencio = (dia - ultimo).days
        umbral = max(float(MIN_DIAS_DORMIDO), FACTOR_DORMIDO * ritmo)
        if silencio < umbral:
            continue
        promedio = sum(entrada["totales"]) / len(entrada["totales"])
        faltantes = int(silencio // ritmo)
        estimado = promedio * faltantes
        nombre = entrada["nombre"]
        salida.append(
            Consejo(
                clase=DORMIDO,
                clave=f"{DORMIDO}:{cliente}",
                sobre=cliente,
                # TODO(idioma): clave `consejo.dormido.titulo`.
                titulo="Un cliente dejó de comprar",
                # TODO(idioma): clave `consejo.dormido.cuerpo`.
                cuerpo=(
                    f"{nombre} compraba cada {ritmo:g} días y hace {silencio} que no "
                    f"pide (último: {ultimo.isoformat()}). Son unos {faltantes} pedidos "
                    f"de menos, cerca de {pesos(estimado)} a su promedio de "
                    f"{pesos(promedio)}."
                ),
                peso=float(estimado),
                datos={
                    "cliente": cliente,
                    "nombre": nombre,
                    "ritmo_dias": ritmo,
                    "silencio_dias": silencio,
                    "ultimo_pedido": ultimo.isoformat(),
                    "pedidos_en_ventana": len(entrada["fechas"]),
                    "promedio": promedio,
                    "estimado": estimado,
                },
            )
        )
    return salida


# ---------------------------------------------------------------- 3. deudas
#
# Una factura vencida hace más de lo que el dueño tolera.

DEUDA_DIAS_DEFAULT = 30.0
# Los tramos en que envejece una deuda. La clave del reclamo lleva el tramo, así
# que el dueño escucha UNA vez por cada empeoramiento real y no una vez por
# semana por lo mismo: a los 30 días, a los 60, a los 90, a los 180 y al año.
TRAMOS_DEUDA = (30, 60, 90, 180, 360)


def dias_tolerados() -> float:
    """Cuántos días de atraso tolera el dueño antes de querer enterarse.

    TODO(limites): esto tendría que ser una fila de `app/limites.py`
    (`CONSEJOS_DEUDA_DIAS`), así el dueño lo cambia por WhatsApp con su código de
    cuatro dígitos como cambia todo lo demás. Va por entorno mientras ese
    archivo lo edita otra sesión. Un valor ilegible o <= 0 APAGA el detector: no
    hay un default «razonable» que no sea una decisión del dueño.
    """
    try:
        dias = float(os.getenv("CONSEJOS_DEUDA_DIAS", str(DEUDA_DIAS_DEFAULT)))
    except (TypeError, ValueError):
        print("[consejos] CONSEJOS_DEUDA_DIAS no es un número de días")
        return 0.0
    return dias if dias > 0 else 0.0


def deuda_minima() -> float:
    """El piso en plata. Una deuda de $12 vencida no vale un mensaje."""
    try:
        return max(0.0, float(os.getenv("CONSEJOS_DEUDA_MINIMA", "0")))
    except (TypeError, ValueError):
        return 0.0


def _tramo(dias: int) -> int:
    """El escalón de antigüedad en el que cae este atraso."""
    elegido = TRAMOS_DEUDA[0]
    for corte in TRAMOS_DEUDA:
        if dias >= corte:
            elegido = corte
    return elegido


def deudas(dia: date) -> list[Consejo]:
    """Clientes con saldo vencido hace más de lo tolerado. Nunca levanta.

    Lee el MISMO reporte que `policy._saldo_vencido` (`Accounts Receivable`,
    `based_on="Due Date"`), con la misma postura: una fila sin fecha de
    vencimiento es un reporte que no es el que creemos, y entonces no se informa
    NADA. Media respuesta de un reporte de cobranzas es peor que ninguna.
    """
    tolerancia = dias_tolerados()
    if tolerancia <= 0:
        return []
    piso = deuda_minima()
    try:
        filas = erpnext.policy_run_report(
            "Accounts Receivable",
            {
                "company": erpnext.default_company(),
                "based_on": "Due Date",
                "report_date": dia.isoformat(),
            },
        )
    except Exception as exc:
        print(f"[consejos] no pude leer las cobranzas ({type(exc).__name__})")
        return []

    por_cliente: dict[str, dict] = {}
    for fila in filas:
        if not isinstance(fila, dict):
            continue
        monto = _float(fila.get("outstanding_amount"))
        if monto <= 0:
            continue
        vence = _dia(fila.get("due_date"))
        if vence is None:
            print("[consejos] el reporte de cobranzas trajo una fila sin vencimiento")
            return []
        atraso = (dia - vence).days
        if atraso <= tolerancia:
            continue
        cliente = str(fila.get("party") or fila.get("customer") or "").strip()
        if not cliente:
            continue
        entrada = por_cliente.setdefault(
            cliente,
            {
                "nombre": str(fila.get("customer_name") or cliente),
                "total": 0.0,
                "atraso": 0,
                "facturas": 0,
            },
        )
        entrada["total"] += monto
        entrada["atraso"] = max(entrada["atraso"], atraso)
        entrada["facturas"] += 1

    salida: list[Consejo] = []
    for cliente, entrada in sorted(por_cliente.items()):
        if entrada["total"] < piso:
            continue
        tramo = _tramo(entrada["atraso"])
        salida.append(
            Consejo(
                clase=DEUDA,
                clave=f"{DEUDA}:{cliente}:{tramo}",
                sobre=cliente,
                # TODO(idioma): clave `consejo.deuda.titulo`.
                titulo="Deuda que está envejeciendo",
                # TODO(idioma): clave `consejo.deuda.cuerpo`.
                cuerpo=(
                    f"{entrada['nombre']} debe {pesos(entrada['total'])} en "
                    f"{entrada['facturas']} factura(s), la más vieja vencida hace "
                    f"{entrada['atraso']} días (tolerás {tolerancia:g})."
                ),
                peso=float(entrada["total"]),
                datos={
                    "cliente": cliente,
                    "nombre": entrada["nombre"],
                    "total": entrada["total"],
                    "atraso_dias": entrada["atraso"],
                    "facturas": entrada["facturas"],
                    "tolerancia_dias": tolerancia,
                    "tramo": tramo,
                },
            )
        )
    return salida


# --------------------------------------------------------------- 4. quiebres
#
# Un producto que cruza su mínimo antes del próximo día de reparto.

MAX_ITEMS_REORDEN = 200
VENTANA_DEMANDA_DIAS = 30


def _proximo_reparto(dia: date) -> date | None:
    """El próximo día de reparto DESPUÉS de hoy, o None si no hay reglas.

    Usa `excepciones._proxima_fecha`, que es la única implementación del «próximo
    día configurado» del repo. Es privada y se usa igual, a propósito: escribir
    un segundo `for adelanto in range(...)` acá sería la segunda definición de
    «cuándo sale el camión», y el repo ya pagó tres veces por tener un concepto
    escrito dos veces (ver el docstring de `app/reloj.py`). `autonomia.py` ya
    lee un privado de `pendientes` por el mismo motivo.

    `desde=1` y no 0: el reparto de HOY ya salió o está por salir, así que el
    stock que importa es el que tiene que llegar al siguiente.
    """
    from app import excepciones

    dias = excepciones.dias_reparto()
    if not dias:
        print("[consejos] no hay días de reparto configurados: no proyecto stock")
        return None
    return _dia(excepciones._proxima_fecha(dias, dia, desde=1))


def quiebres(dia: date) -> list[Consejo]:
    """Productos que no llegan al próximo reparto. Nunca levanta.

    LA POSTURA DE STOCK SE RESPETA Y SE DICE. `inventario.confiable` se llama
    **sin** `ignorar_postura`, así que con `STOCK_CONFIABLE=false` —la postura de
    lanzamiento— contesta «el inventario está marcado como no confiable» para
    todos los productos. Eso NO apaga el consejo: apagarlo sería no avisarle
    nunca al dueño que se queda sin leche. Lo que hace es llenar `supuesto` con
    el motivo y escribirlo en el cuerpo, así que el consejo dice en voz alta qué
    está dando por cierto. Un consejo es una opinión sobre lo que hay que mirar;
    una promesa de stock a un cliente es otra cosa y sigue prohibida donde
    siempre (`policy`, `tools/catalogo`).
    """
    proximo = _proximo_reparto(dia)
    if proximo is None:
        return []
    dias_hasta = max(1, (proximo - dia).days)
    try:
        deposito = erpnext.default_warehouse()
        empresa = erpnext.default_company()
        reorden = erpnext.policy_get_list(
            "Item Reorder",
            filters=[["warehouse", "=", deposito]],
            fields=["parent", "warehouse", "warehouse_reorder_level"],
            limit=MAX_ITEMS_REORDEN + 1,
            parent="Item",
        )
    except Exception as exc:
        print(f"[consejos] no pude leer los mínimos de reposición ({type(exc).__name__})")
        return []
    if len(reorden) > MAX_ITEMS_REORDEN:
        print(f"[consejos] más de {MAX_ITEMS_REORDEN} mínimos configurados: reviso los primeros")
        reorden = reorden[:MAX_ITEMS_REORDEN]
    niveles: dict[str, float] = {}
    for fila in reorden:
        if str(fila.get("warehouse") or "").strip() != deposito:
            continue
        code = str(fila.get("parent") or "").strip()
        nivel = _float(fila.get("warehouse_reorder_level"))
        if code and nivel > 0:
            niveles[code] = nivel
    if not niveles:
        return []

    try:
        demanda = _demanda_diaria(sorted(niveles), empresa, deposito, dia)
        bins = erpnext.policy_get_list(
            "Bin",
            filters=[["warehouse", "=", deposito], ["item_code", "in", sorted(niveles)]],
            fields=["item_code", "warehouse", "actual_qty", "reserved_qty"],
            limit=len(niveles) * 2,
        )
    except Exception as exc:
        print(f"[consejos] no pude proyectar el stock ({type(exc).__name__})")
        return []

    disponible: dict[str, float] = {}
    for fila in bins:
        if str(fila.get("warehouse") or "").strip() != deposito:
            continue
        code = str(fila.get("item_code") or "").strip()
        if code not in niveles:
            continue
        disponible[code] = disponible.get(code, 0.0) + (
            _float(fila.get("actual_qty")) - _float(fila.get("reserved_qty"))
        )

    salida: list[Consejo] = []
    for code in sorted(niveles):
        por_dia = demanda.get(code, 0.0)
        if por_dia <= 0:
            # Sin ventas en la ventana no hay nada que cruce nada. Un producto
            # que no se vende y está bajo el mínimo es una decisión de compras,
            # no una urgencia que justifique un mensaje.
            continue
        hay = disponible.get(code)
        if hay is None:
            continue
        nivel = niveles[code]
        proyectado = hay - por_dia * dias_hasta
        if proyectado > nivel:
            continue
        fresco, motivo = inventario.confiable(code, deposito)
        supuesto = "" if fresco else motivo
        ya = hay <= nivel
        cuerpo = (
            # TODO(idioma): clave `consejo.quiebre.cuerpo`.
            f"{code}: quedan {hay:g} y el mínimo es {nivel:g}. "
            f"Se venden {por_dia:.1f} por día y el próximo reparto es el "
            f"{proximo.isoformat()} ({dias_hasta} día(s)): llegás con {proyectado:.1f}."
        )
        if ya:
            cuerpo += " Ya está por debajo del mínimo."
        if supuesto:
            # TODO(idioma): clave `consejo.quiebre.supuesto`.
            cuerpo += f"\n(Asumo el stock del sistema: {supuesto}.)"
        salida.append(
            Consejo(
                clase=QUIEBRE,
                clave=f"{QUIEBRE}:{code}:{deposito}",
                sobre=code,
                # TODO(idioma): clave `consejo.quiebre.titulo`.
                titulo="Un producto no llega al próximo reparto",
                cuerpo=cuerpo,
                peso=float(nivel - proyectado),
                supuesto=supuesto,
                datos={
                    "item_code": code,
                    "deposito": deposito,
                    "disponible": hay,
                    "minimo": nivel,
                    "demanda_diaria": por_dia,
                    "proximo_reparto": proximo.isoformat(),
                    "dias_hasta_reparto": dias_hasta,
                    "proyectado": proyectado,
                    "ya_bajo_minimo": ya,
                },
            )
        )
    return salida


def _demanda_diaria(
    codigos: list[str], empresa: str, deposito: str, dia: date
) -> dict[str, float]:
    """Unidades por día de cada producto, de las ventas CONFIRMADAS recientes.

    En unidad de STOCK (`stock_qty`), que es la unidad en la que están tanto el
    `Bin` como el mínimo de reposición. Restar una demanda en cajones de un
    stock en litros daría una proyección con la forma correcta y el número
    equivocado — la misma trampa de `uom` que `_costo_de` describe, del otro
    lado del módulo.
    """
    desde = (dia - timedelta(days=VENTANA_DEMANDA_DIAS)).isoformat()
    pedidos = erpnext.policy_get_list(
        "Sales Order",
        filters=[
            ["company", "=", empresa],
            ["docstatus", "=", 1],
            ["transaction_date", ">=", desde],
        ],
        fields=["name"],
        limit=MAX_PEDIDOS_HISTORIA + 1,
        order_by="transaction_date desc",
    )
    nombres = [str(p.get("name") or "").strip() for p in pedidos]
    nombres = [n for n in nombres if n][:MAX_PEDIDOS_HISTORIA]
    if not nombres:
        return {}
    vendido: dict[str, float] = {}
    for inicio in range(0, len(nombres), LOTE_PEDIDOS):
        lote = nombres[inicio : inicio + LOTE_PEDIDOS]
        filas = erpnext.policy_get_list(
            "Sales Order Item",
            filters=[
                ["parent", "in", lote],
                ["item_code", "in", codigos],
                ["warehouse", "=", deposito],
                ["docstatus", "=", 1],
            ],
            fields=["parent", "item_code", "warehouse", "stock_qty"],
            limit=len(lote) * MAX_RENGLONES_POR_PEDIDO,
            parent="Sales Order",
        )
        for fila in filas:
            if str(fila.get("warehouse") or "").strip() != deposito:
                continue
            code = str(fila.get("item_code") or "").strip()
            if code not in set(codigos):
                continue
            vendido[code] = vendido.get(code, 0.0) + _float(fila.get("stock_qty"))
    return {code: total / VENTANA_DEMANDA_DIAS for code, total in vendido.items()}


# ------------------------------------------------------------ la composición

# Los cuatro, en el orden en que se declaran. Un detector nuevo es una entrada
# acá y una clase en `ORDEN_CLASES`: nada más.
DETECTORES = (perdidas, deudas, quiebres, dormidos)


def detectar(dia: date) -> list[Consejo]:
    """Todo lo que hay para decir ese día, ya ordenado. Nunca levanta.

    Cada detector va en su PROPIO try/except, igual que las tres llamadas del
    barrido de `app/main.py`: que las cobranzas no contesten no puede tapar una
    venta bajo el costo.
    """
    encontrados: list[Consejo] = []
    for detector in DETECTORES:
        try:
            encontrados.extend(detector(dia))
        except Exception as exc:
            print(f"[consejos] {detector.__name__} falló ({type(exc).__name__}: {exc})")
    return ordenar(encontrados)


def ordenar(consejos: list[Consejo]) -> list[Consejo]:
    """Primero la clase, después el peso dentro de la clase, después la clave.

    La clave desempata para que el orden sea total y estable: dos consejos con
    el mismo peso tienen que salir siempre en el mismo orden, o el que entra en
    el presupuesto del día depende de en qué orden los devolvió ERPNext.
    """

    def rango(c: Consejo) -> tuple[int, float, str]:
        clase = ORDEN_CLASES.index(c.clase) if c.clase in ORDEN_CLASES else len(ORDEN_CLASES)
        return (clase, -c.peso, c.clave)

    return sorted(consejos, key=rango)


def tick(ahora: datetime | None = None) -> list[Consejo]:
    """Los consejos que hay que decirle al dueño AHORA, ya reclamados.

    Devuelve `Consejo`s, no texto y no mensajes: quien llame los redacta (con el
    modelo o con `cuerpo` tal cual) y los manda con `notificar.avisar_dueno`.
    Cada uno que vuelve de acá ya está reclamado, así que **si el envío falla
    hay que devolverlo** (`devolver`), igual que `digest.liberar`.

    El orden de los frenos es de más barato a más caro, y ninguno es decorativo:

      1. el interruptor;
      2. quién es el dueño — sin destinatario no hay consejo que dar;
      3. las horas de silencio, con el momento PASADO (nunca leído acá);
      4. el presupuesto del día, que evita barrer ERPNext cuando ya se habló;
      5. el turno de barrido, que evita barrerlo 60 veces por hora;
      6. recién ahí se lee ERPNext.

    `dia` sale de `momento` UNA vez y lo consumen `detectar` y `reclamar`. Son
    dos consumidores del mismo valor derivado, que es la forma exacta del bug
    que este repo ya se comió una vez (`digest.enviar` derivaba `dia` y se lo
    pasaba a dos callees); `tests/test_consejos.py` muta cada consumidor por
    separado.
    """
    if not activo():
        return []
    momento = ahora if ahora is not None else _ahora()
    if not destinatario():
        print("[consejos] no sé quién es el dueño: no aviso nada")
        return []

    from app import pendientes

    if pendientes.en_silencio(momento):
        return []
    dia = momento.date()
    if restante(dia) <= 0:
        return []
    if not _puedo_barrer():
        return []

    salida: list[Consejo] = []
    for consejo in detectar(dia):
        resultado = reclamar(consejo, dia)
        if resultado == NUEVO:
            salida.append(consejo)
            continue
        if resultado in CORTAN:
            break
    return salida
