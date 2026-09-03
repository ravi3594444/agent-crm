"""Los dos modelos, un solo proveedor (Qwen en DashScope), configurados por entorno.

POR QUÉ NO HAY UN "FALLBACK"
Si faltara la clave y el código eligiera solo otro proveedor, el negocio
estaría hablando con un modelo que nadie eligió, con otra cuota y otra
factura, sin que nadie se enterara. Acá falta la clave -> el proceso no
arranca, y el mensaje dice qué variable falta.

QUÉ DECIDE EL MODELO Y QUÉ NO
Nada de stock, precio, descuento, crédito, entrega, confirmación o despacho
pasa por acá: eso lo decide app/policy.py, app/entrega.py, app/inventario.py
y app/decisiones.py, en Python. El modelo sólo conversa y llama herramientas.

RAZONAMIENTO ("thinking")
- Ventas: apagado por defecto (QWEN_THINKING_CLIENTES=false). Un cliente que
  pregunta el precio de la manteca no necesita que el modelo piense en voz
  alta, y cada segundo de espera cuenta en WhatsApp.
- Gerencia: sólo cuando hace falta (QWEN_THINKING_GERENCIA=true para
  encenderlo), con un presupuesto de tokens acotado (QWEN_THINKING_BUDGET).
  DashScope exige streaming cuando el razonamiento está encendido, así que se
  activa junto con él.
"""
from __future__ import annotations

import os

from langchain_openai import ChatOpenAI

# Endpoint OpenAI-compatible de Alibaba Model Studio. La región de Beijing es
# https://dashscope.aliyuncs.com/compatible-mode/v1
BASE_URL_DEFAULT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
MODELO_CLIENTES_DEFAULT = "qwen3.7-plus-2026-05-26"
MODELO_GERENCIA_DEFAULT = "qwen3.8-max-0902"

ROLES = ("clientes", "gerencia")


class ConfiguracionModeloError(RuntimeError):
    """El entorno no alcanza para construir el modelo. Nunca se adivina."""


def _bool(nombre: str, default: bool) -> bool:
    crudo = os.getenv(nombre, "").strip().lower()
    if not crudo:
        return default
    if crudo in {"true", "1", "yes", "si", "sí", "on"}:
        return True
    if crudo in {"false", "0", "no", "off"}:
        return False
    raise ConfiguracionModeloError(f"{nombre}={crudo!r} no es sí/no")


def _float(nombre: str, default: float, *, minimo: float) -> float:
    crudo = os.getenv(nombre, "").strip()
    if not crudo:
        return default
    try:
        valor = float(crudo)
    except ValueError as exc:
        raise ConfiguracionModeloError(f"{nombre}={crudo!r} no es un número") from exc
    if valor < minimo:
        raise ConfiguracionModeloError(f"{nombre} tiene que ser >= {minimo:g}")
    return valor


def _int(nombre: str, default: int, *, minimo: int) -> int:
    return int(_float(nombre, float(default), minimo=float(minimo)))


def configuracion(rol: str) -> dict:
    """Parámetros del ChatOpenAI para ese rol, leídos SÓLO del entorno.

    Levanta ConfiguracionModeloError con el nombre de la variable que falta o
    está mal; nunca sustituye por otro proveedor.
    """
    if rol not in ROLES:
        raise ValueError(f"rol desconocido: {rol!r}")

    clave = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not clave:
        raise ConfiguracionModeloError(
            "DASHSCOPE_API_KEY vacía: los agentes usan Qwen (Alibaba Model Studio / "
            "DashScope) y no hay proveedor de respaldo. Cargala en .env."
        )
    base_url = os.getenv("DASHSCOPE_BASE_URL", "").strip() or BASE_URL_DEFAULT
    if not base_url.startswith("https://"):
        raise ConfiguracionModeloError("DASHSCOPE_BASE_URL tiene que ser https://…")

    if rol == "clientes":
        modelo = os.getenv("LLM_MODEL_CLIENTES", "").strip() or MODELO_CLIENTES_DEFAULT
        temperatura = _float("LLM_TEMPERATURA_CLIENTES", 0.3, minimo=0.0)
        pensar = _bool("QWEN_THINKING_CLIENTES", False)
    else:
        modelo = os.getenv("LLM_MODEL_GERENCIA", "").strip() or MODELO_GERENCIA_DEFAULT
        temperatura = _float("LLM_TEMPERATURA_GERENCIA", 0.1, minimo=0.0)
        pensar = _bool("QWEN_THINKING_GERENCIA", False)

    if ":" in modelo:
        # "google_genai:gemini-…" era el formato de init_chat_model. Acá va el
        # nombre pelado del modelo Qwen; un prefijo de proveedor es un .env viejo.
        raise ConfiguracionModeloError(
            f"LLM_MODEL_{rol.upper()}={modelo!r} lleva prefijo de proveedor; "
            "poné sólo el nombre del modelo Qwen (p. ej. qwen3.7-plus-2026-05-26)"
        )

    extra_body: dict = {"enable_thinking": pensar}
    if pensar:
        extra_body["thinking_budget"] = _int("QWEN_THINKING_BUDGET", 2048, minimo=1)

    return {
        "model": modelo,
        "api_key": clave,
        "base_url": base_url,
        "temperature": temperatura,
        "timeout": _float("LLM_TIMEOUT_SECONDS", 60.0, minimo=1.0),
        "max_retries": _int("LLM_MAX_RETRIES", 2, minimo=0),
        # DashScope rechaza enable_thinking=true sin streaming.
        "streaming": pensar,
        "extra_body": extra_body,
    }


def construir(rol: str) -> ChatOpenAI:
    """El modelo listo para el agente. No hace ninguna llamada de red."""
    return ChatOpenAI(**configuracion(rol))
