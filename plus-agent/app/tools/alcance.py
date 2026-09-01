"""De quién es este mensaje: el límite de autorización de las herramientas.

EL AGUJERO QUE ESTO CIERRA
Antes, `crear_pedido(cliente=...)`, `pedido_habitual(cliente=...)` y
`estado_pedido(numero=...)` recibían el identificador COMO PARÁMETRO DEL
MODELO. El código de cliente viajaba en el texto del system prompt, así que
lo único que impedía que un cliente escribiera "¿qué pide siempre Almacén
Don José?" o "estado del pedido SO-0042" era la regla 6 del prompt. Un
prompt no es un control de acceso.

El README decía que el límite lo impone ERPNext. Para el SUBMIT es verdad
(el rol no tiene permiso). Para "en nombre de qué cliente actúo" no lo era:
las dos agentes usan las mismas credenciales de lectura.

CÓMO SE CIERRA
El código de cliente lo resuelve el webhook a partir del número de teléfono
(app/clientes.py) y viaja por `config.configurable`, que el modelo no puede
ver ni escribir. Las herramientas lo leen de ahí. El parámetro desapareció
de la firma que ve el modelo — verificable con `crear_pedido.args`.

FALLA CERRADO: si no hay `alcance` en el config, asumimos "cliente", que es
el más restrictivo.
"""

from __future__ import annotations

from app import log

_log = log.get("alcance")

CLIENTE = "cliente"
GERENCIA = "gerencia"


def _conf(config: dict | None) -> dict:
    return (config or {}).get("configurable") or {}


def alcance(config: dict | None) -> str:
    """ "cliente" (restringido) o "gerencia" (lectura amplia)."""
    valor = _conf(config).get("alcance")
    return GERENCIA if valor == GERENCIA else CLIENTE


def es_gerencia(config: dict | None) -> bool:
    return alcance(config) == GERENCIA


def cliente_code(config: dict | None) -> str:
    """El Customer de ERPNext atado al teléfono que escribió. "" si no está
    registrado."""
    return _conf(config).get("cliente_code") or ""


def telefono(config: dict | None) -> str:
    return _conf(config).get("telefono") or ""


def thread_id(config: dict | None) -> str:
    return _conf(config).get("thread_id") or ""


def puede_ver_pedido(config: dict | None, sales_order: dict) -> bool:
    """¿Puede quien escribió ver este pedido?

    Gerencia ve todo. Un cliente ve solo los suyos. Un desconocido (sin
    ficha en ERPNext) no ve ninguno.
    """
    if es_gerencia(config):
        return True
    propio = cliente_code(config)
    if not propio:
        return False
    if sales_order.get("customer") != propio:
        _log.warning(
            "cliente %s intentó ver el pedido %s (de %s) — bloqueado",
            propio,
            sales_order.get("name"),
            sales_order.get("customer"),
        )
        return False
    return True
