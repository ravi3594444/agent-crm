"""El endpoint del modelo, en dos modos, hablando el protocolo de OpenAI.

  MODO GUION   (offline, determinístico)
      No hay modelo. Un guión dice, para cada mensaje del cliente, qué
      herramientas hay que llamar y en qué orden, y con qué texto se cierra.
      La misma corrida da el mismo resultado siempre, así que un escenario que
      falla, falla por el sistema y no por lo que se le ocurrió a un modelo.

  MODO RELEVO  (Gemini de verdad)
      Reenvía la request TAL CUAL a Google y devuelve la respuesta TAL CUAL,
      sin tocar un byte. Eso importa: la firma de razonamiento de Gemini viaja
      en extra_content.google.thought_signature, y app/modelos.py::ChatGemini
      existe para devolvérsela. Un relevo que "normalice" el JSON escondería
      justo el bug que ese código arregla.

EL ESTADO NO VIVE ACÁ. El guión decide qué paso toca contando los turnos que
ya vinieron en la propia request (el protocolo de OpenAI es sin estado: el
cliente manda toda la conversación cada vez). Así el server no tiene que
seguirle el hilo a nadie y dos escenarios en paralelo no se pisan.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

# ------------------------------------------------------------------ el guión


@dataclass(frozen=True)
class Llamada:
    """Un paso que llama a una herramienta."""

    herramienta: str
    argumentos: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Texto:
    """Un paso que cierra el turno hablándole a la persona."""

    cuerpo: str


Paso = Llamada | Texto
# (¿matchea este mensaje?, pasos). El primer match gana.
Regla = tuple[Callable[[str, str], bool], Sequence[Paso]]


def contiene(*fragmentos: str) -> Callable[[str, str], bool]:
    """Matchea si el mensaje trae TODOS los fragmentos (sin acentos ni caso)."""
    buscados = tuple(_normalizar(f) for f in fragmentos)
    def matchea(mensaje: str, _rol: str) -> bool:
        plano = _normalizar(mensaje)
        return all(f in plano for f in buscados)
    return matchea


def exacto(*mensajes: str) -> Callable[[str, str], bool]:
    """Matchea el mensaje COMPLETO. Para los que son una palabra sola.

    `contiene("ok")` matchearía cualquier mensaje con "ok" adentro, así que un
    "ok" pelado —el caso que hay que probar— no se puede escribir con eso.
    """
    buscados = tuple(_normalizar(m) for m in mensajes)
    def matchea(mensaje: str, _rol: str) -> bool:
        return _normalizar(mensaje) in buscados
    return matchea


def _normalizar(texto: str) -> str:
    import unicodedata
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", str(texto).lower())
        if unicodedata.category(c) != "Mn"
    )
    return " ".join(sin_tildes.split())


# En el guión no se puede escribir el número de un pedido que todavía no
# existe. Este marcador se reemplaza por el último que apareció en un resultado
# de herramienta de ESTA conversación, que es de donde lo sacaría un modelo.
ULTIMO_PEDIDO = "<ULTIMO_PEDIDO>"
# Lo que devolvió la última herramienta, tal cual. Sirve para que la respuesta
# del guión MUESTRE el resultado en vez de taparlo con un texto lindo: si la
# herramienta falló, la transcripción lo tiene que decir.
ULTIMO_RESULTADO = "<ULTIMO_RESULTADO>"
# La forma de una serie de nombres de Frappe (SAL-ORD-2026-00023), no la del
# patrón laxo que usa app/main.py: acá hay que distinguir el número de pedido
# de un código de producto como YOG-FRUT-190, que también tiene letras, guiones
# y dígitos y aparece en el mismo resultado de herramienta.
_RE_PEDIDO = re.compile(r"\b[A-Z]{2,6}(?:-[A-Z]{2,6})?-\d{4}-\d{3,}\b")


def _resolver(argumentos: dict, mensajes: list[dict]) -> dict:
    """Cambia los marcadores por lo que ya se vio en la conversación."""
    crudo = json.dumps(argumentos, ensure_ascii=False)
    if ULTIMO_PEDIDO not in crudo and ULTIMO_RESULTADO not in crudo:
        return argumentos
    pedido, resultado = "", ""
    for m in mensajes:
        if str(m.get("role")) != "tool":
            continue
        contenido = _texto_de_contenido(m.get("content"))
        resultado = contenido
        encontrados = _RE_PEDIDO.findall(contenido)
        if encontrados:
            pedido = encontrados[-1]
    if not pedido:
        # El dueño escribiéndole al agente de gerencia nombra el pedido ÉL: su
        # hilo empieza de cero y no tiene ningún resultado de herramienta de
        # donde sacarlo. Un modelo lo lee de lo que le escribieron, así que el
        # guión también. El resultado de herramienta sigue teniendo prioridad:
        # los escenarios de clientes no cambian en nada.
        pedido = ((_RE_PEDIDO.findall(_ultimo_humano(mensajes)[0]) or [""])[-1])
    crudo = crudo.replace(ULTIMO_PEDIDO, pedido)
    crudo = crudo.replace(
        ULTIMO_RESULTADO, json.dumps(resultado, ensure_ascii=False)[1:-1])
    return json.loads(crudo)


NO_ENTENDI = Texto(
    "No tengo un guion para este mensaje. (Banco de pruebas en modo offline: "
    "agregá una regla en demo/escenarios.py.)"
)


# --------------------------------------------------------------- el protocolo


def _ultimo_humano(mensajes: list[dict]) -> tuple[str, int]:
    """(texto del último mensaje del usuario, su posición)."""
    for i in range(len(mensajes) - 1, -1, -1):
        if str(mensajes[i].get("role")) == "user":
            return _texto_de_contenido(mensajes[i].get("content")), i
    return "", -1


def _texto_de_contenido(contenido: Any) -> str:
    """El contenido puede ser un string o una lista de partes."""
    if isinstance(contenido, str):
        return contenido
    if isinstance(contenido, list):
        return " ".join(
            str(p.get("text") or "") for p in contenido if isinstance(p, dict)
        )
    return ""


def _pasos_ya_dados(mensajes: list[dict], desde: int) -> int:
    """Cuántas veces el asistente ya llamó herramientas después del humano."""
    return sum(
        1 for m in mensajes[desde + 1 :]
        if str(m.get("role")) == "assistant" and m.get("tool_calls")
    )


def _rol_del_sistema(mensajes: list[dict]) -> str:
    """'gerencia' o 'clientes', deducido del prompt de sistema.

    Los dos agentes comparten el endpoint, así que el guión tiene que poder
    distinguirlos: el mismo texto ("aprobar 3") significa cosas distintas.
    """
    for m in mensajes:
        if str(m.get("role")) not in {"system", "developer"}:
            continue
        plano = _normalizar(_texto_de_contenido(m.get("content")))
        if "gerencia" in plano or "dueno" in plano or "duena" in plano:
            return "gerencia"
        return "clientes"
    return "clientes"


def responder(reglas: Sequence[Regla], payload: dict) -> dict:
    """La respuesta que le toca, con la forma de una de OpenAI."""
    mensajes = [m for m in (payload.get("messages") or []) if isinstance(m, dict)]
    texto, donde = _ultimo_humano(mensajes)
    rol = _rol_del_sistema(mensajes)
    dados = _pasos_ya_dados(mensajes, donde)

    pasos: Sequence[Paso] = ()
    for matchea, candidatos in reglas:
        if matchea(texto, rol):
            pasos = candidatos
            break

    paso: Paso = pasos[dados] if dados < len(pasos) else (
        NO_ENTENDI if not pasos else Texto("Listo.")
    )
    if isinstance(paso, Llamada):
        paso = Llamada(paso.herramienta, _resolver(paso.argumentos, mensajes))
    elif ULTIMO_PEDIDO in paso.cuerpo or ULTIMO_RESULTADO in paso.cuerpo:
        # Un modelo cierra el turno nombrando el pedido que acaba de crear,
        # porque lo leyó del resultado de la herramienta. El guión hace lo
        # mismo con el marcador, así la transcripción se parece a la real.
        paso = Texto(_resolver({"t": paso.cuerpo}, mensajes)["t"])
    modelo = str(payload.get("model") or "demo")
    return _sobre(modelo, paso)


def _sobre(modelo: str, paso: Paso) -> dict:
    if isinstance(paso, Llamada):
        mensaje = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_demo_{abs(hash((paso.herramienta, json.dumps(paso.argumentos, sort_keys=True)))) % 10**10}",
                    "type": "function",
                    "function": {
                        "name": paso.herramienta,
                        "arguments": json.dumps(paso.argumentos, ensure_ascii=False),
                    },
                }
            ],
        }
        razon = "tool_calls"
    else:
        mensaje = {"role": "assistant", "content": paso.cuerpo}
        razon = "stop"
    return {
        "id": "chatcmpl-demo",
        "object": "chat.completion",
        "created": 1756944000,
        "model": modelo,
        "choices": [{"index": 0, "message": mensaje, "finish_reason": razon}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ------------------------------------------------------------------- relevo


class Relevo:
    """Reenvía a Google sin tocar el cuerpo. Un solo cliente, reutilizado."""

    def __init__(self, destino: str, *, clave: str = "", timeout: float = 120.0,
                 reintentos: int = 4, espera_maxima: float = 40.0) -> None:
        self.destino = destino.rstrip("/")
        self.reintentos = reintentos
        # Menos que el LLM_TIMEOUT_SECONDS del agente (60): si el relevo
        # esperara más, el agente cortaría la llamada creyendo que se cayó.
        self.espera_maxima = espera_maxima
        # La clave REAL vive sólo acá. El contenedor del agente lleva una de
        # mentira y no tiene salida a internet, así que la única credencial que
        # existe en la corrida está en este proceso y en ningún otro.
        self.clave = clave.strip()
        self.cliente = httpx.Client(timeout=timeout)
        self.candado = threading.Lock()
        self.llamadas = 0

    def __call__(
        self, ruta: str, cuerpo: bytes, cabeceras: dict[str, str]
    ) -> tuple[int, bytes, str]:
        with self.candado:
            self.llamadas += 1
        # Sólo lo que hace falta: nada de Host ni Content-Length del origen.
        pasan = {
            k: v for k, v in cabeceras.items()
            if k.lower() in {"content-type", "accept"}
        }
        if self.clave:
            pasan["Authorization"] = f"Bearer {self.clave}"
        cola = ruta.split("/v1/", 1)[-1] if "/v1/" in ruta else ruta.lstrip("/")
        url = f"{self.destino}/{cola}"
        gastado = 0.0
        for intento in range(1, self.reintentos + 2):
            inicio = time.monotonic()
            try:
                r = self.cliente.post(url, content=cuerpo, headers=pasan)
            except httpx.HTTPError as exc:
                tardo = time.monotonic() - inicio
                print(f"[relevo] {type(exc).__name__} tras {tardo:.1f}s")
                return 502, json.dumps(
                    {"error": {"message": "el relevo no pudo hablar con el "
                                          f"proveedor ({type(exc).__name__})"}}
                ).encode(), "application/json"
            if r.status_code != 429 or intento > self.reintentos:
                return (r.status_code, r.content,
                        r.headers.get("content-type", "application/json"))
            if "PerDay" in r.text:
                # Una cuota DIARIA no se recupera esperando. Reintentar sería
                # tardar dos minutos para dar el mismo error, y taparía la
                # causa: el tier gratuito de Gemini da 20 pedidos por día y
                # por modelo, y se agotó.
                print("[relevo] 429 por cuota DIARIA agotada: no reintento",
                      flush=True)
                return (r.status_code, r.content,
                        r.headers.get("content-type", "application/json"))
            # La cuota del tier gratuito de Gemini se mide por minuto y el
            # propio error dice cuánto falta. Esperar eso acá —y no en el
            # agente— mantiene la corrida dentro de la cuota sin tocar
            # LLM_MAX_RETRIES ni el timeout de la app.
            espera = self._espera(r)
            if gastado + espera > self.espera_maxima:
                print(f"[relevo] 429 y ya esperé {gastado:.0f}s: lo devuelvo")
                return (r.status_code, r.content,
                        r.headers.get("content-type", "application/json"))
            print(f"[relevo] 429 del proveedor: espero {espera:.1f}s "
                  f"(intento {intento}/{self.reintentos})", flush=True)
            time.sleep(espera)
            gastado += espera
        raise AssertionError("inalcanzable")

    @staticmethod
    def _espera(respuesta: httpx.Response) -> float:
        """Cuánto pidió esperar el proveedor. Sin dato, 5 s."""
        crudo = respuesta.headers.get("retry-after")
        if crudo:
            try:
                return min(30.0, max(1.0, float(crudo)))
            except ValueError:
                pass
        m = re.search(r"retry in ([0-9.]+)s", respuesta.text)
        if m:
            try:
                return min(30.0, max(1.0, float(m.group(1)) + 0.5))
            except ValueError:
                pass
        return 5.0
