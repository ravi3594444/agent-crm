"""Outbound WhatsApp via Meta Cloud API.

Free-form messages are suitable only while the recipient's 24-hour customer
service window is open.  Business-initiated staff alerts and delayed customer
confirmations use pre-approved templates instead.
"""
import os

import httpx

PHONE_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
TOKEN = os.environ["WHATSAPP_TOKEN"]
# Meta retires Graph API versions roughly two years after release. Keep the
# version configurable so an upgrade never requires a code change.
GRAPH_VERSION = os.getenv("META_GRAPH_API_VERSION", "v21.0").strip() or "v21.0"

_client = httpx.Client(
    base_url=f"https://graph.facebook.com/{GRAPH_VERSION}",
    headers={"Authorization": f"Bearer {TOKEN}"},
    timeout=15.0,
)


class WhatsAppResponseError(RuntimeError):
    """Meta returned 2xx but did not acknowledge a concrete message."""


# Meta error codes that mean "try again later" even though they arrive as
# HTTP 400: API too many calls, rate limit issues, rate limit hit, generic
# "something went wrong", service unavailable, spam rate limit, pair rate limit.
_TRANSIENT_META_CODES = frozenset({4, 80007, 130429, 131000, 131016, 131048, 131056})


class WhatsAppSendError(RuntimeError):
    """Meta did not accept a send; ``permanent`` says whether retrying can help.

    Carries only the HTTP status, Meta error code, request id and Retry-After.
    It never carries the response body, which can include the recipient number.
    """

    def __init__(
        self,
        message: str,
        *,
        permanent: bool,
        status_code: int | None = None,
        error_code: int | None = None,
        retry_after: float | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.status_code = status_code
        self.error_code = error_code
        self.retry_after = retry_after
        self.request_id = request_id


def es_permanente(status_code: int, error_code: int | None) -> bool:
    """Whether a Meta rejection can never succeed by retrying the same send.

    Timeouts, HTTP 429 and 5xx are transient. Every other 4xx is permanent
    (expired token 190, recipient not allowed 131030, closed 24-hour window
    131047, template errors 132xxx...) unless Meta's own code says otherwise.
    """
    if status_code == 429 or status_code >= 500:
        return False
    return error_code not in _TRANSIENT_META_CODES


def _codigo_error(response: httpx.Response) -> int | None:
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code
    if isinstance(code, str) and code.isdigit():
        return int(code)
    return None


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _post(payload: dict) -> dict:
    """Never fail silently. A send that did not arrive must look different
    from one that did, or you will demo a broken bot without knowing."""
    try:
        r = _client.post(f"/{PHONE_ID}/messages", json=payload)
    except httpx.HTTPError as exc:
        # Network/timeout: nothing reached Meta or the answer was lost. Retry.
        raise WhatsAppSendError(
            f"sin conexión con Meta ({type(exc).__name__})", permanent=False
        ) from exc
    if r.status_code >= 400:
        # Do not dump Meta's response body: it can contain recipient details.
        request_id = r.headers.get("x-fb-request-id", "sin-id")
        code = _codigo_error(r)
        permanent = es_permanente(r.status_code, code)
        print(
            f"[whatsapp] ERROR {r.status_code} code={code} request_id={request_id} "
            f"{'permanente' if permanent else 'transitorio'}"
        )
        raise WhatsAppSendError(
            f"Meta rechazó el envío (HTTP {r.status_code}, "
            f"código {code if code is not None else 'desconocido'})",
            permanent=permanent,
            status_code=r.status_code,
            error_code=code,
            retry_after=_retry_after(r),
            request_id=request_id,
        )
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


def verificar_credenciales() -> tuple[bool, str]:
    """Read-only Graph call proving the token and phone-number id work.

    Never raises and never returns the business phone number: the result is
    meant for a startup log line. A temporary dashboard token dies within 24
    hours and the bot then fails every send; this makes that visible at boot
    instead of at the first customer message.
    """
    try:
        response = _client.get(f"/{PHONE_ID}", params={"fields": "id,quality_rating"})
    except httpx.HTTPError as exc:
        return False, f"sin conexión con Meta ({type(exc).__name__})"
    if response.status_code >= 400:
        code = "desconocido"
        try:
            error = (response.json() or {}).get("error") or {}
            code = str(error.get("code") or code)
        except ValueError:
            pass
        return False, (
            f"Meta rechazó WHATSAPP_TOKEN/WHATSAPP_PHONE_NUMBER_ID "
            f"(HTTP {response.status_code}, código {code}). Generá un token de "
            "System User y reiniciá el agente."
        )
    try:
        data = response.json()
    except ValueError:
        return False, "Meta devolvió una respuesta no JSON al verificar credenciales"
    quality = data.get("quality_rating") if isinstance(data, dict) else None
    return True, f"credenciales de WhatsApp válidas (calidad {quality or 'desconocida'})"


def enviar_mensaje(telefono: str, texto: str) -> dict:
    return _post({
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "text",
        "text": {"body": texto[:4096]},
    })


def enviar_botones(telefono: str, texto: str, botones: list[dict]) -> dict:
    """Free-form reply buttons for a recipient with an open 24-hour window."""
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
