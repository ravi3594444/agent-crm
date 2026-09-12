"""El registro de los marcadores durables, y el único lector que los lee.

POR QUÉ EXISTE (hallazgo 4 de `docs/AUDITORIA.md`, issue #24)
Un marcador durable es un texto que este sistema escribe en ERPNext para
entenderse CONSIGO MISMO entre reinicios: «a este pedido ya le avisé», «este
límite alguna vez se configuró», «este remito lo preparé yo». No es prosa para
una persona: es el formato con el que el barrido de mañana reconoce lo que
escribió el de hoy.

Había diez de esos textos entre corchetes más dos en prosa, cada uno con su
consulta y su parseo, y NINGÚN lector compartido. Lo que puso a esto en la
lista de la auditoría no fue el costo de cambiarlo: fue que el radio de
explosión medido era CERO. Se le cambiaba el texto a `pendientes.MARCA_AVISO`
y no se rompía un solo test. La razón está a la vista en la suite: cada test
escribe la marca con la MISMA constante con la que después la afirma
(`assert any(t.startswith(pendientes.MARCA_AVISO) ...)`), así que renombrarla
mueve las dos mitades juntas y nada puede discrepar. Es el mismo defecto que
la auditoría le encontró al reloj del lado de los tests: seis archivos que
re-derivaron la suposición del código y por eso no podían contradecirla.

Radio 0 acá no es «bajo acoplamiento». Es que la parte del contrato que
sobrevive al proceso no estaba afirmada en ninguna parte. Un marcador
renombrado significa que el barrido de mañana no ve los avisos de hoy y le
vuelve a escribir al cliente — o no cierra lo que había que cerrar. Es la
lección de #9 y de la auditoría estática de #18: un guard que no se puede
romper no está guardando.

QUÉ HACE ESTE MÓDULO, Y QUÉ NO
Hace dos cosas:

  * **El registro.** Una fila por marcador con su TEXTO durable, el doctype
    donde vive, cómo se lee, el techo de cuántos leer y POR QUÉ ese techo.
    `tests/test_marcas.py` fija los textos contra una tabla escrita a mano, y
    ésa es la única razón por la que hoy existe un test que se muere si un
    marcador cambia de nombre.
  * **La consulta.** Los filtros, el techo, el orden y el parseo, una vez.

NO hace la política de errores, y eso es deliberado. Las tres respuestas a «no
pude leer» son distintas a propósito y tienen plata atada:

  * `limites._hubo_cambios_durables` LEVANTA — y con eso deja de auto-
    confirmarse cualquier pedido, porque un almacén vacío que no se puede
    contrastar con ERPNext puede ser pérdida de datos.
  * `limites._hubo_cambios_durables_entrega` devuelve True (no habilita el
    entorno de arranque: sólo puede ensanchar lo que el sistema ofrece).
  * `limites._hubo_cambios_durables_idioma` devuelve False (algo hay que
    contestar, y se contesta en el idioma por defecto).
  * `pendientes._tiene_marca` y `sombra.ya_anotado` devuelven None, que NO es
    False: aplastarlo manda el segundo recordatorio cada vez que ERPNext tose.

Un lector compartido que eligiera una de esas cuatro por todos sería un cambio
de comportamiento con un cliente del otro lado. Así que las funciones de acá
DEJAN SALIR la excepción y cada módulo conserva su envoltorio de tres líneas
con su propia política. Lo que se comparte es la consulta; lo que no, la
decisión.

EL TECHO NO SIGNIFICA NADA SOLO
`sombra.MAX_MARCAS = 5` y `confirmacion.MAX_MARCAS = 20` parecían el mismo
número escrito dos veces con valores distintos. No lo son, y la diferencia no
estaba escrita en ninguna parte — que era el problema, no el número:

  * sombra pide `creation desc` y se queda con la PRIMERA fila parseable: la
    más nueva. Espera un registro por pedido (el comentario ES la marca de
    idempotencia); 5 es holgura por si dos barridos corrieron a la vez.
  * confirmacion pide `creation asc` y se queda con `min(...)`: la MÁS VIEJA,
    porque el plazo de cancelación corre desde la primera confirmación. Su
    propio docstring anticipa más de un registro por pedido.

Los dos piden la página en la DIRECCIÓN del extremo que quieren, así que
truncar no puede dar una respuesta equivocada: sólo puede no encontrar ninguna
si las primeras N filas de esa dirección son todas ilegibles. Un techo sólo
quiere decir algo junto con su orden y su regla de selección — por eso las
tres van juntas en la fila del registro y no sueltas en tres módulos. Así
5 y 20 dejan de ser una contradicción y pasan a ser dos parámetros de dos
marcadores distintos, cada uno con su motivo AL LADO del número. `MARCAS` no
puede tener una fila sin ese motivo: lo exige un test.

LOS TEXTOS YA ESCRITOS NO SE RENOMBRAN
Un marcador que ya está en el ERPNext de un cliente vivo es historia que no se
puede reescribir. Si mañana una fila cambia su texto, hay que leer los dos o el
sistema pierde su propia historia — por eso el registro tiene `heredados`, que
hoy está vacío en todas las filas y existe para que ese día no haya que
inventar dónde ponerlos. Ninguna fila cambia de texto en este PR.

OJO CON EL NOMBRE
`tests/fakes.FakeMarcas` y el fixture `marcas_sin_redis` de `tests/conftest.py`
son un doble de REDIS, no de esto. El choque de nombres es anterior a este
módulo y no se toca acá; lo mismo `digest.MARCA_TTL_SEGUNDOS`, que es un TTL y
no un marcador (ya está anotado al pie de `docs/AUDITORIA.md`).
"""
from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import cache

from app import erpnext

# --------------------------------------------------------------- el portador
#
# Dónde vive el texto. No todos son comentarios, y suponerlo es el error que
# este campo existe para hacer imposible: `[remito-preparado-por-agente]` va en
# el campo `remarks` de un Delivery Note, así que NINGUNA consulta de Comment
# lo encuentra jamás.
COMENTARIO = "comentario"
CAMPO = "campo"

# ---------------------------------------------------------------- la lectura
#
# Cómo se elige la fila que gana. El orden de la consulta SALE de acá y no se
# declara aparte, porque «pedir desc y quedarse con la más vieja» es justo el
# bug silencioso que no queremos que se pueda escribir.
EXISTENCIA = "existencia"  # ¿hay alguna? limit=1, sin orden, sin parseo
MAS_NUEVA = "mas_nueva"  # creation desc, la primera parseable
MAS_VIEJA = "mas_vieja"  # creation asc, la primera parseable
BARRIDO = "barrido"  # creation desc, TODAS las de una ventana, con truncado
SUBCADENA = "subcadena"  # está o no está adentro de un campo del documento
NO_SE_LEE = "no_se_lee"  # se escribe y nadie lo lee de vuelta

# ----------------------------------------------------------------- el parseo
#
# Cómo se lee la carga que va pegada atrás del texto. Se declara el TIPO y no
# el parser ya construido: pasarle el literal a `json_tras` duplicaba el texto
# de la fila y podía separarse de él —una fila con `texto="[a]"` y
# `parser=json_tras("[b]")` no se la agarraba nadie— y además dejaba afuera los
# heredados, así que un renombre traía la historia vieja de ERPNext y la
# descartaba por ilegible.
JSON = "json"

_ORDEN = {
    EXISTENCIA: None,
    MAS_NUEVA: "creation desc",
    MAS_VIEJA: "creation asc",
    BARRIDO: "creation desc",
    SUBCADENA: None,
    NO_SE_LEE: None,
}


def texto_plano(contenido: object) -> str:
    """El contenido de un comentario como texto, salga como salga de ERPNext.

    ERPNext mete etiquetas HTML y escapa entidades adentro del `content` de un
    comentario, así que el crudo no se parsea nunca. Estaba escrito igual en
    `sombra._parsear` y en `solicitudes._parsear`; es la misma operación y
    ahora es la misma función.
    """
    return re.sub(r"<[^>]+>", " ", html.unescape(str(contenido or "")))


@cache
def json_tras(*textos: str) -> Callable[[object], dict | None]:
    """Parser de los marcadores que llevan un JSON pegado atrás del texto.

    `[sombra] {...}` y `[solicitud] {...}` se parseaban con dos regex idénticas
    en dos módulos. Devuelve None —y nunca levanta— si no hay marca, si lo que
    sigue no es JSON, o si el JSON no es un objeto: un comentario ilegible no
    es un registro, y no puede romper un barrido.

    Toma VARIOS textos porque una marca con heredados tiene que poder leer lo
    que escribió con su nombre viejo. Que la consulta trajera los dos textos y
    el parser reconociera uno solo era traerse la historia de ERPNext para
    tirarla por ilegible — la pérdida que `heredados` existe para evitar.
    """
    alternativa = "|".join(re.escape(t) for t in textos)
    patron = re.compile(rf"(?:{alternativa})\s*(\{{.*\}})\s*$", re.DOTALL)

    def parsear(contenido: object) -> dict | None:
        encontrado = patron.search(texto_plano(contenido))
        if not encontrado:
            return None
        try:
            datos = json.loads(encontrado.group(1))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return datos if isinstance(datos, dict) else None

    return parsear


@dataclass(frozen=True)
class Marca:
    """Una fila del registro: el texto durable y todo lo que hace falta para
    leerlo de vuelta igual que se escribió."""

    nombre: str
    # EL FORMATO DURABLE. Lo que sobrevive al proceso, y lo único que
    # `tests/test_marcas.py` fija contra una tabla escrita a mano.
    texto: str
    doctype: str
    portador: str
    lectura: str
    # Cuántas filas pide la consulta. Sólo quiere decir algo junto con
    # `orden` y `lectura`; ver el docstring del módulo.
    techo: int = 1
    # POR QUÉ ese techo, al lado del número. Un test exige que no esté vacío en
    # ninguna fila que lea algo: un techo sin motivo escrito es exactamente lo
    # que el issue #24 señala, y el número no es el problema.
    porque_el_techo: str = ""
    parseo: str = ""
    # El campo del documento, sólo para portador CAMPO.
    campo: str = ""
    # Textos VIEJOS del mismo marcador, que siguen en el ERPNext de algún
    # cliente y hay que seguir leyendo. Vacío hoy en todas las filas: ninguna
    # cambió de texto. Existe para que el día que una cambie, el lugar donde
    # poner el texto viejo ya exista y la lectura de los dos sea el default.
    heredados: tuple[str, ...] = ()
    # Prosa interpolada en vez de un formato que el sistema eligió. Cambia qué
    # se puede afirmar del marcador: ver `tests/test_marcas.py`.
    prosa: bool = False

    @property
    def orden(self) -> str | None:
        """El `order_by` de la consulta, DERIVADO de la regla de selección."""
        return _ORDEN[self.lectura]

    @property
    def textos(self) -> tuple[str, ...]:
        """El texto vigente más los heredados. Lo que hay que buscar para no
        perder la historia."""
        return (self.texto, *self.heredados)

    @property
    def parser(self) -> Callable[[object], object] | None:
        """El parser de la carga, DERIVADO de los textos de esta misma fila.

        Derivarlo es lo que hace imposible que el parser y el texto se separen,
        y lo que hace que un heredado se lea solo.
        """
        if self.parseo != JSON:
            return None
        return json_tras(*self.textos)


# ---------------------------------------------------------------- el registro
#
# Doce filas. La auditoría y el issue dicen «once marcadores ... más dos en
# prosa», pero enumeran DIEZ entre corchetes: el censo estaba corrido en uno.
# Con el registro el conteo dejó de ser una afirmación y pasó a ser algo que se
# cuenta — `tests/test_marcas.py` lo hace.
_FILAS = (
    # --------------------------------------------------------------- limites
    #
    # Los tres van en un Comment de la COMPAÑÍA, no de un pedido, y por eso la
    # consulta no lleva reference_name. Son tres marcas separadas a propósito:
    # compartirlas haría que un cambio de días de reparto armara el fusible que
    # frena las ventas. El motivo largo está en app/limites.py, donde se decide.
    Marca(
        nombre="limite",
        texto="[limite]",
        doctype="Company",
        portador=COMENTARIO,
        lectura=EXISTENCIA,
        techo=1,
        porque_el_techo=(
            "La pregunta es «¿hay alguno?», no «cuáles». Una fila alcanza para "
            "contestarla y el contenido no se lee: `fields=[\"name\"]` mantiene "
            "afuera el teléfono del dueño, que sí está en el texto."
        ),
    ),
    Marca(
        nombre="entrega",
        texto="[entrega]",
        doctype="Company",
        portador=COMENTARIO,
        lectura=EXISTENCIA,
        techo=1,
        porque_el_techo="Igual que [limite]: una fila contesta «¿hay alguno?».",
    ),
    Marca(
        nombre="idioma",
        texto="[idioma]",
        doctype="Company",
        portador=COMENTARIO,
        lectura=EXISTENCIA,
        techo=1,
        porque_el_techo="Igual que [limite]: una fila contesta «¿hay alguno?».",
    ),
    # -------------------------------------------------------------- acciones
    Marca(
        nombre="accion",
        texto="[accion]",
        doctype="Sales Order",
        portador=COMENTARIO,
        lectura=NO_SE_LEE,
        techo=0,
        porque_el_techo=(
            "No tiene techo porque no tiene lector: se escribe y nadie lo "
            "consulta. Está en el registro igual, y es el que más lo necesita: "
            "es el ÚNICO rastro de que una persona autorizó una acción, lo lee "
            "un humano en ERPNext, y sin una fila acá no habría nada que se "
            "muriera si alguien le cambia el texto."
        ),
    ),
    # ----------------------------------------------------------- solicitudes
    Marca(
        nombre="solicitud",
        texto="[solicitud]",
        doctype="Sales Order",
        portador=COMENTARIO,
        lectura=MAS_NUEVA,
        techo=60,
        porque_el_techo=(
            "Un pedido acumula un evento por oferta, aceptación, vencimiento y "
            "revisión, así que acá SÍ se esperan muchos por documento. Se pide "
            "desc y gana el primero parseable: el estado actual. Antes pedía "
            "los 60 más VIEJOS y se quedaba con el último de ésos, que es el "
            "evento más nuevo sólo mientras el pedido tenga menos de 60 en toda "
            "su vida — y pasado eso el «estado actual» era uno que el pedido "
            "había dejado horas antes."
        ),
        parseo=JSON,
    ),
    # ---------------------------------------------------------------- sombra
    Marca(
        nombre="sombra",
        texto="[sombra]",
        doctype="Sales Order",
        portador=COMENTARIO,
        lectura=MAS_NUEVA,
        techo=5,
        porque_el_techo=(
            "Se espera UNO por pedido: el comentario ES la marca de "
            "idempotencia, así que el barrido sólo anota los que no tienen. "
            "Cinco es holgura para el único caso que produce más de uno (dos "
            "barridos a la vez) y para saltear filas ilegibles; se pide desc y "
            "gana la más nueva. No es 20 como confirmacion porque no busca lo "
            "mismo: ver el docstring del módulo."
        ),
        parseo=JSON,
    ),
    # ---------------------------------------------------------- confirmacion
    #
    # EXCLUIDO de la consolidación por el brief de la auditoría (núcleo de
    # seguridad). La fila está igual —el conteo es honesto y el guard de texto
    # lo cubre— pero `app/confirmacion.py` sigue con su consulta y su parser, y
    # su MAX_MARCAS no se toca. Acá se DECLARA lo que ese módulo ya hace.
    Marca(
        nombre="confirmacion",
        texto="[confirmado-por-agente]",
        doctype="Sales Order",
        portador=COMENTARIO,
        lectura=MAS_VIEJA,
        techo=20,
        porque_el_techo=(
            "Busca la MÁS VIEJA, no la más nueva: el plazo de cancelación corre "
            "desde la primera confirmación, que es la dirección que cierra la "
            "ventana antes. Pide asc y se queda con el mínimo, así que truncar "
            "no puede devolver un sello posterior — sólo ninguno. Veinte es "
            "holgura sobre un pedido que acumule varios registros, que su "
            "propio docstring anticipa. No se cambia: núcleo de seguridad."
        ),
    ),
    # ------------------------------------------------------------ decisiones
    #
    # EL DISTINTO. No es un Comment: va en el campo `remarks` del Delivery
    # Note. Ninguna consulta de Comment lo encuentra, y por eso el registro
    # tiene `portador` en vez de dar por hecho que todos son comentarios.
    Marca(
        nombre="remito_agente",
        texto="[remito-preparado-por-agente]",
        doctype="Delivery Note",
        portador=CAMPO,
        campo="remarks",
        lectura=SUBCADENA,
        techo=0,
        porque_el_techo=(
            "No hay techo: no hay consulta. El documento ya está leído y la "
            "marca está adentro de uno de sus campos."
        ),
    ),
    # ------------------------------------------------------------ pendientes
    Marca(
        nombre="pendiente_aviso",
        texto="[pendiente-aviso]",
        doctype="Sales Order",
        portador=COMENTARIO,
        lectura=EXISTENCIA,
        techo=1,
        porque_el_techo=(
            "«¿Ya le avisé?» es una pregunta de sí o no. Una fila la contesta. "
            "Lo que importa acá no es el techo sino el TERCER estado: «no pude "
            "averiguarlo» no es «no», y aplastarlo manda un segundo "
            "recordatorio cada vez que ERPNext tose."
        ),
    ),
    Marca(
        nombre="pendiente_cierre",
        texto="[pendiente-cerrado]",
        doctype="Sales Order",
        portador=COMENTARIO,
        lectura=EXISTENCIA,
        techo=1,
        porque_el_techo=(
            "Igual que el aviso, y más caro de equivocar: el mensaje de cierre "
            "es terminal («no llegamos a confirmarlo»), así que mandarlo dos "
            "veces es el error caro de los dos."
        ),
    ),
    # ----------------------------------------------------------------- agenda
    #
    # La lista durable de cosas que vencen más tarde (`app/agenda.py`). Es la
    # única marca del registro que guarda VARIAS filas vivas por documento: un
    # pedido puede tener a la vez el aviso al cliente antes de la entrega, el
    # re-ping al dueño y un seguimiento que pidió el modelo. Por eso se lee
    # como `[solicitud]` —desc, gana la primera que parsea por id— y no como
    # `[pendiente-aviso]`, que sólo contesta «¿hay alguno?».
    Marca(
        nombre="agenda",
        texto="[agenda]",
        doctype="Sales Order",
        portador=COMENTARIO,
        lectura=MAS_NUEVA,
        techo=80,
        porque_el_techo=(
            "Cada fila escribe un evento al crearse y otro al terminar, y un "
            "pedido puede tener varias filas vivas más las que ya terminaron. "
            "80 son unas veinte filas de historia completa, que es más de lo "
            "que un pedido junta antes de cerrarse. Se pide desc y gana la "
            "primera parseable de CADA id: truncar pierde la historia vieja, "
            "que ya no decide nada, nunca el estado actual — que es el evento "
            "más nuevo y por lo tanto el primero de la página."
        ),
        parseo=JSON,
    ),
    # ----------------------------------------------------- los dos en prosa
    #
    # NO son iguales a los diez de corchetes, y el registro lo dice con
    # `prosa=True` en vez de esconderlo. Los de corchetes son un formato que el
    # sistema eligió; éstos son texto que alguien escribió, EN ESPAÑOL, que el
    # contador de `autonomia` matchea por prefijo. Entran al registro porque el
    # prefijo ES un contrato durable —si cambia, los números de autonomía se
    # van a cero sin decirlo— pero se guardan distinto: ver el PR y
    # `tests/test_marcas.py`.
    Marca(
        nombre="revision_humana",
        texto="Requiere revisión humana:",
        doctype="Sales Order",
        portador=COMENTARIO,
        lectura=BARRIDO,
        techo=500,
        porque_el_techo=(
            "Es un techo de LECTURA sobre una ventana de días y de TODOS los "
            "pedidos, no de un documento: por eso 500 y no 5. Si se llena, el "
            "resumen dice que el número es un piso en vez de informarlo como el "
            "total."
        ),
        prosa=True,
    ),
    Marca(
        nombre="rechazo_manual",
        texto="Rechazado manualmente por",
        doctype="Sales Order",
        portador=COMENTARIO,
        lectura=BARRIDO,
        techo=500,
        porque_el_techo="El mismo barrido por ventana que «Requiere revisión humana:».",
        prosa=True,
    ),
)

MARCAS: dict[str, Marca] = {m.nombre: m for m in _FILAS}


def marca(nombre: str) -> Marca:
    """La fila del registro, o KeyError con la lista de las que hay.

    Levantar acá es a propósito: un nombre mal escrito tiene que morirse en el
    import del módulo que lo pide, no devolver None y hacer que un barrido lea
    la marca equivocada.
    """
    try:
        return MARCAS[nombre]
    except KeyError:
        raise KeyError(
            f"no hay marca durable {nombre!r}; hay {sorted(MARCAS)}"
        ) from None


def texto(nombre: str) -> str:
    """El texto durable de un marcador. El atajo que usan los módulos."""
    return marca(nombre).texto


# --------------------------------------------------------------- la consulta
#
# UNA. Todo lo que sigue la usa, y nada de esto atrapa excepciones: la política
# de errores es de cada módulo, por las razones del docstring del módulo.


def filas(
    nombre: str,
    name: str | list[str] | None = None,
    *,
    desde: datetime | None = None,
    techo: int | None = None,
    campos: list[str] | None = None,
    start: int = 0,
    orden: str | None = None,
) -> list[dict]:
    """Los comentarios de un marcador. La ÚNICA consulta de marcas del sistema.

    `name` puede ser un documento, varios (se usa `in`) o None para preguntar
    por todos — que es lo que hacen las tres marcas de `Company` y el barrido
    por ventana de `autonomia`.

    No atrapa nada: lo que ERPNext levante sale por acá y lo decide el que
    llama.
    """
    m = marca(nombre)
    if m.portador != COMENTARIO:
        raise ValueError(
            f"la marca {m.nombre!r} vive en el campo {m.campo!r} de un "
            f"{m.doctype}, no en un comentario: no se consulta así"
        )
    base: list = [["reference_doctype", "=", m.doctype]]
    if isinstance(name, list):
        base.append(["reference_name", "in", name])
    elif name is not None:
        base.append(["reference_name", "=", name])
    if desde is not None:
        base.append(["creation", ">=", desde.strftime("%Y-%m-%d %H:%M:%S")])
    tope = m.techo if techo is None else techo
    pedidos = campos or ["content", "creation"]
    # El orden sale de la fila salvo que la LECTURA pida otro: ver `barrer`.
    orden_efectivo = m.orden if orden is None else orden

    def consultar(texto_buscado: str) -> list[dict]:
        return erpnext.policy_get_list(
            "Comment",
            filters=[*base, ["content", "like", f"%{texto_buscado}%"]],
            fields=pedidos,
            limit=tope,
            order_by=orden_efectivo,
            start=start,
        )

    if not m.heredados:
        return consultar(m.texto)

    # UN TEXTO VIEJO Y UNO NUEVO. Los filtros de Frappe se ANDean, así que «o
    # el viejo o el nuevo» no entra en una sola consulta: va una por texto y se
    # reordena al unir, porque si no el orden de la página lo decidiría el
    # orden de los textos en la fila del registro y no `creation`.
    if start:
        raise ValueError(
            f"la marca {m.nombre!r} tiene textos heredados: paginar sobre la "
            "unión de dos consultas saltea y repite filas"
        )
    juntas = [fila for t in m.textos for fila in consultar(t)]
    juntas.sort(
        key=lambda f: str((f or {}).get("creation") or ""),
        reverse=orden_efectivo == "creation desc",
    )
    return juntas[:tope] if tope > 0 else juntas


def existe(nombre: str, name: str | None = None) -> bool:
    """¿Hay alguna marca de ésas? True/False, y levanta si no se pudo leer.

    NO devuelve el tercer estado: lo devuelve el que llama, porque los cuatro
    módulos que preguntan esto contestan distinto al «no sé» y esa asimetría es
    deliberada. Ver el docstring del módulo.
    """
    return bool(filas(nombre, name, campos=["name"], techo=1))


def leer(nombre: str, name: str) -> object | None:
    """La carga del marcador que gana, ya parseada. None si no hay ninguna.

    Cuál gana lo dice `lectura`, y el orden de la consulta sale de ahí: MAS_NUEVA
    pide desc, MAS_VIEJA pide asc, y las dos se quedan con la primera fila que
    el parser pueda leer. None NO significa «no pasó» ni «no existe el pedido»:
    significa que no hay registro legible, y el que llama no puede convertirlo
    en un número.
    """
    m = marca(nombre)
    if m.parser is None:
        raise ValueError(f"la marca {m.nombre!r} no declara parser: no se lee así")
    for fila in filas(nombre, name):
        if not isinstance(fila, dict):
            continue
        datos = m.parser(fila.get("content"))
        if datos is not None:
            return datos
    return None


def leer_lote(nombre: str, nombres: list[str]) -> dict[str, object]:
    """La carga de varios documentos en UNA lectura. Los que falten, faltan.

    El techo se multiplica por la cantidad de documentos, que es lo que ese
    número significa acá: cuántas filas por documento hacen falta para que la
    que gana esté en la página. Recorrer documento por documento serían sesenta
    lecturas para un resumen de una semana.
    """
    m = marca(nombre)
    if m.parser is None:
        raise ValueError(f"la marca {m.nombre!r} no declara parser: no se lee así")
    unicos = [p for p in dict.fromkeys(str(x or "").strip() for x in nombres) if p]
    if not unicos:
        return {}
    encontrados: dict[str, object] = {}
    for fila in filas(
        nombre,
        unicos,
        techo=m.techo * len(unicos),
        campos=["content", "reference_name", "creation"],
    ):
        if not isinstance(fila, dict):
            continue
        doc = str(fila.get("reference_name") or "").strip()
        # Viene ordenado: la primera que aparece de cada documento es la que
        # gana, así que una segunda del mismo documento no la pisa.
        if not doc or doc in encontrados:
            continue
        datos = m.parser(fila.get("content"))
        if datos is not None:
            encontrados[doc] = datos
    return encontrados


def barrer(
    nombre: str, desde: datetime, *, techo: int | None = None
) -> tuple[list[dict], bool]:
    """(comentarios de la ventana, se llenó el techo) para TODOS los documentos.

    Pide una fila de más que el techo para poder DECIR que se llenó. Un número
    recortado informado como si fuera el total es el defecto que #19 dejó
    escrito: tres techos que se informaban como el total exacto.
    """
    m = marca(nombre)
    tope = m.techo if techo is None else techo
    leidas = filas(
        nombre,
        desde=desde,
        techo=tope + 1,
        campos=["content", "reference_name", "creation"],
        # SIEMPRE la punta nueva, aunque el marcador lea al revés por pedido.
        # El orden lo decide la LECTURA, no sólo el marcador: `confirmacion`
        # declara `creation asc` porque su lectura POR PEDIDO quiere la primera
        # confirmación, pero un barrido no lee un pedido — lee una ventana
        # sobre todos, y ahí el techo recorta. Con el orden del marcador el
        # resumen se armaba con las 500 confirmaciones MÁS VIEJAS y escondía la
        # actividad reciente. Truncar una ventana tiene que tirar lo viejo.
        orden=_ORDEN[BARRIDO],
    )
    return list(leidas[:tope]), len(leidas) > tope


def en_campo(nombre: str, doc: dict) -> bool:
    """¿El documento lleva la marca en su campo? Para los que no son comentarios.

    Sin consulta: el documento ya está leído. Es como `decisiones` distingue un
    remito que preparó el agente de uno que cargó una persona a mano, y un
    borrador sin la marca no se toca nunca.
    """
    m = marca(nombre)
    if m.portador != CAMPO:
        raise ValueError(
            f"la marca {m.nombre!r} vive en un comentario, no en un campo"
        )
    contenido = str((doc or {}).get(m.campo) or "")
    return any(t in contenido for t in m.textos)


# ---------------------------------------------------------------- la escritura


def escribir(nombre: str, name: str, cuerpo: str = "", *, exigir: bool = False) -> None:
    """Deja la marca en el documento. `exigir` elige cuál de los dos helpers.

    Los dos existen y la diferencia es la de una prueba y una promesa:

      * `exigir=False` usa `add_comment`, que se traga el error. Es para lo que
        no puede cambiar lo que ya le pasó al pedido — si el cierre ya ocurrió,
        que la marca no quede no puede saltearse los avisos que vienen después.
      * `exigir=True` usa `registrar_comentario`, que LEVANTA. Es para lo que el
        comentario mismo sostiene: un cambio de límite sin registro no se
        aplica, una acción sin registro no se ejecuta.

    Elegir mal acá es un cambio de comportamiento, no un detalle de estilo, así
    que el parámetro no tiene un default «razonable» que sirva para los dos: el
    default es el que no puede romper nada.
    """
    texto_completo = f"{texto(nombre)} {cuerpo}".rstrip()
    m = marca(nombre)
    if exigir:
        erpnext.registrar_comentario(m.doctype, name, texto_completo)
    else:
        erpnext.add_comment(m.doctype, name, texto_completo)
