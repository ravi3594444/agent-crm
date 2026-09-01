"""Formato de números como se leen en Argentina.

POR QUÉ ESTO ES UN ARCHIVO
Todo el código usaba `f"${total:,.0f}"`, que en Python da `$12,000`. Un
argentino lee eso como doce pesos con decimales: acá el separador de miles
es el punto y el decimal es la coma. El dueño mirando "Total: $12,000" en la
pantalla de bloqueo, decidiendo si confirma, no puede tener esa duda.

    12000    -> $12.000
    1500.5   -> $1.500,50
"""

from __future__ import annotations


def pesos(monto: float | int | None, decimales: int = 0) -> str:
    """Monto con separador de miles argentino, listo para mostrar."""
    try:
        valor = float(monto or 0)
    except (TypeError, ValueError):
        valor = 0.0
    entero = f"{abs(valor):,.{decimales}f}"
    # Python usa , para miles y . para decimales: los damos vuelta.
    entero = entero.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    signo = "-" if valor < 0 else ""
    return f"{signo}${entero}"


def cantidad(valor: float | int | None) -> str:
    """Cantidad sin ceros al final: 10 en lugar de 10.0, 2,5 en lugar de 2.5."""
    try:
        num = float(valor or 0)
    except (TypeError, ValueError):
        return "0"
    texto = f"{num:g}"
    return texto.replace(".", ",")
