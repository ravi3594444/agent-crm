"""Identificar al cliente que escribió, contra datos cargados a mano.

POR QUÉ NO ES UN `=`
El teléfono en ERPNext lo cargó una persona, en cualquier formato. El que
manda Meta es E.164 sin `+`. Un filtro `["mobile_no", "=", telefono]` no
matchea nunca (ver app/telefono.py).

LA ESTRATEGIA
1. Buscar en ERPNext por los últimos 8 dígitos con `like` — eso sobrevive a
   todos los formatos posibles.
2. Confirmar en Python normalizando ambos lados. El `like` puede traer
   falsos positivos (un número que termina igual); la comparación canónica
   los descarta.

Nunca devolvemos un cliente que no matchea exacto en forma canónica. Un
falso positivo acá significa cargarle un pedido a otra persona.
"""

from __future__ import annotations

from app import erpnext, log, telefono

_log = log.get("clientes")

CAMPOS = ["name", "customer_name", "customer_group", "mobile_no"]


def buscar_por_telefono(numero: str) -> dict | None:
    """Devuelve el Customer que corresponde a ese teléfono, o None."""
    clave = telefono.clave_busqueda(numero)
    if not clave:
        return None

    try:
        candidatos = erpnext.get_list(
            "Customer",
            filters=[["mobile_no", "like", f"%{clave}%"]],
            fields=CAMPOS,
            limit=10,
        )
    except erpnext.ERPNextError:
        _log.warning("no pude consultar Customer para %s", clave)
        raise

    exactos = [c for c in candidatos if telefono.son_el_mismo(c.get("mobile_no"), numero)]
    if not exactos:
        if candidatos:
            _log.info(
                "%d candidato(s) por sufijo %s pero ninguno matchea en forma canónica",
                len(candidatos),
                clave,
            )
        return None
    if len(exactos) > 1:
        # Dos fichas con el mismo teléfono: dato sucio. Elegimos la primera
        # de forma determinística y lo dejamos escrito para que lo limpien.
        exactos.sort(key=lambda c: c["name"])
        _log.warning(
            "teléfono %s está en %d clientes: %s. Usando %s.",
            numero,
            len(exactos),
            [c["name"] for c in exactos],
            exactos[0]["name"],
        )
    return exactos[0]


def contexto_para_prompt(cliente: dict | None, numero: str) -> str:
    """El bloque de contexto que va al system prompt del agente de clientes.

    OJO: esto es texto para el modelo, no un control de seguridad. El código
    de cliente que las herramientas usan de verdad viaja por `config`
    (ver app/tools/pedidos.py), no por acá.
    """
    if not cliente:
        return (
            "Cliente no registrado todavía. Si hace un pedido, registralo primero con crear_lead."
        )
    return (
        f"Cliente registrado: {cliente['customer_name']} "
        f"(grupo {cliente.get('customer_group') or 'general'}). "
        f"Ya está identificado por su teléfono: las herramientas de pedido "
        f"saben a qué cliente corresponde, no hace falta que se lo pidas ni "
        f"que lo pases como parámetro."
    )
