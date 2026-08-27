"""Outbound WhatsApp via Meta Cloud API."""
import os

import httpx

PHONE_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
TOKEN = os.environ["WHATSAPP_TOKEN"]

_client = httpx.Client(
    base_url="https://graph.facebook.com/v21.0",
    headers={"Authorization": f"Bearer {TOKEN}"},
    timeout=15.0,
)


def enviar_mensaje(telefono: str, texto: str) -> None:
    _client.post(
        f"/{PHONE_ID}/messages",
        json={
            "messaging_product": "whatsapp",
            "to": telefono,
            "type": "text",
            "text": {"body": texto[:4096]},
        },
    )


def enviar_botones(telefono: str, texto: str, botones: list[dict]) -> None:
    """WhatsApp interactive reply buttons. Max 3, titles max 20 chars."""
    _client.post(
        f"/{PHONE_ID}/messages",
        json={
            "messaging_product": "whatsapp",
            "to": telefono,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": texto[:1024]},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                        for b in botones[:3]
                    ]
                },
            },
        },
    )
