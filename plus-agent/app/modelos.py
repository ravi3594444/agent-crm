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
from collections.abc import Mapping

from langchain_openai import ChatOpenAI

# Endpoint OpenAI-compatible de Alibaba Model Studio. La región de Beijing es
# https://dashscope.aliyuncs.com/compatible-mode/v1
BASE_URL_DEFAULT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
MODELO_CLIENTES_DEFAULT = "qwen3.7-plus-2026-05-26"
# El nombre documentado. La instantánea fechada (qwen3.8-max-0902) se elige con
# QWEN_MANAGER_MODEL una vez que su endpoint quede verificado
# (make verificar-qwen).
MODELO_GERENCIA_DEFAULT = "qwen3.8-max"

# Variables de entorno de los modelos. Las QWEN_* mandan; las LLM_MODEL_* son
# los nombres anteriores y siguen aceptándose para no romper un .env existente.
VAR_MODELO = {
    "clientes": ("QWEN_SALES_MODEL", "LLM_MODEL_CLIENTES"),
    "gerencia": ("QWEN_MANAGER_MODEL", "LLM_MODEL_GERENCIA"),
}

ROLES = ("clientes", "gerencia")


class ConfiguracionModeloError(RuntimeError):
    """El entorno no alcanza para construir el modelo. Nunca se adivina."""


def _get(env: Mapping[str, str] | None, nombre: str) -> str:
    fuente = os.environ if env is None else env
    return str(fuente.get(nombre, "") or "").strip()


def _bool(nombre: str, default: bool, env: Mapping[str, str] | None = None) -> bool:
    crudo = _get(env, nombre).lower()
    if not crudo:
        return default
    if crudo in {"true", "1", "yes", "si", "sí", "on"}:
        return True
    if crudo in {"false", "0", "no", "off"}:
        return False
    raise ConfiguracionModeloError(f"{nombre}={crudo!r} no es sí/no")


def _float(
    nombre: str, default: float, *, minimo: float, env: Mapping[str, str] | None = None
) -> float:
    crudo = _get(env, nombre)
    if not crudo:
        return default
    try:
        valor = float(crudo)
    except ValueError as exc:
        raise ConfiguracionModeloError(f"{nombre}={crudo!r} no es un número") from exc
    if valor < minimo:
        raise ConfiguracionModeloError(f"{nombre} tiene que ser >= {minimo:g}")
    return valor


def _int(nombre: str, default: int, *, minimo: int, env: Mapping[str, str] | None = None) -> int:
    return int(_float(nombre, float(default), minimo=float(minimo), env=env))


def nombre_modelo(rol: str, env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """(variable que lo fijó, nombre del modelo) para ese rol."""
    if rol not in ROLES:
        raise ValueError(f"rol desconocido: {rol!r}")
    for variable in VAR_MODELO[rol]:
        valor = _get(env, variable)
        if valor:
            return variable, valor
    default = MODELO_CLIENTES_DEFAULT if rol == "clientes" else MODELO_GERENCIA_DEFAULT
    return VAR_MODELO[rol][0], default


def region(base_url: str) -> str:
    """Región del endpoint de DashScope por su host; 'desconocida' si no es uno conocido."""
    host = base_url.split("//", 1)[-1].split("/", 1)[0].lower()
    return {
        "dashscope-intl.aliyuncs.com": "internacional (Singapur)",
        "dashscope.aliyuncs.com": "China (Beijing)",
        "dashscope-us.aliyuncs.com": "Estados Unidos (Virginia)",
    }.get(host, "desconocida")


def configuracion(rol: str, env: Mapping[str, str] | None = None) -> dict:
    """Parámetros del ChatOpenAI para ese rol, leídos SÓLO del entorno.

    Levanta ConfiguracionModeloError con el nombre de la variable que falta o
    está mal; nunca sustituye por otro proveedor.
    """
    if rol not in ROLES:
        raise ValueError(f"rol desconocido: {rol!r}")

    clave = _get(env, "DASHSCOPE_API_KEY")
    if not clave:
        raise ConfiguracionModeloError(
            "DASHSCOPE_API_KEY vacía: los agentes usan Qwen (Alibaba Model Studio / "
            "DashScope) y no hay proveedor de respaldo. Cargala en .env."
        )
    base_url = _get(env, "DASHSCOPE_BASE_URL") or BASE_URL_DEFAULT
    if not base_url.startswith("https://"):
        raise ConfiguracionModeloError("DASHSCOPE_BASE_URL tiene que ser https://…")

    variable, modelo = nombre_modelo(rol, env)
    if rol == "clientes":
        temperatura = _float("LLM_TEMPERATURA_CLIENTES", 0.3, minimo=0.0, env=env)
        pensar = _bool("QWEN_THINKING_CLIENTES", False, env)
    else:
        temperatura = _float("LLM_TEMPERATURA_GERENCIA", 0.1, minimo=0.0, env=env)
        pensar = _bool("QWEN_THINKING_GERENCIA", False, env)

    if ":" in modelo:
        # "google_genai:gemini-…" era el formato de init_chat_model. Acá va el
        # nombre pelado del modelo Qwen; un prefijo de proveedor es un .env viejo.
        raise ConfiguracionModeloError(
            f"{variable}={modelo!r} lleva prefijo de proveedor; "
            "poné sólo el nombre del modelo Qwen (p. ej. qwen3.7-plus-2026-05-26)"
        )

    extra_body: dict = {"enable_thinking": pensar}
    if pensar:
        extra_body["thinking_budget"] = _int("QWEN_THINKING_BUDGET", 2048, minimo=1, env=env)

    return {
        "model": modelo,
        "api_key": clave,
        "base_url": base_url,
        "temperature": temperatura,
        "timeout": _float("LLM_TIMEOUT_SECONDS", 60.0, minimo=1.0, env=env),
        "max_retries": _int("LLM_MAX_RETRIES", 2, minimo=0, env=env),
        # DashScope rechaza enable_thinking=true sin streaming.
        "streaming": pensar,
        "extra_body": extra_body,
    }


def construir(rol: str) -> ChatOpenAI:
    """El modelo listo para el agente. No hace ninguna llamada de red."""
    return ChatOpenAI(**configuracion(rol))
