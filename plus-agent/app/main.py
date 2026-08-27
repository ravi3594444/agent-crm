"""WhatsApp webhook -> agent -> reply.

Handles the three things that bite people in production:
  - Meta signature verification (anyone can POST to your URL otherwise)
  - Idempotency (Meta RETRIES webhooks; without this one retry = two orders)
  - Customer identification against ERPNext before the agent sees anything
"""
import hashlib
import hmac
import os

import redis
from fastapi import FastAPI, HTTPException, Request, Response

from app import erpnext
from app.graph import responder_cliente, responder_gerencia
from app.router import es_equipo
from app.aprobacion import manejar_boton
from app.whatsapp import enviar_mensaje

APP_SECRET = os.environ["META_APP_SECRET"]
VERIFY_TOKEN = os.environ["META_VERIFY_TOKEN"]

app = FastAPI(title="Plus Agent")
r = redis.from_url(os.environ["REDIS_URL"])


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/webhook/whatsapp")
def verify(request: Request):
    """Meta calls this once when you register the webhook."""
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == VERIFY_TOKEN:
        return Response(content=p.get("hub.challenge"), media_type="text/plain")
    raise HTTPException(403, "verify token mismatch")


def _valid_signature(body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def _contexto(telefono: str) -> tuple[str, str]:
    """Who is writing? Returns (erpnext_customer_or_empty, context_for_prompt)."""
    clientes = erpnext.get_list(
        "Customer",
        filters=[["mobile_no", "=", telefono]],
        fields=["name", "customer_name", "customer_group"],
        limit=1,
    )
    if not clientes:
        return "", "Cliente no registrado todavía. Si hace un pedido, registralo primero con crear_lead."
    c = clientes[0]
    return c["name"], (
        f"Cliente registrado: {c['customer_name']} (código {c['name']}, "
        f"grupo {c.get('customer_group', 'general')}). Usá este código al crear pedidos."
    )


@app.post("/webhook/whatsapp")
async def inbound(request: Request):
    body = await request.body()
    if not _valid_signature(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(403, "bad signature")

    payload = await request.json()

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                message_id = msg.get("id", "")

                # --- one-tap approval from the owner's lock screen ---------
                if msg.get("type") == "interactive":
                    if not r.set(f"wa:seen:{message_id}", 1, nx=True, ex=86400):
                        continue
                    reply_id = (
                        msg["interactive"].get("button_reply", {}).get("id", "")
                    )
                    enviar_mensaje(
                        msg["from"], manejar_boton(reply_id, msg["from"])
                    )
                    continue

                if msg.get("type") != "text":
                    continue

                # IDEMPOTENCY. Meta retries. Without this, one retry = two orders.
                if not r.set(f"wa:seen:{message_id}", 1, nx=True, ex=86400):
                    continue

                telefono = msg["from"]
                texto = msg["text"]["body"]

                try:
                    if es_equipo(telefono):
                        # Owner/staff -> management agent. Different tools,
                        # different ERPNext credentials, different model.
                        respuesta = responder_gerencia(
                            texto, thread_id=telefono, usuario=telefono
                        )
                    else:
                        cliente_code, contexto = _contexto(telefono)
                        contexto += f"\nTeléfono del cliente: {telefono}"
                        if cliente_code:
                            contexto += f"\nCódigo de cliente para crear_pedido: {cliente_code}"
                        respuesta = responder_cliente(
                            texto, thread_id=telefono, contexto_cliente=contexto
                        )
                except Exception:
                    respuesta = (
                        "Perdón, tuve un problema técnico. "
                        "Ya avisé al equipo y te responden en un rato."
                    )
                enviar_mensaje(telefono, respuesta)

    # Always 200 fast, or Meta will hammer you with retries.
    return {"status": "ok"}
