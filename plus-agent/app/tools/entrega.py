"""Lo que el dueño configuró sobre la ENTREGA, dicho de una sola vez.

POR QUÉ EXISTE, Y POR QUÉ ES UNA SOLA HERRAMIENTA
El dueño configura doce hechos de entrega por WhatsApp (el grupo `ENTREGA` de
app/limites.py): días y hora de reparto, las dos listas de zona, el retiro por
el local, y la entrega fuera de día con su cargo y su mínimo. Ninguno de los
doce era legible desde el agente de clientes, así que seis preguntas de
mostrador —«¿qué días repartís?», «¿llegás a Alta Córdoba?», «¿a qué hora me lo
llevan?», «¿puedo pasar a buscarlo?», «¿cuánto sale el envío?», «¿hay mínimo?»—
sólo se podían esquivar. El retiro por el local está construido y configurado y
era invisible justamente para el que lo pedía.

Son seis preguntas del MISMO tema, así que son una herramienta y no seis: la
elección se degrada por el solapamiento, no por la cantidad (ver la sección de
superficie de herramientas en docs/MAPA.md).

QUÉ ES ESTO Y QUÉ NO ES
Esto es lo que el negocio hace EN GENERAL. No es una promesa sobre un pedido:
si ESTE pedido llega a ESA dirección lo decide `app/entrega.py` sobre la
dirección completa, y entregar fuera de los días de reparto se pide con
`pedir_excepcion_de_entrega` y lo aprueba una persona (o lo pre-autorizan los
límites del dueño). El texto que se devuelve lo dice en su última línea, en el
mismo lugar y con la misma función que el «No confirmes disponibilidad.» de
`consultar_stock`.

TRES REGLAS QUE NO SE PUEDEN AFLOJAR
1. SÓLO LECTURA. No escribe nada, en ningún lado, y no acepta ningún dato del
   cliente que termine en un documento.
2. TODO SALE DE `limites.vigente(...)`, nunca de `os.getenv`. `vigente()` es lo
   que mueve el WhatsApp del dueño, y devuelve el centinela `limites.NINGUNO`
   cuando las reglas de entrega se PERDIERON del almacén — el estado en que
   `limites.entrega()` no ofrece nada. Leer el entorno acá le contaría al
   cliente la ronda que el sistema no va a hacer.
3. UN AJUSTE QUE NO ESTÁ NO ES UN «NO». «No lo tengo configurado» y «no
   repartimos» son cosas distintas, y confundirlas convierte un agujero de
   configuración en una promesa (o en una puerta en la cara). Todo lo que no se
   puede leer sale como faltante y la instrucción final manda a preguntarle a
   una persona, que es lo que piden las reglas 1 y 5-7 de `app/prompts.py`.

POR QUÉ `zona` ES UN PARÁMETRO Y NO UNA LISTA QUE LEE EL MODELO
Devolver las listas y que el modelo decida si «Alta Córdoba» está adentro es
exactamente lo que `app/entrega.py` existe para no hacer: ahí no hay criterio,
hay una comparación de texto normalizado (tildes, espacios, puntuación, «X5000»
contra «5000»). Con `zona`, la comparación la hace Python con
`entrega.normalizar_localidad` y `entrega.normalizar_cp` —las MISMAS funciones
que deciden la entrega de verdad—, así que lo que escucha el cliente no puede
discrepar con lo que va a hacer el sistema. Es opcional porque las otras cinco
preguntas no traen zona.
"""
from __future__ import annotations

from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field

from app import entrega as entrega_zonas
from app import idioma, limites
from app.formato import pesos
from app.runtime_context import RuntimeContextError, actor_context


def _lengua(config: RunnableConfig) -> str:
    """En qué idioma se arma la respuesta. Nunca levanta.

    `idioma.para_destinatario` es el único lugar que decide esto, y se le pasa
    el teléfono que puso el webhook: ningún texto del mensaje lo elige.
    """
    try:
        actor = actor_context(config)
    except RuntimeContextError:
        return idioma.por_defecto()
    return idioma.para_destinatario(actor.actor_phone)


def _valor(nombre: str) -> str:
    """El valor vigente de UN ajuste de entrega, o "" si no rige ninguno.

    Colapsa a "" tres estados que para el cliente son el mismo —«nunca se
    configuró», «se perdió el almacén» (`limites.NINGUNO`) y «está guardado
    pero no se puede interpretar»— porque los tres significan que nadie puede
    decir qué hace el negocio, y ninguno de los tres es un «no».

    La re-validación es la misma que hace `limites._bruto` para el camino
    determinista: un valor de arranque que nunca pasó por `validar` o uno que
    quedó de una regla vieja se lee como ausente en vez de viajar tal cual a la
    boca del modelo.

    `limites.vigente` LEVANTA si Redis no contesta. Se deja subir a propósito:
    lo atrapa la herramienta y contesta «no pude mirarlo», que no es lo mismo
    que «no hay nada configurado».
    """
    crudo = limites.vigente(nombre)
    if not crudo or crudo == limites.NINGUNO:
        return ""
    try:
        return limites.validar(nombre, crudo, tecleado=False)
    except limites.LimiteError:
        return ""


def _dias(nombre: str, lengua: str) -> str:
    """Los días como los lee una persona: «lunes, viernes» / «Monday, Friday».

    `limites.mostrar` es quien traduce un día, y sólo a la SALIDA: lo guardado
    sigue siendo el castellano que parsea app/excepciones.py.
    """
    valor = _valor(nombre)
    if not valor:
        return ""
    return limites.mostrar(nombre, valor, lengua).replace(",", ", ")


def _plata(valor: str) -> str:
    """Un monto con la forma del despliegue ($1.500 / $1,500). Ver app/formato.py."""
    if not valor:
        return ""
    try:
        return pesos(float(valor))
    except (TypeError, ValueError):
        return ""


def _partes(valor: str) -> list[str]:
    return [parte.strip() for parte in valor.split(",") if parte.strip()]


def _respuesta_de_zona(
    zona: str, localidades: str, codigos: str, lengua: str
) -> str:
    """¿Se reparte en lo que preguntó? Comparación de texto, sin criterio.

    Las cuatro respuestas son distintas y ninguna se puede fusionar:
      * adentro   -> está en la lista, y aun así no se promete el pedido.
      * afuera    -> está fuera de una lista que SÍ podía contestar por él.
      * sin listas-> no hay zonas cargadas: NO se sabe, no es un «no».
      * no se puede comprobar -> preguntó por un nombre y sólo hay códigos
        postales cargados (o al revés). Tampoco es un «no»: falta el dato.
    """
    nombres = {entrega_zonas.normalizar_localidad(p) for p in _partes(localidades)}
    postales = {entrega_zonas.normalizar_cp(p) for p in _partes(codigos)}
    nombres.discard("")
    postales.discard("")
    if not nombres and not postales:
        return idioma.t("condiciones.zona_sin_listas", lengua, zona=zona)

    localidad = entrega_zonas.normalizar_localidad(zona)
    codigo = entrega_zonas.normalizar_cp(zona)
    if (localidad and localidad in nombres) or (codigo and codigo in postales):
        return idioma.t("condiciones.zona_dentro", lengua, zona=zona)

    # Un código postal no se puede buscar en una lista de localidades, ni un
    # nombre en una de códigos. Sin la lista que corresponde no hay «no» que
    # dar: lo que falta es el dato, y eso se pide.
    parece_codigo = any(caracter.isdigit() for caracter in codigo)
    if (parece_codigo and postales) or (not parece_codigo and localidad and nombres):
        return idioma.t(
            "condiciones.zona_fuera",
            lengua,
            zona=zona,
            # La única fila del catálogo que ya existía para esto, y que hasta
            # hoy no llamaba nadie. Va como PRIMERA frase y nunca sola: un «no»
            # solo es una puerta en la cara (ver CÓMO HABLÁS en app/prompts.py).
            frase=idioma.t("entrega.fuera_de_zona", lengua),
        )
    if postales:
        return idioma.t("condiciones.zona_pedir_cp", lengua, zona=zona)
    return idioma.t("condiciones.zona_pedir_localidad", lengua, zona=zona)


def _condiciones(zona: str, lengua: str) -> str:
    sin_dato = idioma.t("condiciones.sin_dato", lengua)
    si = idioma.t("ajustes.si", lengua)
    no = idioma.t("ajustes.no", lengua)
    lineas = [idioma.t("condiciones.titulo", lengua)]

    def poner(clave: str, valor: str) -> None:
        lineas.append(idioma.t(clave, lengua, valor=valor or sin_dato))

    poner("condiciones.dias", _dias("ENTREGA_DIAS", lengua))
    poner("condiciones.hora", _valor("ENTREGA_HORA"))
    localidades = _valor("ZONAS_ENTREGA_LOCALIDADES")
    codigos = _valor("ZONAS_ENTREGA_CP")
    poner("condiciones.localidades", localidades)
    poner("condiciones.cp", codigos)

    # Un booleano tiene TRES estados acá: encendido, apagado y sin valor
    # vigente. `RETIRO_LOCAL_ACTIVO` no es opcional y su default es "false", así
    # que NINGUNO sólo aparece cuando el almacén se perdió — y entonces no se
    # contesta ni que sí ni que no.
    retiro = _valor("RETIRO_LOCAL_ACTIVO")
    poner("condiciones.retiro", {"true": si, "false": no}.get(retiro, ""))
    if retiro == "true":
        poner("condiciones.retiro_dias", _dias("RETIRO_LOCAL_DIAS", lengua))
        poner("condiciones.retiro_hora", _valor("RETIRO_LOCAL_HORA"))

    # Con la excepción apagada no hay cargo ni mínimo en vigencia: decir el
    # número igual sería cotizar un envío que nadie autorizó.
    excepcion = _valor("ENTREGA_EXCEPCION_ACTIVA")
    poner("condiciones.fuera_de_dia", {"true": si, "false": no}.get(excepcion, ""))
    if excepcion == "true":
        poner("condiciones.fuera_de_dia_dias", _dias("ENTREGA_EXCEPCION_DIAS", lengua))
        poner("condiciones.fuera_de_dia_hora", _valor("ENTREGA_EXCEPCION_HORA"))
        poner("condiciones.cargo", _plata(_valor("ENTREGA_EXCEPCION_CARGO")))
        poner("condiciones.minimo", _plata(_valor("ENTREGA_EXCEPCION_MIN_TOTAL")))

    limpia = zona.strip()
    if limpia:
        lineas.append(_respuesta_de_zona(limpia, localidades, codigos, lengua))
    lineas.append(idioma.t("condiciones.instruccion", lengua, sin_dato=sin_dato))
    return "\n".join(lineas)


@tool
def condiciones_de_entrega(
    config: RunnableConfig,
    zona: Annotated[
        str,
        Field(
            description=(
                "La localidad o el código postal por el que preguntó, con SUS "
                "palabras («Alta Córdoba», «5000»). Uno solo. Dejalo VACÍO si "
                "no preguntó por una zona: el resto de la respuesta sale igual."
            )
        ),
    ] = "",
) -> str:
    """Días, horarios, zonas, retiro por el local y cargo de una entrega fuera de día.

    Sólo lectura: no cambia nada y no anota nada.

    Usala apenas pregunten por la entrega, antes de contestar:
    - «¿qué días repartís?», «¿salís los sábados?», «¿a qué hora me lo llevan?»
    - «¿llegás a Alta Córdoba?», «¿repartís en el 5000?» -> pasá eso en `zona`
    - «¿puedo pasar a buscarlo?»
    - «¿cuánto sale el envío?», «¿hay un mínimo?»

    Lo que devuelve es lo que el negocio hace EN GENERAL, nunca una promesa
    sobre un pedido: la entrega de un pedido concreto la decide una persona, y
    una entrega fuera de los días de reparto se pide con
    pedir_excepcion_de_entrega. Lo que salga como no configurado no es un «no»:
    no lo inventes ni lo niegues.
    """
    lengua = _lengua(config)
    try:
        return _condiciones(zona, lengua)
    except limites.LimiteError:
        # Una lectura que no contesta NO es «no hay nada configurado». Misma
        # postura que `consultar_stock` cuando no puede ver el depósito.
        return idioma.t("condiciones.no_pude", lengua)
