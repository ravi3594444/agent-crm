"""Los límites de auto-confirmación: los fija el DUEÑO, no el código.

QUIÉN DECIDE QUÉ
  El dueño (una persona) fija los números desde WhatsApp.
  El agente de gerencia (LLM) interpreta lo que escribió y le cuenta qué pasó.
  app/policy.py —Python determinista— es lo ÚNICO que decide si un pedido se
  auto-confirma, y lee estos números él mismo en cada evaluación, incluida la
  revalidación final adentro del lock. El LLM no participa de esa decisión.

DÓNDE VIVEN
  En Redis, el mismo de los locks de negocio (ver locks.conexion). Las
  variables de entorno quedan como valor de ARRANQUE: sirven para levantar el
  sistema, no para configurarlo. El orden de resolución es

      Redis (lo que fijó el dueño)  ->  variable de entorno  ->  default

  y el default de cada límite reproduce el comportamiento de hoy: con nada
  configurado, esta etapa no cambia qué pedidos se confirman solos. Sólo un
  cambio explícito y confirmado por el dueño afloja algo.

SI NO SE PUEDE LEER
  Nunca se adivina un número. LimiteError -> el pedido queda pendiente con un
  motivo que dice qué límite hay que arreglar.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass

from redis.exceptions import RedisError

from app import erpnext, locks, marcas, reloj
from app import telefono as telefono_mod

# Marca de los comentarios de auditoría en ERPNext. Redis no puede contestar
# «¿me borraron?»: un almacén vacío es idéntico a uno recién instalado. La
# copia durable de cada cambio vive en ERPNext, así que un almacén vacío CON
# cambios registrados es pérdida de datos, no una instalación nueva.
MARCA_DURABLE = marcas.texto("limite")
# Marca APARTE para las reglas de entrega, y en esto está TODO el punto de
# haber separado los dos registros. _hubo_cambios_durables() le pregunta a
# ERPNext por MARCA_DURABLE para distinguir «nunca se configuró» de «se perdió
# el almacén», y un almacén vacío CON cambios registrados hace que
# configuracion() levante — o sea, que no se confirme solo ningún pedido.
# Compartir la marca significaba que un cambio de días de reparto de hoy dejaba
# armado ese fusible para siempre, y un flush de Redis el mes que viene frenaba
# las ventas por un cambio de horario. Son dos hechos distintos y se anotan
# distinto.
MARCA_DURABLE_ENTREGA = marcas.texto("entrega")
# Y una TERCERA marca para el idioma, por la misma razón que la de entrega es
# distinta de la de límites: son hechos distintos y se anotan distinto. Un
# cambio de idioma no puede armar el fusible que frena las ventas, y un flush
# de Redis no puede dejar al sistema mudo esperando que alguien reconfigure un
# idioma. Perder el idioma cuesta una respuesta en el otro idioma; perder un
# límite cuesta un pedido que se confirma solo. No se comparte la marca.
MARCA_DURABLE_IDIOMA = marcas.texto("idioma")
# Los datos del negocio y las plantillas de Meta. Marca propia y NO la de
# límites: ver la fila `negocio` de app/marcas.py, que explica el fusible que
# esto evita armar. No tiene lector ni caché porque no gatea nada — se escribe
# para que el cambio quede anotado donde vive la contabilidad del dueño.
MARCA_DURABLE_NEGOCIO = marcas.texto("negocio")
DURABLE_CACHE_SEGUNDOS = 60.0

CLAVE_VALORES = "plus-agent:limites"
CLAVE_AUDITORIA = "plus-agent:limites:auditoria"
CLAVE_PROPUESTA = "plus-agent:limites:propuesta"

# Un cambio de límite se confirma o se cae solo. Diez minutos es tiempo de
# sobra para leer el mensaje y contestar, y poco para que quede colgado.
PROPUESTA_TTL_SEGUNDOS = 600
AUDITORIA_MAXIMA = 500


class LimiteError(RuntimeError):
    """Un límite falta, no se puede leer, o no es un número creíble.

    ``clave`` nombra una entrada del catálogo de app/idioma.py y ``datos`` son
    los valores que ese texto interpola — el alias del ajuste, lo que tecleó el
    dueño, un mínimo, un máximo. Los dos juntos son lo que hace que el motivo
    salga en el idioma de quien lo lee: `app/ajustes.py` y `app/main.py` meten
    ese motivo adentro de un mensaje que ya sale en inglés, así que un motivo en
    español era media frase en cada idioma.

    ``datos`` existe porque casi ningún motivo es una frase fija: «no es un
    número: 'abc'» necesita el 'abc'. Sin él, la clave sólo alcanzaba para los
    cuatro errores del código de confirmación, que son los únicos sin datos, y
    por eso eran los únicos cuatro que la tenían.

    El texto en español se sigue pasando y sigue siendo `str(exc)`: es lo que
    va al log, y lo que ve cualquier camino que todavía no conozca la clave.
    """

    def __init__(
        self, mensaje: object = "", clave: str = "", datos: dict | None = None
    ) -> None:
        super().__init__(mensaje)
        self.clave = clave
        self.datos = dict(datos or {})


def motivo(exc: Exception, lengua: str | None = None) -> str:
    """El texto de un `LimiteError` en el idioma de quien lo va a leer.

    Vive acá y no copiado en cada handler porque `app/ajustes.py` y
    `app/main.py` hacían la misma línea con el mismo ternario, y dos copias de
    una regla son dos reglas: la segunda se olvida el día que la primera
    aprende algo. Ahora las dos preguntan lo mismo.

    Sin clave cae al texto en español, que es exactamente lo que hacía antes:
    una excepción de otro módulo o una vieja sigue saliendo, nunca vacía.
    """
    from app import idioma as idioma_mod

    return idioma_mod.motivo_de(exc, lengua)


# What KIND of value a setting holds. Each kind has exactly one validator and
# one normal form, so "validated" never means "the model thought it looked ok".
NUMERO = "numero"
BOOLEANO = "booleano"
DIAS = "dias"
HORA = "hora"
# Las zonas de reparto. Son dos listas y no una porque app/entrega.py las
# evalúa por separado: con las dos cargadas la dirección necesita LOS DOS
# datos permitidos, y con una sola manda esa sobre su dato.
LOCALIDADES = "localidades"
CODIGOS_POSTALES = "codigos_postales"
# Un idioma. Sólo dos valores posibles y los valida app/idioma.py, que es el
# único lugar donde se decide qué texto significa qué idioma.
IDIOMA = "idioma"
# Un dato del negocio escrito en prosa por el dueño: cómo se llama, a qué se
# dedica, en qué horario atiende. No es un número, no es una lista y no decide
# nada — entra en una frase del prompt y se lee en el panel.
#
# LO QUE ESTA VALIDACIÓN NO ES. El texto que termina EN el prompt lo limpia
# `app/conversacion.py::_dato_de_entorno`, y sigue limpiándolo ahí: esa
# limpieza es del HUECO y no de la variable, así que un valor que ahora puede
# llegar del panel pasa por exactamente la misma puerta que el que llegaba del
# `.env`. Acá se valida lo que se GUARDA —un renglón, acotado, sin caracteres
# de control—, que es otra pregunta: que el dueño lea de vuelta lo que escribió.
TEXTO = "texto"
# El nombre de una plantilla de Meta. Meta las acepta en minúsculas, dígitos y
# guiones bajos, y un nombre que no existe no falla al guardarse: falla horas
# después, cuando el aviso no sale y el cliente no se entera de nada.
PLANTILLA = "plantilla"
# El idioma en que la plantilla está REGISTRADA en Meta ("es_AR", "en_US"). Es
# distinto de IDIOMA —que es en qué idioma le hablamos al dueño— y tiene que
# coincidir con el registro o Meta contesta que la plantilla no existe.
IDIOMA_PLANTILLA = "idioma_plantilla"

# The normal form for "nothing configured". An EMPTY string cannot mean that:
# _resolver treats "" in the store as "unset" and falls through to the
# bootstrap environment, so "borrá los días de reparto" would silently restore
# whatever the .env said. This sentinel stores, resolves and reads back as
# "none", which is what the owner actually asked for.
NINGUNO = "-"
# Las formas de decir "apagalo". Las inglesas están porque el primer límite con
# alias en inglés (AVISO_ANTES_DE_ENTREGA_HORAS) es `opcional`, y sin esto el
# dueño que lee en inglés tendría una forma de ENCENDERLO y ninguna de apagarlo:
# `none`/`off` caían en `_numero` y contestaban «no es un número». La asimetría
# es la misma que `_VERDADEROS`/`_FALSOS` ya resolvieron para los booleanos.
_NINGUNO_DICHO = frozenset(
    {
        "-", "ninguno", "ninguna", "nada", "no", "vacio", "vacío",
        "none", "off", "never", "nothing",
    }
)

# Where a delivery value comes from when the store was WIPED: not the owner,
# not the bootstrap environment, not a default — NOTHING is in effect, because
# entrega() offers nothing in that state. resumen() reports the row this way so
# readiness and ver_reglas_de_entrega say what the system will actually do,
# instead of showing .env values it will not offer.
PERDIDO = "perdido"
PROBLEMA_ENTREGA_PERDIDA = (
    "las reglas de entrega se perdieron del almacén y ERPNext tiene cambios "
    "registrados: no rige ningún valor, tampoco el del .env, y no se ofrece "
    "reparto, entrega fuera de día ni retiro hasta que las vuelvas a fijar"
)


@dataclass(frozen=True)
class Definicion:
    nombre: str
    alias: tuple[str, ...]
    significado: str
    unidad: str
    default: str
    minimo: float = 0.0
    maximo: float = 0.0
    tipo: str = NUMERO
    # Only a setting marked opcional may hold NINGUNO. A ceiling cannot be
    # "none"; a list of delivery days can.
    opcional: bool = False
    # Cuántos caracteres puede tener un TEXTO. Campo propio y no `maximo`
    # reutilizado: `maximo` es el techo de un NÚMERO y lo lee `_numero` para
    # decidir que el valor es un error de tipeo. Dos preguntas distintas en un
    # solo campo es cómo se escribe un bug que nadie ve hasta que un nombre de
    # sesenta caracteres se guarda contra un techo pensado para pesos.
    largo: int = 0

    @property
    def booleano(self) -> bool:
        return self.tipo == BOOLEANO


# Los que fija el dueño. `maximo` no es una preferencia: arriba de eso el
# número es un error de tipeo, no una decisión, y aplicarlo sería peor que
# rechazarlo.
LIMITES: dict[str, Definicion] = {
    "AUTO_CONFIRM_MAX": Definicion(
        nombre="AUTO_CONFIRM_MAX",
        alias=("monto maximo", "monto máximo", "tope", "tope maximo", "monto"),
        significado="Pedido más grande que se puede confirmar sin que lo mire nadie",
        unidad="$",
        default="0",
        maximo=100_000_000.0,
    ),
    "AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO": Definicion(
        nombre="AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO",
        alias=(
            "cantidad maxima por producto",
            "cantidad máxima por producto",
            "cantidad por producto",
            "maximo por producto",
        ),
        significado=(
            "Lo máximo de UN producto que puede llevarse un pedido automático, "
            "en la unidad de stock del producto (litro, kilo, unidad)"
        ),
        unidad="unidad de stock",
        default="0",
        maximo=1_000_000.0,
    ),
    "STOCK_BUFFER_PCT": Definicion(
        nombre="STOCK_BUFFER_PCT",
        alias=("colchon de stock", "colchón de stock", "colchon", "buffer"),
        significado="Margen de stock que se guarda para las ventas todavía no cargadas",
        unidad="%",
        default="20",
        maximo=95.0,
    ),
    "AUTO_CONFIRM_MAX_CLIENTE_NUEVO": Definicion(
        nombre="AUTO_CONFIRM_MAX_CLIENTE_NUEVO",
        alias=("tope cliente nuevo", "cliente nuevo", "clientes nuevos"),
        significado=(
            "Tope para un cliente sin historial suficiente. Con 0, un cliente "
            "nuevo siempre espera a una persona"
        ),
        unidad="$",
        default="0",
        maximo=100_000_000.0,
    ),
    "AUTO_CONFIRM_MAX_DEBT": Definicion(
        nombre="AUTO_CONFIRM_MAX_DEBT",
        alias=("deuda tolerada", "deuda", "deuda vencida"),
        significado="Deuda vencida que se tolera antes de que lo mire una persona",
        unidad="$",
        default="0",
        maximo=100_000_000.0,
    ),
    "AUTO_CONFIRM_MAX_DESCUENTO_PCT": Definicion(
        nombre="AUTO_CONFIRM_MAX_DESCUENTO_PCT",
        alias=(
            "descuento maximo",
            "descuento máximo",
            "tope de descuento",
            "maximo descuento",
        ),
        significado=(
            "Descuento máximo —renglón y pedido SUMADOS— que puede "
            "auto-confirmarse cuando la aprobación de descuentos está en no"
        ),
        unidad="%",
        default="5",
        # Un tope de más de la mitad del precio no es una decisión de negocio
        # que pueda tomar un sistema sin nadie mirando.
        maximo=50.0,
    ),
    "APROBACION_TIMEOUT_HORAS": Definicion(
        nombre="APROBACION_TIMEOUT_HORAS",
        alias=(
            "timeout de aprobacion",
            "timeout de aprobación",
            "espera de aprobacion",
            "espera de aprobación",
            "horas para decidir",
        ),
        significado=(
            "Cuánto espera una decisión pendiente antes de vencer. Es también "
            "lo que un borrador esperando respuesta puede retener stock: "
            "vencido el plazo, el pedido deja de competir con los pedidos vivos"
        ),
        unidad="h",
        default="4",
        # Más de una semana no es una espera, es un pedido olvidado que
        # retiene stock que otro cliente podía llevarse.
        maximo=168.0,
    ),
    "REVISION_TIMEOUT_HORAS": Definicion(
        nombre="REVISION_TIMEOUT_HORAS",
        alias=(
            "plazo de revision",
            "plazo de revisión",
            "revision manual",
            "revisión manual",
            "plazo para revisar",
        ),
        significado=(
            "Cuánto puede quedar un pedido esperando que una persona lo revise "
            "—después de que el cliente aceptó y algo había cambiado— antes de "
            "que el borrador se cierre y deje de retener stock"
        ),
        unidad="h",
        default="24",
        # A review nobody does is a draft holding units another customer could
        # have had. A week is already generous; more is a forgotten order.
        maximo=168.0,
    ),
    "AUTO_CONFIRM_DESCUENTOS_APRUEBAN": Definicion(
        nombre="AUTO_CONFIRM_DESCUENTOS_APRUEBAN",
        alias=("descuentos", "aprobar descuentos", "descuentos aprueban"),
        significado=(
            "Si está en sí, cualquier descuento —del pedido o de un renglón— "
            "pasa por una persona. En no, un descuento puede auto-confirmarse "
            "mientras el precio no supere la lista"
        ),
        unidad="sí/no",
        default="true",
        tipo=BOOLEANO,
    ),
    "PENDIENTE_AVISO_HORAS": Definicion(
        nombre="PENDIENTE_AVISO_HORAS",
        alias=(
            "aviso de pendiente",
            "horas para avisar",
            "recordatorio de pendiente",
        ),
        significado=(
            "Cuánto espera un pedido sin decisión antes de que se le avise al "
            "cliente que todavía no está confirmado, y de que te lo recuerde a "
            "vos. No confirma ni cancela nada: sólo deja de haber silencio. "
            "En NINGUNO no se avisa nada"
        ),
        unidad="h",
        # NINGUNO, como el cierre. Las dos mitades las enciende el dueño cuando
        # lo decide, con su código: desplegar esto no puede cambiar una sola
        # palabra de lo que recibe un cliente. Un default de 2 h significaba que
        # entre el deploy y el mensaje que lo apagaba había clientes recibiendo
        # WhatsApps de una función que nadie armó — y que quedaba encendida si
        # nadie se acordaba de mandarlo. Un valor razonable para arrancar es 2.
        default=NINGUNO,
        # Más de dos días no es un recordatorio: el cliente ya se fue a otro
        # proveedor y el aviso llega para confirmárselo.
        maximo=48.0,
        opcional=True,
    ),
    "AVISO_ANTES_DE_ENTREGA_HORAS": Definicion(
        nombre="AVISO_ANTES_DE_ENTREGA_HORAS",
        alias=(
            # `alias[0]` es el nombre que se muestra en TODOS lados, y por eso
            # va en español (tests/test_idioma_salida.py lo trata como un dato
            # legítimamente español). Los ingleses van después, nunca primero.
            #
            # Ninguno dice sólo «aviso» ni sólo «entrega»: la resolución por
            # subcadena de `definicion()` es global, y una palabra suelta que ya
            # resuelve a otro límite pasaría a ser ambigua para el dueño.
            "aviso antes de la entrega",
            "aviso previo a la entrega",
            "horas antes de la entrega",
            "delivery lead notice",
            "notice before delivery",
        ),
        significado=(
            "Cuánto antes de la entrega prometida se le avisa al cliente que "
            "su pedido TODAVÍA no está confirmado. No promete día, ni hora, ni "
            "precio: repite el plazo que el cliente ya conocía y dice que no "
            "llegó a confirmarse. Necesita además una hora de reparto "
            "configurada (ENTREGA_HORA), porque la fecha de entrega de ERPNext "
            "no tiene hora. En NINGUNO no se avisa nada"
        ),
        unidad="h",
        # NINGUNO, igual que el aviso y el cierre de pendientes, y por el mismo
        # motivo escrito arriba: entre el deploy y el mensaje que lo apagara
        # habría clientes recibiendo WhatsApps de una función que nadie armó.
        # Un valor razonable para arrancar es 3.
        default=NINGUNO,
        # Un día. Más que eso no es «antes de la entrega», es otro recordatorio
        # de que el pedido sigue sin confirmar — y ése ya existe, y es
        # PENDIENTE_AVISO_HORAS.
        maximo=24.0,
        opcional=True,
    ),
    "PENDIENTE_CIERRE_HORAS": Definicion(
        nombre="PENDIENTE_CIERRE_HORAS",
        alias=(
            "cierre de pendiente",
            "horas para cerrar",
            "cerrar pendientes",
        ),
        significado=(
            "Después de cuántas horas sin decisión se cierra un pedido que "
            "nadie miró, para que deje de retener stock que otro cliente podía "
            "llevarse. Al cliente se le dice, sin vueltas, que no se confirmó. "
            "En NINGUNO no se cierra nada y el borrador espera para siempre"
        ),
        unidad="h",
        default=NINGUNO,
        # Una semana. Treinta días no es un plazo, es un borrador olvidado
        # reteniendo stock que otro cliente podía llevarse — que es justo lo
        # que este límite existe para evitar.
        maximo=168.0,
        opcional=True,
    ),
    # Las horas de silencio. Frenan lo que el barrido le diría a un cliente sin
    # que lo haya pedido: el recordatorio de arriba y el cierre automático, que
    # también le habla. NO frenan la confirmación de un pedido, que sale cuando
    # el pedido se confirma, sean las 22:10 o las 6 de la mañana, porque el
    # cliente la está esperando.
    "PENDIENTE_NOCHE_DESDE": Definicion(
        nombre="PENDIENTE_NOCHE_DESDE",
        alias=("no molestar desde", "silencio desde", "noche desde"),
        significado="Desde qué hora no se le manda un recordatorio a un cliente",
        unidad="hh:mm",
        default="22:00",
        tipo=HORA,
    ),
    "PENDIENTE_NOCHE_HASTA": Definicion(
        nombre="PENDIENTE_NOCHE_HASTA",
        alias=("no molestar hasta", "silencio hasta", "noche hasta"),
        significado="Hasta qué hora no se le manda un recordatorio a un cliente",
        unidad="hh:mm",
        default="07:00",
        tipo=HORA,
    ),
    "AUTO_CONFIRM_SOMBRA": Definicion(
        nombre="AUTO_CONFIRM_SOMBRA",
        alias=("modo sombra", "sombra"),
        significado=(
            "Si está en sí, cada pedido que queda esperando anota qué habrían "
            "dicho las reglas si el tope y el stock estuvieran encendidos. No "
            "confirma nada: sólo deja el número para poder decidir con datos"
        ),
        unidad="sí/no",
        default="false",
        tipo=BOOLEANO,
    ),
    # LA BANDA DE PRECIO. Es lo que vuelve AUTOMÁTICO el cambio de precio: se
    # pone UNA vez y después el agente mueve precios solo, sin código y sin que
    # nadie confirme cada vez. En 0 —el default— no se mueve ninguno.
    #
    # Por qué hay banda y no es burocracia: un precio gobierna EN SILENCIO lo
    # que se auto-confirma. Con AUTO_CONFIRM_MAX en 0 un precio equivocado no
    # puede emitir nada; el día que el dueño lo levanta las dos cosas se
    # multiplican y un cero de más se convierte en pedidos emitidos a un precio
    # que nadie miró. La banda acota ESO — no le pide permiso a nadie.
    #
    # Es un PORCENTAJE contra el precio que ya está, así que un producto sin
    # precio previo no tiene contra qué medirse: ese caso se rechaza y lo
    # siembra `deploy/`. El máximo de 100 no es preferencia: arriba de duplicar,
    # «cambiar un precio» dejó de ser la palabra para lo que está pasando.
    "PRECIO_CAMBIO_MAX_PCT": Definicion(
        nombre="PRECIO_CAMBIO_MAX_PCT",
        alias=(
            "banda de precio", "cambio de precio", "variacion de precio",
            "variación de precio", "cuanto puede mover un precio",
            "cuánto puede mover un precio",
        ),
        significado=(
            "Cuánto puede moverse un precio de una sola vez, en por ciento, sin "
            "que lo mire nadie. En 0 el agente no cambia ningún precio"
        ),
        unidad="%",
        default="0",
        maximo=100.0,
    ),
}

_VERDADEROS = frozenset({"true", "si", "sí", "1", "on", "yes", "y"})
_FALSOS = frozenset({"false", "no", "0", "off", "n"})


# ---------------------------------------------------------------------------
# Las reglas de ENTREGA: mismo dueño, mismo código de dos pasos, misma
# auditoría durable. Registro aparte a propósito.
# ---------------------------------------------------------------------------
#
# WHY A SECOND REGISTRY AND NOT NINE MORE ENTRIES IN LIMITES
# configuracion() validates EVERY entry of LIMITES and raises on the first bad
# one, and app/policy.py calls it once per order LINE and again inside the
# submit lock. Put a delivery day in there and a typo in "martes" stops every
# customer's order from confirming — a delivery-schedule mistake would become
# an outage. These settings are read by app/excepciones.py instead, on their
# own, where a bad value fails closed as "no exception is pre-authorized" and
# affects nothing else.
#
# Everything the owner touches is shared: definicion(), validar(), vigente(),
# resumen(), proponer() and aplicar() all work over TODOS, so a delivery
# setting changes through exactly the same two-step confirmation code and lands
# in the same append-only audit — in Redis and in the durable ERPNext comment.

ENTREGA: dict[str, Definicion] = {
    # Las zonas van PRIMERO porque son la regla que gatea a todas las demás:
    # sin ninguna lista, app/entrega.py no entrega nada solo, no importa qué
    # días ni a qué hora esté configurado. Hasta ahora salían únicamente del
    # entorno, así que "permití reparto en tal ciudad" era la única regla de
    # entrega que el dueño NO podía cambiar por WhatsApp — tenía que editar el
    # .env y reiniciar. Ahora pasa por el mismo propose+código de cuatro
    # dígitos que el resto.
    "ZONAS_ENTREGA_LOCALIDADES": Definicion(
        nombre="ZONAS_ENTREGA_LOCALIDADES",
        alias=(
            "localidades de reparto",
            "localidades",
            "zonas de reparto",
            "zonas",
            "ciudades de reparto",
            "ciudades",
        ),
        significado=(
            "Las localidades donde se reparte sin que lo mire una persona. Con "
            "los códigos postales también cargados, la dirección necesita LOS "
            "DOS datos permitidos"
        ),
        unidad="localidades",
        default=NINGUNO,
        tipo=LOCALIDADES,
        opcional=True,
    ),
    "ZONAS_ENTREGA_CP": Definicion(
        nombre="ZONAS_ENTREGA_CP",
        alias=(
            "codigos postales",
            "códigos postales",
            "codigo postal",
            "código postal",
            "cp de reparto",
            "cp",
        ),
        significado=(
            "Los códigos postales donde se reparte sin que lo mire una "
            "persona. Con las localidades también cargadas, la dirección "
            "necesita LOS DOS datos permitidos"
        ),
        unidad="códigos postales",
        default=NINGUNO,
        tipo=CODIGOS_POSTALES,
        opcional=True,
    ),
    "ENTREGA_DIAS": Definicion(
        nombre="ENTREGA_DIAS",
        alias=("días de reparto", "dias de reparto", "días de entrega", "dias de entrega"),
        significado=(
            "Los días en que sale el reparto normal. Es lo que se le ofrece a "
            "un cliente cuando una solicitud vence sin que nadie la conteste"
        ),
        unidad="días",
        default=NINGUNO,
        tipo=DIAS,
        opcional=True,
    ),
    "ENTREGA_HORA": Definicion(
        nombre="ENTREGA_HORA",
        alias=("hora de reparto", "hora de entrega", "horario de reparto"),
        significado="La hora que se promete para el reparto normal",
        unidad="hh:mm",
        default=NINGUNO,
        tipo=HORA,
        opcional=True,
    ),
    "ENTREGA_EXCEPCION_ACTIVA": Definicion(
        nombre="ENTREGA_EXCEPCION_ACTIVA",
        alias=("entregas fuera de día", "entregas fuera de dia", "excepciones de entrega"),
        significado=(
            "Si está en sí, se puede entregar un día sin reparto sin que lo "
            "mire nadie, siempre que el resto de la configuración cierre"
        ),
        unidad="sí/no",
        default="false",
        tipo=BOOLEANO,
    ),
    "ENTREGA_EXCEPCION_DIAS": Definicion(
        nombre="ENTREGA_EXCEPCION_DIAS",
        alias=("días fuera de día", "dias fuera de dia", "días de excepción", "dias de excepcion"),
        significado="Los días en que sí se entrega fuera del reparto normal",
        unidad="días",
        default=NINGUNO,
        tipo=DIAS,
        opcional=True,
    ),
    "ENTREGA_EXCEPCION_HORA": Definicion(
        nombre="ENTREGA_EXCEPCION_HORA",
        alias=("hora fuera de día", "hora fuera de dia", "hora de excepción", "hora de excepcion"),
        significado="La hora que se promete para una entrega fuera de día",
        unidad="hh:mm",
        default=NINGUNO,
        tipo=HORA,
        opcional=True,
    ),
    "ENTREGA_EXCEPCION_CARGO": Definicion(
        nombre="ENTREGA_EXCEPCION_CARGO",
        alias=("cargo fuera de día", "cargo fuera de dia", "cargo de excepción", "cargo de envío"),
        significado=(
            "Lo que se cobra por una entrega fuera de día. Sólo se escribe en "
            "el pedido si además está configurada la cuenta contable"
        ),
        unidad="$",
        default=NINGUNO,
        opcional=True,
        maximo=10_000_000.0,
    ),
    "ENTREGA_EXCEPCION_MIN_TOTAL": Definicion(
        nombre="ENTREGA_EXCEPCION_MIN_TOTAL",
        alias=("mínimo fuera de día", "minimo fuera de dia", "mínimo de excepción", "pedido mínimo"),
        significado=(
            "Total mínimo del pedido para que una entrega fuera de día esté "
            "pre-autorizada. Con 0 no hay mínimo"
        ),
        unidad="$",
        default="0",
        maximo=100_000_000.0,
    ),
    "RETIRO_LOCAL_ACTIVO": Definicion(
        nombre="RETIRO_LOCAL_ACTIVO",
        alias=("retiro en el local", "retiro por el local", "retiros"),
        significado=(
            "Si está en sí, cuando no hay reparto al que subir un pedido se le "
            "puede ofrecer al cliente que lo pase a buscar"
        ),
        unidad="sí/no",
        default="false",
        tipo=BOOLEANO,
    ),
    "RETIRO_LOCAL_DIAS": Definicion(
        nombre="RETIRO_LOCAL_DIAS",
        alias=("días de retiro", "dias de retiro"),
        significado="Los días en que se puede pasar a buscar un pedido por el local",
        unidad="días",
        default=NINGUNO,
        tipo=DIAS,
        opcional=True,
    ),
    "RETIRO_LOCAL_HORA": Definicion(
        nombre="RETIRO_LOCAL_HORA",
        alias=("hora de retiro", "horario de retiro"),
        significado="La hora a la que se puede pasar a buscar un pedido",
        unidad="hh:mm",
        default=NINGUNO,
        tipo=HORA,
        opcional=True,
    ),
}

# El idioma del agente de gestión. Grupo aparte de LIMITES y de ENTREGA, con
# su propia marca durable: no alimenta configuracion() —no decide si un pedido
# se confirma solo— y su pérdida no puede frenar nada.
IDIOMAS: dict[str, Definicion] = {
    "IDIOMA_GERENCIA": Definicion(
        nombre="IDIOMA_GERENCIA",
        alias=(
            "idioma de gerencia",
            "idioma gerencia",
            "manager language",
            "idioma del gerente",
            "idioma de gestion",
            "idioma de gestión",
            "language",
            "idioma",
        ),
        significado="En qué idioma le contesta el sistema al equipo",
        unidad="idioma",
        default="es",
        tipo=IDIOMA,
    ),
}

# ---------------------------------------------------------------------------
# EL NEGOCIO: cómo se llama, a qué se dedica, cuándo atiende.
# ---------------------------------------------------------------------------
#
# POR QUÉ ESTOS CUATRO Y NO EL `.env` ENTERO. La lista es de lo PERMITIDO y no
# de lo prohibido, que es la misma decisión que toma `memoria.CLAVES_PARA_
# CLIENTES` y por la misma razón: lo que nadie clasificó no se puede tocar, y
# ése es el único default que no se equivoca en la dirección peligrosa. Lo que
# queda afuera a propósito, con el motivo:
#
#   * las credenciales —las tres de ERPNext, la de Meta, la del modelo—: quien
#     las puede cambiar puede reemplazar el sistema entero, y el panel se
#     entra con un token que puede robarse;
#   * TELEFONOS_EQUIPO y TELEFONO_DUENO: son la frontera de AUTORIZACIÓN. Un
#     número agregado ahí es un agente de gerencia nuevo, o sea que un ajuste
#     se convertiría en una forma de darse permisos;
#   * BUSINESS_TIMEZONE, LOCALE, AUTO_CONFIRM_PRICE_LIST y AUTO_CONFIRM_
#     CURRENCY: se fijan en la instalación y los leen módulos que los toman una
#     sola vez al importarse (`app/policy.py`), así que cambiarlos en caliente
#     diría que cambiaron sin que cambie nada;
#   * ERPNEXT_COMPANY y ERPNEXT_WAREHOUSE: nombran registros de ERPNext que
#     tienen que existir, y nadie puede verificar acá que existan.
#
# Los cuatro de acá no deciden nada: entran en una frase. Por eso el registro
# es propio y no alimenta configuracion() — un nombre de negocio mal escrito no
# puede frenar un pedido, igual que un día de reparto no puede (ver ENTREGA).
NEGOCIO: dict[str, Definicion] = {
    "NOMBRE_NEGOCIO": Definicion(
        nombre="NOMBRE_NEGOCIO",
        alias=("nombre del negocio", "nombre de la empresa", "business name"),
        significado="Cómo se llama el negocio. Es la primera línea de los dos prompts",
        unidad="texto",
        # Opcional con default NINGUNO y no `default=""`: sin nombre cargado,
        # `conversacion.negocio()` contesta «la empresa» desde antes de esto, y
        # un ajuste nuevo no puede cambiar lo que ve un cliente. "" en el
        # almacén significa «no fijado» para `_resolver`, así que el sentinela
        # es la única forma de que borrarlo no reviva el valor del `.env`.
        default="-",
        opcional=True,
        tipo=TEXTO,
        # 60, igual que el recorte de `conversacion._dato_de_entorno`: guardar
        # más de lo que esa función va a dejar pasar le muestra al dueño un
        # nombre y le hace usar otro al agente.
        largo=60,
    ),
    "NOMBRE_AGENTE": Definicion(
        nombre="NOMBRE_AGENTE",
        alias=("nombre del agente", "nombre del asistente", "agent name"),
        significado=(
            "Con qué nombre se presenta el que atiende el WhatsApp. Vacío, se "
            "presenta por lo que hace y no se inventa ninguno"
        ),
        unidad="texto",
        default="-",
        tipo=TEXTO,
        largo=40,
        opcional=True,
    ),
    "RUBRO_NEGOCIO": Definicion(
        nombre="RUBRO_NEGOCIO",
        alias=("rubro", "rubro del negocio", "line of business"),
        significado="A qué se dedica el negocio, en pocas palabras",
        unidad="texto",
        default="-",
        tipo=TEXTO,
        largo=60,
        opcional=True,
    ),
    "HORARIO_ATENCION": Definicion(
        nombre="HORARIO_ATENCION",
        alias=(
            "horario de atencion",
            "horario de atención",
            "opening hours",
            "business hours",
        ),
        significado="En qué horario atiende el negocio, como se lo contás a un cliente",
        unidad="texto",
        default="lunes a viernes de 8 a 17",
        tipo=TEXTO,
        largo=120,
    ),
}

# ---------------------------------------------------------------------------
# LAS PLANTILLAS DE META. Registro aparte, y el motivo no es el mismo que el de
# ENTREGA.
# ---------------------------------------------------------------------------
#
# Una plantilla no es una regla del negocio: es el NOMBRE con el que Meta
# conoce un mensaje que ya aprobó. Cambiarla no cambia lo que el sistema
# decide, cambia si el aviso sale o no sale. Por eso están acá y no en
# LIMITES, y por eso el panel las muestra en su propio grupo: son la
# configuración que más veces se toca al instalar —una por una, a medida que
# Meta va aprobando— y la que peor se toca por `.env`, porque cada cambio
# pedía editar el archivo en el servidor y recrear el contenedor.
#
# TODAS OPCIONALES, y no es flojera: vacía significa «esta plantilla todavía no
# está registrada», que es el estado normal durante media instalación.
# app/readiness.py es el que decide cuál de esas ausencias es un AVISO y cuál
# es un FALTA, y sigue decidiéndolo él.
PLANTILLAS: dict[str, Definicion] = {
    "WHATSAPP_TEMPLATE_LANGUAGE": Definicion(
        nombre="WHATSAPP_TEMPLATE_LANGUAGE",
        alias=("idioma de las plantillas", "template language"),
        significado=(
            "En qué idioma registraste las plantillas en Meta. Si no coincide "
            "con el registro, Meta contesta que la plantilla no existe"
        ),
        unidad="idioma de Meta",
        default="es_AR",
        tipo=IDIOMA_PLANTILLA,
    ),
}

# Las doce plantillas, en una sola forma. Escribir doce `Definicion` a mano era
# doce oportunidades de que una quedara sin `tipo=PLANTILLA` —y un tipo que no
# se nombra en `validar` se valida como NÚMERO, que es el bug que el comentario
# de ahí arriba describe—. Acá el tipo es uno solo y lo pone el bucle.
_PLANTILLAS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE", ("plantilla de confirmado",),
     "Le avisa al cliente que su pedido quedó confirmado"),
    ("WHATSAPP_CUSTOMER_REJECTED_TEMPLATE", ("plantilla de rechazado",),
     "Le avisa al cliente que su pedido no se pudo tomar"),
    ("WHATSAPP_CUSTOMER_CANCELLED_TEMPLATE", ("plantilla de cancelado",),
     "Le avisa al cliente que su pedido se canceló"),
    ("WHATSAPP_CUSTOMER_EXPIRED_TEMPLATE", ("plantilla de vencido",),
     "Le avisa al cliente que su solicitud venció sin respuesta"),
    ("WHATSAPP_CUSTOMER_FALLBACK_TEMPLATE", ("plantilla de respaldo",),
     "El aviso al cliente cuando ningún otro texto aplica"),
    ("WHATSAPP_CUSTOMER_REVIEW_EXPIRED_TEMPLATE", ("plantilla de revision vencida",),
     "Le avisa al cliente que venció el plazo de revisión de su pedido"),
    ("WHATSAPP_CUSTOMER_PENDING_TEMPLATE", ("plantilla de pendiente",),
     "Le recuerda al cliente que su pedido sigue esperando una decisión"),
    ("WHATSAPP_CUSTOMER_PENDING_CLOSED_TEMPLATE", ("plantilla de pendiente cerrado",),
     "Le avisa al cliente que su pedido pendiente se cerró"),
    ("WHATSAPP_CUSTOMER_DELIVERY_LEAD_TEMPLATE", ("plantilla de aviso de entrega",),
     "Le avisa al cliente unas horas antes de que le llegue el reparto"),
    ("WHATSAPP_STAFF_PENDING_TEMPLATE", ("plantilla de pendiente al equipo",),
     "Le avisa al equipo que hay un pedido esperando que alguien lo mire"),
    ("WHATSAPP_STAFF_CONFIRMED_TEMPLATE", ("plantilla de confirmado al equipo",),
     "Le avisa al equipo que un pedido quedó confirmado"),
    ("WHATSAPP_STAFF_ALERT_TEMPLATE", ("plantilla de alerta al equipo",),
     "El aviso al equipo cuando algo necesita atención ahora"),
)
for _nombre, _alias, _significado in _PLANTILLAS:
    PLANTILLAS[_nombre] = Definicion(
        nombre=_nombre,
        alias=_alias,
        significado=_significado,
        unidad="plantilla de Meta",
        default="-",
        tipo=PLANTILLA,
        opcional=True,
    )
del _nombre, _alias, _significado

# Everything the owner can set, in one mapping. LIMITES stays separate above
# because only it feeds configuracion().
TODOS: dict[str, Definicion] = {**LIMITES, **ENTREGA, **IDIOMAS, **NEGOCIO, **PLANTILLAS}


# Para qué pantalla es cada ajuste. Se deriva de EN QUÉ REGISTRO está y no de
# un campo nuevo, así que no hay forma de que un ajuste diga que pertenece a un
# grupo y esté en otro: la membresía es la única fuente.
GRUPO_LIMITES = "limites"
GRUPO_ENTREGA = "entrega"
GRUPO_IDIOMA = "idioma"
GRUPO_NEGOCIO = "negocio"
GRUPO_PLANTILLAS = "plantillas"


def grupo(nombre: str) -> str:
    """En qué grupo cae un ajuste. Lo usa el panel para agruparlos."""
    if nombre in LIMITES:
        return GRUPO_LIMITES
    if nombre in ENTREGA:
        return GRUPO_ENTREGA
    if nombre in IDIOMAS:
        return GRUPO_IDIOMA
    if nombre in NEGOCIO:
        return GRUPO_NEGOCIO
    return GRUPO_PLANTILLAS

# La cuenta contable NO se toca por WhatsApp. Es un account head real de
# ERPNext: escribir el nombre equivocado no rompe el bot, desbalancea la
# contabilidad del dueño, y ningún modelo interpretando "poneme la cuenta de
# fletes" puede verificar que exista. Se sigue configurando por entorno.
CUENTA_CARGO = "ENTREGA_CARGO_CUENTA"

# Accent-free and lowercase, so "Miércoles" and "miercoles" are one day. The
# normal form stored is this spelling, which is also what app/excepciones.py
# parses — one vocabulary, so a value cannot validate here and fail there.
_ORDEN_DIAS = (
    "lunes",
    "martes",
    "miercoles",
    "jueves",
    "viernes",
    "sabado",
    "domingo",
)
_DIAS_SEMANA = {nombre: indice for indice, nombre in enumerate(_ORDEN_DIAS)}

# CÓMO LO ESCRIBE UNA PERSONA, y por qué lo guardado sigue siendo lo de arriba.
#
# Un dueño que lee en inglés no podía configurar NINGUNA de las tres reglas de
# días —ENTREGA_DIAS, ENTREGA_EXCEPCION_DIAS y RETIRO_LOCAL_DIAS— porque
# "delivery days monday,friday" moría en «"monday" no es un día de la semana».
#
# Lo que se guarda NO cambia: "lunes,viernes" en los dos casos. Es la decisión
# entera de este cambio. El valor normal es lo que ya está en el almacén de cada
# despliegue, lo que `app/excepciones.py` parsea, lo que la auditoría durable
# escribió y lo que dicen los mensajes que ya salieron; traducir el valor
# guardado sería migrar todo eso para que un owner pueda teclear otra palabra.
# Acá se amplía lo que se ACEPTA, y `mostrar()` traduce a la salida.
#
# Las abreviaturas son las que una persona escribe de verdad (mon, tue, tues,
# wed, thu, thurs, fri, sat, sun) y los plurales que salen solos al dictar una
# lista ("mondays and fridays"). No se inventan abreviaturas en español: el
# español ya andaba entero y agregarle formas nuevas es superficie sin pedido.
_DICHOS_EN = {
    "lunes": ("monday", "mondays", "mon"),
    "martes": ("tuesday", "tuesdays", "tue", "tues"),
    "miercoles": ("wednesday", "wednesdays", "wed", "weds"),
    "jueves": ("thursday", "thursdays", "thu", "thur", "thurs"),
    "viernes": ("friday", "fridays", "fri"),
    "sabado": ("saturday", "saturdays", "sat"),
    "domingo": ("sunday", "sundays", "sun"),
}
_DIAS_DICHOS = {canonico: canonico for canonico in _ORDEN_DIAS}
for _canonico, _formas in _DICHOS_EN.items():
    for _forma in _formas:
        _DIAS_DICHOS[_forma] = _canonico
_HORA_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _sin_tildes(texto: object) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", str(texto or "").lower())
        if unicodedata.category(char) != "Mn"
    ).strip()


@dataclass(frozen=True)
class Configuracion:
    """Los límites ya validados, para una evaluación."""

    tope: float
    tope_qty_por_producto: float
    buffer: float  # fracción 0..0.95, ya dividida por 100
    tope_cliente_nuevo: float
    tope_deuda: float
    descuentos_aprueban: bool
    tope_descuento_pct: float  # fracción 0..0.5, ya dividida por 100
    # Horas que espera una decisión pendiente, y que su borrador puede retener
    # stock. Lo lee app/solicitudes.py; nunca es 0 (un plazo de 0 vencería
    # todo al instante y un plazo infinito congelaría el stock).
    timeout_aprobacion: float = 4.0
    # Lo mismo para un pedido que quedó esperando que lo revise una persona.
    # Sin plazo, ese borrador retendría stock para siempre: es la única salida
    # del flujo que no la tenía.
    timeout_revision: float = 24.0
    # ¿Se anota lo que las reglas habrían dicho? No decide nada: es el registro
    # con el que el dueño después mueve un límite. Default false, así que un
    # .env que no la nombra se comporta exactamente como hoy.
    sombra: bool = False


def _texto(valor: object) -> str:
    if isinstance(valor, bytes):
        return valor.decode("utf-8", "replace")
    return "" if valor is None else str(valor)


_durable_cache: tuple[float, bool] | None = None
_durable_cache_entrega: tuple[float, bool] | None = None
_durable_cache_idioma: tuple[float, bool] | None = None


def _consultar_marca(nombre: str, queja: str, clave: str = "") -> bool:
    """¿Hay en ERPNext algún comentario de auditoría con esa marca?

    Sin cachear: los tres que preguntan tienen su propio caché, porque una marca
    puede estar y la otra no y ésa es exactamente la distinción que importa.

    La consulta la comparte `app/marcas.py`; la POLÍTICA DE ERROR se queda acá,
    y es la razón de que el lector compartido deje salir la excepción en vez de
    contestar por todos. Los tres que llaman contestan distinto al «no pude
    averiguarlo»: límites LEVANTA —y con eso no se auto-confirma nada—, entrega
    devuelve True y idioma devuelve False. Un lector que eligiera una de las
    tres sería un cambio de comportamiento con plata atada.
    """
    try:
        return marcas.existe(nombre)
    except erpnext.ERPNextError as exc:
        # `clave` la pone quien llama, igual que `queja`, y por el mismo motivo:
        # los tres contestan distinto al «no pude averiguarlo». El de LÍMITES es
        # el único que LEVANTA, y su excepción llega hasta `ajustes.preparar`,
        # que la mete adentro de una respuesta ya traducida — así que necesita
        # su clave. Los otros dos la miran y la loguean, y por eso no la traen.
        raise LimiteError(queja, clave=clave) from exc


def _hubo_cambios_durables() -> bool:
    """Si ERPNext recuerda que alguna vez se configuró un LÍMITE.

    Es la única pregunta que Redis no puede contestar sobre sí mismo. La
    respuesta se cachea un minuto: si es «sí» el sistema ya está fallando
    cerrado, y si es «no» es porque nunca se configuró nada y no hay nada que
    perder.

    Pregunta SÓLO por MARCA_DURABLE. Un cambio de reglas de entrega no arma
    este fusible: no es un límite y no decide si un pedido se confirma solo.
    """
    global _durable_cache
    ahora = time.monotonic()
    if _durable_cache and _durable_cache[0] > ahora:
        return _durable_cache[1]
    hubo = _consultar_marca(
        "limite",
        "no pude verificar en ERPNext si los límites se configuraron antes",
        clave="limite.marca_no_verificable",
    )
    _durable_cache = (ahora + DURABLE_CACHE_SEGUNDOS, hubo)
    return hubo


def _hubo_cambios_durables_entrega() -> bool:
    """Si ERPNext recuerda que alguna vez se configuró una regla de ENTREGA.

    NUNCA levanta: una regla de entrega que no se puede resolver cuesta un
    mensaje de WhatsApp, no una venta que no cierra, y ésa es la asimetría
    entera entre los dos registros.

    «No pude averiguarlo» devuelve True, o sea se trata como almacén perdido.
    No es pesimismo: lo único que hace ese True es NO habilitar el entorno de
    arranque, y todo lo que hay en Entrega sólo puede ensanchar lo que el
    sistema ofrece por su cuenta. Fallar para el otro lado sería restaurar un
    día de reparto que el dueño borró.
    """
    global _durable_cache_entrega
    ahora = time.monotonic()
    if _durable_cache_entrega and _durable_cache_entrega[0] > ahora:
        return _durable_cache_entrega[1]
    try:
        hubo = _consultar_marca("entrega", "entrega no verificable")
    except LimiteError:
        print(
            "[limites] no pude verificar en ERPNext si las reglas de entrega "
            "se configuraron antes: no habilito el entorno de arranque"
        )
        return True
    _durable_cache_entrega = (ahora + DURABLE_CACHE_SEGUNDOS, hubo)
    return hubo


def _reglas_de_entrega_perdidas(almacen: dict[str, str]) -> bool:
    """An EMPTY delivery store with [entrega] changes on record: it was wiped.

    ONE question, asked by entrega() — which decides — and by resumen() and
    vigente() — which show — so what the owner is shown cannot disagree with
    what the system will do. Falling back to the bootstrap environment in this
    state would restore whatever the .env says: a day he removed, an exception
    he turned off. A silent WIDENING, so nothing is in effect until he sets
    the rules again.
    """
    sin_reglas_del_dueno = not any(almacen.get(nombre, "").strip() for nombre in ENTREGA)
    return sin_reglas_del_dueno and _hubo_cambios_durables_entrega()


def _hubo_cambios_durables_idioma() -> bool:
    """Si ERPNext recuerda que alguna vez se fijó un idioma. NUNCA levanta.

    A diferencia de entrega, «no pude averiguarlo» devuelve False. No hay tal
    cosa como «ningún idioma en vigencia»: algo hay que escribir. Ante la duda
    se contesta en el idioma por defecto, que es exactamente lo que hacía el
    sistema antes de que este ajuste existiera.
    """
    global _durable_cache_idioma
    ahora = time.monotonic()
    if _durable_cache_idioma and _durable_cache_idioma[0] > ahora:
        return _durable_cache_idioma[1]
    try:
        hubo = _consultar_marca("idioma", "idioma no verificable")
    except LimiteError:
        print(
            "[limites] no pude verificar en ERPNext si el idioma se fijó antes; "
            "sigo con el idioma por defecto"
        )
        return False
    _durable_cache_idioma = (ahora + DURABLE_CACHE_SEGUNDOS, hubo)
    return hubo


def idioma_gerencia() -> str:
    """El idioma del agente de gestión. NUNCA levanta y nunca frena nada.

    Lee el almacén DIRECTO y no por _almacen(): ese fusible existe para que un
    límite perdido no deje que algo se confirme solo, y un idioma no autoriza
    nada. Si se aplicara acá, perder el almacén de límites dejaría al sistema
    sin poder contestarle al dueño — que es peor que contestarle en español.

    El orden es el mismo de _resolver: lo que fijó el dueño, después el
    entorno de arranque, después el default.
    """
    from app import idioma as idioma_mod

    fijado = idioma_gerencia_guardado()
    if fijado is None:
        # No se pudo LEER, que no es lo mismo que «no fijó nada»: se va al
        # default sin mirar el entorno, exactamente como antes de separar esto
        # en dos funciones. Un refactor que corrige de paso una ruta de fallo
        # que nadie pidió es un cambio de comportamiento escondido en un
        # cambio de forma.
        return idioma_mod.por_defecto()
    if fijado:
        return fijado
    del_entorno = idioma_mod.normalizar(os.getenv("IDIOMA_GERENCIA", ""))
    if del_entorno:
        return del_entorno
    return idioma_mod.por_defecto()


def idioma_gerencia_guardado() -> str | None:
    """SÓLO lo que el dueño dejó fijado por WhatsApp.

    TRES respuestas, y hay que distinguir las tres:
      - ``None`` -> no se pudo leer el almacén. No es «no fijó nada».
      - ``""``   -> se leyó bien y no hay nada fijado.
      - un idioma -> lo que el dueño eligió a mano, con su código.

    Existe separado de `idioma_gerencia()` porque hay un llamador que necesita
    las dos mitades por separado y no la resolución ya hecha: `readiness`, que
    valida un `.env` CANDIDATO. Preguntando por la resolución completa recibía
    el `os.environ` del proceso que está corriendo el chequeo —o sea el `.env`
    VIEJO— y podía informar «el dueño recibe castellano» sobre un archivo que
    dice `IDIOMA_GERENCIA=en`. Un preflight que contesta sobre otro archivo es
    peor que no tenerlo.

    Lo guardado le sigue GANANDO al entorno en los dos llamadores: es lo que el
    dueño eligió a mano, con su código de cuatro dígitos.
    """
    from app import idioma as idioma_mod

    try:
        crudo = locks.conexion().hgetall(CLAVE_VALORES)
    except (locks.CoordinationError, RedisError) as exc:
        print(f"[limites] no pude leer el idioma de gerencia ({type(exc).__name__})")
        return None
    valores = {_texto(k): _texto(v) for k, v in (crudo or {}).items()}
    return idioma_mod.normalizar(valores.get("IDIOMA_GERENCIA", "").strip()) or ""


def _almacen() -> dict[str, str]:
    """Lo que fijó el dueño. Falla cerrada si Redis no contesta.

    No cae al entorno cuando Redis está caído: eso convertiría una caída en un
    aflojamiento silencioso de un límite que el dueño había apretado. Y si el
    almacén aparece VACÍO pero ERPNext tiene cambios registrados, entonces se
    perdieron los datos: tampoco se cae al entorno, porque el valor de arranque
    puede ser más flojo que el que había fijado el dueño.
    """
    try:
        crudo = locks.conexion().hgetall(CLAVE_VALORES)
    except (locks.CoordinationError, RedisError) as exc:
        raise LimiteError(
            "no pude leer los límites configurados", clave="limite.no_pude_leer"
        ) from exc
    valores = {_texto(k): _texto(v) for k, v in (crudo or {}).items()}
    # El hash es UNO para límites, reglas de entrega e idioma. La pregunta es
    # si faltan LOS LÍMITES, no si el hash está vacío: después de una pérdida,
    # un cambio de idioma o de un día de reparto vuelve a llenar el hash y, con
    # la pregunta anterior, desarmaba este fusible para siempre — cada tope
    # pasaba a resolverse del .env sin aviso. Misma forma que
    # _reglas_de_entrega_perdidas: se mira el registro propio.
    sin_limites_del_dueno = not any(valores.get(nombre, "").strip() for nombre in LIMITES)
    if sin_limites_del_dueno and _hubo_cambios_durables():
        raise LimiteError(
            "los límites que configuró el dueño no están en el almacén, y "
            "ERPNext tiene cambios registrados: hay que restaurarlos antes de "
            "que algo se confirme solo",
            clave="limite.perdidos",
        )
    return valores


# De dónde salió un valor. Constantes y no literales sueltos porque hay un
# segundo lector fuera de este módulo —`readiness` distingue lo que fijó el
# dueño de lo que dice el `.env` candidato— y dos copias de una palabra son dos
# vocabularios que se separan sin que nada se ponga rojo.
ORIGEN_DUENO = "dueño"
ORIGEN_ARRANQUE = "arranque"
ORIGEN_DEFAULT = "default"


def _resolver(nombre: str, almacen: dict[str, str]) -> tuple[str, str]:
    """(valor, origen). origen: 'dueño' | 'arranque' | 'default'."""
    fijado = almacen.get(nombre, "").strip()
    if fijado:
        return fijado, ORIGEN_DUENO
    del_entorno = os.getenv(nombre, "").strip()
    if del_entorno:
        return del_entorno, ORIGEN_ARRANQUE
    return TODOS[nombre].default, ORIGEN_DEFAULT


# "1.500" is fifteen hundred pesos to an Argentine owner and one-and-a-half to
# float(). app/solicitudes.py::parsear_terminos already strips the dots when
# the manager types a delivery fee, so a money SETTING has to read the same
# way or the same three keystrokes mean two things. The rule is deliberately
# narrow — groups of exactly three digits — so "1.5" stays 1.5 and a percentage
# or a number of hours is never touched.
_MILES = re.compile(r"^\d{1,3}(\.\d{3})+$")
# Which units a "." can be a THOUSANDS separator in. An owner writing "1.000"
# means a thousand pesos and a thousand litres, but "1.5" hours and "1.5" per
# cent are decimals — and in es-AR nobody groups a percentage. Getting this
# wrong on a ceiling is a 1000x error in the direction that oversells, so the
# rule is a list of units rather than a guess about the digits.
_CON_MILES = frozenset({"$", "unidad de stock"})


def _numero(defi: Definicion, crudo: str, *, tecleado: bool = True) -> float:
    """El número, o LimiteError.

    ``tecleado`` dice que este texto lo escribió una PERSONA, que es la única
    vez que se puede aplicar la regla de miles — porque esa regla NO es
    idempotente. "1,125" es un peso doce; normaliza a "1.125"; y volver a
    normalizar ESO lee el punto como separador de miles y da 1125. Corriendo
    dos veces, una en proponer() y otra en aplicar(), le muestra al dueño un
    número y guarda otro mil veces más grande. Así que un valor que ya está en
    forma normal se lee como float y nunca se re-agrupa.
    """
    texto = str(crudo).strip().replace("$", "").strip()
    if tecleado and defi.unidad in _CON_MILES:
        entero, coma, decimales = texto.partition(",")
        if _MILES.match(entero):
            entero = entero.replace(".", "")
        texto = f"{entero},{decimales}" if coma else entero
    try:
        valor = float(texto.replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise LimiteError(
            f"«{defi.alias[0]}» no es un número: {crudo!r}",
            clave="limite.no_es_numero",
            datos={"ajuste": defi.alias[0], "valor": repr(crudo)},
        ) from exc
    if valor != valor or valor in (float("inf"), float("-inf")):
        raise LimiteError(
            f"«{defi.alias[0]}» no es un número usable: {crudo!r}",
            clave="limite.no_es_numero_usable",
            datos={"ajuste": defi.alias[0], "valor": repr(crudo)},
        )
    if valor < defi.minimo:
        raise LimiteError(
            f"«{defi.alias[0]}» no puede ser menor que {defi.minimo:g}",
            clave="limite.minimo",
            datos={"ajuste": defi.alias[0], "minimo": f"{defi.minimo:g}"},
        )
    if valor > defi.maximo:
        raise LimiteError(
            f"«{defi.alias[0]}» {valor:g} es imposible: el máximo es "
            f"{defi.maximo:g} {defi.unidad}".strip(),
            clave="limite.maximo",
            datos={
                "ajuste": defi.alias[0],
                "valor": f"{valor:g}",
                "maximo": f"{defi.maximo:g} {defi.unidad}".strip(),
            },
        )
    return valor


def _bool(defi: Definicion, crudo: str) -> bool:
    normal = str(crudo).strip().lower()
    if normal in _VERDADEROS:
        return True
    if normal in _FALSOS:
        return False
    raise LimiteError(
        f"«{defi.alias[0]}» tiene que ser sí o no, no {crudo!r}",
        clave="limite.si_o_no",
        datos={"ajuste": defi.alias[0], "valor": repr(crudo)},
    )


def _dias(defi: Definicion, crudo: str) -> str:
    """"Martes y viernes" y "tuesday and friday" -> "martes,viernes".

    Deterministic, no judgement. Separators are commas, whitespace and the
    words "y" and "and", because that is how a person writes a list in each
    language. Anything that is not a weekday is refused by name: silently
    dropping it would schedule a round the owner did not ask for.

    THE STORED VALUE IS ALWAYS THE SPANISH ONE. Accepting a second vocabulary
    is not storing a second one: what goes to the store is the normal form that
    every deployment already has, that app/excepciones.py reads and that the
    durable audit wrote. `mostrar()` is what translates it on the way out.
    """
    texto = _sin_tildes(crudo).replace(" y ", ",").replace(" and ", ",")
    partes = [parte for parte in re.split(r"[,\s]+", texto) if parte]
    if not partes:
        raise LimiteError(
            f"«{defi.alias[0]}» está vacío: decime qué días",
            clave="limite.dias_vacio",
            datos={"ajuste": defi.alias[0]},
        )
    elegidos: set[str] = set()
    for parte in partes:
        canonico = _DIAS_DICHOS.get(parte)
        if canonico is None:
            raise LimiteError(
                f"«{parte}» no es un día de la semana. Van así: "
                f"{', '.join(_ORDEN_DIAS)}",
                clave="limite.dia_desconocido",
                # La lista de días válidos NO se interpola: cada idioma nombra
                # en el catálogo las formas que de verdad parsean en él, que
                # desde #33 son las dos. Un test cruza esa lista contra `_dias`
                # para que no se separen.
                datos={"valor": parte},
            )
        elegidos.add(canonico)
    return ",".join(dia for dia in _ORDEN_DIAS if dia in elegidos)


def _partes_de_lista(crudo: str) -> list[str]:
    """Lo que una persona escribe como lista: comas, y la palabra "y".

    NO se corta por espacios: "Villa Allende" es UNA localidad. Por eso esto
    no es `_dias`, que sí puede cortar por espacios porque ningún día de la
    semana lleva uno.
    """
    texto = str(crudo or "")
    # " y " sólo como separador entre elementos, nunca dentro de una palabra.
    texto = re.sub(r"\s+y\s+", ",", texto, flags=re.IGNORECASE)
    return [parte.strip() for parte in texto.split(",") if parte.strip()]


def _localidades(defi: Definicion, crudo: str) -> str:
    """"Villa Allende y Cordoba" -> "Villa Allende, Cordoba".

    Se guarda COMO LO ESCRIBIÓ el dueño, porque esto se le muestra de vuelta
    en `ver_reglas_de_entrega` y en el pedido de confirmación. La comparación
    contra la dirección de un pedido la normaliza app/entrega.py, que ya lo
    hacía cuando esto salía del entorno: acá no hay criterio, hay una lista.
    """
    partes = _partes_de_lista(crudo)
    if not partes:
        raise LimiteError(
            f"«{defi.alias[0]}» está vacío: decime en qué localidades repartís",
            clave="limite.localidades_vacio",
            datos={"ajuste": defi.alias[0]},
        )
    elegidas: list[str] = []
    vistas: set[str] = set()
    for parte in partes:
        limpia = " ".join(parte.split())
        # Dedup sin tildes y sin caso: "Córdoba" y "cordoba" son la misma.
        clave = re.sub(r"[^a-z0-9]+", " ", _sin_tildes(limpia)).strip()
        if not clave:
            raise LimiteError(
                f"«{parte}» no es una localidad: no tiene ni una letra ni un número",
                clave="limite.no_es_localidad",
                datos={"valor": parte},
            )
        if clave not in vistas:
            vistas.add(clave)
            elegidas.append(limpia)
    return ", ".join(elegidas)


def _codigos_postales(defi: Definicion, crudo: str) -> str:
    """"5000, x5105abc" -> "5000, X5105ABC". Alfanumérico, en mayúsculas.

    Misma forma normal que usa app/entrega.py::normalizar_cp para comparar, así
    que lo que se guarda es exactamente lo que se va a comparar.
    """
    partes: list[str] = []
    for grupo in _partes_de_lista(crudo):
        partes.extend(p for p in re.split(r"\s+", grupo) if p)
    if not partes:
        raise LimiteError(
            f"«{defi.alias[0]}» está vacío: decime qué códigos postales",
            clave="limite.cp_vacio",
            datos={"ajuste": defi.alias[0]},
        )
    elegidos: list[str] = []
    for parte in partes:
        limpio = re.sub(r"[^A-Z0-9]+", "", parte.upper())
        if not limpio:
            raise LimiteError(
                f"«{parte}» no es un código postal",
                clave="limite.no_es_cp",
                datos={"valor": parte},
            )
        if limpio not in elegidos:
            elegidos.append(limpio)
    return ", ".join(elegidos)


def _hora(defi: Definicion, crudo: str) -> str:
    """"9", "9:30", "09:30" -> "09:30". Refuses anything that is not a time."""
    texto = _sin_tildes(crudo).replace(".", ":").replace("hs", "").replace("h", "").strip()
    if re.fullmatch(r"\d{1,2}", texto):
        texto = f"{texto}:00"
    encontrado = _HORA_RE.match(texto)
    if not encontrado:
        raise LimiteError(
            f"«{defi.alias[0]}» tiene que ser una hora tipo 08:00, no {crudo!r}",
            clave="limite.hora_invalida",
            datos={"ajuste": defi.alias[0], "valor": repr(crudo)},
        )
    return f"{int(encontrado.group(1)):02d}:{encontrado.group(2)}"


def _idioma(defi: Definicion, crudo: str) -> str:
    """«inglés», «english», «español»… -> "en" | "es", o LimiteError.

    Quién decide qué texto nombra qué idioma es app/idioma.py y nadie más: acá
    sólo se traduce el pedido del dueño a la forma normal que se guarda, se le
    muestra de vuelta y se escribe en la auditoría.
    """
    from app import idioma as idioma_mod

    elegido = idioma_mod.normalizar(crudo)
    if elegido is None:
        raise LimiteError(
            f"«{defi.alias[0]}» sólo puede ser español o inglés, no {crudo!r}",
            clave="limite.idioma_invalido",
            datos={"ajuste": defi.alias[0], "valor": repr(crudo)},
        )
    return elegido


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Meta las crea en minúsculas, dígitos y guión bajo. No es nuestra regla: es la
# de ellos, y escribirla acá hace que un nombre imposible se rechace cuando el
# dueño lo tipea y no seis horas después, cuando el aviso no sale.
_NOMBRE_PLANTILLA = re.compile(r"^[a-z0-9_]{1,512}$")
# "es", "es_AR", "en_US", "pt_BR". El idioma en dos o tres letras y la región
# —si está— en dos, que es como Meta las registra.
_IDIOMA_DE_PLANTILLA = re.compile(r"^[a-z]{2,3}(_[A-Z]{2})?$")


def _texto_normal(defi: Definicion, crudo: str) -> str:
    """Un renglón sin caracteres de control. SIN recortar y sin levantar.

    Separado de `_texto_libre` porque las dos preguntas que se le hacen a un
    texto demasiado largo tienen respuestas distintas, y confundirlas costó una
    regresión: TECLEARLO tiene que fallar —el dueño necesita enterarse de que
    no entra—, pero LEERLO no puede fallar, porque un `.env` con un rubro largo
    ya existía y se venía recortando en `conversacion._dato_de_entorno`. Con una
    sola respuesta, ese rubro pasó de recortarse a desaparecer, y el agente dejó
    de decir a qué se dedica el negocio. Se ve en
    `test_un_rubro_hostil_se_queda_del_lado_de_adentro_de_la_frase`.
    """
    return " ".join(_CONTROL.sub("", str(crudo or "")).split())


def _texto_libre(defi: Definicion, crudo: str) -> str:
    """Un renglón, acotado y sin caracteres de control. O LimiteError.

    QUÉ PROTEGE ESTO Y QUÉ NO. No protege al prompt: de eso se sigue ocupando
    `app/conversacion.py::_dato_de_entorno`, en el hueco donde el valor cae,
    porque ahí es donde se sabe que está entrando en una frase. Un valor que
    pasa por acá y va al panel no necesita esa limpieza, y uno que va al prompt
    la sigue necesitando aunque haya pasado por acá; por eso son dos y no una.

    Lo que sí protege: que lo guardado sea lo que el dueño va a leer de vuelta.
    Un salto de línea en el medio de un nombre se ve igual en el panel y
    distinto en todos los renglones que arma el sistema, y un carácter de
    control no se ve en ninguno de los dos — que es peor, porque el valor que
    el dueño cree haber corregido sigue teniendo lo que no ve.
    """
    texto = _texto_normal(defi, crudo)
    if not texto:
        raise LimiteError(
            f"«{defi.alias[0]}» no puede quedar vacío",
            clave="limite.texto_vacio",
            datos={"ajuste": defi.alias[0]},
        )
    tope = defi.largo or 120
    if len(texto) > tope:
        raise LimiteError(
            f"«{defi.alias[0]}» entra en {tope} caracteres y escribiste {len(texto)}",
            clave="limite.texto_largo",
            datos={"ajuste": defi.alias[0], "tope": str(tope), "largo": str(len(texto))},
        )
    return texto


def _plantilla(defi: Definicion, crudo: str) -> str:
    """El nombre de una plantilla de Meta, o LimiteError.

    Se pasa a minúsculas ANTES de validar y no después, y eso es una decisión:
    Meta sólo crea plantillas en minúsculas, así que «Pedido_Confirmado» no es
    un nombre distinto sino el mismo mal tipeado, y rechazarlo le haría creer
    al dueño que su plantilla no sirve. Bajar mayúsculas es idempotente, que es
    lo que `validar(tecleado=False)` necesita para releer lo ya guardado.

    LO QUE ESTO NO COMPRUEBA: que la plantilla EXISTA en Meta y esté aprobada.
    Eso es una llamada a Graph y vive en app/readiness.py. Acá sólo se descarta
    lo que Meta no podría haber creado nunca.
    """
    # SÓLO LAS PUNTAS. Aplastando TODOS los blancos, «pedido confirmado_v3» se
    # guardaba como «pedidoconfirmado_v3»: un nombre distinto, válido para el
    # regex, y que en Meta o no existe o es otra plantilla. Un espacio de más
    # tiene que fallar al teclearse, no convertirse en silencio en otra cosa.
    texto = str(crudo or "").strip().lower()
    if not _NOMBRE_PLANTILLA.match(texto):
        raise LimiteError(
            f"«{defi.alias[0]}» tiene que ser el nombre de una plantilla de Meta "
            f"—minúsculas, números y guión bajo—, no {crudo!r}",
            clave="limite.plantilla_invalida",
            datos={"ajuste": defi.alias[0], "valor": repr(crudo)},
        )
    return texto


def _idioma_de_plantilla(defi: Definicion, crudo: str) -> str:
    """"es_AR", "en_US"… el idioma en que la plantilla está REGISTRADA.

    La forma normal es la de Meta: idioma en minúsculas, región en mayúsculas.
    Normalizar acá evita el caso que más caro sale de todos los de plantillas:
    el nombre existe, el idioma no coincide con el registro, y Meta contesta
    que la plantilla no existe — o sea, el mismo error que un nombre mal
    escrito, con el nombre bien escrito.
    """
    texto = "".join(str(crudo or "").split()).replace("-", "_")
    partes = texto.split("_")
    texto = partes[0].lower() + ("_" + partes[1].upper() if len(partes) == 2 else "")
    if len(partes) > 2 or not _IDIOMA_DE_PLANTILLA.match(texto):
        raise LimiteError(
            f"«{defi.alias[0]}» es el idioma en que registraste la plantilla en "
            f"Meta, como «es_AR» o «en_US», no {crudo!r}",
            clave="limite.idioma_plantilla_invalido",
            datos={"ajuste": defi.alias[0], "valor": repr(crudo)},
        )
    return texto


def mostrar(nombre: str, valor: object, en_idioma: str | None = None) -> str:
    """Cómo se le MUESTRA al dueño el valor de un ajuste, en su idioma.

    Sólo la prosa cambia de idioma. Un tope en pesos, una hora y una lista de
    localidades se muestran igual en los dos: son datos, no texto. El único
    que se traduce es un idioma, porque "en" no es una respuesta que alguien
    quiera leer.
    """
    crudo = str(valor if valor is not None else "").strip()
    defi = TODOS.get(nombre)
    if defi is not None and defi.tipo == IDIOMA and crudo and crudo != NINGUNO:
        from app import idioma as idioma_mod

        elegido = idioma_mod.normalizar(crudo)
        if elegido:
            return idioma_mod.nombre(elegido, en_idioma)
    # Un día SÍ es prosa, aunque se guarde como valor. "lunes,viernes" es lo que
    # está en el almacén en todos los despliegues y lo que se sigue guardando; lo
    # que lee el dueño es "Monday, Friday" si lee en inglés. La traducción es acá
    # y sólo acá: un día traducido que volviera al almacén dejaría de matchear en
    # app/excepciones.py.
    if defi is not None and defi.tipo == DIAS and crudo and crudo != NINGUNO:
        from app import idioma as idioma_mod

        dias = [d for d in (p.strip() for p in crudo.split(",")) if d]
        if dias and all(d in _DIAS_SEMANA for d in dias):
            # Se traduce cada día y NADA MÁS: el separador queda como está
            # guardado, sin espacio. Lo que cambia acá es el idioma, y un
            # cambio cosmético escondido adentro de uno de idioma es un cambio
            # que nadie pidió — y dos tests del camino en español lo dicen.
            return ",".join(idioma_mod.t(f"dia.{d}", en_idioma) for d in dias)
    return crudo


def validar(nombre: str, crudo: str, *, tecleado: bool = True) -> str:
    """Normaliza un valor para ese ajuste, o levanta LimiteError.

    The normal form is what gets stored, shown back to the owner and written
    into the audit, so every kind has exactly one — and a value that validates
    here is a value app/excepciones.py can read without re-interpreting it.

    ``tecleado=False`` re-lee un valor que YA está en forma normal: una
    propuesta pendiente, o lo que el dueño confirmó hace un mes y está en el
    almacén. Todas las clases de acá son idempotentes menos la plata; ver
    ``_numero``.
    """
    defi = TODOS[nombre]
    if defi.opcional and _sin_tildes(crudo) in _NINGUNO_DICHO:
        return NINGUNO
    if defi.tipo == BOOLEANO:
        return "true" if _bool(defi, crudo) else "false"
    if defi.tipo == DIAS:
        return _dias(defi, crudo)
    if defi.tipo == HORA:
        return _hora(defi, crudo)
    if defi.tipo == LOCALIDADES:
        return _localidades(defi, crudo)
    if defi.tipo == CODIGOS_POSTALES:
        return _codigos_postales(defi, crudo)
    if defi.tipo == IDIOMA:
        return _idioma(defi, crudo)
    # LOS TRES DE ABAJO TIENEN QUE DESPACHARSE ACÁ, Y NO ES DECORATIVO: lo que
    # sigue es un `return _numero(...)` sin `if`, o sea que cualquier tipo que
    # no se nombre en esta cadena se valida como si fuera un número. Un ajuste
    # de texto que se olvide de su renglón no falla al agregarse: falla la
    # primera vez que el dueño escribe el nombre de su negocio y el sistema le
    # contesta que «Lácteos Plus» no es un número.
    if defi.tipo == TEXTO:
        return _texto_libre(defi, crudo)
    if defi.tipo == PLANTILLA:
        return _plantilla(defi, crudo)
    if defi.tipo == IDIOMA_PLANTILLA:
        return _idioma_de_plantilla(defi, crudo)
    # 12 significant digits, not the default 6: at :g an owner who sets a
    # 1234567 ceiling gets "1.23457e+06" stored, shown back to him and audited,
    # and reads back as 1234570. Every money limit here reaches seven digits.
    return f"{_numero(defi, crudo, tecleado=tecleado):.12g}"


def configuracion() -> Configuracion:
    """Los límites vigentes AHORA. Levanta LimiteError si algo no cierra."""
    almacen = _almacen()
    # LIMITES only, deliberately: see the note above ENTREGA. A malformed
    # delivery day must not be able to stop an order from confirming.
    crudos = {nombre: _resolver(nombre, almacen) for nombre in LIMITES}

    def _num(nombre: str) -> float:
        """El número de ese límite, respetando de dónde salió el texto.

        ESTE es el camino que decide si un pedido se auto-confirma, así que es
        el que más importa que no re-agrupe: un "1.125" que el dueño confirmó
        está guardado en forma normal, y leerlo como miles acá ensancharía el
        tope por mil sin que nadie haya cambiado nada.
        """
        crudo, origen = crudos[nombre]
        return _numero(LIMITES[nombre], crudo, tecleado=origen != "dueño")

    return Configuracion(
        tope=_num("AUTO_CONFIRM_MAX"),
        tope_qty_por_producto=_num("AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO"),
        buffer=_num("STOCK_BUFFER_PCT") / 100.0,
        tope_cliente_nuevo=_num("AUTO_CONFIRM_MAX_CLIENTE_NUEVO"),
        tope_deuda=_num("AUTO_CONFIRM_MAX_DEBT"),
        descuentos_aprueban=_bool(
            LIMITES["AUTO_CONFIRM_DESCUENTOS_APRUEBAN"],
            crudos["AUTO_CONFIRM_DESCUENTOS_APRUEBAN"][0],
        ),
        tope_descuento_pct=_num("AUTO_CONFIRM_MAX_DESCUENTO_PCT") / 100.0,
        timeout_aprobacion=_timeout(
            _num("APROBACION_TIMEOUT_HORAS"), "APROBACION_TIMEOUT_HORAS"
        ),
        timeout_revision=_timeout(
            _num("REVISION_TIMEOUT_HORAS"), "REVISION_TIMEOUT_HORAS"
        ),
        sombra=_bool(
            LIMITES["AUTO_CONFIRM_SOMBRA"],
            crudos["AUTO_CONFIRM_SOMBRA"][0],
        ),
    )


def _timeout(horas: float, nombre: str) -> float:
    """Un plazo de 0 vencería todo al instante: vuelve al default del límite."""
    return horas if horas > 0 else float(LIMITES[nombre].default)


@dataclass(frozen=True)
class Entrega:
    """Las reglas de entrega vigentes AHORA, ya normalizadas.

    Read per operation by app/excepciones.py, so a change the owner confirms
    applies to the next message with nothing restarted.

    FAILS SOFT, ON PURPOSE. Every field here can only ever WIDEN what the
    system offers by itself, so "unreadable" has to mean "offer nothing" — an
    empty day list, an empty time, a disabled switch, no fee. That is the same
    direction app/excepciones.py already fails in, and it is why a typo in a
    delivery day costs one WhatsApp message instead of stopping every order:
    unlike Configuracion, nothing here raises.
    """

    dias_reparto: tuple[int, ...] = ()
    hora_reparto: str = ""
    excepcion_activa: bool = False
    excepcion_dias: tuple[int, ...] = ()
    excepcion_hora: str = ""
    excepcion_cargo: float | None = None
    # None means "configured but unreadable", and it is NOT the same as 0.
    # Every other field here can only WIDEN what the system offers by itself,
    # so failing soft on them offers less. The minimum is the one field that
    # NARROWS — it is what stops a $200 order earning a free off-day trip — so
    # losing it would fail OPEN. None therefore blocks the exception outright.
    excepcion_minimo: float | None = 0.0
    retiro_activo: bool = False
    retiro_dias: tuple[int, ...] = ()
    retiro_hora: str = ""


def _bruto(nombre: str, almacen: dict[str, str]) -> tuple[str, bool]:
    """(normalized value or '', whether it could be read at all).

    Validation happens on the way IN (validar, behind the confirmation code),
    but a bootstrap environment variable never went through it and a stored
    value can predate a rule. So it is re-normalized here and a bad one reads
    as absent rather than raising into the deterministic path.

    The second element exists because "unset" and "unreadable" are the same
    thing for a widening setting and OPPOSITE things for a narrowing one — see
    Entrega.excepcion_minimo. Callers that do not care use ``_crudo``.
    """
    crudo, origen = _resolver(nombre, almacen)
    try:
        # Lo que fijó el dueño ya está en forma normal; el entorno de arranque
        # lo escribió una persona y conserva la lectura tecleada.
        valor = validar(nombre, crudo, tecleado=origen != "dueño")
    except LimiteError as exc:
        print(f"[limites] {nombre} no usable: {exc}")
        return "", False
    return ("" if valor == NINGUNO else valor), True


def _crudo(nombre: str, almacen: dict[str, str]) -> str:
    """The normalized value of one delivery setting, or '' if it is unusable."""
    return _bruto(nombre, almacen)[0]


def _indices(valor: str) -> tuple[int, ...]:
    return tuple(
        sorted(
            _DIAS_SEMANA[parte]
            for parte in valor.split(",")
            if parte in _DIAS_SEMANA
        )
    )


def _plata(valor: str) -> float | None:
    if not valor:
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def entrega() -> Entrega:
    """Las reglas de entrega vigentes. Nunca levanta: ver Entrega."""
    try:
        almacen = _almacen()
    except LimiteError as exc:
        # A store that cannot be read must not be talked into an offer. Same
        # rule as the limits: the difference is that here it costs an
        # exception nobody was promised, not an order that cannot confirm.
        print(f"[limites] reglas de entrega no legibles: {exc}")
        return Entrega()
    # An empty delivery store plus changes on record means the store was WIPED,
    # and falling back to the bootstrap environment here would restore whatever
    # the .env says — a day he removed, an exception he turned off. That is a
    # silent WIDENING of what the system offers on its own, so it offers
    # nothing instead and a person is asked. Unlike the limits this never
    # raises: see _hubo_cambios_durables_entrega. resumen() and vigente() ask
    # the SAME question, so readiness and the owner's ver_reglas_de_entrega
    # report these rows as lost rather than as .env values.
    if _reglas_de_entrega_perdidas(almacen):
        print(
            "[limites] las reglas de entrega no están en el almacén y ERPNext "
            "tiene cambios registrados: no ofrezco nada por mi cuenta"
        )
        return Entrega()
    # Read through _bruto, not _crudo: a minimum nobody can parse must not
    # read as "no minimum". See Entrega.excepcion_minimo.
    minimo_texto, minimo_legible = _bruto("ENTREGA_EXCEPCION_MIN_TOTAL", almacen)
    if minimo_legible:
        minimo = _plata(minimo_texto)
        minimo = 0.0 if minimo is None else minimo
    else:
        minimo = None
    return Entrega(
        dias_reparto=_indices(_crudo("ENTREGA_DIAS", almacen)),
        hora_reparto=_crudo("ENTREGA_HORA", almacen),
        excepcion_activa=_crudo("ENTREGA_EXCEPCION_ACTIVA", almacen) == "true",
        excepcion_dias=_indices(_crudo("ENTREGA_EXCEPCION_DIAS", almacen)),
        excepcion_hora=_crudo("ENTREGA_EXCEPCION_HORA", almacen),
        excepcion_cargo=_plata(_crudo("ENTREGA_EXCEPCION_CARGO", almacen)),
        excepcion_minimo=minimo,
        retiro_activo=_crudo("RETIRO_LOCAL_ACTIVO", almacen) == "true",
        retiro_dias=_indices(_crudo("RETIRO_LOCAL_DIAS", almacen)),
        retiro_hora=_crudo("RETIRO_LOCAL_HORA", almacen),
    )


def zonas() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(códigos postales, localidades) donde se reparte. Nunca levanta.

    Misma resolución y mismo fusible que `entrega()`: almacén -> entorno ->
    default, y con el almacén de entrega VACÍO y cambios registrados en
    ERPNext devuelve las dos listas vacías. Eso hace que app/entrega.py
    conteste SIN_ZONAS y ningún pedido se entregue solo — que es lo correcto
    cuando se perdió la configuración: un flush de Redis no puede ensanchar
    la zona de reparto de vuelta a lo que decía el .env.

    Se lee en cada llamada, así que un cambio confirmado rige en el próximo
    pedido sin reiniciar nada.
    """
    try:
        almacen = _almacen()
    except LimiteError as exc:
        print(f"[limites] zonas de reparto no legibles: {exc}")
        return (), ()
    if _reglas_de_entrega_perdidas(almacen):
        print(
            "[limites] las zonas de reparto no están en el almacén y ERPNext "
            "tiene cambios registrados: no entrego nada por mi cuenta"
        )
        return (), ()
    def _partes(nombre: str) -> tuple[str, ...]:
        valor = _crudo(nombre, almacen)
        return tuple(p.strip() for p in valor.split(",") if p.strip())

    return _partes("ZONAS_ENTREGA_CP"), _partes("ZONAS_ENTREGA_LOCALIDADES")


def cuenta_cargo() -> str:
    """La cuenta contable del cargo de envío. SÓLO por entorno.

    Deliberately not in any registry, so no natural-language path can reach
    it: it is a real ERPNext account head, a wrong name silently unbalances
    the owner's books rather than breaking the bot, and no model interpreting
    "poneme la cuenta de fletes" can check that the account exists. Without it
    a fee is simply never written and a person is asked to add the charge —
    which app/solicitudes.py already does.
    """
    return os.getenv(CUENTA_CARGO, "").strip()


# LOS DOCE QUE VE UN DUEÑO. El resto sigue existiendo, se sigue pudiendo cambiar
# y se sigue validando igual: lo único que cambia es que no se le ponen delante a
# alguien que vino a vender queso.
#
# POR QUÉ DOCE Y NO CUARENTA Y SEIS. El panel mostraba los 46, y 46 ajustes no son
# una configuración: son un formulario que nadie termina. Trece de ellos son
# nombres de plantillas de Meta —infraestructura, y opcionales en el piloto
# porque adentro de la ventana de 24 h se manda texto libre—, y nueve más son
# afinado fino (el colchón de stock, los plazos de aviso, la banda de descuento)
# que tiene un default razonable y que el dueño no toca en su vida.
#
# ES UNA LISTA BLANCA Y NO UNA NEGRA, a propósito: un ajuste nuevo nace OCULTO.
# Con una lista negra, el próximo que alguien agregue aparecería solo en la cara
# del dueño, y volver a 46 no requeriría ninguna decisión — pasaría de a uno.
BASICOS: frozenset[str] = frozenset({
    # Quién es el negocio. Los cuatro son del dueño y no hay default posible.
    "NOMBRE_NEGOCIO",
    "NOMBRE_AGENTE",
    "RUBRO_NEGOCIO",
    "HORARIO_ATENCION",
    # En qué idioma le habla el sistema al equipo.
    "IDIOMA_GERENCIA",
    # Las cuatro decisiones de plata. `AUTO_CONFIRM_MAX` es la que define el
    # modo entero: en 0 no se confirma nada solo (ver `app/modos.py`).
    "AUTO_CONFIRM_MAX",
    "AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO",
    "AUTO_CONFIRM_MAX_DEBT",
    "PRECIO_CAMBIO_MAX_PCT",
    # Las tres preguntas de reparto que hace cualquier cliente por WhatsApp:
    # ¿llegás a mi barrio?, ¿qué días?, ¿puedo pasar a buscarlo?
    "ZONAS_ENTREGA_LOCALIDADES",
    "ENTREGA_DIAS",
    "RETIRO_LOCAL_ACTIVO",
})


def es_basico(nombre: str) -> bool:
    """¿Este ajuste va en la pantalla del dueño, o en «avanzado»?"""
    return nombre in BASICOS


def resumen(lengua: str | None = None) -> list[dict]:
    """Cada límite con su valor vigente y de dónde salió, para el dueño.

    ``lengua`` decide en qué idioma sale `problema`. Sin ella el texto es el de
    siempre (`str(exc)`, castellano), que es lo que mira el panel y lo que va
    al log. LO QUE ARREGLA: `LimiteError` ya viajaba con `clave` y `datos`
    justamente para esto, y acá se tiraban con un `str(exc)`; el resultado era
    que `ver_ajustes` armaba una frase en inglés y le metía adentro el motivo
    en castellano — media frase en cada idioma, que es el mismo defecto que
    `motivo()` existe para no repetir.

    The delivery rows after a wipe read as LOST — valor "", origen PERDIDO and
    the problem spelled out — because that is the state entrega() decides in,
    and this list is what readiness and ver_reglas_de_entrega show. Resolving
    them from the .env here told him he had a round the system would not run,
    and hid the one thing he needed to know: that his rules were gone.
    """
    almacen = _almacen()
    entrega_perdida = _reglas_de_entrega_perdidas(almacen)
    filas = []
    for nombre, defi in TODOS.items():
        if entrega_perdida and nombre in ENTREGA:
            valor, origen, problema = "", PERDIDO, PROBLEMA_ENTREGA_PERDIDA
        else:
            crudo, origen = _resolver(nombre, almacen)
            try:
                valor = validar(nombre, crudo, tecleado=origen != "dueño")
                problema = ""
            except LimiteError as exc:
                valor = crudo
                problema = motivo(exc, lengua)
        filas.append(
            {
                "nombre": nombre,
                "alias": defi.alias[0],
                "significado": defi.significado,
                "unidad": defi.unidad,
                "valor": valor,
                "origen": origen,
                "problema": problema,
            }
        )
    return filas


def definicion(nombre_o_alias: str) -> Definicion:
    """Encuentra el límite por su nombre técnico o por como lo dice el dueño."""
    buscado = str(nombre_o_alias or "").strip().lower()
    if not buscado:
        raise LimiteError("no me dijiste qué límite", clave="limite.cual")
    for nombre, defi in TODOS.items():
        if buscado == nombre.lower() or buscado in defi.alias:
            return defi
    # Substring fallback, and it must be UNAMBIGUOUS. "hora" alone matches the
    # approval timeout, the review deadline and four delivery times; picking
    # the first one in dict order would let a vague word from the model move a
    # setting the owner never mentioned. Asking is the fail-closed answer.
    parecidos = [
        defi
        for defi in TODOS.values()
        if buscado in defi.nombre.lower() or any(buscado in a for a in defi.alias)
    ]
    if len(parecidos) == 1:
        return parecidos[0]
    if parecidos:
        opciones = ", ".join(f"«{defi.alias[0]}»" for defi in parecidos)
        raise LimiteError(
            f"«{nombre_o_alias}» puede ser varias cosas: {opciones}. Decime cuál",
            clave="limite.ambiguo",
            datos={"valor": nombre_o_alias, "opciones": opciones},
        )
    conocidos = ", ".join(defi.alias[0] for defi in TODOS.values())
    raise LimiteError(
        # La LISTA de alias sigue en español en los dos idiomas, y no es un
        # olvido: son los comandos que el dueño teclea (sección 4 del
        # allowlist). Lo que se traduce es la frase alrededor.
        f"no conozco el ajuste «{nombre_o_alias}». Hay: {conocidos}",
        clave="limite.ajuste_desconocido",
        datos={"valor": nombre_o_alias, "conocidos": conocidos},
    )


def _ahora() -> str:
    """Sella el registro durable `[limite]`, así que el respaldo importa: era
    `datetime.now()` sin zona, o sea el reloj del servidor, escribiendo el
    rastro de auditoría con un reloj distinto del que decide todo lo demás y
    sin dejar constancia. Ahora es el default del negocio, con log."""
    return reloj.ahora_con_respaldo("limites").isoformat(timespec="seconds")


def _codigo() -> str:
    return f"{secrets.randbelow(9000) + 1000}"


def vigente(nombre: str) -> str:
    """El valor vigente de un límite, tal como se guardaría.

    A lost delivery rule has NO value in effect — not the .env's either — so it
    reads as NINGUNO: the «anterior» a proposal shows and the audit records is
    what entrega() is actually working with.
    """
    almacen = _almacen()
    if nombre in ENTREGA and _reglas_de_entrega_perdidas(almacen):
        return NINGUNO
    crudo, origen = _resolver(nombre, almacen)
    try:
        return validar(nombre, crudo, tecleado=origen != "dueño")
    except LimiteError:
        return crudo


def de_negocio(nombre: str) -> str:
    """Un dato del negocio o una plantilla: lo del dueño, si no el `.env`.

    POR QUÉ ESTO NO ES `vigente()`, que es la pregunta importante de este
    módulo. `vigente()` pasa por `_almacen()`, que FALLA CERRADO: si Redis no
    contesta, levanta en vez de caer al entorno. Para un tope eso es lo único
    correcto — caer al `.env` convertiría una caída de Redis en un límite más
    flojo que el que el dueño apretó, y sin que nadie se entere.

    Acá no hay flojo. El nombre de una plantilla de Meta no tiene una dirección
    peligrosa: el `.env` no dice un nombre «más permisivo», dice el mismo
    nombre de antes. Lo que sí es peligroso es lo otro, y es lo que esta
    función existe para no hacer: que una caída de Redis deje al cliente sin el
    aviso de que su pedido venció, porque no se pudo leer cómo se llama la
    plantilla. Antes de este módulo eso lo resolvía un `os.getenv` que no podía
    fallar, y hacer settable un valor no puede volver frágil lo que era firme.

    Devuelve "" —y no el sentinela— cuando no hay nada configurado, así que
    todos los `if not plantilla:` que ya existían siguen leyéndose igual.
    """
    return varios_de_negocio(nombre)[nombre]


def varios_de_negocio(*nombres: str) -> dict[str, str]:
    """Lo mismo que `de_negocio`, con UNA sola lectura del almacén.

    Existe por el camino caliente: `app/conversacion.py` arma la primera línea
    del prompt con cuatro de estos valores en cada mensaje, y cuatro `hgetall`
    donde alcanza uno es la clase de costo que se paga por turno y nadie mira.
    """
    faltantes = [n for n in nombres if n not in TODOS]
    if faltantes:
        raise KeyError(faltantes[0])
    try:
        almacen = _almacen()
    except LimiteError:
        # Con el almacén ilegible el `.env` es la mejor respuesta que hay, y
        # sigue siendo mejor que ninguna. Se deja dicho en el log: un valor que
        # el dueño cambió por el panel y no está rigiendo tiene que poder verse.
        print("[limites] no pude leer el almacén, uso el entorno para el negocio")
        almacen = {}
    resuelto = {}
    for nombre in nombres:
        crudo, origen = _resolver(nombre, almacen)
        try:
            valor = validar(nombre, crudo, tecleado=origen != "dueño")
        except LimiteError:
            defi = TODOS[nombre]
            if defi.tipo == TEXTO:
                # Un texto que no valida es, casi siempre, uno que no entra en
                # el tope — y el valor de arranque del `.env` se venía
                # recortando, no tirando. Se sigue recortando: perder el nombre
                # del negocio es peor que mostrarlo corto. Teclear uno largo
                # sigue fallando, que es donde el aviso sirve de algo.
                valor = _texto_normal(defi, crudo)[: defi.largo or 120]
            else:
                # Lo demás no tiene recorte que lo salve: una plantilla a medias
                # no existe en Meta. Vacío es lo que ya significaba «esto no
                # está configurado» en cada llamador.
                print(f"[limites] {nombre}: el valor guardado no es válido, lo ignoro")
                valor = ""
        resuelto[nombre] = "" if valor == NINGUNO else valor
    return resuelto


def _tag(telefono: str) -> str:
    """Hash corto, para el log. El número entero no va a stdout."""
    return hashlib.sha256(str(telefono or "").encode()).hexdigest()[:10]


def _clave_propuesta(telefono: str) -> str:
    """La clave de la propuesta, en forma canónica.

    Proponer un cambio y confirmarlo con el código llegan por caminos
    distintos: la herramienta de gerencia (con el teléfono del contexto) y el
    router determinista de app/main.py (con el del webhook). Si cada uno
    normaliza distinto, el código correcto no encuentra nada que aplicar.
    """
    return f"{CLAVE_PROPUESTA}:{telefono_mod.normalizar(telefono) or telefono}"


def _huella_propuesta(telefono: str, limite: str, nuevo: str) -> str:
    """Identidad del cambio: quién, qué ajuste y qué valor YA normalizado.

    El valor entra normalizado a propósito: «20», « 20 » y «20%» son el MISMO
    cambio, y lo son recién después de validar(). Comparar lo tecleado haría
    que tres maneras de escribir lo mismo fueran tres cambios distintos, que es
    justo lo que esto existe para que no pase.
    """
    crudo = json.dumps(
        [telefono_mod.normalizar(telefono) or telefono, limite, nuevo],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(crudo.encode()).hexdigest()[:16]


def _vencida(propuesta: dict, ahora: float | None = None) -> bool:
    """El vencimiento va ADENTRO, no sólo en el TTL.

    Un TTL que no corrió —un Redis restaurado de un backup, un reloj movido— no
    puede revivir un código de ayer.
    """
    try:
        expira = float(propuesta.get("expira") or 0)
    except (TypeError, ValueError):
        return True
    return expira <= (time.time() if ahora is None else ahora)


def _propuesta_viva(telefono: str) -> dict | None:
    """El cambio que ese teléfono dejó esperando, con código y todo."""
    try:
        crudo = locks.conexion().get(_clave_propuesta(telefono))
    except (locks.CoordinationError, RedisError):
        return None
    if not crudo:
        return None
    try:
        propuesta = json.loads(_texto(crudo))
    except ValueError:
        return None
    if not isinstance(propuesta, dict) or _vencida(propuesta):
        return None
    return propuesta


def proponer(nombre_o_alias: str, valor_crudo: str, telefono: str) -> dict:
    """Valida un cambio y lo deja PENDIENTE de confirmación. No cambia nada.

    El código vuelve al dueño y tiene que volver escrito por él: así ningún
    malentendido del LLM mueve un límite por su cuenta.

    PEDIR DOS VECES LO MISMO ES UN PEDIDO, NO DOS. Si ya hay un cambio idéntico
    esperando —mismo teléfono, mismo ajuste, mismo valor normalizado— se
    devuelve ESE, con SU código, y `repetida` en True. Antes cada llamada
    sorteaba un código nuevo y pisaba el anterior: si el turno se reintentaba
    —porque el resultado no se pudo cachear, porque Meta reentregó el
    mensaje— al dueño le llegaban dos mensajes con dos códigos y sólo el
    último servía. Contestar el primero, que es el que estaba mirando, no
    aplicaba nada.
    """
    if not telefono:
        raise LimiteError("no sé quién pide el cambio", clave="limite.sin_quien_pide")
    defi = definicion(nombre_o_alias)
    nuevo = validar(defi.nombre, valor_crudo)
    anterior = vigente(defi.nombre)
    huella = _huella_propuesta(telefono, defi.nombre, nuevo)

    esperando = _propuesta_viva(telefono)
    if esperando and esperando.get("id") == huella:
        # El MISMO código, a propósito: el que él tiene en el teléfono.
        return {**esperando, "repetida": True}

    propuesta = {
        "id": huella,
        "codigo": _codigo(),
        "limite": defi.nombre,
        "alias": defi.alias[0],
        "anterior": anterior,
        "nuevo": nuevo,
        "telefono": telefono,
        "ts": _ahora(),
        "expira": time.time() + PROPUESTA_TTL_SEGUNDOS,
    }
    try:
        locks.conexion().setex(
            _clave_propuesta(telefono),
            PROPUESTA_TTL_SEGUNDOS,
            json.dumps(propuesta, ensure_ascii=False),
        )
    except (locks.CoordinationError, RedisError) as exc:
        raise LimiteError(
            "no pude registrar el cambio para confirmarlo",
            clave="limite.no_registre_propuesta",
        ) from exc
    return {**propuesta, "repetida": False}


def pendiente(telefono: str) -> dict | None:
    """El cambio que ese teléfono dejó esperando confirmación, si hay uno.

    Lo usa el router determinista de app/main.py para saber si un mensaje de
    cuatro dígitos ES un código de confirmación. Sin esto tendría que llamar a
    aplicar() para averiguarlo, y un "no hay nada pendiente" no se distinguiría
    de un "ese código está mal": el primero es un mensaje cualquiera que le toca
    contestar al agente, el segundo es algo que el dueño tiene que saber.

    NUNCA devuelve el código. Falla cerrada: si no se puede leer, no hay nada.
    Un cambio vencido no está pendiente, aunque el TTL no haya corrido.
    """
    if not telefono:
        return None
    propuesta = _propuesta_viva(telefono)
    if propuesta is None:
        return None
    return {k: v for k, v in propuesta.items() if k != "codigo"}


def descartar(telefono: str) -> None:
    """Tira el cambio pendiente de ese teléfono. Para cuando el código no llegó.

    Un cambio que espera un código que el dueño nunca vio no se puede confirmar
    y sí puede confundirlo diez minutos después. Mejor no dejarlo.
    """
    if not telefono:
        return
    try:
        locks.conexion().delete(_clave_propuesta(telefono))
    except (locks.CoordinationError, RedisError) as exc:
        print(f"[limites] no pude descartar la propuesta ({type(exc).__name__})")


def aplicar(codigo: str, telefono: str) -> dict:
    """Aplica el cambio pendiente de ESE teléfono si el código coincide.

    Se mira primero y se RECLAMA después. Un código equivocado no consume nada
    —mirar no borra—, y el que sí coincide se lleva la propuesta con un GETDEL,
    que es una sola operación: dos entregas del mismo mensaje, o dos workers a
    la vez, encuentran uno el cambio y el otro nada. Sin eso, el mismo código
    contestado dos veces escribía dos veces la auditoría y dos comentarios en
    ERPNext, y el historial contaba dos cambios donde el dueño hizo uno.
    """
    if not telefono:
        raise LimiteError(
            "no sé quién confirma el cambio", clave="limite.sin_quien_confirma"
        )
    limpio = str(codigo or "").strip()
    clave = _clave_propuesta(telefono)
    try:
        crudo = locks.conexion().get(clave)
    except (locks.CoordinationError, RedisError) as exc:
        raise LimiteError(
            "no pude leer el cambio pendiente", clave="codigo.pendiente_no_legible"
        ) from exc
    if not crudo:
        raise LimiteError(
            "no hay ningún cambio esperando confirmación",
            clave="codigo.sin_pendiente",
        )
    try:
        propuesta = json.loads(_texto(crudo))
    except ValueError as exc:
        raise LimiteError(
            "el cambio pendiente quedó ilegible", clave="codigo.pendiente_ilegible"
        ) from exc

    if str(propuesta.get("codigo")) != limpio:
        raise LimiteError(
            "ese código no es el del cambio pendiente",
            clave="codigo.invalido",
        )
    if _vencida(propuesta):
        raise LimiteError(
            "ese código ya venció. No cambié nada: pedime el cambio de nuevo",
            clave="codigo.vencido",
        )
    # Atado al teléfono: el código que le llegó a uno no lo aplica otro, aunque
    # los dos estén en la lista del equipo.
    if telefono_mod.normalizar(propuesta.get("telefono")) != telefono_mod.normalizar(
        telefono
    ):
        raise LimiteError(
            "ese código no es de este número", clave="codigo.otro_numero"
        )

    # El reclamo. A partir de acá la propuesta es de este turno y de nadie más.
    try:
        reclamado = locks.conexion().getdel(clave)
    except (locks.CoordinationError, RedisError) as exc:
        raise LimiteError(
            "no pude leer el cambio pendiente", clave="codigo.pendiente_no_legible"
        ) from exc
    if not reclamado:
        raise LimiteError("ese cambio ya se confirmó", clave="codigo.ya_confirmado")
    try:
        propuesta = json.loads(_texto(reclamado))
    except ValueError as exc:
        raise LimiteError(
            "el cambio pendiente quedó ilegible", clave="codigo.pendiente_ilegible"
        ) from exc
    # Entre el vistazo y el reclamo pudo entrar otra propuesta: la que se
    # aplica es la que el código nombra, nunca la que quedó en su lugar.
    if str(propuesta.get("codigo")) != limpio:
        raise LimiteError(
            "ese código no es el del cambio pendiente", clave="codigo.invalido"
        )

    nombre = str(propuesta.get("limite") or "")
    if nombre not in TODOS:
        raise LimiteError(
            "el cambio pendiente apunta a un ajuste que no existe",
            clave="codigo.ajuste_inexistente",
        )
    # tecleado=False: proponer() ya normalizó esto. Re-agruparlo es el error
    # de mil veces que describe el docstring de _numero.
    return _escribir(nombre, str(propuesta.get("nuevo")), telefono, tecleado=False)


def _escribir(
    nombre: str, valor_crudo: str, telefono: str, *, tecleado: bool
) -> dict:
    """Valida el valor, lo guarda y lo audita. El ÚNICO camino de escritura.

    Lo comparten `aplicar` (con código) y `fijar` (sin). Dos copias de esto
    serían dos reglas sobre qué queda auditado y cuándo, y se separarían en la
    dirección peligrosa: la que escribe sin registrar.
    """
    nuevo = validar(nombre, valor_crudo, tecleado=tecleado)
    anterior = vigente(nombre)

    entrada = {
        "ts": _ahora(),
        "telefono": telefono,
        "limite": nombre,
        "anterior": anterior,
        "nuevo": nuevo,
    }
    # The durable copy goes in FIRST, and a failure here cancels the change.
    # An audit that only lives in Redis disappears with Redis, and then a wiped
    # store looks like a brand-new install.
    _auditar_en_erpnext(entrada)
    try:
        cliente = locks.conexion()
        cliente.hset(CLAVE_VALORES, nombre, nuevo)
        cliente.rpush(CLAVE_AUDITORIA, json.dumps(entrada, ensure_ascii=False))
        cliente.ltrim(CLAVE_AUDITORIA, -AUDITORIA_MAXIMA, -1)
    except (locks.CoordinationError, RedisError) as exc:
        raise LimiteError(
            "no pude guardar el cambio", clave="limite.no_pude_guardar"
        ) from exc
    print(
        f"[limites] {nombre}: {anterior} -> {nuevo} "
        f"por {_tag(telefono)} ({entrada['ts']})"
    )
    return entrada


def fijar(nombre_o_alias: str, valor_crudo: str, telefono: str) -> dict:
    """Cambia un ajuste YA, sin código de confirmación.

    DECISIÓN DEL DUEÑO, EXPLÍCITA Y REPETIDA. El código de cuatro dígitos era
    el freno determinista: el modelo PROPONÍA y sólo el dueño, tecleando un
    número que nunca entró en el contexto de ningún modelo, aplicaba. Sacarlo
    significa que lo que el modelo decida cambiar, se cambia.

    No es la primera vez ni es un camino nuevo: `cambiar_precio` ya escribe el
    precio de lista «sin código y sin confirmar», y el comentario de
    `graph.py` deja dicho que lo pidió el dueño con las mismas palabras («no
    one can confirm everytime i need automated»). Esto es la misma decisión,
    aplicada al resto de los ajustes.

    LO QUE SIGUE EN PIE, porque no dependía del código:
      * `validar` — un valor fuera de rango, mal tipeado o imposible se
        rechaza igual. El código nunca fue lo que hacía legal a un número.
      * `_escribir` es el ÚNICO camino de escritura y audita antes de guardar:
        si no se puede dejar el registro durable en ERPNext, el cambio no se
        aplica. Un ajuste que se mueve sin quedar anotado es peor que uno que
        no se mueve.
      * Quién llama sigue decidiendo quién puede: `require_management` en la
        herramienta, `es_equipo` en el ruteo. Esto no autoriza a nadie.

    `telefono` tiene que venir YA verificado por quien llama, y queda en la
    auditoría: sin código, el registro de QUIÉN lo pidió es lo único que queda
    para reconstruir un cambio que nadie recuerda haber hecho.
    """
    defi = definicion(nombre_o_alias)
    if not telefono:
        raise LimiteError(
            "no sé quién pide el cambio", clave="limite.sin_quien_confirma"
        )
    # tecleado=True: esto viene de una persona escribiendo, así que «1.500» son
    # mil quinientos. Es la misma normalización que hacía `proponer`.
    return _escribir(defi.nombre, valor_crudo, telefono, tecleado=True)


def _auditar_en_erpnext(entrada: dict) -> None:
    """Deja el cambio anotado en ERPNext, que es lo que sobrevive a todo.

    Sirve para dos cosas: el dueño puede leer el historial en el sistema donde
    vive su contabilidad, y app/limites.py puede distinguir «nunca se
    configuró» de «se perdió el almacén». Si no se puede escribir, el cambio
    NO se aplica: prefiero no mover el límite antes que moverlo sin registro.
    """
    global _durable_cache, _durable_cache_entrega, _durable_cache_idioma
    entrega_cambio = entrada["limite"] in ENTREGA
    idioma_cambio = entrada["limite"] in IDIOMAS
    # El `else` de abajo era de `[limite]`, y por eso esta rama tiene que estar
    # ANTES: cambiar el nombre de una plantilla caía en él y dejaba escrita la
    # marca que arma el fusible de `_almacen()`. Ver la fila `negocio` de
    # app/marcas.py — es el camino que rompe las ventas sin tocar un límite.
    negocio_cambio = entrada["limite"] in NEGOCIO or entrada["limite"] in PLANTILLAS
    if idioma_cambio:
        marca = MARCA_DURABLE_IDIOMA
    elif entrega_cambio:
        marca = MARCA_DURABLE_ENTREGA
    elif negocio_cambio:
        marca = MARCA_DURABLE_NEGOCIO
    else:
        marca = MARCA_DURABLE
    texto = (
        f"{marca} {entrada['limite']}: {entrada['anterior']} -> "
        f"{entrada['nuevo']} · lo cambió {entrada['telefono']} "
        f"el {entrada['ts']}"
    )
    try:
        erpnext.registrar_comentario(
            "Company", erpnext.default_company(), texto
        )
    except erpnext.ERPNextError as exc:
        raise LimiteError(
            "no pude registrar el cambio en ERPNext, así que no lo apliqué",
            clave="limite.no_registre_en_erpnext",
        ) from exc
    if idioma_cambio:
        _durable_cache_idioma = None
    elif entrega_cambio:
        _durable_cache_entrega = None
    elif negocio_cambio:
        # A propósito no hay caché que invalidar: `[negocio]` no tiene lector.
        pass
    else:
        _durable_cache = None


def auditoria(maximo: int = 10) -> list[dict]:
    """Los últimos cambios, del más nuevo al más viejo."""
    try:
        crudos = locks.conexion().lrange(CLAVE_AUDITORIA, -max(1, maximo), -1)
    except (locks.CoordinationError, RedisError) as exc:
        raise LimiteError(
            "no pude leer el historial de cambios", clave="limite.no_pude_leer_historial"
        ) from exc
    entradas = []
    for crudo in reversed(list(crudos or [])):
        try:
            entradas.append(json.loads(_texto(crudo)))
        except ValueError:
            continue
    return entradas
