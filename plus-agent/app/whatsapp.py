"""Outbound WhatsApp via Meta Cloud API.

Free-form messages are suitable only while the recipient's 24-hour customer
service window is open.  Business-initiated staff alerts and delayed customer
confirmations use pre-approved templates instead.
"""
import os

import httpx

from app.formato import whatsapp_texto

PHONE_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
TOKEN = os.environ["WHATSAPP_TOKEN"]

_client = httpx.Client(
    base_url="https://graph.facebook.com/v21.0",
    headers={"Authorization": f"Bearer {TOKEN}"},
    timeout=15.0,
)


class WhatsAppResponseError(RuntimeError):
    """Meta returned 2xx but did not acknowledge a concrete message."""


def _post(payload: dict) -> dict:
    """Never fail silently. A send that did not arrive must look different
    from one that did, or you will demo a broken bot without knowing."""
    r = _client.post(f"/{PHONE_ID}/messages", json=payload)
    if r.status_code >= 400:
        # Do not dump Meta's response body: it can contain recipient details.
        request_id = r.headers.get("x-fb-request-id", "sin-id")
        print(f"[whatsapp] ERROR {r.status_code} request_id={request_id}")
        r.raise_for_status()
    try:
        data = r.json()
    except ValueError as exc:
        raise WhatsAppResponseError(
            "Meta devolvió una respuesta no JSON al enviar el mensaje"
        ) from exc
    messages = data.get("messages") if isinstance(data, dict) else None
    message_id = messages[0].get("id") if isinstance(messages, list) and messages else None
    if not isinstance(message_id, str) or not message_id.strip():
        raise WhatsAppResponseError(
            "Meta no devolvió un identificador para el mensaje"
        )
    return data


def enviar_mensaje(telefono: str, texto: str) -> dict:
    # El modelo escribe Markdown; WhatsApp no. Traducir aquí cubre todas
    # las salidas de texto libre sin tocar graph.py ni main.py.
    texto = whatsapp_texto(texto)
    return _post({
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "text",
        "text": {"body": texto[:4096]},
    })


def enviar_botones(telefono: str, texto: str, botones: list[dict]) -> dict:
    """Free-form reply buttons for a recipient with an open 24-hour window."""
    texto = whatsapp_texto(texto)
    return _post({
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
    })


def enviar_plantilla(
    telefono: str,
    nombre: str,
    idioma: str,
    parametros: list[str],
    acciones: list[str] | None = None,
) -> dict:
    """Send an approved positional-parameter template.

    ``acciones`` maps, in order, to quick-reply buttons already defined on the
    approved template.  Meta returns each value as the button payload, which
    lets the signed webhook identify the order without putting it in the
    visible button title.
    """
    if not nombre.strip():
        raise ValueError("falta el nombre de la plantilla de WhatsApp")

    components: list[dict] = []
    if parametros:
        components.append(
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": str(valor)[:1024]}
                    for valor in parametros
                ],
            }
        )
    for index, payload in enumerate((acciones or [])[:3]):
        components.append(
            {
                "type": "button",
                "sub_type": "quick_reply",
                "index": str(index),
                "parameters": [{"type": "payload", "payload": payload[:256]}],
            }
        )

    template: dict = {
        "name": nombre,
        "language": {"code": idioma},
    }
    if components:
        template["components"] = components
    return _post(
        {
            "messaging_product": "whatsapp",
            "to": telefono,
            "type": "template",
            "template": template,
        }
    )
