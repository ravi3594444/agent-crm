"""Outbound WhatsApp via Meta Cloud API.

ANTES: se hacía el POST y no se miraba la respuesta. Un token vencido, un
número mal formado o un 24h-window cerrado se perdían en silencio — el
cliente no recibía nada y en el log no quedaba nada. El silencio es
justamente el fallo que este sistema existe para evitar.

AHORA: se chequea el status, se reintenta lo que vale la pena reintentar, y
se devuelve True/False para que el llamador pueda decidir.
"""

from __future__ import annotations

import os
import time

import httpx

from app import log, telefono

_log = log.get("whatsapp")

PHONE_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
TOKEN = os.environ["WHATSAPP_TOKEN"]
GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v21.0")

_REINTENTOS = int(os.getenv("WHATSAPP_RETRIES", "2"))
_ESPERA_BASE = float(os.getenv("WHATSAPP_RETRY_BACKOFF", "1.0"))

_client = httpx.Client(
    base_url=f"https://graph.facebook.com/{GRAPH_VERSION}",
    headers={"Authorization": f"Bearer {TOKEN}"},
    timeout=float(os.getenv("WHATSAPP_TIMEOUT", "15")),
)


def _post(payload: dict, destino: str) -> bool:
    """POST con reintentos. Devuelve True si Meta lo aceptó."""
    ultimo = ""
    for intento in range(_REINTENTOS + 1):
        try:
            r = _client.post(f"/{PHONE_ID}/messages", json=payload)
            if r.status_code < 400:
                return True
            ultimo = f"{r.status_code}: {r.text[:300]}"
            # 4xx (token vencido, número inválido, ventana de 24h cerrada) no
            # se arregla reintentando. 5xx y 429 sí.
            if r.status_code < 500 and r.status_code != 429:
                break
        except httpx.HTTPError as e:
            ultimo = str(e)
        if intento < _REINTENTOS:
            time.sleep(_ESPERA_BASE * (2**intento))
    _log.error("no pude enviar a %s: %s", destino, ultimo)
    return False


def enviar_mensaje(numero: str, texto: str) -> bool:
    destino = telefono.normalizar(numero) or numero
    if not texto or not texto.strip():
        _log.warning("mensaje vacío para %s, no lo mando", destino)
        return False
    ok = _post(
        {
            "messaging_product": "whatsapp",
            "to": destino,
            "type": "text",
            "text": {"body": texto[:4096]},
        },
        destino,
    )
    if ok:
        _log.info("enviado a %s (%d chars)", destino, len(texto))
    return ok


def enviar_botones(numero: str, texto: str, botones: list[dict]) -> bool:
    """WhatsApp interactive reply buttons. Max 3, titles max 20 chars."""
    destino = telefono.normalizar(numero) or numero
    ok = _post(
        {
            "messaging_product": "whatsapp",
            "to": destino,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": texto[:1024]},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": b["id"][:256], "title": b["title"][:20]}}
                        for b in botones[:3]
                    ]
                },
            },
        },
        destino,
    )
    if ok:
        _log.info("botones enviados a %s", destino)
    return ok
