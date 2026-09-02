"""Identificar al cliente que escribió, contra datos cargados a mano.

POR QUÉ NO ES UN `=`
El teléfono en ERPNext lo cargó una persona, en cualquier formato:
`+5493511234567`, `+54 9 351 123-4567`, `0351 15 123-4567`, `351 1234567`.
El que manda Meta es E.164 sin `+`: `5493511234567`. Un filtro
`["mobile_no", "=", telefono]` solo matchea cuando el dato está limpio
(ver app/telefono.py).

LA ESTRATEGIA
1. Buscar en ERPNext con `like` por los últimos 8 dígitos del número
   canónico. Como el dato guardado puede tener espacios, guiones o
   paréntesis entre esos dígitos, el patrón intercala `%` entre cada uno:
   `%5%1%2%3%4%5%6%7%`. Eso sobrevive a cualquier separador.
2. Confirmar en Python normalizando ambos lados. El `like` trae falsos
   positivos (otro número que termina igual, o que contiene esos dígitos
   en orden); la comparación canónica los descarta.

Nunca devolvemos un cliente que no matchea exacto en forma canónica. Un
falso positivo acá significa cargarle un pedido a otra persona.
"""

from __future__ import annotations

import os

from app import erpnext, telefono
from app.entrega import normalizar_cp, normalizar_localidad
from app.locks import distributed_lock

CAMPOS = ["name", "customer_name", "customer_group", "mobile_no"]

# Cota generosa: el patrón intercalado es permisivo y el que buscamos no
# tiene por qué venir primero. La confirmación canónica hace el filtrado fino.
LIMITE_CANDIDATOS = 50


def patron_like(clave: str) -> str:
    """`%d%d%...%` para que separadores entre dígitos no rompan el `like`."""
    return "%" + "%".join(clave) + "%"


def buscar_por_telefono(numero: str, get_list=None) -> dict | None:
    """Devuelve el Customer que corresponde a ese teléfono, o None."""
    clave = telefono.clave_busqueda(numero)
    if not clave:
        return None

    candidatos = (get_list or erpnext.get_list)(
        "Customer",
        filters=[["mobile_no", "like", patron_like(clave)]],
        fields=CAMPOS,
        limit=LIMITE_CANDIDATOS,
    )

    exactos = [
        c for c in candidatos if telefono.son_el_mismo(c.get("mobile_no"), numero)
    ]
    if not exactos:
        if candidatos:
            print(
                f"[clientes] {len(candidatos)} candidato(s) por sufijo pero "
                "ninguno matchea en forma canónica"
            )
        return None
    if len(exactos) > 1:
        # Dos fichas con el mismo teléfono: dato sucio. Elegimos la primera
        # de forma determinística y lo dejamos escrito para que lo limpien.
        exactos.sort(key=lambda c: str(c["name"]))
        print(
            f"[clientes] teléfono en {len(exactos)} clientes: "
            f"{[c['name'] for c in exactos]}. Usando {exactos[0]['name']}."
        )
    return exactos[0]


# ---------------------------------------------------------------------------
# Alta del cliente nuevo, en la misma conversación
#
# EL TELÉFONO NO ES UN PARÁMETRO QUE ALGUIEN ELIJA. Llega del webhook firmado
# de Meta y se normaliza acá. Ninguna herramienta lo acepta como argumento:
# si el modelo pudiera pasarlo, un mensaje bastaría para dar de alta —o para
# pedir por— el número de otra persona.
#
# Y NADA SE DUPLICA. Meta reintenta los webhooks y la gente manda dos mensajes
# seguidos, así que el alta corre bajo el mismo lock distribuido que protege la
# creación de pedidos, y vuelve a buscar adentro del lock antes de crear.
# ---------------------------------------------------------------------------

# La ficha de un cliente y su dirección: dos documentos, un solo intento.
LOCK_ALTA_SEGUNDOS = 60
ESPERA_ALTA_SEGUNDOS = 20
MAX_DIRECCIONES = 20


def _campo(direccion: dict, nombre: str) -> str:
    return str(direccion.get(nombre) or "").strip()


def misma_direccion(guardada: dict, pedida: dict) -> bool:
    """Si son la misma dirección, comparando como la leería una persona."""
    return (
        normalizar_localidad(guardada.get("address_line1"))
        == normalizar_localidad(pedida.get("address_line1"))
        and normalizar_localidad(guardada.get("city"))
        == normalizar_localidad(pedida.get("city"))
        and normalizar_cp(guardada.get("pincode")) == normalizar_cp(pedida.get("pincode"))
    )


def direcciones_de(cliente: str) -> list[str]:
    """Las Address vinculadas a ese Customer, por su Dynamic Link."""
    if not cliente:
        return []
    filas = erpnext.get_list(
        "Dynamic Link",
        filters=[
            ["link_doctype", "=", "Customer"],
            ["link_name", "=", cliente],
            ["parenttype", "=", "Address"],
        ],
        fields=["parent", "link_name", "parenttype"],
        limit=MAX_DIRECCIONES,
        parent="Address",
    )
    nombres: list[str] = []
    for fila in filas:
        if str(fila.get("link_name") or cliente).strip() != cliente:
            continue
        nombre = str(fila.get("parent") or "").strip()
        if nombre and nombre not in nombres:
            nombres.append(nombre)
    return nombres


def direccion_principal(cliente: str) -> str:
    """Una dirección del cliente, elegida de forma determinística.

    El pedido tiene que decir a dónde va: si ERPNext lo dedujera solo, dos
    pedidos del mismo cliente podrían salir con direcciones distintas según
    qué documento tocó primero. Sin dirección, devuelve "" y la política deja
    el pedido en borrador.
    """
    nombres = sorted(direcciones_de(cliente))
    return nombres[0] if nombres else ""


def asegurar_direccion(cliente: str, direccion: dict) -> str:
    """La Address del cliente con esos datos, creándola sólo si no está.

    Un reintento con la misma dirección devuelve la que ya existe en vez de
    dejar tres copias iguales colgadas del mismo cliente.
    """
    linea = _campo(direccion, "address_line1")
    if not linea:
        raise erpnext.ERPNextError("la dirección no tiene calle y número")
    for nombre in direcciones_de(cliente):
        try:
            guardada = erpnext.get_doc("Address", nombre)
        except erpnext.ERPNextError:
            continue
        if misma_direccion(guardada, direccion):
            return nombre

    payload = {
        "address_title": cliente,
        "address_type": "Shipping",
        "address_line1": linea,
        "city": _campo(direccion, "city") or "Sin especificar",
        "country": os.getenv("ERPNEXT_COUNTRY", "Argentina").strip() or "Argentina",
        "links": [{"link_doctype": "Customer", "link_name": cliente}],
    }
    for campo in ("address_line2", "pincode", "state"):
        valor = _campo(direccion, campo)
        if valor:
            payload[campo] = valor
    doc = erpnext.create_doc("Address", payload)
    nombre = str(doc.get("name") or "").strip()
    if not nombre:
        raise erpnext.ERPNextError("ERPNext no devolvió la dirección creada")
    return nombre


def crear(nombre_negocio: str, numero: str, direccion: dict) -> dict:
    """Da de alta al cliente que escribió, con su dirección de entrega.

    Devuelve {"cliente", "direccion", "creado"}. `creado` es False cuando ya
    existía: el alta es idempotente por teléfono, así que un reintento o dos
    mensajes simultáneos del mismo número no dejan dos fichas.
    """
    canonico = telefono.normalizar(numero)
    if not canonico:
        raise erpnext.ERPNextError("el teléfono del remitente no es válido")
    nombre = " ".join(str(nombre_negocio or "").split())
    if not nombre:
        raise erpnext.ERPNextError("falta el nombre del cliente")

    with distributed_lock(
        f"alta-cliente:{canonico}",
        lease_seconds=LOCK_ALTA_SEGUNDOS,
        wait_seconds=ESPERA_ALTA_SEGUNDOS,
    ):
        existente = buscar_por_telefono(canonico)
        if existente:
            return {
                "cliente": existente["name"],
                "direccion": asegurar_direccion(existente["name"], direccion),
                "creado": False,
            }

        payload = {"customer_name": nombre, "mobile_no": canonico}
        for variable, campo in (
            ("ERPNEXT_CUSTOMER_GROUP", "customer_group"),
            ("ERPNEXT_TERRITORY", "territory"),
        ):
            valor = os.getenv(variable, "").strip()
            if valor:
                payload[campo] = valor
        try:
            doc = erpnext.create_doc("Customer", payload)
        except erpnext.ERPNextError:
            # Puede haber fallado por nombre duplicado, o porque otro worker
            # lo creó recién. Resolver por teléfono antes de decir que falló.
            reintento = buscar_por_telefono(canonico)
            if not reintento:
                raise
            return {
                "cliente": reintento["name"],
                "direccion": asegurar_direccion(reintento["name"], direccion),
                "creado": False,
            }

        cliente = str(doc.get("name") or "").strip()
        if not cliente:
            raise erpnext.ERPNextError("ERPNext no devolvió la ficha creada")
        erpnext.add_comment(
            "Customer", cliente, "Alta por WhatsApp: el número lo verificó el webhook."
        )
        return {
            "cliente": cliente,
            "direccion": asegurar_direccion(cliente, direccion),
            "creado": True,
        }
