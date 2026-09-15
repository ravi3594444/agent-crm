"""Herramientas de OTROS servidores MCP, adentro del agente de gerencia.

PARA QUÉ EXISTE ESTE ARCHIVO
----------------------------
`app/mcp_server.py` es la puerta de SALIDA: cualquier harness se conecta y usa
nuestras herramientas. Éste es la de ENTRADA: el agente de gerencia usa las
herramientas de un servidor MCP de terceros. Hoy el caso concreto es
`@casys/mcp-erpnext`, que trae 125 herramientas de ERPNext y nueve visores
interactivos que nadie de este lado va a escribir.

LO QUE SE MIDIÓ ANTES DE ESCRIBIR ESTO (servidor real, v3.0.4, `tools/list`)
---------------------------------------------------------------------------
- 125 herramientas, 79.867 bytes de esquema (~22.200 tokens) por turno.
- 90 declaran `readOnlyHint`; 35 escriben.
- Con `--categories=sales,inventory,delivery,crm,accounting,analytics`: 62
  herramientas, 37.316 bytes (~10.400 tokens), 15 que escriben.
- Para comparar: nuestras 19 son 20.627 bytes (~5.700 tokens).

LA CATEGORÍA ES EL ÚNICO FILTRO QUE TIENE, Y NO ALCANZA PARA SACAR EL SUBMIT.
`--categories` es lo único que existe: no hay `--read-only` ni lista de
exclusión. Y el submit vive en DOS categorías —`operations`
(`erpnext_doc_submit`, `_cancel`, `_delete`) y `sales`
(`erpnext_sales_order_submit`, `_cancel`, `erpnext_sales_invoice_submit`)—, así
que sacarlas del todo se lleva puestas las 17 herramientas de venta, o sea
también la de crear el borrador. Crear borrador y emitir están en la misma
categoría por construcción.

Por eso el filtro de acá NO es por categoría: es por NOMBRE, sobre la lista que
el servidor devolvió, y corre de este lado. `MCP_EXTERNOS_BLOQUEAR` acepta
patrones y lo que matchea no se carga: no llega al modelo, no aparece en su
catálogo y no existe para él. Deja la decisión escrita en el `.env`, en una
línea que se lee, en vez de repartida entre roles de ERPNext que hay que ir a
mirar a otra pantalla.

EL DUEÑO DE LO QUE PASA SIGUE SIENDO ERPNEXT
--------------------------------------------
Un servidor MCP de terceros actúa con UNA credencial de ERPNext, la suya, la
que tiene en su propio entorno. No hay identidades ni permisos por herramienta:
lo que puede hacer lo decide esa clave. Así que la pregunta operativa no es
«¿qué herramientas cargué?» sino «¿con qué usuario de ERPNext arranqué el
servidor?», y esa decisión vive en el `.env` del OTRO contenedor, fuera del
alcance de `app/readiness.py`. `resumen()` existe para que al menos se vea
desde acá qué se cargó.

LO QUE MIDIÓ ESTE ARCHIVO Y NO SE ARREGLA CONFIGURANDO
------------------------------------------------------
`erpnext_sales_order_create` declara `items[].required = ["item_code", "qty",
"rate"]` — verificado contra el servidor, no leído en un README. O sea: **el
precio lo pone el modelo**. No hay resolución de lista de precios del lado del
servidor, y `policy._precio_autorizado` —que filtra Item Prices por
`price_list`, `currency` Y `uom`— queda afuera del camino. Eso no es un permiso
mal puesto: es la forma del esquema. Por eso este módulo se monta en el agente
de GERENCIA, donde del otro lado hay alguien del equipo, y nunca en el de
clientes, donde del otro lado hay un desconocido escribiendo.
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import threading
from typing import Any

from langchain_core.tools import StructuredTool

# Cuánto se espera una herramienta de otro servidor antes de darla por perdida.
# Es más largo que una llamada nuestra a ERPNext porque hay un proceso más en el
# medio; y es finito porque el turno de un dueño esperando no puede no tenerlo.
TIMEOUT_SEGUNDOS = 60.0

# Techo del texto que devuelve una herramienta ajena. El mismo motivo que
# `mcp_server.MAX_RESULTADO`: un `doc_list` sin filtros puede volver enorme.
MAX_RESULTADO = 60_000


class MCPExternoError(RuntimeError):
    """La configuración de un servidor externo no se puede interpretar."""


# ------------------------------------------------------------ configuración

def servidores() -> dict[str, dict]:
    """`MCP_EXTERNOS` -> la configuración que espera MultiServerMCPClient.

    Formato, una entrada por servidor separadas por coma:

        nombre=comando arg1 arg2        -> transporte stdio
        nombre=http://host:puerto/mcp   -> transporte streamable_http

    Se eligió por cómo se escribe en un `.env` de una línea. Un JSON acá sería
    más expresivo y se rompe al primer paste con comillas.
    """
    crudo = os.getenv("MCP_EXTERNOS", "").strip()
    if not crudo:
        return {}
    salida: dict[str, dict] = {}
    for posicion, entrada in enumerate(crudo.split(","), start=1):
        entrada = entrada.strip()
        if not entrada:
            continue
        nombre, _, destino = entrada.partition("=")
        nombre, destino = nombre.strip(), destino.strip()
        if not nombre or not destino:
            raise MCPExternoError(
                f"la entrada {posicion} de MCP_EXTERNOS no tiene «nombre=destino»"
            )
        if destino.startswith(("http://", "https://")):
            salida[nombre] = {"url": destino, "transport": "streamable_http"}
        else:
            partes = destino.split()
            salida[nombre] = {
                "command": partes[0],
                "args": partes[1:],
                "transport": "stdio",
                # El entorno entero: el servidor de terceros lee SUS propias
                # variables (su URL y su clave de ERPNext) y no las nuestras.
                "env": dict(os.environ),
            }
    return salida


def patrones_bloqueados() -> list[str]:
    """`MCP_EXTERNOS_BLOQUEAR`: nombres que no se cargan. Acepta comodines.

    Vacío = no se bloquea nada, y es el default a propósito: este archivo no
    decide la política del negocio. Lo que hace es que la política se pueda
    escribir en una línea y se pueda leer.
    """
    crudo = os.getenv("MCP_EXTERNOS_BLOQUEAR", "")
    return [p.strip() for p in crudo.split(",") if p.strip()]


def _bloqueada(nombre: str, patrones: list[str]) -> bool:
    return any(fnmatch.fnmatch(nombre, patron) for patron in patrones)


# ------------------------------------------------- el bucle de fondo, uno solo

_bucle: asyncio.AbstractEventLoop | None = None
_candado = threading.Lock()


def _loop() -> asyncio.AbstractEventLoop:
    """UN bucle de eventos en un hilo de fondo, para todo el proceso.

    Las herramientas de MCP son asíncronas y todo nuestro runtime es síncrono
    (`agente_gerencia.invoke`). Un `asyncio.run` por llamada abriría y cerraría
    un bucle —y con él la sesión al servidor— en cada turno; peor, `asyncio.run`
    dentro de un hilo que ya tiene un bucle corriendo levanta. Un solo bucle
    compartido resuelve las dos cosas y es lo que hace que estas herramientas
    se puedan llamar desde donde se llaman las nuestras.
    """
    global _bucle
    with _candado:
        if _bucle is None:
            _bucle = asyncio.new_event_loop()
            threading.Thread(
                target=_bucle.run_forever, name="mcp-externos", daemon=True
            ).start()
        return _bucle


def _correr(corrutina) -> Any:
    return asyncio.run_coroutine_threadsafe(corrutina, _loop()).result(
        timeout=TIMEOUT_SEGUNDOS
    )


# ------------------------------------------------------------- las herramientas

def _sincronizar(herramienta: Any) -> StructuredTool:
    """Una herramienta asíncrona de MCP, invocable desde código síncrono.

    `args_schema` de estas herramientas es un DICT (el JSON Schema que mandó el
    otro servidor), no un modelo de pydantic como en las nuestras. StructuredTool
    lo acepta así, y quien lea el esquema tiene que estar preparado para las dos
    formas — es la diferencia que rompió el primer intento de medirlas.
    """

    def _llamar(**argumentos: Any) -> str:
        salida = _correr(herramienta.ainvoke(argumentos))
        texto = salida if isinstance(salida, str) else str(salida)
        return texto[:MAX_RESULTADO]

    return StructuredTool(
        name=herramienta.name,
        description=herramienta.description,
        args_schema=herramienta.args_schema,
        func=_llamar,
    )


_cargadas: list[StructuredTool] | None = None
_motivos: list[str] = []


def cargar(propias: list[Any] | None = None) -> list[StructuredTool]:
    """Las herramientas externas, una vez por proceso.

    ``propias`` son las herramientas que el agente ya tiene: un nombre repetido
    NO se carga. Sin esto, una herramienta ajena que se llame igual que una
    nuestra la tapa en el `ToolNode` —gana la última— y el agente creería estar
    llamando a la que tiene las guardas puestas.
    """
    global _cargadas
    if _cargadas is not None:
        return _cargadas

    configuracion = servidores()
    if not configuracion:
        _cargadas = []
        return _cargadas

    from langchain_mcp_adapters.client import MultiServerMCPClient

    cliente = MultiServerMCPClient(configuracion)
    crudas = _correr(cliente.get_tools())

    ya_existen = {h.name for h in (propias or [])}
    patrones = patrones_bloqueados()
    salida: list[StructuredTool] = []
    _motivos.clear()
    for herramienta in crudas:
        if herramienta.name in ya_existen:
            _motivos.append(f"{herramienta.name}: ya existe en el agente")
            continue
        if _bloqueada(herramienta.name, patrones):
            _motivos.append(f"{herramienta.name}: bloqueada por MCP_EXTERNOS_BLOQUEAR")
            continue
        salida.append(_sincronizar(herramienta))
    _cargadas = salida
    return _cargadas


def resumen(propias: list[Any] | None = None) -> str:
    """Qué se cargó y qué no. Para `readiness` y para el log de arranque.

    Existe porque lo que un servidor externo trae NO está escrito en este repo:
    lo decide su versión y sus `--categories`. Sin una línea que lo diga, el
    catálogo del agente cambia cuando alguien actualiza el otro contenedor y
    acá no se entera nadie.
    """
    configuracion = servidores()
    if not configuracion:
        return "MCP externos: ninguno configurado."
    herramientas = cargar(propias)
    lineas = [
        f"MCP externos: {len(configuracion)} servidor(es), "
        f"{len(herramientas)} herramientas cargadas."
    ]
    for nombre, ajustes in configuracion.items():
        lineas.append(f"  · {nombre}: {ajustes.get('url') or ajustes.get('command')}")
    for motivo in _motivos:
        lineas.append(f"  · NO cargada — {motivo}")
    return "\n".join(lineas)
