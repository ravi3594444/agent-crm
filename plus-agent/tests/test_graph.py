"""El armado del agente: prompt dinámico y extracción de texto.

No se instancia ningún modelo ni Redis acá: los agentes se construyen lazy
(ver app/graph.py), justamente para que esto se pueda testear.
"""

from __future__ import annotations

from app.graph import (
    TOOLS_CLIENTES,
    TOOLS_GERENCIA,
    _prompt_clientes,
    _prompt_gerencia,
    texto_de,
)


class MensajeFalso:
    def __init__(self, content):
        self.content = content


# --------------------------------------------------------------------------
# Extracción de texto
# --------------------------------------------------------------------------


def test_texto_de_un_string():
    assert texto_de(MensajeFalso("Hola, te lo cargo.")) == "Hola, te lo cargo."


def test_texto_de_bloques_de_contenido():
    """EL BUG: `.content` de Anthropic puede ser una lista de bloques.
    `texto[:4096]` sobre una lista devolvía una lista, y Meta rechazaba el
    envío con un 400 que nadie miraba: el cliente no recibía nada."""
    mensaje = MensajeFalso(
        [
            {"type": "text", "text": "Tenemos queso cremoso."},
            {"type": "text", "text": "¿Cuántos kilos querés?"},
        ]
    )
    salida = texto_de(mensaje)
    assert isinstance(salida, str)
    assert "queso cremoso" in salida
    assert "kilos" in salida


def test_los_bloques_de_pensamiento_no_se_le_mandan_al_cliente():
    """Un bloque `thinking` es razonamiento interno. Mandárselo al cliente
    sería una filtración."""
    mensaje = MensajeFalso(
        [
            {"type": "thinking", "thinking": "El cliente parece dudoso, y el margen acá es bajo"},
            {"type": "text", "text": "Sí, tenemos."},
        ]
    )
    salida = texto_de(mensaje)
    assert salida == "Sí, tenemos."
    assert "margen" not in salida


def test_texto_de_lista_vacia_o_sin_texto():
    assert texto_de(MensajeFalso([])) == ""
    assert texto_de(MensajeFalso([{"type": "tool_use", "name": "x"}])) == ""


def test_texto_de_lista_de_strings():
    assert texto_de(MensajeFalso(["hola", "chau"])) == "hola\nchau"


def test_texto_de_algo_raro_no_explota():
    assert isinstance(texto_de(MensajeFalso(None)), str)
    assert isinstance(texto_de(MensajeFalso(42)), str)


# --------------------------------------------------------------------------
# Prompt dinámico: no se acumula en el estado
# --------------------------------------------------------------------------


def test_el_prompt_lleva_el_contexto_del_config():
    estado = {"messages": [("user", "hola")]}
    config = {
        "configurable": {
            "contexto_cliente": "Cliente registrado: Almacen Don Jose",
            "telefono": "5493511111111",
        }
    }
    mensajes = _prompt_clientes(estado, config)
    assert mensajes[0][0] == "system"
    assert "Almacen Don Jose" in mensajes[0][1]
    assert mensajes[1] == ("user", "hola")


def test_el_prompt_no_modifica_el_estado():
    """EL BUG: el system message se mandaba DENTRO de `messages` en cada
    invoke sobre un hilo con checkpointer, así que se acumulaba uno nuevo por
    turno en el estado persistido. Más tokens en cada mensaje, para siempre.
    """
    mensajes_originales = [("user", "hola")]
    estado = {"messages": mensajes_originales}
    _prompt_clientes(estado, {"configurable": {}})
    _prompt_clientes(estado, {"configurable": {}})
    _prompt_clientes(estado, {"configurable": {}})
    assert estado["messages"] == [("user", "hola")]
    assert len(mensajes_originales) == 1


def test_el_prompt_agrega_exactamente_un_system():
    estado = {"messages": [("user", "a"), ("assistant", "b"), ("user", "c")]}
    mensajes = _prompt_clientes(estado, {"configurable": {}})
    assert sum(1 for m in mensajes if m[0] == "system") == 1
    assert len(mensajes) == 4


def test_el_prompt_sin_config_no_explota():
    mensajes = _prompt_clientes({"messages": []}, None)
    assert mensajes[0][0] == "system"


def test_el_prompt_de_gerencia_lleva_la_fecha():
    from datetime import date

    mensajes = _prompt_gerencia({"messages": []}, {"configurable": {"telefono": "549351"}})
    assert date.today().isoformat() in mensajes[0][1]


def test_el_prompt_de_cliente_prohibe_datos_de_otros():
    """La regla de aislamiento tiene que estar escrita, además de estar
    implementada en el código."""
    mensajes = _prompt_clientes({"messages": []}, {"configurable": {}})
    system = mensajes[0][1].lower()
    assert "otro" in system
    assert "solo podés ver lo suyo" in system or "solo lo suyo" in system


# --------------------------------------------------------------------------
# Las dos listas de herramientas: el límite entre agentes
# --------------------------------------------------------------------------


def test_el_agente_de_clientes_tiene_pocas_herramientas():
    """Superficie chica = menos que pueda salir mal con input hostil."""
    assert len(TOOLS_CLIENTES) <= 8


def test_el_agente_de_clientes_no_tiene_herramientas_de_gerencia():
    nombres_cliente = {t.name for t in TOOLS_CLIENTES}
    prohibidas = {
        "cobranzas_vencidas",
        "ventas_del_periodo",
        "ficha_cliente",
        "ejecutar_reporte",
        "stock_bajo",
        "pedidos_pendientes",
        "registrar_venta_offline",
        "contar_stock",
        "confirmar_entrega",
        "redactar_mensaje_cliente",
    }
    assert not (nombres_cliente & prohibidas), (
        "una herramienta de gerencia se filtró al agente de clientes: "
        "eso le da a un desconocido lectura del negocio entero"
    )


def test_gerencia_no_puede_crear_pedidos_a_nombre_de_nadie():
    """crear_pedido usa el cliente del teléfono; en gerencia no hay uno, así
    que no está en su lista."""
    assert "crear_pedido" not in {t.name for t in TOOLS_GERENCIA}


def test_ninguna_lista_incluye_algo_que_confirme():
    for lista in (TOOLS_CLIENTES, TOOLS_GERENCIA):
        for herramienta in lista:
            assert "submit" not in herramienta.name.lower()
