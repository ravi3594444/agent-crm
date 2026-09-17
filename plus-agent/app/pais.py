"""UN SOLO AJUSTE DE PAÍS, del que salen los otros tres.

EL PROBLEMA QUE RESUELVE
Instalar un cliente en otro país eran cuatro variables que hay que mantener
coherentes entre sí —`PAIS_TELEFONO`, `LOCALE`, la moneda y la zona— y que se
pueden contradecir sin que nadie avise: `PAIS_TELEFONO=1` con `LOCALE=es_AR`
son números estadounidenses escritos con separadores argentinos, y nada falla.

Con `PAIS_NEGOCIO=US` alcanza. Los tres datos —código de discado, locale y
moneda— son DATOS PUBLICADOS de cada país, y ya están adentro de CLDR y de
libphonenumber, que este proyecto ya usa para otra cosa. No hay tabla que
mantener acá: se le pregunta a los datos.

    PAIS_NEGOCIO=AR  ->  +54   es_AR   ARS
    PAIS_NEGOCIO=US  ->  +1    en_US   USD
    PAIS_NEGOCIO=IN  ->  +91   hi_IN   INR
    PAIS_NEGOCIO=BR  ->  +55   pt_BR   BRL

LAS VARIABLES VIEJAS SIGUEN GANANDO. `PAIS_TELEFONO` y `LOCALE` puestas a mano
mandan sobre lo derivado: un despliegue que ya existe no cambia de conducta por
actualizar, y un caso raro —un negocio argentino que factura en dólares— se
resuelve poniendo la de siempre. Sin ninguna de las tres, todo queda como
estaba: 54 y es_AR.

POR QUÉ ESTO NO ES UN AJUSTE DEL PANEL. El código de país decide QUIÉN es un
teléfono: `app/telefono.py` normaliza con él, y con eso se busca al cliente que
escribió. Cambiarlo con clientes ya cargados no reescribe lo que está guardado
en ERPNext, así que los de antes dejan de matchear y vuelven como desconocidos.
Es una decisión del día que se instala, no una preferencia. `LOCALE` sí es
cosmético —cómo se escribe un número— y ése puede vivir donde se quiera.
"""
from __future__ import annotations

import os

import phonenumbers
from babel.core import get_global

# Lo que este archivo asume sin ninguna variable puesta, que es lo que el
# proyecto asumió siempre. Son constantes y no literales sueltos porque
# `app/readiness.py` tiene que rehacer esta MISMA cadena de precedencia sobre un
# .env candidato, y dos copias del "54" se despegan la primera vez que una
# cambia.
CODIGO_POR_DEFECTO = "54"
LOCALE_POR_DEFECTO = "es_AR"


def _limpio(lugar: str | None) -> str:
    """Un código ISO de dos letras en mayúsculas, o "" si no tiene esa forma."""
    crudo = str(lugar or "").strip().upper()
    return crudo if len(crudo) == 2 and crudo.isalpha() else ""


def territorio() -> str:
    """`PAIS_NEGOCIO` como código ISO de dos letras, o "" si no está puesto."""
    return _limpio(os.getenv("PAIS_NEGOCIO", ""))


def codigo_de(lugar: str | None) -> str:
    """El código de discado de ESE territorio, o "" si no es un país real.

    Separada de `codigo_telefono()` porque son dos preguntas distintas, y
    confundirlas es lo que dejaba pasar un `PAIS_NEGOCIO=ZZ`. «Qué código uso»
    siempre tiene respuesta —termina en el default—; «este territorio existe»
    tiene que poder contestar que no, y es la única de las dos que sirve para
    validar lo que alguien escribió en el .env.
    """
    lugar = _limpio(lugar)
    if not lugar:
        return ""
    codigo = phonenumbers.country_code_for_region(lugar)
    return str(codigo) if codigo else ""


def locale_de(lugar: str | None) -> str:
    """El locale de ESE territorio, o "" si CLDR no le conoce idioma oficial."""
    lugar = _limpio(lugar)
    if not lugar:
        return ""
    idioma = idioma_del_territorio(lugar)
    return f"{idioma}_{lugar}" if idioma else ""


def codigo_telefono(por_defecto: str = CODIGO_POR_DEFECTO) -> str:
    """El código de discado: lo explícito, lo derivado, o el de siempre.

    Devuelve un string de dígitos porque es lo que `app/telefono.py` compara
    contra el principio de un número.
    """
    explicito = str(os.getenv("PAIS_TELEFONO", "") or "").strip()
    if explicito:
        return explicito
    return codigo_de(territorio()) or por_defecto


def idioma_del_territorio(lugar: str) -> str:
    """El idioma OFICIAL más hablado de ese territorio, según CLDR.

    Oficial y por población: para la India da `hi` y no `en`, y para los dos el
    número se escribe igual —₹12,00,000—, porque lo que decide la forma es el
    territorio. El idioma en que HABLA el agente es otra cosa y vive en
    `app/idioma.py`: esto sólo arma el locale con el que se formatea un monto.
    """
    datos = get_global("territory_languages").get(lugar) or {}
    oficiales = sorted(
        (
            (datos[codigo].get("population_percent") or 0, codigo)
            for codigo in datos
            if datos[codigo].get("official_status")
        ),
        reverse=True,
    )
    return oficiales[0][1] if oficiales else ""


def locale(por_defecto: str = LOCALE_POR_DEFECTO) -> str:
    """El locale para los montos: lo explícito, lo derivado, o el de siempre."""
    explicito = str(os.getenv("LOCALE", "") or "").strip()
    if explicito:
        return explicito.replace("-", "_")
    return locale_de(territorio()) or por_defecto
