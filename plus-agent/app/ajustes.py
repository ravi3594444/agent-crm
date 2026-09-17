"""Preparar un cambio de ajuste y mandarle el código al dueño. UNA vez.

POR QUÉ ESTE MÓDULO EXISTE
Hay DOS puertas por las que entra un cambio de ajuste y tiene que haber UNA
implementación:

  * la herramienta de gerencia (app/tools/configuracion.py::proponer_limite),
    para cuando el dueño lo pide en prosa y el modelo interpreta qué ajuste es;
  * el ruteo determinista (app/main.py::_comando_de_idioma), para los comandos
    EXACTOS y documentados, que se atienden antes de que ningún modelo los vea.

La segunda existe por un fallo en vivo: `manager language English` llegó al
modelo, el modelo contestó que sus instrucciones lo obligaban a hablar en
español, no llamó a ninguna herramienta, y no se generó ningún código. Un ajuste
del dueño no puede depender de cómo lo interpretó un modelo.

Lo que NO se duplica acá, y es el punto: el almacenamiento de la propuesta, la
huella de idempotencia, el vencimiento, el código y la auditoría durable son los
de app/limites.py. Este módulo sólo arma el texto y usa la puerta de salida de
app/notificar.py.
"""
from __future__ import annotations

from app import idioma, limites


def preparar(limite: str, valor: str, telefono: str) -> str:
    """Cambia el ajuste YA y devuelve qué decirle al dueño. SIN código.

    ``telefono`` tiene que ser un número YA verificado: este módulo no
    autoriza a nadie. Quien llama decide si esa persona puede
    (`require_management` en la herramienta, `es_equipo` en el ruteo).

    ESTO ERA DE DOS PASOS Y AHORA ES DE UNO, POR DECISIÓN DEL DUEÑO. El modelo
    proponía, Python le mandaba cuatro dígitos al teléfono del dueño por un
    canal aparte, y el router determinista de `app/main.py` aplicaba; el código
    nunca entraba en el contexto de ningún modelo, así que nada que el modelo
    hiciera —ni que lo convencieran de hacer— podía proveerlo. Sacarlo
    significa que lo que el modelo decida cambiar, se cambia.

    El dueño lo pidió expresamente y más de una vez: configurar un negocio
    tecleando un código por cada ajuste es la fricción que, según él, hace que
    el producto no se venda. Y no es un camino nuevo en este repo:
    `cambiar_precio` ya escribe el precio de lista «sin código y sin
    confirmar», con el mismo argumento suyo citado en `graph.py`.

    LO QUE NO CAMBIÓ, porque nunca dependió del código: `limites.validar` sigue
    rechazando un valor imposible, `limites._escribir` sigue siendo el único
    camino de escritura y sigue auditando en ERPNext ANTES de guardar —si no se
    puede dejar el registro durable, el cambio no se aplica—, y el teléfono
    verificado sigue quedando en esa auditoría. Sin código, ese registro es lo
    único que queda para reconstruir quién pidió un cambio que nadie recuerda.

    La maquinaria de propuestas (`limites.proponer`, `aplicar`, `pendiente`,
    `descartar`) queda intacta y sin usar desde acá: revertir esto es volver a
    llamarla, no reescribirla.
    """
    # El idioma se lee ANTES de escribir, no después: si el cambio es el del
    # propio idioma, el acuse tiene que salir en el que el dueño venía leyendo.
    # Leerlo después contestaría en el nuevo, sobre un mensaje que mandó en el
    # viejo — que es exactamente lo que el comentario de la versión con código
    # dejaba dicho, y sigue valiendo.
    lengua = idioma.gerencia()
    try:
        entrada = limites.fijar(limite, valor, telefono)
    except limites.LimiteError as exc:
        # Quién convierte la excepción en texto es `limites.motivo`, en un solo
        # lugar: acá y en app/main.py estaba la MISMA línea escrita dos veces, y
        # dos copias de una regla son dos reglas.
        return idioma.t(
            "codigo.ajuste_no_preparado", lengua, motivo=limites.motivo(exc, lengua)
        )

    # El acuse nombra el ajuste con su alias de siempre —el que el dueño teclea
    # por WhatsApp— y muestra de dónde a dónde fue. Sin código que teclear, este
    # mensaje es lo ÚNICO que le dice que algo cambió: si no se lee claro, un
    # cambio que él no quiso pasa desapercibido hasta que muerde.
    # Se reusa el MISMO texto que usaba el camino con código
    # (`codigo.ajuste_aplicado`): ya está en los dos idiomas, ya dice que rige
    # desde el próximo pedido y sin reiniciar, y ya deja dicho que queda
    # registrado a nombre del dueño. Escribir uno nuevo sería una segunda
    # redacción de la misma noticia, que es como se separan dos mensajes que
    # tienen que decir lo mismo.
    nombre_ajuste = entrada["limite"]
    return idioma.t(
        "codigo.ajuste_aplicado",
        lengua,
        ajuste=limites.definicion(nombre_ajuste).alias[0],
        anterior=limites.mostrar(nombre_ajuste, entrada["anterior"], lengua),
        nuevo=limites.mostrar(nombre_ajuste, entrada["nuevo"], lengua),
        ts=entrada["ts"],
    )
