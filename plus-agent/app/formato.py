"""Formato de números y texto como se leen en el WhatsApp de quien mira.

POR QUÉ ESTO ES UN ARCHIVO
Todo el código usaba `f"${total:,.0f}"`, que en Python da `$12,000`. Un
argentino lee eso como doce pesos con decimales: acá el separador de miles
es el punto y el decimal es la coma. El dueño mirando "Total: $12,000" en la
pantalla de bloqueo, decidiendo si confirma, no puede tener esa duda.

    12000    -> $12.000        (LOCALE=es_AR)
    1500.5   -> $1.500,50      (LOCALE=es_AR)

Y el mismo problema existe al revés. Un cliente estadounidense lee `$1.200,50`
como un dólar veinte: la misma duda, en la otra dirección y por el mismo
motivo. Por eso los separadores ya no se dan vuelta a mano — los pone Babel,
con el locale del despliegue.

    1200.5   -> $1,200.50      (LOCALE=en_US)

Segundo problema visto en vivo: el modelo escribe Markdown (`**negrita**`,
viñetas con `- `, títulos con `#`). WhatsApp no entiende Markdown: la negrita
es UN asterisco, y `**` se muestra tal cual. El cliente ve asteriscos sueltos.
`whatsapp_texto` traduce lo mínimo indispensable sin tocar lo que ya está bien.
"""

from __future__ import annotations

import os
import re

from babel.numbers import format_currency

# LOCALE ES UN VALOR DE DESPLIEGUE, NO UN LÍMITE DEL DUEÑO.
# Se fija en el onboarding al lado de AUTO_CONFIRM_CURRENCY y se lee del
# entorno. NUNCA llega por WhatsApp y no vive en el almacén de límites: quién
# lo pone es quien instala el sistema, no un mensaje. Cambiar cómo se escribe
# la plata a mitad de una conversación es exactamente la clase de cambio que
# no tiene por qué poder hacerse desde un teléfono.
#
# La moneda sale del locale y no de AUTO_CONFIRM_CURRENCY a propósito. Esa
# variable es el nombre EXACTO de la moneda en ERPNext —un valor que se
# compara, no un idioma—, y pasársela a Babel con otro locale daría
# «ARS 1,200.50»: el código en vez del símbolo, que es justo lo que un
# prospecto no tiene que ver en una demo.
_LOCALES = {
    "es_AR": "ARS",
    "en_US": "USD",
}
_LOCALE_POR_DEFECTO = "es_AR"


def locale_actual() -> str:
    """El locale del despliegue. Cualquier cosa rara es es_AR.

    Igual que `idioma.por_defecto`: nunca vacío y nunca levanta. Un locale mal
    escrito en un .env no puede ser la razón por la que un total no se muestra.
    """
    crudo = str(os.getenv("LOCALE", "") or "").strip()
    return crudo if crudo in _LOCALES else _LOCALE_POR_DEFECTO


def pesos(monto: float | int | None, decimales: int = 0) -> str:
    """Monto con los separadores del locale del despliegue, listo para mostrar.

    Sigue llamándose `pesos` y sigue recibiendo lo mismo: es la envoltura fina
    de sus ~40 call sites, que no tienen por qué enterarse de Babel ni del
    locale. Lo único que cambió es quién pone el punto y quién la coma.

    Babel NO es el módulo `locale` de la stdlib. Recibe el locale en cada
    llamada y no deja estado en el proceso, así que los barridos que corren en
    hilos no se pisan entre sí — la objeción de `setlocale` no aplica acá.
    """
    try:
        valor = float(monto or 0)
    except (TypeError, ValueError):
        valor = 0.0
    loc = locale_actual()
    # El patrón viene con las cifras decimales que pidió quien llama, y
    # `currency_digits=False` es lo que hace que se respeten: con el default
    # Babel impone las dos de la moneda y `pesos(12000)` diría «$12.000,00».
    patron = "¤#,##0" + ("." + "0" * decimales if decimales > 0 else "")
    return format_currency(
        valor,
        _LOCALES[loc],
        format=patron,
        locale=loc,
        currency_digits=False,
    )


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
