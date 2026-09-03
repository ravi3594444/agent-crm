"""Una llamada real, mínima y con herramientas, a cada modelo Qwen. MANUAL.

    make verificar-qwen           (o: .venv/bin/python deploy/verificar_qwen.py)

Qué prueba: que la clave y el endpoint funcionan, que cada modelo existe en esa
región y que sabe LLAMAR UNA HERRAMIENTA (function calling), que es lo único que
los agentes le piden. No toca ERPNext, no manda WhatsApp, no usa datos de
clientes: la única herramienta es un `ping` y el texto es sintético.

Qué NO hace: correr en CI (se niega si ve la variable CI), imprimir la clave
(nunca sale del objeto de configuración; los errores se sanitizan) ni cambiar
nada en ningún sistema.

Salida: una línea por modelo con OK/FALLA, latencia y tokens usados si el
proveedor los informa. Código de salida 1 si alguno falla.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: F401  (carga .env antes de leer os.environ)
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from app import modelos

_CLAVE = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")


@tool
def ping(texto: str) -> str:
    """Devuelve 'pong:' seguido del texto recibido. Sólo para probar herramientas."""
    return f"pong:{texto}"


def _sanitizar(texto: object) -> str:
    """Nunca dejar pasar la clave ni nada que se le parezca."""
    salida = str(texto)
    clave = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if clave:
        salida = salida.replace(clave, "sk-***")
    return _CLAVE.sub("sk-***", salida)[:400]


def _host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


def probar(rol: str) -> bool:
    try:
        cfg = modelos.configuracion(rol)
    except modelos.ConfiguracionModeloError as exc:
        print(f"FALLA {rol}: configuración — {_sanitizar(exc)}")
        return False
    pensar = cfg["extra_body"].get("enable_thinking", False)
    print(
        f"...   {rol}: modelo={cfg['model']} endpoint={_host(cfg['base_url'])} "
        f"región={modelos.region(cfg['base_url'])} thinking={'sí' if pensar else 'no'} "
        f"timeout={cfg['timeout']:g}s"
    )
    modelo = modelos.construir(rol).bind_tools([ping])
    mensajes = [
        SystemMessage(content="Sos un verificador. Cuando te lo pidan, usá la herramienta ping."),
        HumanMessage(content="Llamá a la herramienta ping con el texto 'ok' y no hagas nada más."),
    ]
    inicio = time.monotonic()
    try:
        primera = modelo.invoke(mensajes)
        llamadas = list(getattr(primera, "tool_calls", None) or [])
        if not llamadas:
            print(
                f"FALLA {rol}: el modelo respondió sin llamar la herramienta "
                f"({time.monotonic() - inicio:.1f}s). Los agentes dependen de function calling."
            )
            return False
        llamada = llamadas[0]
        resultado = ping.invoke(llamada.get("args") or {"texto": "ok"})
        final = modelo.invoke(
            [*mensajes, primera, ToolMessage(content=str(resultado), tool_call_id=llamada.get("id") or "ping")]
        )
    except Exception as exc:
        print(f"FALLA {rol}: {type(exc).__name__} — {_sanitizar(exc)}")
        return False
    latencia = time.monotonic() - inicio
    uso = getattr(final, "usage_metadata", None) or {}
    tokens = ""
    if isinstance(uso, dict) and uso.get("total_tokens"):
        tokens = f" tokens={uso.get('input_tokens', '?')}+{uso.get('output_tokens', '?')}"
    print(
        f"OK    {rol}: herramienta '{llamada.get('name')}' llamada y respondida en {latencia:.1f}s"
        f"{tokens}"
    )
    return True


def main() -> int:
    if os.getenv("CI", "").strip():
        print("Este chequeo hace llamadas reales al proveedor y no corre en CI.")
        return 2
    if not os.getenv("DASHSCOPE_API_KEY", "").strip():
        print("FALLA: DASHSCOPE_API_KEY vacía en .env; no hay nada que verificar.")
        return 1
    resultados = [probar("clientes"), probar("gerencia")]
    return 0 if all(resultados) else 1


if __name__ == "__main__":
    sys.exit(main())
