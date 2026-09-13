"""Formato de números y texto como se leen en el WhatsApp de quien mira.

POR QUÉ ESTO ES UN ARCHIVO
Todo el código usaba `f"${total:,.0f}"`, que en Python da `$12,000`. Un
argentino lee eso como doce pesos con decimales: acá el separador de miles
es el punto y el decimal es la coma. El dueño mirando "Total: $12,000" en la
pantalla de bloqueo, decidiendo si confirma, no puede tener esa duda.

    12000    -> $12.000      (LOCALE=es_AR)
    1500.5   -> $1.500,50

Y el mismo problema al revés, que es el que trajo el primer cliente que habla
inglés: `$1.200,50` le dice a un estadounidense "un dólar veinte". La forma del
número no es una preferencia estética, es cuánta plata dice que es.

    1200.5   -> $1,200.50    (LOCALE=en_US)

Segundo problema visto en vivo: el modelo escribe Markdown (`**negrita**`,
viñetas con `- `, títulos con `#`). WhatsApp no entiende Markdown: la negrita
es UN asterisco, y `**` se muestra tal cual. El cliente ve asteriscos sueltos.
`whatsapp_texto` traduce lo mínimo indispensable sin tocar lo que ya está bien.
"""

from __future__ import annotations

import math
import os
import re
from decimal import Decimal, InvalidOperation

from babel.numbers import format_currency

# --- LOCALE: la forma del número, no el idioma de la prosa -----------------
#
# ES UN VALOR DE DESPLIEGUE, y eso es una decisión de seguridad, no de comodidad.
# Se fija en el onboarding al lado de AUTO_CONFIRM_CURRENCY y NUNCA llega por
# WhatsApp: no es un límite del dueño, no tiene código de dos pasos y no está en
# `app/limites.py` a propósito. Un mensaje entrante no puede cambiar cómo se lee
# un monto en la pantalla en que alguien aprueba ese monto.
#
# ES INDEPENDIENTE DEL IDIOMA, y también a propósito. `IDIOMA_GERENCIA` decide en
# qué idioma se le habla al dueño; `LOCALE` decide qué forma tiene un número. Un
# almacén argentino con un dueño que lee en inglés existe —es el caso del piloto—
# y sus pesos se siguen escribiendo $12.000.
LOCALE_POR_DEFECTO = "es_AR"
LOCALES = (LOCALE_POR_DEFECTO, "en_US")

# El símbolo sale del locale, no de AUTO_CONFIRM_CURRENCY, y la diferencia se
# puede defender en una línea: `pesos` SIEMPRE escribió "$" y nunca un código de
# moneda. Los call sites que necesitan decir cuál moneda es le pegan el código de
# ERPNext al lado —"$4.800,00 INR"— porque ése es el dato, y el dato no tiene
# idioma. Si acá se usara AUTO_CONFIRM_CURRENCY, un despliegue en ARS con
# LOCALE=en_US escribiría "ARS 1,200.50": un código de moneda donde antes había
# un símbolo, en los cuarenta lugares donde se muestra plata.
_MONEDA_DEL_LOCALE = {"es_AR": "ARS", "en_US": "USD"}

_NORMALES = {codigo.lower(): codigo for codigo in LOCALES}


def locale_configurado() -> str:
    """El locale del despliegue. NUNCA levanta y nunca queda vacío.

    Cualquier cosa que no sea uno de `LOCALES` es el de por defecto, igual que
    `idioma.por_defecto()`: un locale mal escrito no puede dejar sin salir un
    mensaje. Quien avisa que está mal escrito es `readiness`, en el arranque y
    una sola vez, en vez de este módulo cuarenta veces por mensaje.
    """
    crudo = str(os.getenv("LOCALE", "") or "").strip().replace("-", "_").lower()
    return _NORMALES.get(crudo, LOCALE_POR_DEFECTO)


def _decimal(monto: object) -> Decimal:
    """El monto como Decimal, o cero. Ni una excepción ni un infinito.

    `float` para no cambiar qué acepta —hoy entra un str, un None y un objeto
    cualquiera— y `Decimal(str(...))` para redondear sobre el número que se
    escribió y no sobre el binario más cercano.
    """
    try:
        valor = float(monto or 0)
    except (TypeError, ValueError):
        return Decimal(0)
    if not math.isfinite(valor):
        # Un total que es inf o nan no es un total. Antes salía "$inf" en la
        # pantalla en que alguien aprueba un pedido.
        return Decimal(0)
    try:
        return Decimal(str(valor))
    except InvalidOperation:
        return Decimal(0)


def pesos(monto: float | int | None, decimales: int = 0) -> str:
    """Monto como lo lee quien mira, según LOCALE. Listo para mostrar.

    Sigue llamándose `pesos` y toma lo mismo que siempre: son cuarenta call
    sites, y renombrarlos no cambia nada de lo que ve una persona.

    El patrón se arma acá en vez de dejar el de CLDR porque `decimales` es del
    llamador —un total va con dos, un tope va sin ninguno— y `format_currency`
    usa por defecto los dígitos de la moneda, que para ARS y USD son dos
    siempre. `currency_digits=False` es lo que le devuelve la decisión al
    patrón.
    """
    patron = "\u00a4#,##0" + ("." + "0" * decimales if decimales > 0 else "")
    idioma_del_numero = locale_configurado()
    try:
        return format_currency(
            _decimal(monto),
            _MONEDA_DEL_LOCALE[idioma_del_numero],
            locale=idioma_del_numero,
            format=patron,
            currency_digits=False,
        )
    except Exception as exc:  # pragma: no cover - defensa, no comportamiento
        # Un mensaje que no sale es peor que un número sin separadores. Misma
        # regla que `idioma.t`: se degrada y se anota, nunca se levanta.
        print(f"[formato] no pude formatear un monto ({type(exc).__name__})")
        return f"${_decimal(monto):.{decimales}f}"


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
