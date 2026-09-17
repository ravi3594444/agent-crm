"""La etiqueta con la que dos líneas del log hablan del MISMO pedido.

Un `sha256` recortado a 12, y lo único que importa de él es que sea EL MISMO en
todos lados. `tools/pedidos.py` lo tenía como `_log_ref` con el docstring que
fija la regla —«Non-reversible correlation tag for logs; never log ERP/customer
IDs»— y cuando `entrega.autorizada` empezó a escribir su propia línea hizo falta
la misma etiqueta del otro lado del archivo.

Por qué una función compartida y no una copia: correlacionar es comparar dos
etiquetas, así que dos implementaciones que TIENEN que coincidir, sin nada que
las obligue, es exactamente la forma de defecto que `CLAUDE.md` describe. Con
una copia, cambiar el recorte de 12 a 16 en un archivo no rompe ningún test y
deja las dos líneas sin poder juntarse nunca más.

NO es un secreto ni un anonimizador: es irreversible sólo en el sentido de que
del log no se saca el nombre del documento. `app/notificar.py` hashea teléfonos
con la misma forma y a propósito no se tocó acá: eso identifica a una persona,
no correlaciona dos líneas, y mezclarlos invita a usar una para lo otro.
"""
from __future__ import annotations

import hashlib

LARGO = 12


def ref(valor: object) -> str:
    """La etiqueta de correlación de `valor`. Vacío da vacío, no un hash fijo.

    Un `""` que hashea siempre al mismo string haría que todos los pedidos sin
    nombre parecieran el mismo pedido en el log, que es peor que no tener
    etiqueta.
    """
    texto = str(valor or "")
    if not texto:
        return ""
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()[:LARGO]
