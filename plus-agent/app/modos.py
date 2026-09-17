"""Cómo trabaja ESTE negocio, en una sola respuesta en vez de siete ajustes.

EL PROBLEMA, MEDIDO
`policy._evaluar` tiene 38 motivos distintos para no auto-confirmar. Casi todos
protegen plata de verdad y no sobran. Lo que sobra es cómo se los encuentra: de
a uno, en producción, un pedido por vez. Un despliegue entero del 17/09 se fue
en eso — primero un permiso de ERPNext, después otro, después un techo de $5 que
el `.env` no podía cambiar, después un conteo vencido. Cada uno correcto, cada
uno invisible hasta que un cliente pidió algo.

Y la asimetría es lo peor: «los confirmo yo todos» es UN ajuste
(`AUTO_CONFIRM_MAX=0`). «Tomá el pedido y confirmalo» son SIETE, y cada uno de
los siete, sin poner, bloquea el total sin decir nada:

    STOCK_CONFIABLE                     sin poner -> se frena todo
    AUTO_CONFIRM_MAX                    sin poner -> se frena todo
    AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO   en 0 SIGNIFICA BLOQUEADO, no «sin tope»
    AUTO_CONFIRM_MAX_CLIENTE_NUEVO      ídem
    AUTO_CONFIRM_PRICE_LIST             sin poner -> se frena todo
    AUTO_CONFIRM_CURRENCY               sin poner -> se frena todo
    zonas de reparto                    sin poner -> se frena todo

Y el nombre que más confunde: `STOCK_CONFIABLE=false` NO quiere decir «no mires
el stock». Quiere decir «frená todos los pedidos». La postura de lanzamiento
apaga la automatización entera, y ningún texto lo dice.

QUÉ ES ESTO Y QUÉ NO
Esto NO ESCRIBE NADA. Es la pregunta que no se podía hacer: «con lo que hay
configurado ahora mismo, ¿se confirmaría algo solo, y si no, qué lo frena?».
Hoy esa pregunta sólo se contesta mandando un pedido de verdad.

Separado a propósito de `limites.py`, que sabe de ajustes de a uno y no tiene
por qué saber que siete de ellos son una decisión sola. Y separado de
`readiness.py`, que valida que la configuración sea VÁLIDA — otra pregunta: un
`.env` impecable puede no confirmar un solo pedido nunca, y hoy sale en verde.

NO ES `policy.evaluar`. Esto mira la CONFIGURACIÓN; `evaluar` mira UN PEDIDO
contra ERPNext en vivo. Los dos pueden frenar un pedido y son cosas distintas:
acá se contesta antes de que exista un cliente, y por eso no se puede pedir
prestada la lista de motivos de allá — la mitad de esos motivos necesitan un
pedido. Lo que sí se comparte es el nombre de la variable, no una copia del
texto: si `limites` le cambia el nombre a un ajuste, esto se rompe fuerte y no
en silencio.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app import limites

# Los ajustes que, sin poner o en 0, frenan TODOS los pedidos. No es la lista de
# los 38 motivos de policy: es la de los que dependen sólo de la configuración y
# no de qué pidió el cliente. Un pedido puede frenarse igual por deuda o por
# stock; eso es sobre ESE pedido y se ve en su tarjeta.
#
# El texto dice qué pasa SI FALTA, no qué significa el ajuste: el dueño que lee
# esto ya sabe qué quiso configurar, lo que no sabe es por qué no pasa nada.
FRENOS_DE_CONFIGURACION: tuple[tuple[str, str], ...] = (
    (
        "AUTO_CONFIRM_MAX",
        "el tope está en 0, así que NINGÚN pedido se confirma solo",
    ),
    (
        "AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO",
        "sin cantidad máxima por producto no se confirma nada: un 0 acá "
        "significa BLOQUEADO, no «sin límite»",
    ),
    (
        "AUTO_CONFIRM_MAX_CLIENTE_NUEVO",
        "sin tope para clientes nuevos, ningún cliente nuevo pasa",
    ),
    (
        "AUTO_CONFIRM_PRICE_LIST",
        "sin lista de precios autorizada no se puede verificar un solo precio",
    ),
    (
        "AUTO_CONFIRM_CURRENCY",
        "sin moneda autorizada no se puede verificar un solo precio",
    ),
)

# El interruptor de stock, aparte porque es un sí/no y porque su nombre engaña.
FRENO_STOCK = (
    "STOCK_CONFIABLE",
    "en «no» se frena TODO pedido, no sólo los que tienen poco stock: el "
    "nombre suena a «no mires el stock» y quiere decir «que no pase nada»",
)


@dataclass(frozen=True)
class Modo:
    """Una forma de trabajar, con los ajustes que la definen."""

    clave: str
    titulo: str
    explicacion: str
    # Qué tiene que valer cada ajuste. `None` = «lo elige el dueño, pero tiene
    # que estar puesto»: un tope es un número del negocio y ningún modo puede
    # inventarlo. Por eso un modo describe y no rellena.
    ajustes: dict[str, str | None] = field(default_factory=dict)


MODOS: tuple[Modo, ...] = (
    Modo(
        clave="reviso_todo",
        titulo="Reviso todos los pedidos",
        explicacion=(
            "El agente toma el pedido y lo deja en borrador. Nada se confirma "
            "hasta que una persona lo toca. Es la postura de lanzamiento."
        ),
        ajustes={"AUTO_CONFIRM_MAX": "0"},
    ),
    Modo(
        clave="confirmo_solo",
        titulo="Que se confirmen solos, yo anulo si hace falta",
        explicacion=(
            "El agente toma el pedido, lo confirma y avisa. Si después no hay "
            "tanto, se anula con `cancelar` dentro de las 24 h. Para esto los "
            "productos que no se cuentan a diario van marcados sin seguimiento "
            "en ERPNext (`is_stock_item = 0`), o cada pedido se frena igual "
            "esperando un conteo que nadie va a hacer."
        ),
        ajustes={
            "STOCK_CONFIABLE": "true",
            "AUTO_CONFIRM_MAX": None,
            "AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO": None,
            "AUTO_CONFIRM_MAX_CLIENTE_NUEVO": None,
            "AUTO_CONFIRM_PRICE_LIST": None,
            "AUTO_CONFIRM_CURRENCY": None,
        },
    ),
)


@dataclass
class Freno:
    """Un ajuste que hoy está frenando todo, con el nombre que ve el dueño."""

    nombre: str
    consecuencia: str
    valor: str
    origen: str


@dataclass
class Diagnostico:
    """Qué está pasando con la configuración de auto-confirmación, hoy."""

    modo: str
    frenos: list[Freno] = field(default_factory=list)
    problema: str = ""

    @property
    def confirma_algo(self) -> bool:
        """¿Puede confirmarse ALGÚN pedido con esta configuración?

        No promete que un pedido concreto pase: `policy.evaluar` lo mira contra
        ERPNext y tiene sus propios motivos. Promete lo contrario, que es lo
        útil — con un freno puesto, NINGUNO pasa, y probar con un pedido de
        verdad es tiempo perdido.
        """
        return not self.frenos and not self.problema


def _es_cero_o_vacio(valor: str) -> bool:
    """¿Este valor apaga el ajuste? Un 0 y un vacío apagan igual."""
    crudo = str(valor or "").strip()
    if not crudo:
        return True
    try:
        return float(crudo.replace(".", "").replace(",", ".")) <= 0
    except ValueError:
        # Un texto que no es número —el nombre de una lista de precios— apaga
        # sólo si está vacío, y eso ya se contestó arriba.
        return False


def diagnosticar() -> Diagnostico:
    """Qué frena la auto-confirmación AHORA, sin mandar un pedido de prueba.

    Nunca levanta: si no se puede leer el almacén del dueño se devuelve un
    `Diagnostico` con `problema`, no una excepción. Lo llaman un preflight y un
    panel, y los dos prefieren decir «no pude mirar» a caerse — pero
    `confirma_algo` es False en ese caso, porque no haber podido mirar no es
    una autorización.
    """
    try:
        filas = {f["nombre"]: f for f in limites.resumen()}
    except limites.LimiteError as exc:
        return Diagnostico(modo="", problema=limites.motivo(exc))

    frenos: list[Freno] = []

    nombre_stock, consecuencia_stock = FRENO_STOCK
    fila = filas.get(nombre_stock)
    if fila is not None and str(fila.get("valor") or "").strip().lower() != "true":
        frenos.append(
            Freno(
                nombre=nombre_stock,
                consecuencia=consecuencia_stock,
                valor=str(fila.get("valor") or ""),
                origen=str(fila.get("origen") or ""),
            )
        )

    for nombre, consecuencia in FRENOS_DE_CONFIGURACION:
        fila = filas.get(nombre)
        if fila is None:
            continue
        valor = str(fila.get("valor") or "")
        if _es_cero_o_vacio(valor):
            frenos.append(
                Freno(
                    nombre=nombre,
                    consecuencia=consecuencia,
                    valor=valor,
                    origen=str(fila.get("origen") or ""),
                )
            )

    return Diagnostico(modo=_modo_actual(filas), frenos=frenos)


def _modo_actual(filas: dict[str, dict]) -> str:
    """En qué modo está, o "" si no coincide con ninguno.

    `None` en `ajustes` quiere decir «puesto, el valor lo elige el dueño», así
    que se comprueba que NO esté apagado en vez de compararlo con algo. Un modo
    que exigiera un número concreto no describiría a ningún negocio real.
    """
    for modo in MODOS:
        for nombre, esperado in modo.ajustes.items():
            fila = filas.get(nombre)
            if fila is None:
                break
            valor = str(fila.get("valor") or "").strip()
            if esperado is None:
                if _es_cero_o_vacio(valor):
                    break
            elif valor.lower() != esperado.lower():
                break
        else:
            return modo.clave
    return ""
