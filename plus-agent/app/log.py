"""Logging. Reemplaza los `print` sueltos que había por todo el código.

POR QUÉ IMPORTA ACÁ
Casi todos los fallos de este sistema son silenciosos por diseño: el
`add_comment` se traga las excepciones para no tirar un pedido, el
`_saldo_vencido` devuelve infinito si no puede verificar, el envío de
WhatsApp puede fallar sin que nadie se entere. Eso está bien —siempre y
cuando quede escrito en algún lado. Sin logs, "la auto-confirmación no
anda" es indebuggeable.

Salida a stdout, que es lo que Docker recoge.
"""

from __future__ import annotations

import logging
import os
import sys

_NIVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_configurado = False


def _configurar() -> None:
    global _configurado
    if _configurado:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger("plus")
    root.handlers = [handler]
    root.setLevel(_NIVEL)
    root.propagate = False
    _configurado = True


def get(nombre: str) -> logging.Logger:
    _configurar()
    return logging.getLogger(f"plus.{nombre}")
