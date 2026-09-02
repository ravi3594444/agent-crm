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

from app import erpnext, telefono

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
