"""WhatsApp webhook -> agent -> reply.

Handles the things that bite people in production:
  - Meta signature verification (anyone can POST to your URL otherwise)
  - Idempotency (Meta RETRIES webhooks; without this one retry = two orders)
  - Customer identification against ERPNext before the agent sees anything
  - Un 200 inmediato: el trabajo pesado va a un thread aparte

POR QUÉ EL TRABAJO NO VA EN EL REQUEST
`inbound` es `async def`, pero invocar al agente es sincrónico y tarda entre
10 y 60 segundos (LLM + varias llamadas REST). Hacerlo dentro del handler
bloquea el event loop entero: ningún otro webhook, ni /health, se atiende
mientras tanto, y Meta —que espera un 200 rápido— empieza a reintentar y
después deshabilita el webhook. Ahora el handler valida, encola y devuelve.
Starlette corre las funciones sincrónicas de BackgroundTasks en su
threadpool, así que el event loop queda libre.

IDEMPOTENCIA EN DOS TIEMPOS
Antes la marca se ponía con TTL de 24h ANTES de procesar. Si el proceso se
caía en medio (deploy, OOM), el reintento de Meta veía la marca y el mensaje
del cliente se perdía para siempre. Ahora se toma una marca corta mientras
se procesa y se extiende a 24h al terminar bien: un duplicado real se
descarta, pero un mensaje que murió a mitad de camino se puede reintentar.
"""

from __future__ import annotations

import hashlib
import hmac
import os

import redis
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app import clientes, erpnext, log, notificar
from app.aprobacion import manejar_boton
from app.graph import responder_cliente, responder_gerencia
from app.router import es_equipo
from app.whatsapp import enviar_mensaje

_log = log.get("main")

APP_SECRET = os.environ["META_APP_SECRET"]
VERIFY_TOKEN = os.environ["META_VERIFY_TOKEN"]

# Cuánto dura la marca "estoy procesando esto". Tiene que ser mayor que el
# peor caso de procesamiento y menor que la ventana de reintentos de Meta.
TTL_PROCESANDO = int(os.getenv("IDEMPOTENCIA_TTL_PROCESANDO", "600"))
TTL_LISTO = int(os.getenv("IDEMPOTENCIA_TTL_LISTO", "86400"))

DISCULPA = "Perdón, tuve un problema técnico. Ya avisé al equipo y te responden en un rato."

SIN_TEXTO = {
    "audio": (
        "Perdón, todavía no puedo escuchar audios. ¿Me lo escribís? "
        "Si preferís, te llama alguien del equipo."
    ),
    "image": ("Recibí tu imagen pero todavía no puedo verla. ¿Me contás por texto qué necesitás?"),
    "video": "Recibí tu video pero no puedo verlo. ¿Me lo escribís?",
    "document": (
        "Recibí tu archivo pero todavía no puedo abrirlo. ¿Me contás por texto qué necesitás?"
    ),
    "sticker": "🙂 ¿En qué te puedo ayudar?",
    "location": "Gracias por la ubicación. ¿Qué necesitás que te lleve?",
}
SIN_TEXTO_DEFECTO = (
    "Perdón, solo puedo leer mensajes de texto por ahora. ¿Me escribís qué necesitás?"
)

app = FastAPI(title="Plus Agent")
r = redis.from_url(os.environ["REDIS_URL"])


@app.get("/health")
def health():
    """Liveness. No toca Redis ni ERPNext a propósito: si esto falla, el
    proceso está muerto."""
    return {"ok": True}


@app.get("/ready")
def ready():
    """Readiness: ¿están las dependencias? Esto es lo que hay que mirar
    cuando "el bot no contesta"."""
    estado = {"redis": False, "erpnext": False}
    try:
        r.ping()
        estado["redis"] = True
    except Exception as e:
        _log.error("Redis no responde: %s", e)
    try:
        erpnext.get_list("Company", fields=["name"], limit=1)
        estado["erpnext"] = True
    except Exception as e:
        _log.error("ERPNext no responde: %s", e)
    estado["ok"] = all(v for k, v in estado.items() if k != "ok")
    return JSONResponse(estado, status_code=200 if estado["ok"] else 503)


@app.get("/webhook/whatsapp")
def verify(request: Request):
    """Meta calls this once when you register the webhook."""
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and hmac.compare_digest(
        p.get("hub.verify_token") or "", VERIFY_TOKEN
    ):
        return Response(content=p.get("hub.challenge"), media_type="text/plain")
    _log.warning("verify token mismatch")
    raise HTTPException(403, "verify token mismatch")


def _valid_signature(body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def _reclamar(message_id: str) -> bool:
    """Toma la marca de procesamiento. False = ya lo está haciendo otro (o ya
    se hizo).

    Si Redis está caído no procesamos: sin idempotencia, los reintentos de
    Meta duplican pedidos, y un pedido duplicado es peor que uno demorado.
    """
    if not message_id:
        return False
    try:
        return bool(r.set(f"wa:seen:{message_id}", "procesando", nx=True, ex=TTL_PROCESANDO))
    except Exception as e:
        _log.error("Redis no disponible, descarto el mensaje %s: %s", message_id, e)
        return False


def _marcar_listo(message_id: str) -> None:
    try:
        r.set(f"wa:seen:{message_id}", "listo", ex=TTL_LISTO)
    except Exception as e:
        _log.warning("no pude extender la marca de %s: %s", message_id, e)


def _liberar(message_id: str) -> None:
    """Suelta la marca para que el reintento de Meta pueda volver a intentar."""
    try:
        r.delete(f"wa:seen:{message_id}")
    except Exception as e:
        _log.warning("no pude liberar la marca de %s: %s", message_id, e)


def procesar_texto(message_id: str, telefono: str, texto: str) -> None:
    """Corre en el threadpool, fuera del request. Nunca levanta."""
    try:
        if es_equipo(telefono):
            # Owner/staff -> management agent. Different tools,
            # different scope, different model.
            respuesta = responder_gerencia(texto, thread_id=telefono, usuario=telefono)
        else:
            cliente = clientes.buscar_por_telefono(telefono)
            respuesta = responder_cliente(
                texto,
                thread_id=telefono,
                contexto_cliente=clientes.contexto_para_prompt(cliente, telefono),
                cliente_code=(cliente or {}).get("name", ""),
                telefono=telefono,
            )
        if not respuesta:
            _log.error("respuesta vacía para %s, mando la disculpa", telefono)
            respuesta = DISCULPA
        enviar_mensaje(telefono, respuesta)
        _marcar_listo(message_id)
    except Exception as e:
        _log.exception("falló el procesamiento de %s", telefono)
        enviar_mensaje(telefono, DISCULPA)
        # El mensaje de arriba dice "ya avisé al equipo". Que sea verdad.
        notificar.avisar_falla_tecnica(telefono, texto, str(e))
        # No marcamos listo: si Meta reintenta después del TTL corto, se
        # vuelve a intentar en lugar de perderse.
        _liberar(message_id)


def procesar_boton(message_id: str, telefono: str, reply_id: str) -> None:
    """Corre en el threadpool. Antes esto estaba fuera de cualquier
    try/except: un fallo acá era un 500 al webhook, y como la marca de
    idempotencia ya estaba puesta, el reintento se descartaba y el toque del
    dueño no hacía nada, sin ninguna señal."""
    try:
        enviar_mensaje(telefono, manejar_boton(reply_id, telefono))
        _marcar_listo(message_id)
    except Exception as e:
        _log.exception("falló el botón %s de %s", reply_id, telefono)
        enviar_mensaje(
            telefono,
            "No pude procesar esa acción. Probá de nuevo o entrá al sistema.",
        )
        notificar.avisar_falla_tecnica(telefono, f"botón {reply_id}", str(e))
        _liberar(message_id)


@app.post("/webhook/whatsapp")
async def inbound(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not _valid_signature(body, request.headers.get("X-Hub-Signature-256")):
        _log.warning("firma inválida desde %s", request.client.host if request.client else "?")
        raise HTTPException(403, "bad signature")

    try:
        payload = await request.json()
    except Exception:
        _log.warning("body no es JSON válido")
        return {"status": "ok"}

    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            valor = change.get("value") or {}
            for msg in valor.get("messages", []) or []:
                _encolar(msg, background)

    # Always 200 fast, or Meta will hammer you with retries.
    return {"status": "ok"}


def _encolar(msg: dict, background: BackgroundTasks) -> None:
    message_id = msg.get("id", "")
    telefono = msg.get("from", "")
    tipo = msg.get("type")

    if not telefono:
        _log.warning("mensaje sin remitente, lo ignoro")
        return

    # --- one-tap approval from the owner's lock screen ---------------------
    if tipo == "interactive":
        if not _reclamar(message_id):
            return
        reply_id = (msg.get("interactive") or {}).get("button_reply", {}).get("id", "")
        background.add_task(procesar_boton, message_id, telefono, reply_id)
        return

    if tipo == "text":
        if not _reclamar(message_id):
            return
        texto = (msg.get("text") or {}).get("body", "")
        if not texto.strip():
            _marcar_listo(message_id)
            return
        background.add_task(procesar_texto, message_id, telefono, texto)
        return

    # --- cualquier otra cosa: NUNCA silencio -------------------------------
    # Los audios son constantes en Argentina. Antes esto era un `continue` y
    # el cliente no recibía nada: quedaba esperando una respuesta que no iba
    # a llegar nunca.
    if not _reclamar(message_id):
        return
    _log.info("mensaje tipo %s de %s: respondo el fallback", tipo, telefono)
    background.add_task(
        _responder_sin_texto, message_id, telefono, SIN_TEXTO.get(tipo or "", SIN_TEXTO_DEFECTO)
    )


def _responder_sin_texto(message_id: str, telefono: str, respuesta: str) -> None:
    try:
        enviar_mensaje(telefono, respuesta)
        _marcar_listo(message_id)
    except Exception:
        _log.exception("no pude responder el fallback a %s", telefono)
        _liberar(message_id)
