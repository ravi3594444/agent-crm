"""Formato de números y texto como se leen en un WhatsApp argentino.

POR QUÉ ESTO ES UN ARCHIVO
Todo el código usaba `f"${total:,.0f}"`, que en Python da `$12,000`. Un
argentino lee eso como doce pesos con decimales: acá el separador de miles
es el punto y el decimal es la coma. El dueño mirando "Total: $12,000" en la
pantalla de bloqueo, decidiendo si confirma, no puede tener esa duda.

    12000    -> $12.000
    1500.5   -> $1.500,50

Segundo problema visto en vivo: el modelo escribe Markdown (`**negrita**`,
viñetas con `- `, títulos con `#`). WhatsApp no entiende Markdown: la negrita
es UN asterisco, y `**` se muestra tal cual. El cliente ve asteriscos sueltos.
`whatsapp_texto` traduce lo mínimo indispensable sin tocar lo que ya está bien.
"""

from __future__ import annotations

import re


def pesos(monto: float | int | None, decimales: int = 0) -> str:
    """Monto con separador de miles argentino, listo para mostrar."""
    try:
        valor = float(monto or 0)
    except (TypeError, ValueError):
        valor = 0.0
    entero = f"{abs(valor):,.{decimales}f}"
    # Python usa , para miles y . para decimales: los damos vuelta.
    entero = entero.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    signo = "-" if valor < 0 else ""
    return f"{signo}${entero}"


def cantidad(valor: float | int | None) -> str:
    """Cantidad sin ceros al final: 10 en lugar de 10.0, 2,5 en lugar de 2.5."""
    try:
        num = float(valor or 0)
    except (TypeError, ValueError):
        return "0"
    texto = f"{num:g}"
    return texto.replace(".", ",")


# --- Markdown -> WhatsApp -------------------------------------------------

# Las URLs se apartan antes de tocar nada: un `_` o `*` dentro de un enlace
# es parte del enlace, no formato.
_URL = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]]+")

# `**x**` -> `*x*`. El interior no puede tener `*` ni saltos de línea, y debe
# empezar y terminar en algo visible: así un `*negrita*` de WhatsApp que ya
# está bien (un solo asterisco) nunca coincide.
_BOLD = re.compile(r"\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*")

# `__x__` -> `_x_`. Se exige borde de palabra para no romper códigos tipo
# `ITEM__A__B`.
_UNDER = re.compile(r"(?<!\w)__(?=\S)([^_\n]+?)(?<=\S)__(?!\w)")

# Viñetas Markdown al inicio de línea. Se exige el espacio después del guion
# o asterisco: `*Total:*` al inicio de línea es negrita, no una viñeta.
_BULLET = re.compile(r"^(\s*)[-*][ \t]+(?=\S)", re.MULTILINE)

# Títulos `# ...` al inicio de línea. `pedido #8` en medio de una frase no
# coincide porque no está al inicio ni tiene espacio después de los `#`.
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$", re.MULTILINE)

_BLANK_LINES = re.compile(r"\n{3,}")


def _titulo(match: re.Match[str]) -> str:
    texto = match.group(1).strip()
    if not texto:
        return ""
    # Si el título ya trae negrita (por ejemplo `# **Resumen**` ya convertido
    # a `*Resumen*`), no volver a envolverlo: quedarían dobles asteriscos.
    if "*" in texto:
        return texto
    return f"*{texto}*"


def whatsapp_texto(texto: str | None) -> str:
    """Traduce el Markdown que suele emitir el modelo al formato de WhatsApp.

    Conservador a propósito: sólo toca `**`, `__`, viñetas y títulos al
    inicio de línea, y exceso de líneas en blanco. Un texto que ya está en
    formato WhatsApp sale igual que entró. URLs y números de pedido no se
    tocan nunca.
    """
    if texto is None:
        return ""
    if not isinstance(texto, str):
        texto = str(texto)
    if not texto:
        return texto

    texto = texto.replace("\r\n", "\n").replace("\r", "\n")

    # Apartar URLs con marcadores que ninguna regla de arriba puede tocar.
    urls: list[str] = []

    def _guardar(match: re.Match[str]) -> str:
        urls.append(match.group(0))
        return f"\x00{len(urls) - 1}\x00"

    texto = _URL.sub(_guardar, texto)

    texto = _BOLD.sub(r"*\1*", texto)
    texto = _UNDER.sub(r"_\1_", texto)
    texto = _HEADING.sub(_titulo, texto)
    texto = _BULLET.sub(r"\1• ", texto)
    texto = _BLANK_LINES.sub("\n\n", texto)

    for indice, url in enumerate(urls):
        texto = texto.replace(f"\x00{indice}\x00", url)
    return texto


# --- Citas de WhatsApp ----------------------------------------------------

# app/solicitudes.py::citar antepone "> " a cada línea de lo que escribió un
# cliente, justo para que se lea como una cita. Cuando el dueño responde
# CITANDO ese mensaje, esas líneas vuelven adentro de su respuesta — y lo que
# él escribe se lee como el motivo de un rechazo o los términos de una
# contraoferta. Así que la cita se saca ANTES de que nada de eso se lea como
# un argumento: lo que escribió un cliente es un dato, nunca una instrucción,
# ni siquiera después de que una persona lo reenvió.
#
# Vive acá, y no en el módulo que lo usa, porque lo usan dos: el router
# determinista (app/main.py) y la capa que convierte la prosa del dueño en una
# acción (app/acciones.py). Dos copias de una regla de seguridad son dos
# reglas, y la segunda se olvida.
_CITA = re.compile(r"^\s*>.*$", re.MULTILINE)


def sin_citas(texto: object) -> str:
    """El texto sin las líneas citadas, en una sola línea."""
    return " ".join(_CITA.sub(" ", str(texto or "")).split())
