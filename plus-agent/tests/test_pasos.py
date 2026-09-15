"""Un turno tiene techo, y cuando se llega al techo se contesta, no se pide perdón.

EL DEFECTO QUE ESTO FIJA
------------------------
`graph._config()` no ponía `recursion_limit`, y el default de LangGraph 1.2.11
no es el 25 de `langchain_core` sino **10007** superpasos: ~3335 llamadas al
modelo en UN mensaje de WhatsApp. Medido por `responder_cliente`, con un modelo
que siempre pide herramienta: 121 llamadas al modelo y 120 a herramientas, y
paró porque se acabó el guion. Con Gemini en free tier (7-34 s por llamada, el
problema #1 de CLAUDE.md) eso es la cuota del día y horas de reloj, con Meta
reintentando el mensaje por atrás.

Y las dos salidas de LangGraph están las dos mal para un WhatsApp:
`GraphRecursionError` se volvía «tuve un problema técnico» —tirando todo lo que
el turno YA había averiguado—, y el centinela `"Sorry, need more steps to
process this request."` salía tal cual, en inglés, a un almacenero porteño.

LO QUE SE PRUEBA ACÁ
--------------------
1. el techo existe y se gasta (y es distinto por rol)
2. la aritmética superpasos->llamadas, MEDIDA contra el grafo real
3. el cierre ve la conversación Y ve lo que averiguaron las herramientas
   —son DOS consumidores del mismo `mensajes`, y van con dos mutaciones—
4. el cierre no le manda al proveedor un `tool_calls` colgando
5. ni la excepción ni el centinela llegan a la persona
6. si el cierre falla, la disculpa de siempre sigue abajo
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_progreso import (  # noqa: F401  (mundo/webhook son fixtures)
    CLIENTE,
    GERENTE,
    Mundo,
    herramientas,
    mundo,
    texto,
)
from test_whatsapp_webhook import webhook  # noqa: F401

from app import graph as graph_real
from app import pasos

pytestmark = pytest.mark.idioma("es")


# --------------------------------------------------------------- los dobles


class ModeloDeCierre:
    """El modelo de la llamada de cierre: guarda lo que le pasaron y responde.

    Lo que devuelve SALE de lo que recibe —la última línea del último mensaje—,
    así que no puede estar de acuerdo con el código sobre un payload que nunca
    miró. Un doble que contestara una constante dejaría pasar un cierre al que
    se le arma mal la entrada.
    """

    def __init__(self) -> None:
        self.entradas: list[list] = []

    def invoke(self, entrada, config=None):
        del config
        self.entradas.append(list(entrada))
        ultimo = entrada[-1]
        cuerpo = str(getattr(ultimo, "content", ""))
        return AIMessage(content=f"cierre sobre: {cuerpo.strip().splitlines()[-1]}")

    @property
    def recibido(self) -> list:
        assert self.entradas, "el cierre nunca llamó al modelo"
        return self.entradas[-1]

    def texto_recibido(self) -> str:
        return "\n".join(str(getattr(m, "content", "")) for m in self.recibido)


class ModeloDeCierreRoto:
    def invoke(self, entrada, config=None):
        del entrada, config
        raise RuntimeError("el proveedor no contestó")


def _prompt_de_prueba(state, config):
    """Un `armar_prompt` con la forma del real: system + lo que le pasan."""
    del config
    return [SystemMessage(content="sos el agente"), *state["messages"]]


def _hilo(pregunta: str = "cuanto sale la leche") -> list:
    """Un hilo cortado EXACTAMENTE como lo corta el techo de pasos.

    Termina en un `AIMessage` con `tool_calls` cuyo `ToolMessage` nunca llegó,
    que es el estado del que un proveedor compatible con OpenAI contesta 400.

    Y esos dos `AIMessage` con `tool_calls` TIENEN TEXTO, que es lo que manda
    un modelo de verdad: Gemini y Claude dicen «ahora te lo miro» y piden la
    herramienta en el mismo mensaje. Con el texto vacío —como estaba escrito
    este doble al principio— los descartaba el guarda de «mensaje sin texto» y
    el de `tool_calls` quedaba sin probar: sacarlo dejaba los 17 tests en
    verde. El doble tiene que poder distinguir los dos guardas.
    """
    llamada = {"name": "consultar_stock", "args": {"producto": "leche"},
               "id": "call_1", "type": "tool_call"}
    colgada = {"name": "consultar_stock", "args": {"producto": "queso"},
               "id": "call_9", "type": "tool_call"}
    return [
        HumanMessage(content=pregunta),
        AIMessage(content="Ya te lo busco.", tool_calls=[llamada]),
        ToolMessage(content="stock de leche: 12 unidades", name="consultar_stock",
                    tool_call_id="call_1"),
        AIMessage(content="Dame un segundo que lo veo."),
        AIMessage(content="Ahora te miro el queso.", tool_calls=[colgada]),
    ]


# ------------------------------------------------- 1. el techo, y por rol


def test_el_techo_es_distinto_por_rol_y_gerencia_tiene_mas():
    """Los dos agentes no se parecen: 12 herramientas contra 125 de un MCP."""
    assert pasos.techo("clientes") == pasos.PASOS_CLIENTES
    assert pasos.techo("gerencia") == pasos.PASOS_GERENCIA
    assert pasos.techo("gerencia") > pasos.techo("clientes")
    # Cualquier otra cosa cae del lado angosto, no del ancho.
    assert pasos.techo("lo-que-sea") == pasos.PASOS_CLIENTES


def test_un_techo_que_no_es_entero_positivo_no_arranca(monkeypatch):
    """Un techo en 0 es un agente mudo; se falla al importar, no en el primer
    WhatsApp del día."""
    for malo in ("0", "-3", "ocho", ""):
        monkeypatch.setenv("PASOS_MAX_CLIENTES", malo)
        if malo == "":
            # Vacío cae al default, que sí es válido: es lo que pasa cuando
            # alguien deja la variable puesta y sin valor en el .env.
            assert pasos._entero_positivo("PASOS_MAX_CLIENTES", "8") == 8
            continue
        with pytest.raises(RuntimeError, match="entero positivo"):
            pasos._entero_positivo("PASOS_MAX_CLIENTES", "8")


# --------------------- 2. la aritmética, medida contra el grafo de verdad


@pytest.mark.parametrize("llamadas", [2, 3, 5])
def test_el_limite_compra_exactamente_las_llamadas_al_modelo_que_dice(mundo, llamadas):
    """`limite_de_recursion(n)` tiene que dar n llamadas al modelo. MEDIDO.

    El 3 de `SUPERPASOS_POR_VUELTA` sale de que hay un `pre_model_hook`, y eso
    es una decisión de LangGraph, no de este repo. Si una versión la cambia,
    rompe acá —donde se lee el motivo— y no en la conversación de un cliente.
    """
    guion = [herramientas("consultar_stock_prueba", producto="leche") for _ in range(60)]
    compilado = mundo.agente(guion, rol="clientes")
    modelo = mundo.modelo
    cfg = {
        "configurable": {
            "thread_id": f"cli:techo:{uuid.uuid4().hex[:8]}",
            "actor_scope": "customer", "customer_code": "C1",
            "customer_name": "c", "actor_phone": "5493511234567",
            "inbound_message_id": "m1",
        },
        "recursion_limit": pasos.limite_de_recursion(llamadas),
    }
    from langgraph.errors import GraphRecursionError

    with pytest.raises(GraphRecursionError):
        compilado.invoke({"messages": [("user", "hola")]}, config=cfg)
    assert len(modelo.vistos) == llamadas


def test_sin_techo_el_turno_no_tiene_freno(mundo):
    """El defecto, escrito como test: sin `recursion_limit` no para nada.

    Si algún día LangGraph le pone un default humano, este test falla y hay que
    releer si el techo propio sigue haciendo falta. Hoy su default es 10007.
    """
    from langgraph._internal._config import DEFAULT_RECURSION_LIMIT

    assert DEFAULT_RECURSION_LIMIT > 1000
    # Y el techo que este repo compra es MUCHO más chico que ese default.
    assert pasos.limite_de_recursion(pasos.PASOS_GERENCIA) < DEFAULT_RECURSION_LIMIT / 10


# ----------------- 3. el cierre: DOS consumidores del mismo `mensajes`


def test_el_cierre_le_muestra_al_modelo_lo_que_averiguaron_las_herramientas():
    """Consumidor 1 de `mensajes`: `_hallazgos`.

    Sin esto el cierre contesta sin saber nada y el turno entero se tiró igual
    que con la disculpa, sólo que con mejor letra.
    """
    modelo = ModeloDeCierre()
    pasos.cerrar(_hilo(), modelo=modelo, armar_prompt=_prompt_de_prueba, config={})
    recibido = modelo.texto_recibido()
    assert "stock de leche: 12 unidades" in recibido
    assert "consultar_stock" in recibido


def test_el_cierre_le_muestra_al_modelo_lo_que_dijo_la_persona():
    """Consumidor 2 del MISMO `mensajes`: `_conversacion`.

    Es la mitad que el test de arriba no dice. Un cierre que ve los datos y no
    ve la pregunta contesta otra cosa.
    """
    modelo = ModeloDeCierre()
    pasos.cerrar(_hilo("¿me llega hoy a Villa Crespo?"), modelo=modelo,
                 armar_prompt=_prompt_de_prueba, config={})
    recibido = modelo.texto_recibido()
    assert "Villa Crespo" in recibido
    # Y lo que el agente ya había contestado en texto sigue estando.
    assert "Dame un segundo que lo veo." in recibido


def test_el_cierre_no_le_manda_al_proveedor_una_llamada_colgada():
    """El hilo se corta justo después de pedir herramientas: la cola tiene un
    `AIMessage` con `tool_calls` sin su `ToolMessage`. Eso es un 400."""
    modelo = ModeloDeCierre()
    pasos.cerrar(_hilo(), modelo=modelo, armar_prompt=_prompt_de_prueba, config={})
    for msg in modelo.recibido:
        assert not getattr(msg, "tool_calls", None), f"viajó un tool_call: {msg}"
        assert not isinstance(msg, ToolMessage), f"viajó un ToolMessage: {msg}"


def test_el_cierre_sin_nada_averiguado_igual_contesta():
    """Se llegó al techo sin que ninguna herramienta devolviera nada útil."""
    modelo = ModeloDeCierre()
    salida = pasos.cerrar(
        [HumanMessage(content="hola")],
        modelo=modelo, armar_prompt=_prompt_de_prueba, config={},
    )
    assert salida
    assert pasos._SIN_HALLAZGOS in modelo.texto_recibido()


def test_un_cierre_que_no_contesta_levanta():
    """La disculpa de `_generate_response` es el piso, y no se tapa con un
    texto armado acá: eso sería inventarle al cliente una respuesta."""
    with pytest.raises(RuntimeError):
        pasos.cerrar(_hilo(), modelo=ModeloDeCierreRoto(),
                     armar_prompt=_prompt_de_prueba, config={})

    class Vacio:
        def invoke(self, entrada, config=None):
            del entrada, config
            return AIMessage(content="   ")

    with pytest.raises(RuntimeError):
        pasos.cerrar(_hilo(), modelo=Vacio(), armar_prompt=_prompt_de_prueba, config={})


# -------------------- 4. y 5. de punta a punta: qué le llega a la persona


def _instalar_cierre(monkeypatch, rol: str) -> ModeloDeCierre:
    modelo = ModeloDeCierre()
    nombre = "_modelo_gerencia" if rol == "gerencia" else "_modelo_clientes"
    monkeypatch.setattr(graph_real, nombre, modelo)
    return modelo


def test_al_techo_la_persona_recibe_lo_averiguado_y_no_una_disculpa(
    mundo, monkeypatch, webhook
):
    """El camino real: webhook -> worker -> responder_cliente -> techo -> cierre."""
    guion = [herramientas("consultar_stock_prueba", producto="leche") for _ in range(60)]
    mundo.instalar("clientes", guion)
    cierre = _instalar_cierre(monkeypatch, "clientes")

    mundo.turno(CLIENTE, "cuanto sale la leche")
    dichos = mundo.salida.textos(CLIENTE)

    assert cierre.entradas, "no se llamó al cierre: el turno murió en la disculpa"
    assert dichos, "no le salió nada a la persona"
    final = dichos[-1]
    assert final.startswith("cierre sobre:"), final
    assert webhook.texto_error_tecnico("es") not in dichos
    assert webhook.texto_error_tecnico_avisado("es") not in dichos
    # Y el modelo se llamó las veces que compra el techo, no 121.
    assert len(mundo.modelo.vistos) == pasos.techo("clientes")
    # Lo que devolvió la herramienta llegó al cierre.
    assert "stock de leche: 12" in cierre.texto_recibido()


def test_el_centinela_en_ingles_de_langgraph_no_llega_nunca_a_la_persona(
    mundo, monkeypatch
):
    """`recursion_limit = 3n` sale por el camino lindo de LangGraph, que no
    levanta y no se loguea: devuelve «Sorry, need more steps…» como si fuera la
    respuesta del agente. `_non_empty` no lo ataja porque no está vacío."""
    guion = [herramientas("consultar_stock_prueba", producto="leche") for _ in range(60)]
    mundo.instalar("clientes", guion)
    cierre = _instalar_cierre(monkeypatch, "clientes")
    # El múltiplo exacto de 3 es el que cae en la ventana del centinela.
    monkeypatch.setattr(pasos, "limite_de_recursion", lambda n: 3 * n)

    mundo.turno(CLIENTE, "cuanto sale la leche")
    dichos = mundo.salida.textos(CLIENTE)

    assert cierre.entradas, "el centinela pasó de largo sin cerrar el turno"
    assert dichos and dichos[-1].startswith("cierre sobre:")
    for dicho in dichos:
        assert pasos.CENTINELA_SIN_PASOS not in dicho


def test_el_centinela_sigue_siendo_el_texto_que_pone_langgraph():
    """Se compara por texto porque es una constante de la biblioteca. Si una
    versión se lo cambia, se entera este test y no un cliente."""
    from langgraph.prebuilt import chat_agent_executor

    fuente = Path(chat_agent_executor.__file__).read_text(encoding="utf-8")
    assert pasos.CENTINELA_SIN_PASOS in fuente


def test_si_el_cierre_tambien_falla_queda_la_disculpa_de_siempre(
    mundo, monkeypatch, webhook
):
    """El piso no se movió: un cierre roto vuelve al comportamiento anterior."""
    guion = [herramientas("consultar_stock_prueba", producto="leche") for _ in range(60)]
    mundo.instalar("clientes", guion)
    monkeypatch.setattr(graph_real, "_modelo_clientes", ModeloDeCierreRoto())

    mundo.turno(CLIENTE, "cuanto sale la leche")
    dichos = mundo.salida.textos(CLIENTE)

    assert dichos, "no le salió nada a la persona"
    assert dichos[-1] in {
        webhook.texto_error_tecnico("es"),
        webhook.texto_error_tecnico_avisado("es"),
    }


def test_el_turno_normal_no_pasa_por_el_cierre(mundo, monkeypatch):
    """Lo de siempre sigue siendo lo de siempre: una vuelta, una respuesta."""
    mundo.instalar("clientes", [texto("Hola! La leche sale $1000 el litro.")])
    cierre = _instalar_cierre(monkeypatch, "clientes")

    mundo.turno(CLIENTE, "cuanto sale la leche")
    dichos = mundo.salida.textos(CLIENTE)

    assert not cierre.entradas, "un turno sano no puede pagar la llamada de cierre"
    assert dichos[-1] == "Hola! La leche sale $1000 el litro."


def test_el_turno_de_gerencia_usa_SU_techo_y_no_el_del_cliente(mundo, monkeypatch):
    """Dos techos o uno: si `rol` no llegara, gerencia correría con 8."""
    guion = [herramientas("consultar_stock_prueba", producto="leche") for _ in range(60)]
    mundo.instalar("gerencia", guion)
    cierre = _instalar_cierre(monkeypatch, "gerencia")

    mundo.turno(GERENTE, "como venimos hoy")

    assert cierre.entradas, "el turno de gerencia murió en la disculpa"
    assert len(mundo.modelo.vistos) == pasos.techo("gerencia")


# ------------------------- 6. lo que el cierre NO puede hacer: gastar de más


def test_el_cierre_no_le_manda_al_modelo_el_hilo_entero():
    """El cierre se saltea el `pre_model_hook`, así que tiene que recortar él.

    Sin esto, la llamada que existe para ACOTAR el gasto le manda al proveedor
    hasta 30 días de conversación —el TTL del checkpointer— y es la más cara
    del turno. Se recorta con `CONVERSATION_MAX_MESSAGES`, que es la misma
    definición de «cuánta historia ve el modelo» que usa el grafo: una segunda
    sería una segunda respuesta a la misma pregunta.
    """
    from app.conversacion import max_history

    largo = []
    for i in range(max_history() * 3):
        largo.append(HumanMessage(content=f"pregunta {i}"))
        largo.append(AIMessage(content=f"respuesta {i}"))
    modelo = ModeloDeCierre()
    pasos.cerrar(largo, modelo=modelo, armar_prompt=_prompt_de_prueba, config={})

    # +2: el system del prompt y la nota de cierre, que no son historia.
    assert len(modelo.recibido) <= max_history() + 2, len(modelo.recibido)
    # Y lo que sobrevive es la COLA, que es lo que se estaba hablando.
    assert "pregunta 0" not in modelo.texto_recibido()
    assert f"respuesta {max_history() * 3 - 1}" in modelo.texto_recibido()


def test_el_cierre_queda_anotado_en_el_hilo(mundo, monkeypatch):
    """Si no se anota, el turno siguiente no sabe qué ofreció el agente.

    El cliente lee «¿te mando 10?», contesta «sí dale», y el modelo ve un «sí
    dale» suelto: vuelve a preguntar todo. La respuesta del cierre salió por el
    camino de excepción, así que el grafo no la escribió —sólo guarda lo que
    pasó por sus nodos—.
    """
    guion = [herramientas("consultar_stock_prueba", producto="leche") for _ in range(60)]
    mundo.instalar("clientes", guion)
    cierre = _instalar_cierre(monkeypatch, "clientes")

    mundo.turno(CLIENTE, "cuanto sale la leche")
    dicho = mundo.salida.textos(CLIENTE)[-1]

    # El turno SIGUIENTE, sobre el mismo hilo, tiene que ver esa respuesta.
    siguiente = mundo.instalar("clientes", [texto("Perfecto, te lo anoto.")])
    mundo.turno(CLIENTE, "si dale")

    visto = "\n".join(
        str(getattr(m, "content", "")) for m in siguiente.vistos[0]
    )
    assert dicho in visto, "el cierre no quedó en el hilo: el agente se olvidó lo que dijo"
    assert cierre.entradas


def test_una_anotacion_fallida_no_le_saca_la_respuesta_al_cliente(monkeypatch):
    """Un hilo incompleto es un turno peor; no contestar es el negocio parado."""

    class AgenteQueNoAnota:
        def update_state(self, config, valores):
            raise RuntimeError("Redis no contestó")

    salida = pasos._cerrar_y_recordar(
        AgenteQueNoAnota(), _hilo(),
        modelo=ModeloDeCierre(), armar_prompt=_prompt_de_prueba, config={},
    )
    assert salida.startswith("cierre sobre:")


def test_el_cierre_recorta_DESPUES_de_limpiar_y_no_antes():
    """El orden importa justo en el caso para el que existe el cierre.

    Un turno que llegó al techo dejó una cola de decenas de llamadas a
    herramienta. Recortar PRIMERO gasta el presupuesto de mensajes en
    ToolMessages —que después se tiran igual, porque no viajan— y el cierre se
    queda sin la conversación, que es lo único que viene a buscar: contestaría
    con los datos y sin la pregunta.

    El test de arriba no distingue los dos órdenes, porque su hilo es puro
    ida y vuelta; éste tiene la cola que deja un bucle de verdad.
    """
    from app.conversacion import max_history

    hilo = [HumanMessage(content="¿me llega hoy a Villa Crespo?"),
            AIMessage(content="Dejame que lo miro.")]
    for i in range(max_history() * 2):
        hilo.append(AIMessage(
            content=f"busco {i}",
            tool_calls=[{"name": "consultar_stock", "args": {}, "id": f"c{i}",
                         "type": "tool_call"}],
        ))
        hilo.append(ToolMessage(content=f"resultado {i}", name="consultar_stock",
                                tool_call_id=f"c{i}"))

    modelo = ModeloDeCierre()
    pasos.cerrar(hilo, modelo=modelo, armar_prompt=_prompt_de_prueba, config={})
    recibido = modelo.texto_recibido()

    assert "Villa Crespo" in recibido, "recortó antes de limpiar: perdió la pregunta"
    assert "Dejame que lo miro." in recibido
    # Y los datos siguen llegando por su propio camino.
    assert f"resultado {max_history() * 2 - 1}" in recibido
