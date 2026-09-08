"""Herramientas para auditar lo que SALE, no lo que dice el catálogo.

La diferencia importa. Un test que le pide un texto al catálogo prueba el
catálogo. Lo que hay que probar es el camino real: que cuando el idioma es
inglés, lo que Meta recibe no tiene una palabra en español.

CÓMO SE DECIDE QUE UN TEXTO ESTÁ EN ESPAÑOL
Con una lista de palabras que sólo existen en español, más los acentos y los
signos de apertura. No es un detector de idiomas: es un detector de restos.
Alcanza porque los datos de prueba están escritos en inglés a propósito —el
cliente se llama "Demo Bakery" y el producto "Whole Milk 1 L"— así que
cualquier palabra en español que aparezca en la salida vino de una plantilla
sin migrar, que es exactamente lo que se busca.

LA COMPARACIÓN ES POR FICHA ENTERA, Y DE ESO DEPENDE TODO LO DEMÁS
`restos_en_espanol` parte el texto en fichas y las cruza contra un conjunto:
nunca busca una palabra como substring de otra. Ésa es la única razón por la
que la lista puede tener palabras de dos y tres letras. Con comparación por
substring, `sin` matchearía `business` y `de` matchearía `order`, y el audit se
volvería ruido puro — o sea, inservible, que es peor que no tenerlo.

El recorte de `permitido` sigue la misma regla: recorta en los bordes de la
ficha, no en cualquier parte de una palabra. Recortando substrings, el comando
`ver` convertía `Delivered` en `Deli ed` y fabricaba fichas que nadie escribió.

Si algún día hay que volver a tocar esto: primero mirar cómo se tokeniza,
después agregar palabras. Nunca al revés.
"""
from __future__ import annotations

import functools
import re
import unicodedata

# Acentos y signos de apertura: no existen en inglés.
_ACENTOS = re.compile(r"[áéíóúüñ¿¡]", re.IGNORECASE)

# Palabras que en inglés no significan nada. Deliberadamente cortas y comunes:
# lo que se busca es el resto de una plantilla, no una traducción perfecta.
_RESTOS_DE_PLANTILLA = frozenset(
    ["pedido", "pedidos", "cliente", "clientes", "entrega", "entregas", "codigo", "codigos", "confirmado", "confirmada", "confirmar", "confirma", "pendiente", "pendientes", "revision", "rechazado", "rechazada", "rechazar", "cancelado", "cancelada", "cancelar", "motivo", "motivos", "origen", "informativo", "respondé", "responde", "contesta", "contestá", "los", "las", "del", "una", "unas", "unos", "cuando", "donde", "esto", "estan", "estos", "estas", "hace", "falta", "queda", "quedan", "quedo", "dias", "horas", "fecha", "fechas", "monto", "montos", "deposito", "precio", "precios", "equipo", "dueño", "gerencia", "aviso", "avisos", "alerta", "alertas", "pero", "porque", "tambien", "ahora", "despues", "antes", "tuve", "problema", "tecnico", "disculpa", "disculpas"]
)

# Las funcionales cortas. `los`, `las`, `del`, `una`, `unos` y `unas` ya estaban
# en la lista de arriba; faltaban `de` y `sin`, que son las DOS MÁS COMUNES del
# idioma. Sin ellas, el audit que existe para agarrar español filtrándose a un
# mensaje en inglés no podía agarrar justamente los dos casos más frecuentes:
# el separador de un conteo (`5 de 6`) y una etiqueta de cubeta (`sin stock 3`).
#
# Son seguras SÓLO porque la comparación es por ficha entera (ver el docstring
# del módulo y `restos_en_espanol`): `business` no matchea `sin`, `order` no
# matchea `de`, y `Panadería López` no matchea nada. Si esa comparación alguna
# vez se volviera de substring, estas dos palabras solas alcanzan para llenar el
# audit de ruido y dejarlo inservible.
_FUNCIONALES_ES = frozenset(["de", "sin"])

_PALABRAS_ES = _RESTOS_DE_PLANTILLA | _FUNCIONALES_ES

# Lo que SÍ puede aparecer en español aunque el idioma sea inglés, porque no es
# prosa: nombres propios de ERPNext, estados canónicos, marcas de auditoría.
# Todo lo que entre acá tiene que estar justificado en la lista de abajo.
PERMITIDO = (
    # Estados canónicos de ERPNext: son valores, no texto.
    "To Deliver and Bill", "To Bill", "To Deliver", "Completed", "Closed",
    "Draft", "Cancelled",
    # Marcas de auditoría. Nunca se traducen: las lee el propio código.
    "[limite]", "[entrega]", "[idioma]", "[confirmado-por-agente]",
    # Comandos que tienen que seguir funcionando en español.
    "confirmar", "rechazar", "cancelar", "ver", "preparar", "despachar",
)


def _sin_tildes(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )


@functools.cache
def _patron_permitido(dato: str) -> re.Pattern[str]:
    r"""El dato permitido, anclado a los bordes de la ficha.

    Recortar substrings fabricaba fichas que nadie escribió: con el comando
    `ver` en la lista, `Delivered` quedaba como `Deli ed` y `server` como
    `ser`. Cada pedazo así es una ficha inventada, y una ficha inventada puede
    coincidir con una palabra corta de la lista — el ruido que hace inservible
    a un audit.

    El ancla es condicional porque no todo lo permitido empieza y termina en
    letra: `[limite]` y las otras marcas de auditoría arrancan con `[`, y ahí
    un `\b` no coincide nunca. Se ancla el borde sólo cuando ese borde es
    alfanumérico.

    Cacheado porque el audit llama a `restos_en_espanol` miles de veces con la
    misma lista de permitidos: sin caché son ~66 patrones recompilados por
    llamada.
    """
    izquierda = r"(?<!\w)" if dato[:1].isalnum() or dato[:1] == "_" else ""
    derecha = r"(?!\w)" if dato[-1:].isalnum() or dato[-1:] == "_" else ""
    return re.compile(izquierda + re.escape(dato) + derecha)


def restos_en_espanol(texto: object, permitido: tuple[str, ...] = ()) -> list[str]:
    """Las marcas de español que quedan en ese texto. Vacío = limpio.

    ``permitido`` son los datos de la prueba que legítimamente vienen en
    español (el nombre de un producto, el de un cliente). Se recortan del texto
    antes de mirar, para que un dato no se lea como una plantilla sin migrar.

    El recorte y la búsqueda trabajan los dos por FICHA ENTERA. Eso es lo que
    deja que la lista tenga `de` y `sin` sin ahogar el audit en falsos
    positivos; leer el docstring del módulo antes de cambiarlo.
    """
    crudo = str(texto or "")
    for dato in tuple(permitido) + PERMITIDO:
        # Un permitido vacío recortaba entre CADA letra del texto y dejaba el
        # mensaje entero partido en fichas de un caracter — o sea, dejaba al
        # detector CIEGO: `restos_en_espanol("5 de 6", ("",))` no encontraba
        # nada.
        if not (limpio := str(dato)):
            continue
        crudo = _patron_permitido(limpio).sub(" ", crudo)
    hallados = []
    if _ACENTOS.search(crudo):
        hallados.extend(sorted(set(_ACENTOS.findall(crudo))))
    plano = _sin_tildes(crudo).lower()
    fichas = {f.strip(".,;:!?()[]'\"*·—-…") for f in plano.split()}
    hallados.extend(sorted(fichas & _PALABRAS_ES))
    return hallados


class Salida:
    """Todo lo que el sistema le mandó a una persona durante un test."""

    def __init__(self) -> None:
        self.mensajes: list[tuple[str, str]] = []      # (telefono, texto)
        self.botones: list[tuple[str, str]] = []       # (telefono, cuerpo)
        self.plantillas: list[tuple[str, str]] = []    # (telefono, nombre)

    # -- lo que se captura -------------------------------------------------
    def enviar_mensaje(self, telefono, texto, *a, **k):
        self.mensajes.append((str(telefono), str(texto)))
        return {"messages": [{"id": f"wamid.fake-{len(self.mensajes)}"}]}

    def enviar_botones(self, telefono, cuerpo, botones, *a, **k):
        self.botones.append((str(telefono), str(cuerpo)))
        return {"messages": [{"id": f"wamid.fakeb-{len(self.botones)}"}]}

    def enviar_plantilla(self, telefono, nombre, *a, **k):
        self.plantillas.append((str(telefono), str(nombre)))
        return {"messages": [{"id": f"wamid.fakep-{len(self.plantillas)}"}]}

    # -- lo que se pregunta ------------------------------------------------
    @property
    def textos(self) -> list[str]:
        """Todo el texto en prosa que recibió una persona."""
        return [t for _, t in self.mensajes] + [c for _, c in self.botones]

    def para(self, telefono: str) -> list[str]:
        objetivo = str(telefono)
        return [t for tel, t in self.mensajes if tel == objetivo] + [
            c for tel, c in self.botones if tel == objetivo
        ]

    def limpiar(self) -> None:
        self.mensajes.clear()
        self.botones.clear()
        self.plantillas.clear()


def parchar_salida(monkeypatch) -> Salida:
    """Intercepta las TRES puertas de salida y devuelve lo que se mandó."""
    from app import avisos, decisiones, notificar, whatsapp

    salida = Salida()
    for modulo in (whatsapp, notificar, avisos, decisiones):
        for nombre, funcion in (
            ("enviar_mensaje", salida.enviar_mensaje),
            ("enviar_botones", salida.enviar_botones),
            ("enviar_plantilla", salida.enviar_plantilla),
        ):
            if hasattr(modulo, nombre):
                monkeypatch.setattr(modulo, nombre, funcion, raising=False)
    return salida
