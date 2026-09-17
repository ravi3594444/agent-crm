"""Teléfonos de cualquier país: un solo formato canónico, en un solo lugar.

EL PROBLEMA QUE ESTO RESUELVE
Meta manda el remitente como dígitos sin `+`:      5493511234567
ERPNext guarda lo que el humano escribió:          +54 9 351 123-4567
                                                    0351 15 123-4567
                                                    351 1234567
Comparar esos strings con `=` no matchea NUNCA. Sin esto, todo cliente
registrado entra al bot como desconocido y el `_avisar_cliente` no encuentra
a nadie.

LA FORMA CANÓNICA: E.164 sin `+`. Para un móvil argentino son 13 dígitos
—54 + 9 + área + abonado—; para uno indio, 12 —91 + abonado—.

QUIÉN SABE LAS REGLAS DE CADA PAÍS: `phonenumbers`, el port de libphonenumber
de Google. Antes las reglas argentinas estaban escritas a mano acá —sacar el 0
troncal, sacar el 15 legacy según el largo, agregar el 9 de móvil— y eso tenía
dos consecuencias. Una: sólo andaba para Argentina, y este producto se vende a
quien sea. Dos, y peor: `PAIS_TELEFONO` NO era «el país», era «asumí reglas
argentinas con este prefijo». Medido — con `PAIS_TELEFONO=91`, un número indio
`918084794983` salía `9198084794983`, con el 9 de móvil ARGENTINO metido en el
medio, y desde ahí no matcheaba con nada.

LO QUE LA LIBRERÍA NO PUEDE SABER, Y SIGUE ACÁ
1. Un E.164 de otro país SIN `+` es indistinguible de un número nacional largo.
   `5511987654321` (Brasil) parseado como argentino da `+545511987654321`. La
   heurística de siempre —11 a 15 dígitos, sin 0 inicial, que no empiece con el
   código del país configurado— lo deja pasar intacto.
2. WHATSAPP ES SIEMPRE MÓVIL. En Argentina un móvil lleva un 9 después del 54
   que un fijo no lleva, y `351 123 4567` —sin el 15— la librería lo lee como
   fijo, así que no se lo pone. Ese 9 es una convención real del país, no una
   suposición nuestra, y vive en `_PREFIJO_MOVIL`: una tabla por código de
   país, no un `if` sobre Argentina.
"""

from __future__ import annotations

import re

import phonenumbers

from app import pais as _pais

# El código de país del negocio: 54 Argentina, 1 Estados Unidos, 91 India…
# `PAIS_TELEFONO` explícito gana; si no está, sale de `PAIS_NEGOCIO`; si no
# está ninguna, 54, que es lo que este archivo asumió siempre.
PAIS = _pais.codigo_telefono()


def _region(codigo: str) -> str:
    """De «54» a «AR», que es lo que `phonenumbers` entiende.

    Devuelve "" si el código no es un número o no corresponde a ningún país;
    con eso `normalizar` no intenta el parseo nacional y se comporta como
    antes en vez de levantar.
    """
    try:
        region = phonenumbers.region_code_for_country_code(int(codigo))
    except (TypeError, ValueError):
        return ""
    return "" if region in ("", "ZZ") else region


REGION = _region(PAIS)

# EL PREFIJO DE MÓVIL QUE LA LIBRERÍA NO AGREGA SOLA. En Argentina un móvil es
# 54 **9** área abonado y un fijo es 54 área abonado: escrito sin el 15 legacy,
# `351 123 4567` se lee como fijo y sale sin el 9. Para WhatsApp eso está mal
# por definición —del otro lado siempre hay un móvil—. Es una tabla por código
# de país y no un `if` sobre Argentina: el próximo país con la misma costumbre
# se agrega con una línea.
_PREFIJO_MOVIL = {54: "9"}

_SOLO_DIGITOS = re.compile(r"\D")

# Largo del número de abonado que usamos como clave de búsqueda difusa en
# ERPNext. 8 dígitos es lo más largo que se conserva igual en todos los
# formatos de arriba (área 11 + 8 dígitos de abonado).
LARGO_BUSQUEDA = 8


def solo_digitos(raw: str | None) -> str:
    return _SOLO_DIGITOS.sub("", raw or "")


def _parsear(texto: str, region: str | None = None):
    """`phonenumbers.parse` sin excepciones: None cuando no se puede."""
    try:
        return phonenumbers.parse(texto, region or None)
    except phonenumbers.NumberParseException:
        return None


def _canonico(numero) -> str:
    """E.164 sin `+`, con el prefijo de móvil del país si le falta."""
    if numero is None:
        return ""
    nsn = str(numero.national_number)
    prefijo = _PREFIJO_MOVIL.get(numero.country_code, "")
    if prefijo and not nsn.startswith(prefijo):
        nsn = prefijo + nsn
    return f"{numero.country_code}{nsn}"


def normalizar(raw: str | None, pais: str | None = None) -> str:
    """Devuelve el teléfono en forma canónica (dígitos, E.164 sin `+`).

    Devuelve "" si no hay nada usable. Nunca levanta excepción: esto corre
    sobre datos cargados a mano y tiene que tolerar cualquier basura.

    `pais` es el código de discado a usar en lugar del del proceso, y existe
    para UN caso: `app/readiness.py` chequea un .env CANDIDATO, que puede
    declarar otro país que el que está corriendo. `PAIS` y `REGION` se congelan
    al importar este módulo, así que sin este parámetro un preflight de un .env
    estadounidense normaliza con reglas argentinas y contesta sobre duplicados
    y sobre el número del dueño mirando otra cosa. En el runtime no se pasa:
    ahí el país del proceso ES el país.
    """
    codigo = str(pais or "").strip() or PAIS
    region = REGION if codigo == PAIS else _region(codigo)

    d = solo_digitos(raw)
    if not d:
        return ""

    # Prefijo de salida internacional.
    if d.startswith("00"):
        d = d[2:]

    # 1. YA TRAE CÓDIGO DE PAÍS. Con `+` es explícito; sin `+` lo decide que
    #    empiece con el del negocio. En los dos casos la librería sabe de qué
    #    país es y cómo se escribe — incluido el caso que rompía antes, un
    #    número indio con PAIS_TELEFONO=91.
    if str(raw or "").strip().startswith("+") or d.startswith(codigo):
        canonico = _canonico(_parsear(f"+{d}"))
        if canonico:
            return canonico

    # 2. NACIONAL VÁLIDO DEL PAÍS CONFIGURADO, antes de la heurística de abajo.
    #    Existe por Brasil: un móvil nacional brasileño son ONCE dígitos sin 0
    #    adelante, o sea exactamente lo que el paso 3 se lleva puesto, y
    #    `11 98765-4321` salía sin código de país. Acá decide la librería y no
    #    un largo: si para ESTE país es un número válido, es nacional.
    #
    #    Se exige `is_valid_number` y no que parsee: los números de prueba
    #    argentinos no son válidos —son inventados— y tienen que seguir
    #    llegando al paso 4, que es el permisivo.
    if region and 11 <= len(d) <= 15 and not d.startswith("0"):
        numero = _parsear(d, region)
        if numero is not None and phonenumbers.is_valid_number(numero):
            canonico = _canonico(numero)
            if canonico:
                return canonico

    # 3. E.164 DE OTRO PAÍS, SIN `+`. Indistinguible de un nacional largo para
    #    cualquier parser, así que se deja como vino. Ver el docstring.
    if 11 <= len(d) <= 15 and not d.startswith("0") and not d.startswith(codigo):
        return d

    # 4. NACIONAL DEL PAÍS CONFIGURADO: el 0 troncal, el 15 argentino y los
    #    códigos de área de 2 a 4 dígitos los resuelve la librería.
    if region:
        canonico = _canonico(_parsear(d, region))
        if canonico:
            return canonico

    # Basura, o un país que no reconoce: devolvemos los dígitos para que al
    # menos la comparación sea consistente de los dos lados.
    return d


def clave_busqueda(raw: str | None) -> str:
    """Los últimos dígitos del abonado, para un `like` en ERPNext.

    Buscamos por esto porque es lo único que sobrevive igual a todos los
    formatos con los que un humano puede haber cargado el teléfono.
    """
    nsn = normalizar(raw)
    return nsn[-LARGO_BUSQUEDA:] if len(nsn) >= LARGO_BUSQUEDA else nsn


def son_el_mismo(a: str | None, b: str | None) -> bool:
    na, nb = normalizar(a), normalizar(b)
    return bool(na) and na == nb
