"""¿Se puede entregar en esta dirección sin que lo mire una persona?

POR QUÉ NO LO DECIDE EL MODELO
Si el LLM pudiera opinar sobre si una dirección "está cerca", un cliente lo
convencería con un mensaje: "es acá al lado, mandámelo igual". Acá no hay
criterio, hay comparaciones contra las zonas que configuró el negocio. El
modelo puede PEDIR la dirección y repetir lo que se decidió; no puede decidir.

CÓMO SE DECIDE, EN ESTE ORDEN
  1. El código postal de la dirección está en ZONAS_ENTREGA_CP  -> se entrega.
  2. Sin código postal, la localidad está en ZONAS_ENTREGA_LOCALIDADES
     (comparada sin tildes ni mayúsculas) -> se entrega.
  3. Ya se entregó antes en esa misma dirección —hay un pedido CONFIRMADO del
     mismo cliente a esa dirección— -> se entrega. Es la evidencia más fuerte
     que existe: una persona ya la aprobó y el reparto llegó.
  4. Cualquier otra cosa —sin dirección, sin CP y localidad desconocida, fuera
     de zona, o ERPNext que no contesta— NO habilita nada: el pedido queda en
     BORRADOR y lo revisa una persona.

El punto 3 es lo que hace que esto no moleste a los clientes de siempre: su
dirección ya está probada, aunque no tenga el CP cargado. Un cliente conocido
que estrena dirección pasa por los puntos 1 y 2 como cualquiera.
"""
from __future__ import annotations

import os
import re
import unicodedata

from app import erpnext

# Cuántos pedidos confirmados del cliente se miran para ver si ya se entregó
# en esa dirección. Con el más reciente alcanza, pero se piden varios porque
# puede tener direcciones distintas.
MAX_PEDIDOS_ENTREGADOS = 50

# Todos los motivos de esta capa arrancan igual, para que el resto del sistema
# sepa que ESTE pedido está esperando por la entrega y no por otra regla: el
# aviso al equipo lo dice y al cliente se le habla distinto.
MOTIVO = "entrega a revisar"


def _sin_tildes(texto: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", texto.lower())
        if unicodedata.category(char) != "Mn"
    )


def normalizar_localidad(texto: object) -> str:
    """Minúsculas, sin tildes y sin puntuación: "Villa Allende" == "villa allende"."""
    limpio = _sin_tildes(str(texto or ""))
    return re.sub(r"[^a-z0-9]+", " ", limpio).strip()


def normalizar_cp(texto: object) -> str:
    """Sólo los caracteres alfanuméricos, en mayúsculas: "X5000" == "x 5000"."""
    return re.sub(r"[^A-Z0-9]+", "", str(texto or "").upper())


def _lista(variable: str) -> list[str]:
    return [parte.strip() for parte in os.getenv(variable, "").split(",") if parte.strip()]


def zonas_configuradas() -> tuple[frozenset[str], frozenset[str]]:
    """(códigos postales, localidades) donde el negocio reparte.

    Se leen en cada llamada: cambiar una zona no necesita reiniciar nada.
    """
    codigos = frozenset(normalizar_cp(cp) for cp in _lista("ZONAS_ENTREGA_CP"))
    localidades = frozenset(
        normalizar_localidad(loc) for loc in _lista("ZONAS_ENTREGA_LOCALIDADES")
    )
    return (
        frozenset(cp for cp in codigos if cp),
        frozenset(loc for loc in localidades if loc),
    )


def texto_direccion(direccion: dict) -> str:
    """La dirección como la leería una persona, para el aviso al equipo."""
    partes = [
        str(direccion.get("address_line1") or "").strip(),
        str(direccion.get("address_line2") or "").strip(),
        str(direccion.get("city") or "").strip(),
    ]
    escrito = ", ".join(parte for parte in partes if parte)
    cp = str(direccion.get("pincode") or "").strip()
    if cp:
        escrito = f"{escrito} (CP {cp})" if escrito else f"CP {cp}"
    return escrito or "sin datos de dirección"


def en_zona(direccion: dict) -> tuple[bool, str]:
    """(en_zona, motivo). Determinista y sin red: sólo compara texto."""
    codigos, localidades = zonas_configuradas()
    if not codigos and not localidades:
        return False, "no hay zonas de reparto configuradas"

    cp = normalizar_cp(direccion.get("pincode"))
    localidad = normalizar_localidad(direccion.get("city"))

    if cp:
        if cp in codigos:
            return True, ""
        # Con CP cargado, el CP manda: una localidad que "suena parecida" no
        # puede habilitar un reparto a 200 km.
        return False, f"el código postal {cp} no está en las zonas de reparto"
    if not localidad:
        return False, "la dirección no tiene código postal ni localidad"
    if localidad in localidades:
        return True, ""
    return False, (
        f"sin código postal, y la localidad «{direccion.get('city')}» no está "
        "en las zonas de reparto"
    )


def nombre_direccion(sales_order: dict) -> str:
    """La dirección de ENTREGA del pedido, que es la que importa."""
    for campo in ("shipping_address_name", "customer_address"):
        valor = str(sales_order.get(campo) or "").strip()
        if valor:
            return valor
    return ""


def _ya_se_entrego(cliente: str, direccion: str) -> bool:
    """Si hay un pedido CONFIRMADO de este cliente a esa misma dirección.

    Una persona ya aprobó ese reparto y el camión llegó: es mejor evidencia
    que cualquier zona configurada. Ante un error de lectura devuelve False
    (no habilita), nunca True.
    """
    if not cliente or not direccion:
        return False
    try:
        previos = erpnext.policy_get_list(
            "Sales Order",
            filters=[["customer", "=", cliente], ["docstatus", "=", 1]],
            fields=["name", "customer", "shipping_address_name", "customer_address"],
            limit=MAX_PEDIDOS_ENTREGADOS,
        )
    except erpnext.ERPNextError as exc:
        print(f"[entrega] no pude revisar entregas previas de {cliente}: {exc}")
        return False
    for previo in previos:
        if str(previo.get("customer") or "").strip() != cliente:
            continue
        if direccion in {
            str(previo.get("shipping_address_name") or "").strip(),
            str(previo.get("customer_address") or "").strip(),
        }:
            return True
    return False


def autorizada(sales_order: dict) -> tuple[bool, str]:
    """(se puede entregar sin revisión humana, motivo para el equipo).

    Nunca levanta: cualquier duda vuelve como False, que es lo que la política
    necesita para dejar el pedido en borrador en vez de prometer una entrega.
    """
    nombre = nombre_direccion(sales_order)
    if not nombre:
        return False, f"{MOTIVO}: el pedido no tiene dirección cargada"
    try:
        direccion = erpnext.policy_get_doc("Address", nombre)
    except erpnext.ERPNextError as exc:
        print(f"[entrega] no pude leer la dirección {nombre}: {exc}")
        return False, f"{MOTIVO}: no pude leer la dirección {nombre}"
    if not isinstance(direccion, dict):
        return False, f"{MOTIVO}: no pude leer la dirección {nombre}"

    dentro, motivo = en_zona(direccion)
    if dentro:
        return True, ""

    cliente = str(sales_order.get("customer") or "").strip()
    if _ya_se_entrego(cliente, nombre):
        return True, ""

    return False, f"{MOTIVO}: {texto_direccion(direccion)} — {motivo}"
