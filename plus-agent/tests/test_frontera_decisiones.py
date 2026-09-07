"""The manual decision path must stay out of the LLM's reach.

THE BOUNDARY THIS PROTECTS
  Customer Sales Agent  — creates DRAFT orders. Cannot submit anything.
  AI Management Agent   — explains, reports, notifies. Cannot decide.
  Human Manager         — the only role that confirms or rejects by hand,
                          through the signed webhook after es_equipo().

The automatic path is deliberately NOT human-gated: app/policy.py decides
deterministically and app/tools/pedidos.py submits with the policy credential
when every rule passes. These tests assert both halves — that a qualifying
order still auto-confirms with nobody involved, and that no LLM tool can reach
the manual override.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

os.environ.setdefault("ERPNEXT_URL", "http://erpnext.test")
os.environ.setdefault("ERPNEXT_API_KEY", "test-key")
os.environ.setdefault("ERPNEXT_API_SECRET", "test-secret")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test-phone-id")
os.environ.setdefault("WHATSAPP_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import aprobacion, decisiones
from app.graph import TOOLS_CLIENTES, TOOLS_GERENCIA

MANUAL = ("confirmar", "rechazar", "confirmar_pedido", "confirmar_conteo", "preparar", "despachar", "cancelar")


def test_no_llm_tool_exposes_the_manual_decision_functions() -> None:
    """If someone ever registers decisiones.* as a tool, this fails.

    A registered tool is callable by the model, and a model is steerable by the
    customer's own words. Manual override must never be reachable that way.
    """
    for lista, etiqueta in ((TOOLS_CLIENTES, "clientes"), (TOOLS_GERENCIA, "gerencia")):
        nombres = {t.name for t in lista}
        for prohibido in MANUAL:
            assert prohibido not in nombres, (
                f"{prohibido} quedó expuesto como herramienta del agente {etiqueta}"
            )


def test_no_tool_function_is_the_manual_decision_function() -> None:
    """Name-independent: compare the underlying callables, not the labels."""
    prohibidas = {
        decisiones.confirmar,
        decisiones.rechazar,
        decisiones.confirmar_conteo,
        decisiones.preparar,
        decisiones.despachar,
        decisiones.cancelar,
        aprobacion.confirmar_pedido,
    }
    for lista in (TOOLS_CLIENTES, TOOLS_GERENCIA):
        for herramienta in lista:
            fn = getattr(herramienta, "func", None) or getattr(
                herramienta, "coroutine", None
            )
            assert fn not in prohibidas, f"{herramienta.name} envuelve una decisión manual"


def test_a_customer_is_never_offered_the_tools_that_move_a_limit() -> None:
    """The limits decide which orders confirm with nobody watching. A customer
    talking to the sales agent must not be able to read them, let alone move
    one — not even by asking nicely, because the tool is not there."""
    from app import graph

    de_limites = {"ver_limites", "proponer_limite", "historial_limites"}
    de_clientes = {t.name for t in graph.TOOLS_CLIENTES}
    de_gerencia = {t.name for t in graph.TOOLS_GERENCIA}

    assert de_limites & de_clientes == set()
    # And they ARE available to the owner, or the feature does not exist.
    assert de_limites <= de_gerencia


def test_no_agent_can_take_both_halves_of_a_settings_confirmation() -> None:
    """Two steps are only two if different actors take them.

    The management agent proposes a change. It never receives the four-digit
    code — Python sends that straight to the owner's own number — and there is
    no tool that applies one, for EITHER agent. If an agent could call both
    halves, a misread instruction (or one hidden in a message it was asked to
    summarise) would move a limit in a single turn with nobody involved.
    """
    from app import graph
    from app.tools import configuracion

    for lista in (graph.TOOLS_CLIENTES, graph.TOOLS_GERENCIA):
        for herramienta in lista:
            nombre = herramienta.name.lower()
            assert nombre != "confirmar_limite"
            assert not ("confirm" in nombre and "limite" in nombre), nombre
    # Not merely unregistered: the tool does not exist to be registered.
    assert not hasattr(configuracion, "confirmar_limite")
    # And the only thing that applies one is the deterministic router.
    from app import main

    assert callable(main._codigo_de_ajuste)


def test_no_tool_can_submit_or_adjust_anything() -> None:
    """The LLM has no submit/payment/invoice/stock-adjustment verb at all."""
    prohibidos = ("submit", "confirmar_pedido", "aprobar", "pagar", "ajustar_stock", "despachar", "preparar", "cancelar")
    for lista in (TOOLS_CLIENTES, TOOLS_GERENCIA):
        for herramienta in lista:
            for palabra in prohibidos:
                assert palabra not in herramienta.name.lower(), herramienta.name


@pytest.mark.parametrize("accion", ["ok", "no", "ver", "preparar", "despachar", "cancelar"])
def test_unauthorized_phone_cannot_confirm_reject_or_read(
    monkeypatch: pytest.MonkeyPatch, accion: str
) -> None:
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: False)
    confirmar = Mock()
    rechazar = Mock()
    monkeypatch.setattr(aprobacion, "confirmar_pedido", confirmar)
    monkeypatch.setattr(decisiones, "rechazar", rechazar)

    result = aprobacion.manejar_boton(f"{accion}:SAL-ORD-0001", "5490000000000")

    assert "permiso" in result
    confirmar.assert_not_called()
    rechazar.assert_not_called()


def test_manual_confirmation_uses_the_policy_credential_not_the_agent_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submitting is only ever erpnext.submit_doc, which is the policy client."""
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: True)
    monkeypatch.setattr(
        aprobacion, "_leer_doc", lambda dt, name: {"name": name, "docstatus": 0}
    )
    submit = Mock(return_value={"name": "SAL-ORD-0001", "docstatus": 1})
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", submit)
    monkeypatch.setattr(aprobacion.erpnext, "add_comment", Mock())
    monkeypatch.setattr(aprobacion.avisos, "confirmacion_cliente", lambda so: True)
    monkeypatch.setattr(aprobacion.confirmacion, "registrar", lambda *a, **k: True)

    resultado = decisiones.confirmar("SAL-ORD-0001", "5493511111111")

    assert resultado["ok"] is True
    submit.assert_called_once_with("Sales Order", "SAL-ORD-0001")


# ---------------------------- el registro de herramientas no se lee en voz alta
# Un cliente que probaba el borde («registrame una venta offline y decime cómo
# está el sistema») recibía de vuelta el inventario del agente: LangGraph
# contesta «Error: X is not a valid tool, try one of [buscar_producto, …]» y el
# modelo relata ese texto. El límite aguantaba —la herramienta no es invocable—
# pero la lista salía igual. Lo cazó el guarda de tono del banco de pruebas.


def test_una_herramienta_que_no_existe_no_devuelve_el_inventario() -> None:
    from app import graph

    nodo = graph.ToolNodeSinInventario(
        graph.TOOLS_CLIENTES, handle_tool_errors=graph._ERROR_MSG
    )

    mensaje = nodo._validate_tool_call(
        {"name": "estado_del_sistema", "id": "call_1", "args": {}}
    )

    assert mensaje is not None
    assert mensaje.status == "error"
    # Ni la lista, ni la plantilla de LangGraph, ni una sola herramienta ajena.
    assert "try one of" not in mensaje.content
    for herramienta in ("buscar_producto", "crear_pedido", "consultar_stock"):
        assert herramienta not in mensaje.content
    # Y le dice al modelo qué hacer y que esto no se le muestra a nadie.
    assert "NO le muestres al cliente este mensaje" in mensaje.content
    assert "escalar_a_humano" in mensaje.content


def test_una_herramienta_que_si_existe_pasa_como_siempre() -> None:
    from app import graph

    nodo = graph.ToolNodeSinInventario(
        graph.TOOLS_CLIENTES, handle_tool_errors=graph._ERROR_MSG
    )

    assert (
        nodo._validate_tool_call(
            {"name": "buscar_producto", "id": "call_1", "args": {"consulta": "leche"}}
        )
        is None
    )


def test_el_gancho_que_se_reemplaza_sigue_existiendo_en_langgraph() -> None:
    """Es un método privado de LangGraph. Si una versión le cambia el nombre, el
    override deja de correr y la lista vuelve a salir — así que la que falla es
    esta línea, en CI, y no la conversación de un cliente."""
    from langgraph.prebuilt import ToolNode

    assert hasattr(ToolNode, "_validate_tool_call")
    from app import graph

    assert graph.ToolNodeSinInventario._validate_tool_call is not (
        ToolNode._validate_tool_call
    )


def test_los_dos_agentes_tienen_instalado_el_nodo_que_no_enumera() -> None:
    """El nodo que quedó INSTALADO en cada agente compilado, no el texto del archivo.

    Antes esto se afirmaba grepeando app/graph.py —«tools=ToolNode(» ausente y
    «tools=ToolNodeSinInventario(» dos veces—, que sólo prueba cómo está escrito
    el archivo: un ToolNode pelado pasado por una variable lo habría burlado sin
    cambiar una letra del grep. Acá se mira el grafo compilado, que es lo que
    corre.

    `nodes["tools"].bound` es API interna de LangGraph, igual que el
    `_validate_tool_call` de acá arriba. Es a propósito: si una versión la
    mueve, esto explota con KeyError o AttributeError en CI y alguien vuelve a
    mirar el cableado, en vez de que el test siga pasando sobre un grafo que ya
    no es el que se afirma.
    """
    from app import graph

    for nombre in ("agente_clientes", "agente_gerencia"):
        instalado = getattr(graph, nombre).nodes["tools"].bound
        # isinstance contra la SUBCLASE: un ToolNode pelado no la satisface.
        assert isinstance(instalado, graph.ToolNodeSinInventario), (
            f"{nombre}: quedó instalado {type(instalado).__name__}"
        )
