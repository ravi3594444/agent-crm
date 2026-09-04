"""Los dos modelos, UN proveedor elegido a mano, configurado por entorno.

QUÉ PROVEEDOR
  LLM_PROVIDER=qwen    (default) Qwen en Alibaba Model Studio (DashScope)
  LLM_PROVIDER=gemini            Gemini por el endpoint OpenAI-compatible de
                                 Google (generativelanguage.googleapis.com)

Los dos hablan el protocolo de OpenAI, así que el cliente es el mismo
(`langchain_openai.ChatOpenAI`) y lo único que cambia es la clave, el endpoint
y los nombres de modelo. UNA clave por proveedor, la misma para los dos
agentes.

POR QUÉ NO HAY UN "FALLBACK"
Si faltara la clave y el código eligiera solo otro proveedor, el negocio
estaría hablando con un modelo que nadie eligió, con otra cuota y otra
factura, sin que nadie se enterara. Acá el proveedor se elige EXPLÍCITAMENTE y
falta su clave -> el proceso no arranca, y el mensaje dice qué variable falta.
La clave de un proveedor NUNCA sirve para el otro: elegir gemini y dejar sólo
DASHSCOPE_API_KEY cargada es un error, no un arranque silencioso con Qwen.

QUÉ DECIDE EL MODELO Y QUÉ NO
Nada de stock, precio, descuento, crédito, entrega, confirmación o despacho
pasa por acá: eso lo decide app/policy.py, app/entrega.py, app/inventario.py
y app/decisiones.py, en Python. El modelo sólo conversa y llama herramientas.

RAZONAMIENTO ("thinking")
Sólo Qwen. Ventas apagado por defecto (QWEN_THINKING_CLIENTES=false): un
cliente que pregunta el precio de la manteca no necesita que el modelo piense
en voz alta. Gerencia sólo cuando hace falta (QWEN_THINKING_GERENCIA=true), con
un presupuesto acotado (QWEN_THINKING_BUDGET); DashScope exige streaming
cuando el razonamiento está encendido, así que el código lo activa junto con
él. En Gemini estas variables NO se aplican, y `make check-env` lo avisa en vez
de dejar creer que sí.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI

ROLES = ("clientes", "gerencia")

# Endpoint OpenAI-compatible de Alibaba Model Studio. La región de Beijing es
# https://dashscope.aliyuncs.com/compatible-mode/v1
BASE_URL_DEFAULT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
MODELO_CLIENTES_DEFAULT = "qwen3.7-plus-2026-05-26"
# El nombre documentado. La instantánea fechada (qwen3.8-max-0902) se elige con
# QWEN_MANAGER_MODEL una vez que su endpoint quede verificado
# (make verificar-modelos).
MODELO_GERENCIA_DEFAULT = "qwen3.8-max"

# Endpoint OpenAI-compatible de Google, tal como Google lo documenta (con la
# barra final). Un solo modelo para los dos roles alcanza para una prueba.
GEMINI_BASE_URL_DEFAULT = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_MODELO_DEFAULT = "gemini-3.5-flash"

VAR_PROVEEDOR = "LLM_PROVIDER"
PROVEEDOR_DEFAULT = "qwen"


class ConfiguracionModeloError(RuntimeError):
    """El entorno no alcanza para construir el modelo. Nunca se adivina."""


@dataclass(frozen=True)
class Proveedor:
    """Todo lo que cambia entre un proveedor y otro, en un solo lugar."""

    nombre: str
    etiqueta: str
    # Variables de clave aceptadas, en orden de precedencia. La PRIMERA es la
    # que se documenta y la que nombra el error cuando falta.
    claves: tuple[str, ...]
    var_base_url: str
    base_url_default: str
    # Por rol: variables de nombre de modelo, en orden de precedencia.
    var_modelo: Mapping[str, tuple[str, ...]]
    modelo_default: Mapping[str, str]
    # ¿Acepta los controles de razonamiento QWEN_THINKING_*?
    razona: bool
    # Nombre de la clase de cliente, resuelto en construir() por globals(): el
    # protocolo es el mismo, pero Gemini necesita que se le devuelva la firma
    # de su llamada a herramienta (ver ChatGemini).
    clase: str = "ChatOpenAI"

    @property
    def clave_principal(self) -> str:
        return self.claves[0]


PROVEEDORES: Mapping[str, Proveedor] = {
    "qwen": Proveedor(
        nombre="qwen",
        etiqueta="Qwen (Alibaba Model Studio / DashScope)",
        claves=("DASHSCOPE_API_KEY",),
        var_base_url="DASHSCOPE_BASE_URL",
        base_url_default=BASE_URL_DEFAULT,
        var_modelo={
            # Las QWEN_* mandan; las LLM_MODEL_* son los nombres anteriores y
            # siguen aceptándose para no romper un .env existente.
            "clientes": ("QWEN_SALES_MODEL", "LLM_MODEL_CLIENTES"),
            "gerencia": ("QWEN_MANAGER_MODEL", "LLM_MODEL_GERENCIA"),
        },
        modelo_default={
            "clientes": MODELO_CLIENTES_DEFAULT,
            "gerencia": MODELO_GERENCIA_DEFAULT,
        },
        razona=True,
    ),
    "gemini": Proveedor(
        nombre="gemini",
        etiqueta="Gemini (Google, endpoint OpenAI-compatible)",
        # GEMINI_API_KEY es la documentada. GOOGLE_API_KEY es el nombre estándar
        # de Google y se acepta como sinónimo para no obligar a duplicar la
        # clave en el .env. NUNCA se lee DASHSCOPE_API_KEY: son proveedores
        # distintos y una clave no sustituye a la otra.
        claves=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        var_base_url="GEMINI_BASE_URL",
        base_url_default=GEMINI_BASE_URL_DEFAULT,
        var_modelo={
            "clientes": ("GEMINI_SALES_MODEL", "LLM_MODEL_CLIENTES"),
            "gerencia": ("GEMINI_MANAGER_MODEL", "LLM_MODEL_GERENCIA"),
        },
        modelo_default={
            "clientes": GEMINI_MODELO_DEFAULT,
            "gerencia": GEMINI_MODELO_DEFAULT,
        },
        razona=False,
        clase="ChatGemini",
    ),
}

# Región por host del endpoint, para que `make check-env` diga contra qué
# servidor va a hablar el agente sin mostrar ninguna credencial.
_REGIONES = {
    "dashscope-intl.aliyuncs.com": "internacional (Singapur)",
    "dashscope.aliyuncs.com": "China (Beijing)",
    "dashscope-us.aliyuncs.com": "Estados Unidos (Virginia)",
    "generativelanguage.googleapis.com": "global (Google)",
}

# Toda variable que puede contener una credencial de modelo, de CUALQUIER
# proveedor: enmascarar() las tapa todas, no sólo la del proveedor activo, así
# una clave vieja que quedó en el .env tampoco se filtra a un log.
CLAVES_CONOCIDAS = tuple(
    dict.fromkeys(clave for prov in PROVEEDORES.values() for clave in prov.claves)
)

# Formas de clave que se reconocen por sí solas, para cuando el texto viene del
# proveedor y no del entorno (un mensaje de error que repite la credencial).
_FORMAS_DE_CLAVE = (
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),
)
OCULTO = "***"
# Debajo de esto un "secreto" es demasiado corto para reemplazarlo sin
# destrozar el texto (el contenedor de CI usa DASHSCOPE_API_KEY=noop).
_MINIMO_ENMASCARABLE = 8


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


def proveedor(env: Mapping[str, str] | None = None) -> Proveedor:
    """El proveedor elegido. Un nombre desconocido es un error, no un default.

    Vacío significa qwen, que es con lo que venía corriendo esto: un .env que
    no conoce la variable sigue arrancando igual.
    """
    elegido = _get(env, VAR_PROVEEDOR).lower() or PROVEEDOR_DEFAULT
    if elegido not in PROVEEDORES:
        conocidos = ", ".join(sorted(PROVEEDORES))
        raise ConfiguracionModeloError(
            f"{VAR_PROVEEDOR}={elegido!r} no es un proveedor conocido. Hay: {conocidos}"
        )
    return PROVEEDORES[elegido]


def clave_api(prov: Proveedor, env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """(variable que la trajo, valor) de la clave de ESE proveedor.

    ('', '') si no hay ninguna. Sólo se miran las variables de ese proveedor:
    la clave del otro no lo habilita ni lo reemplaza.
    """
    for variable in prov.claves:
        valor = _get(env, variable)
        if valor:
            return variable, valor
    return "", ""


def nombre_modelo(
    rol: str, env: Mapping[str, str] | None = None, prov: Proveedor | None = None
) -> tuple[str, str]:
    """(variable que lo fijó, nombre del modelo) para ese rol."""
    if rol not in ROLES:
        raise ValueError(f"rol desconocido: {rol!r}")
    prov = prov or proveedor(env)
    for variable in prov.var_modelo[rol]:
        valor = _get(env, variable)
        if valor:
            return variable, valor
    return prov.var_modelo[rol][0], prov.modelo_default[rol]


def region(base_url: str) -> str:
    """Región del endpoint por su host; 'desconocida' si no es uno conocido."""
    host = base_url.split("//", 1)[-1].split("/", 1)[0].lower()
    return _REGIONES.get(host, "desconocida")


def enmascarar(texto: object, env: Mapping[str, str] | None = None, *, limite: int = 400) -> str:
    """El texto sin ninguna credencial de modelo, para logs y mensajes de error.

    Tapa el VALOR de cada variable de clave conocida —de los dos proveedores— y
    además cualquier cosa con forma de clave (sk-…, AIza…), que es lo que
    aparece cuando el que repite la credencial es el proveedor en su respuesta
    de error. Se aplica siempre antes de imprimir algo que vino de la red.
    """
    salida = str(texto)
    for variable in CLAVES_CONOCIDAS:
        valor = _get(env, variable)
        if len(valor) >= _MINIMO_ENMASCARABLE:
            salida = salida.replace(valor, OCULTO)
    for forma in _FORMAS_DE_CLAVE:
        salida = forma.sub(OCULTO, salida)
    return salida[:limite]


def configuracion(rol: str, env: Mapping[str, str] | None = None) -> dict:
    """Parámetros del ChatOpenAI para ese rol, leídos SÓLO del entorno.

    Levanta ConfiguracionModeloError con el nombre de la variable que falta o
    está mal; nunca sustituye por otro proveedor.
    """
    if rol not in ROLES:
        raise ValueError(f"rol desconocido: {rol!r}")

    prov = proveedor(env)
    variable_clave, clave = clave_api(prov, env)
    if not clave:
        aceptadas = " o ".join(prov.claves)
        raise ConfiguracionModeloError(
            f"{prov.clave_principal} vacía: con {VAR_PROVEEDOR}={prov.nombre} los dos "
            f"agentes usan {prov.etiqueta} y no hay proveedor de respaldo. "
            f"Cargá {aceptadas} en .env."
        )
    del variable_clave  # sólo interesa para el reporte de readiness

    base_url = _get(env, prov.var_base_url) or prov.base_url_default
    if not base_url.startswith("https://"):
        raise ConfiguracionModeloError(f"{prov.var_base_url} tiene que ser https://…")

    variable, modelo = nombre_modelo(rol, env, prov)
    if ":" in modelo:
        # "google_genai:gemini-…" era el formato de init_chat_model. Acá va el
        # nombre pelado del modelo; un prefijo de proveedor es un .env viejo.
        raise ConfiguracionModeloError(
            f"{variable}={modelo!r} lleva prefijo de proveedor; poné sólo el nombre "
            f"del modelo (p. ej. {prov.modelo_default[rol]})"
        )

    if rol == "clientes":
        temperatura = _float("LLM_TEMPERATURA_CLIENTES", 0.3, minimo=0.0, env=env)
        pensar = prov.razona and _bool("QWEN_THINKING_CLIENTES", False, env)
    else:
        temperatura = _float("LLM_TEMPERATURA_GERENCIA", 0.1, minimo=0.0, env=env)
        pensar = prov.razona and _bool("QWEN_THINKING_GERENCIA", False, env)

    extra_body: dict = {}
    if prov.razona:
        extra_body["enable_thinking"] = pensar
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


# Dónde guarda ChatGemini las firmas dentro del AIMessage: en
# additional_kwargs, que es lo que el checkpointer de LangGraph serializa junto
# con el mensaje, así que la firma sobrevive al turno y al reinicio.
CLAVE_FIRMAS = "gemini_thought_signatures"


def firmas_de_respuesta(respuesta: object) -> dict[str, dict]:
    """{tool_call_id: extra_content} de una respuesta cruda de Gemini.

    Gemini devuelve cada llamada a herramienta con
    ``extra_content.google.thought_signature``, y el cliente OpenAI la descarta
    porque no es un campo del protocolo. Acá se rescata para poder devolverla.
    """
    if hasattr(respuesta, "model_dump"):
        try:
            respuesta = respuesta.model_dump()
        except Exception:  # pragma: no cover - defensivo
            return {}
    if not isinstance(respuesta, dict):
        return {}
    firmas: dict[str, dict] = {}
    for eleccion in respuesta.get("choices") or []:
        if not isinstance(eleccion, dict):
            continue
        mensaje = eleccion.get("message")
        if not isinstance(mensaje, dict):
            continue
        for llamada in mensaje.get("tool_calls") or []:
            if not isinstance(llamada, dict):
                continue
            extra = llamada.get("extra_content")
            identificador = llamada.get("id")
            if extra and identificador:
                firmas[str(identificador)] = extra
    return firmas


class ChatGemini(ChatOpenAI):
    """ChatOpenAI que le devuelve a Gemini la firma de su llamada a herramienta.

    EL PROBLEMA, QUE NO ES TEÓRICO
    Gemini contesta una llamada a herramienta con un campo propio,
    ``extra_content.google.thought_signature``. El cliente OpenAI lo descarta
    —no es del protocolo— y en el turno siguiente reenvía la llamada sin él.
    Gemini rechaza ESO con 400: «Function call is missing a thought_signature
    in functionCall parts». Verificado contra el endpoint real: sin la firma da
    400, con la firma los dos pasos dan 200.

    Y no es un detalle de un script de verificación: los dos agentes viven de
    llamar herramientas y leer su resultado. Sin esto, el primer cliente que
    pregunta un precio recibe un error. Apagar el razonamiento no lo evita
    (también probado): la firma se exige igual.

    Sólo el camino sin streaming, que es el único que usa este sistema con
    Gemini (configuracion() deja streaming=False porque los controles de
    razonamiento son de Qwen). Si algún día se enciende, hay que hacer lo mismo
    en el camino de chunks.
    """

    def _create_chat_result(
        self, response: object, generation_info: dict | None = None
    ) -> ChatResult:
        resultado = super()._create_chat_result(response, generation_info)
        firmas = firmas_de_respuesta(response)
        if not firmas:
            return resultado
        for generacion in resultado.generations:
            mensaje = generacion.message
            if isinstance(mensaje, AIMessage):
                mensaje.additional_kwargs[CLAVE_FIRMAS] = firmas
        return resultado

    def _get_request_payload(self, input_: object, *, stop: object = None, **kwargs) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        try:
            mensajes = self._convert_input(input_).to_messages()
        except Exception:  # pragma: no cover - la conversión ya la hizo super()
            return payload
        firmas: dict[str, dict] = {}
        for mensaje in mensajes:
            if isinstance(mensaje, AIMessage):
                guardadas = mensaje.additional_kwargs.get(CLAVE_FIRMAS)
                if isinstance(guardadas, dict):
                    firmas.update(guardadas)
        if not firmas:
            return payload
        for mensaje in payload.get("messages") or []:
            if not isinstance(mensaje, dict):
                continue
            for llamada in mensaje.get("tool_calls") or []:
                if not isinstance(llamada, dict) or llamada.get("extra_content"):
                    continue
                extra = firmas.get(str(llamada.get("id")))
                if extra:
                    llamada["extra_content"] = extra
        return payload


def construir(rol: str) -> ChatOpenAI:
    """El modelo listo para el agente. No hace ninguna llamada de red.

    La clase sale del proveedor por nombre y se resuelve acá, así el cliente de
    Gemini es el que devuelve la firma y el de Qwen sigue siendo el de siempre.
    """
    prov = proveedor()
    clase = globals().get(prov.clase, ChatOpenAI)
    return clase(**configuracion(rol))
