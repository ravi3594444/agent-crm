"""Las herramientas del cliente, en la forma que entiende el relay de voz.

DOS TRADUCCIONES Y NADA MÁS
  * `especificaciones()` — cada `@tool` de LangChain declarada como JSON Schema.
  * `ejecutar()` — un `tool.call` del relay convertido en una llamada real, con
    el contexto de autorización del servidor.

La lista NO se escribe acá. Sale de `TOOLS_CLIENTES`, que es la misma que monta
el agente de WhatsApp: un canal con su propia lista se desincroniza del otro en
la primera herramienta nueva y nadie se entera hasta que un cliente pide por
teléfono algo que sí puede por WhatsApp.

EL `$ref` QUE ROMPE LA LLAMADA
`crear_pedido` recibe una lista de `LineaPedido`, y pydantic la declara como
`$ref` a `$defs`. Es JSON Schema válido, pero un proveedor que no resuelva
referencias rechaza la sesión entera con un 1008 —una llamada caída, no una
herramienta menos— así que acá se inlinean. `_sin_refs` es recursiva a
propósito: una línea anidada dentro de otra estructura sigue teniendo su `$ref`.

NUNCA LEVANTA
El relay espera `(texto, es_error)`. Una excepción que suba desde acá corta la
llamada mientras el cliente está hablando, así que todo fallo se convierte en un
resultado normal, con el MISMO texto que recibe el agente de WhatsApp
(`ERROR_DE_HERRAMIENTA`): la salida correcta ante un fallo —escalar y no hablar
de sistemas— no puede depender de por dónde entró el cliente.

Y se LOGUEA entero, con traceback. Lo que el cliente oye es genérico a
propósito; lo que queda en el log es la única copia que existe de por qué falló,
porque acá la excepción muere. Un log con el nombre de la clase y nada más deja
«ERPNextError» para una caída, un 404, un permiso y un campo mal escrito.
"""
from __future__ import annotations

import copy
import sys
import traceback
from typing import Any

from app.tools.registro import (
    ERROR_DE_HERRAMIENTA,
    HERRAMIENTA_INEXISTENTE,
    TOOLS_CLIENTES,
)

_POR_NOMBRE = {herramienta.name: herramienta for herramienta in TOOLS_CLIENTES}


def _sin_refs(esquema: dict[str, Any]) -> dict[str, Any]:
    """El mismo esquema con cada `$ref` reemplazado por lo que define.

    Se trabaja sobre una copia: `model_json_schema()` devuelve una estructura
    nueva en cada llamada, pero los `$defs` se comparten entre nodos, así que
    modificar en el lugar puede escribir dos veces sobre el mismo dict.
    """
    definiciones = esquema.get("$defs") or {}

    def resolver(nodo: Any) -> Any:
        if isinstance(nodo, list):
            return [resolver(item) for item in nodo]
        if not isinstance(nodo, dict):
            return nodo
        referencia = nodo.get("$ref")
        if isinstance(referencia, str) and referencia.startswith("#/$defs/"):
            nombre = referencia.split("/")[-1]
            destino = definiciones.get(nombre)
            if destino is None:
                # Una referencia que no resuelve se deja como está: romperla a
                # medias es peor que entregarla entera y que el proveedor diga
                # qué no entendió.
                return nodo
            resuelto = resolver(copy.deepcopy(destino))
            # Lo que acompaña al $ref (un `description` propio del campo) gana:
            # es lo que el modelo lee para decidir qué mandar.
            resuelto.update({k: resolver(v) for k, v in nodo.items() if k != "$ref"})
            return resuelto
        return {clave: resolver(valor) for clave, valor in nodo.items()}

    resuelto = resolver(copy.deepcopy(esquema))
    resuelto.pop("$defs", None)
    return resuelto


def _especificacion(herramienta: Any) -> dict[str, Any]:
    esquema = _sin_refs(herramienta.tool_call_schema.model_json_schema())
    # `title` es de pydantic y no le dice nada al modelo; la descripción de la
    # herramienta ya viaja en su propio campo.
    esquema.pop("title", None)
    esquema.pop("description", None)
    return {
        "type": "function",
        "name": herramienta.name,
        "description": (herramienta.description or "").strip(),
        "parameters": esquema,
    }


def especificaciones() -> list[dict[str, Any]]:
    """`TOOLS_CLIENTES` entera, declarada para el relay. Mismo orden."""
    return [_especificacion(herramienta) for herramienta in TOOLS_CLIENTES]


def ejecutar(
    nombre: str,
    argumentos: dict[str, Any] | None,
    *,
    configurable: dict[str, Any],
) -> tuple[str, bool]:
    """Corre una herramienta del cliente. Devuelve `(texto, es_error)`.

    `configurable` es el contexto autenticado por el SERVIDOR
    (app/runtime_context.py): el teléfono verificado, el código de cuenta y el
    alcance. No sale de los argumentos ni de nada que haya dicho el que llama —
    ninguna herramienta de este registro acepta un teléfono como parámetro, y
    ese es el motivo por el que `crear_pedido` no puede cargarle un pedido a
    otro aunque el cliente lo pida en voz alta.
    """
    herramienta = _POR_NOMBRE.get(nombre)
    if herramienta is None:
        return HERRAMIENTA_INEXISTENTE, True
    try:
        resultado = herramienta.invoke(
            dict(argumentos or {}), config={"configurable": dict(configurable)}
        )
    except Exception as exc:
        # El mensaje Y el traceback: acá la excepción se muere, así que esto es
        # lo único que va a quedar de ella. Los ARGUMENTOS no se loguean —son
        # las palabras del cliente— y el hilo se identifica por el id de
        # llamada, que no es un teléfono.
        print(
            f"[voz] herramienta {nombre} falló en "
            f"{configurable.get('thread_id', '?')}: {type(exc).__name__}: {exc}"
        )
        traceback.print_exc(file=sys.stdout)
        return ERROR_DE_HERRAMIENTA, True
    return str(resultado), False
