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

from app import idioma, limites, notificar


def preparar(limite: str, valor: str, telefono: str) -> str:
    """Prepara el cambio, le manda el código al dueño y devuelve qué decirle.

    ``telefono`` tiene que ser un número YA verificado: este módulo no
    autoriza a nadie. Quien llama decide si esa persona puede (require_management
    en la herramienta, es_equipo en el ruteo).

    Todo el estado —la propuesta, su código, su vencimiento y su huella— lo
    maneja limites.proponer(). Pedir dos veces el mismo cambio devuelve la MISMA
    propuesta con el MISMO código, así que una entrega doble de Meta o un turno
    reintentado no dejan dos códigos vivos.
    """
    try:
        propuesta = limites.proponer(limite, valor, telefono)
    except limites.LimiteError as exc:
        return f"No cambié nada: {exc}."

    # El idioma en que se le habla al equipo AHORA — no el propuesto. Si está
    # pasando de español a inglés, el pedido de confirmación llega todavía en
    # español: recién cuando confirma cambia el idioma.
    lengua = idioma.gerencia()
    nombre_ajuste = propuesta.get("limite") or propuesta.get("nombre") or ""
    anterior = limites.mostrar(nombre_ajuste, propuesta["anterior"], lengua)
    nuevo = limites.mostrar(nombre_ajuste, propuesta["nuevo"], lengua)
    cambio = f"*{propuesta['alias']}*: {anterior} → {nuevo}"

    # Determinista, y a SU número. Este envío es la razón por la que los dos
    # pasos son dos: el código nunca entra en el contexto del modelo, así que
    # nada que el modelo haga —ni que lo convenzan de hacer— puede proveerlo.
    #
    # Se reenvía en un repetido, CON EL MISMO CÓDIGO. Un turno reintentado es el
    # caso normal acá —el modelo llamó la herramienta dos veces, Meta reentregó
    # el mensaje— y la falla que importa es el envío que nunca llegó. Reenviar
    # los mismos dígitos es seguro: es una propuesta y un código, así que lo
    # peor que pasa es que lea el mismo mensaje dos veces. Sortear uno nuevo era
    # el defecto: dos mensajes, dos códigos, y sólo el último servía.
    entregado = notificar.pedir_codigo_de_ajuste(
        telefono,
        idioma.t(
            "codigo.ajuste_pedido",
            lengua,
            cambio=cambio,
            codigo=propuesta["codigo"],
            minutos=int(limites.PROPUESTA_TTL_SEGUNDOS // 60),
        ),
    )
    if not entregado:
        # Un cambio esperando un código que nunca vio no se puede confirmar, y
        # puede confundirlo diez minutos después. Mejor no dejarlo.
        limites.descartar(telefono)
        return idioma.t("codigo.ajuste_sin_codigo", lengua, cambio=cambio)
    if propuesta.get("repetida"):
        return idioma.t("codigo.ajuste_repetido", lengua, cambio=cambio)
    return idioma.t("codigo.ajuste_preparado", lengua, cambio=cambio)
