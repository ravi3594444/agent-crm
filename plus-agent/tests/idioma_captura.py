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
"""
from __future__ import annotations

import re
import unicodedata

# Acentos y signos de apertura: no existen en inglés.
_ACENTOS = re.compile(r"[áéíóúüñ¿¡]", re.IGNORECASE)

# Palabras que en inglés no significan nada. Deliberadamente cortas y comunes:
# lo que se busca es el resto de una plantilla, no una traducción perfecta.
_PALABRAS_ES = frozenset(
    ["pedido", "pedidos", "cliente", "clientes", "entrega", "entregas", "codigo", "codigos", "confirmado", "confirmada", "confirmar", "confirma", "pendiente", "pendientes", "revision", "rechazado", "rechazada", "rechazar", "cancelado", "cancelada", "cancelar", "motivo", "motivos", "origen", "informativo", "respondé", "responde", "contesta", "contestá", "los", "las", "del", "una", "unas", "unos", "cuando", "donde", "esto", "estan", "estos", "estas", "hace", "falta", "queda", "quedan", "quedo", "dias", "horas", "fecha", "fechas", "monto", "montos", "deposito", "precio", "precios", "equipo", "dueño", "gerencia", "aviso", "avisos", "alerta", "alertas", "pero", "porque", "tambien", "ahora", "despues", "antes", "tuve", "problema", "tecnico", "disculpa", "disculpas"]
)

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


def restos_en_espanol(texto: object, permitido: tuple[str, ...] = ()) -> list[str]:
    """Las marcas de español que quedan en ese texto. Vacío = limpio.

    ``permitido`` son los datos de la prueba que legítimamente vienen en
    español (el nombre de un producto, el de un cliente). Se recortan del texto
    antes de mirar, para que un dato no se lea como una plantilla sin migrar.
    """
    crudo = str(texto or "")
    for dato in tuple(permitido) + PERMITIDO:
        crudo = crudo.replace(str(dato), " ")
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
