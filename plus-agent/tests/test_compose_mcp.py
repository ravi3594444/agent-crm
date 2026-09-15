"""El overlay de mcp-erpnext: que el agente pueda ALCANZARLO.

POR QUÉ ESTO EXISTE. La integración entera con Casys depende de una línea de
red que no rompe ningún test: `docker-compose.yml` no declara `networks`, así
que `agente` queda en la red por defecto del proyecto, y el overlay ponía a
`mcp-erpnext` sólo en `frappe_docker_default`. Con eso
`http://mcp-erpnext:3012/mcp` no resuelve y NADA de las 125 herramientas carga
—en silencio, porque `mcp_cliente` captura el fallo y sigue sin ellas—.

La prueba en vivo de esta sesión no lo agarró y no podía: corrió los dos
procesos en localhost, no en la topología que documenta el compose. Esto es lo
que cubre esa diferencia.
"""
from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

_RAIZ = pathlib.Path(__file__).resolve().parents[1]
RED_DE_ERPNEXT = "frappe_docker_default"


def _cargar(nombre: str) -> dict:
    return yaml.safe_load((_RAIZ / nombre).read_text())


def test_el_agente_y_el_mcp_comparten_una_red():
    """Sin red compartida, el nombre `mcp-erpnext` no resuelve desde el agente.

    MUTACIÓN: sacarle `frappe_docker_default` a `agente` en el overlay. Cae ésta
    y sólo ésta.
    """
    overlay = _cargar("deploy/mcp-erpnext.compose.yml")
    del_agente = set(overlay["services"]["agente"]["networks"])
    del_mcp = set(overlay["services"]["mcp-erpnext"]["networks"])

    assert del_agente & del_mcp, (
        "el agente y mcp-erpnext no comparten ninguna red: el agente no puede "
        "resolver el nombre del servicio"
    )
    assert RED_DE_ERPNEXT in del_mcp, (
        "mcp-erpnext tiene que estar en la red de ERPNext para llegar a frontend:8080"
    )


def test_el_agente_no_pierde_la_red_del_proyecto():
    """Nombrar redes REEMPLAZA la lista, no la amplía.

    Y en la del proyecto vive `redis`, que el agente necesita en cada turno. Un
    overlay que sólo pusiera `frappe_docker_default` lo dejaría hablando con
    ERPNext y sin checkpointer.

    MUTACIÓN: sacarle `default` a la lista de `agente`. Cae ésta y sólo ésta.
    """
    overlay = _cargar("deploy/mcp-erpnext.compose.yml")
    assert "default" in overlay["services"]["agente"]["networks"], (
        "el agente perdería la red del proyecto, y con ella a redis"
    )
    # Y que redis siga estando ahí: si algún día se mueve, esta prueba miente.
    base = _cargar("docker-compose.yml")
    assert "redis" in base["services"]
    assert "networks" not in base["services"]["redis"], (
        "redis dejó la red por defecto: revisá qué red comparte ahora con el agente"
    )


def test_la_red_de_erpnext_es_externa_y_se_llama_como_la_de_verdad():
    """`frappe_docker_default`, no `erpnext_default`.

    Es una de las trampas de CLAUDE.md: `docker compose config` le horneó el
    nombre del directorio de origen, así que la red real lleva el nombre del
    directorio `frappe_docker`. Declararla `external` es lo que impide que
    compose cree una nueva vacía con ese nombre y todo "ande" sin conectar nada.

    MUTACIÓN: sacarle `external: true`. Cae ésta y sólo ésta.
    """
    overlay = _cargar("deploy/mcp-erpnext.compose.yml")
    declarada = overlay["networks"][RED_DE_ERPNEXT]
    assert declarada.get("external") is True, (
        "sin `external: true` compose crea una red nueva y vacía con ese nombre"
    )


def test_el_mcp_no_publica_puertos_al_host():
    """Lo alcanza el agente por la red de docker y nadie más.

    Ese contenedor tiene UNA credencial de ERPNext adentro y no tiene permisos
    por herramienta: publicarlo al host lo deja a un firewall mal puesto de
    distancia de ser internet.

    MUTACIÓN: agregarle `ports:`. Cae ésta y sólo ésta.
    """
    overlay = _cargar("deploy/mcp-erpnext.compose.yml")
    assert "ports" not in overlay["services"]["mcp-erpnext"], (
        "mcp-erpnext no puede publicar puertos al host"
    )
