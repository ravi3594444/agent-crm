"""Teléfonos argentinos: un solo formato canónico, en un solo lugar.

EL PROBLEMA QUE ESTO RESUELVE
Meta manda el remitente como dígitos sin `+`:      5493511234567
ERPNext guarda lo que el humano escribió:          +54 9 351 123-4567
                                                    0351 15 123-4567
                                                    351 1234567
Comparar esos strings con `=` no matchea NUNCA. Sin esto, todo cliente
registrado entra al bot como desconocido y el `_avisar_cliente` no encuentra
a nadie.

LA FORMA CANÓNICA
Móvil argentino en E.164 sin `+`: 54 + 9 + área + abonado = 13 dígitos.
    54 9 11  1234 5678   -> 5491112345678
    54 9 351 123 4567    -> 5493511234567

El número nacional significativo (NSN) argentino de un móvil tiene:
    10 dígitos  sin el 15 legacy   (área 2-4 + abonado)
    12 dígitos  con el 15 legacy   (área 2-4 + "15" + abonado)
Eso hace que el largo alcance para decidir si hay que sacar el 15.

WHATSAPP ES SIEMPRE MÓVIL. Un fijo cargado como `mobile_no` se normaliza
igual (le agregamos el 9) porque no hay forma de distinguirlo por largo, y
para nuestro uso —buscar al cliente que nos escribió— es lo correcto.
"""

from __future__ import annotations

import os
import re

# Números de otros países se dejan como vienen: ya son E.164.
PAIS = os.getenv("PAIS_TELEFONO", "54")

_SOLO_DIGITOS = re.compile(r"\D")

# Largo del número de abonado que usamos como clave de búsqueda difusa en
# ERPNext. 8 dígitos es lo más largo que se conserva igual en todos los
# formatos de arriba (área 11 + 8 dígitos de abonado).
LARGO_BUSQUEDA = 8


def solo_digitos(raw: str | None) -> str:
    return _SOLO_DIGITOS.sub("", raw or "")


def normalizar(raw: str | None) -> str:
    """Devuelve el teléfono en forma canónica (dígitos, E.164 sin `+`).

    Devuelve "" si no hay nada usable. Nunca levanta excepción: esto corre
    sobre datos cargados a mano y tiene que tolerar cualquier basura.
    """
    d = solo_digitos(raw)
    if not d:
        return ""

    # Prefijo de salida internacional.
    if d.startswith("00"):
        d = d[2:]

    if d.startswith(PAIS):
        nsn = d[len(PAIS) :]
    elif 11 <= len(d) <= 15 and not d.startswith("0"):
        # Ya viene en E.164 de otro país: no lo toquemos.
        return d
    else:
        nsn = d

    nsn = nsn.lstrip("0")  # prefijo troncal nacional

    # El 9 de móvil lo agregamos nosotros al final; si ya vino, lo sacamos
    # para no duplicarlo.
    if nsn.startswith("9") and len(nsn) in (11, 13):
        nsn = nsn[1:]

    # El 15 legacy va inmediatamente después del código de área (2 a 4
    # dígitos). Solo puede estar si el NSN quedó en 12 dígitos.
    if len(nsn) == 12:
        for largo_area in (2, 3, 4):
            if nsn[largo_area : largo_area + 2] == "15":
                nsn = nsn[:largo_area] + nsn[largo_area + 2 :]
                break

    if not nsn:
        return ""

    if len(nsn) == 10:
        return f"{PAIS}9{nsn}"

    # Algo no encaja con el patrón argentino (largo raro, número corto de
    # servicio, dato mal cargado). Devolvemos lo que hay, normalizado, para
    # que al menos la comparación sea consistente entre ambos lados.
    return f"{PAIS}{nsn}"


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
