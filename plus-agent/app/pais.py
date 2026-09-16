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


def territorio() -> str:
    """`PAIS_NEGOCIO` como código ISO de dos letras, o "" si no está puesto."""
    crudo = str(os.getenv("PAIS_NEGOCIO", "") or "").strip().upper()
    return crudo if len(crudo) == 2 and crudo.isalpha() else ""


def codigo_telefono(por_defecto: str = "54") -> str:
    """El código de discado: lo explícito, lo derivado, o el de siempre.

    Devuelve un string de dígitos porque es lo que `app/telefono.py` compara
    contra el principio de un número.
    """
    explicito = str(os.getenv("PAIS_TELEFONO", "") or "").strip()
    if explicito:
        return explicito
    lugar = territorio()
    if lugar:
        codigo = phonenumbers.country_code_for_region(lugar)
        if codigo:
            return str(codigo)
    return por_defecto


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


def locale(por_defecto: str = "es_AR") -> str:
    """El locale para los montos: lo explícito, lo derivado, o el de siempre."""
    explicito = str(os.getenv("LOCALE", "") or "").strip()
    if explicito:
        return explicito.replace("-", "_")
    lugar = territorio()
    if lugar:
        idioma = idioma_del_territorio(lugar)
        if idioma:
            return f"{idioma}_{lugar}"
    return por_defecto
