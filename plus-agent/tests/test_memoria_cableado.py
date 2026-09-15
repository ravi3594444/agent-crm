"""Lo que el dueño anotó llega al prompt de gerencia — y a ningún otro.

`app/memoria.py` y sus dos herramientas se prueban solas en
tests/test_memoria.py. Acá se prueba el CABLE: que el bloque entre de verdad en
el mensaje de sistema del agente de gerencia, que no entre en el del agente de
clientes, y que un Redis caído no deje al dueño sin respuesta.

POR QUÉ LO DEL AGENTE DE CLIENTES ES LO MÁS IMPORTANTE DE ESTE ARCHIVO
Estos datos son el conocimiento comercial privado del dueño: «a la panadería no
le fíes», «a éste cobrale antes de cargar». Metidos en el prompt del agente que
atiende a desconocidos, se los cuenta al primero que pregunte bien. El bloque
se arma en `prompt_gerencia` y en ninguna otra función, y esto lo afirma sobre
el texto que sale, no sobre dónde está escrita la llamada.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

# Afirma el literal castellano «Fecha de hoy» del prompt de gerencia, así
# que declara su idioma en vez de heredarlo del entorno.
pytestmark = pytest.mark.idioma("es")

from app import conversacion, locks, memoria

GERENTE = "5493511234567"
DATO = "La panadería San José paga los viernes."


@pytest.fixture
def redis_real(monkeypatch: pytest.MonkeyPatch):
    """Sin el doble: `memoria` guarda un HASH y el FakeRedis no tiene hdel."""
    monkeypatch.setattr(locks, "conexion", locks._redis)
    cliente = locks._redis()
    cliente.delete(memoria.CLAVE_DATOS, memoria.CLAVE_PREGUNTA, memoria.CLAVE_PREGUNTADO)
    yield cliente
    cliente.delete(memoria.CLAVE_DATOS, memoria.CLAVE_PREGUNTA, memoria.CLAVE_PREGUNTADO)


def _estado() -> dict:
    from langchain_core.messages import HumanMessage

    return {"messages": [HumanMessage(content="¿cómo venimos?")]}


def _config(scope: str = "management") -> dict:
    return {"configurable": {"thread_id": f"{scope}:t", "actor_scope": scope,
                             "actor_phone": GERENTE, "customer_code": "",
                             "inbound_message_id": "w"}}


def _sistema(mensajes) -> str:
    return str(mensajes[0].content)


def test_lo_anotado_aparece_en_el_prompt_del_dueno(redis_real) -> None:
    memoria.anotar("pagos", DATO, GERENTE)

    sistema = _sistema(conversacion.prompt_gerencia(_estado(), _config()))

    assert DATO in sistema, sistema[-800:]


def test_lo_anotado_NO_aparece_en_el_prompt_de_un_cliente(redis_real) -> None:
    """La mitad que importa. Es el conocimiento comercial privado del dueño."""
    memoria.anotar("pagos", DATO, GERENTE)
    config = {"configurable": {"thread_id": "customer:t", "actor_scope": "customer",
                               "customer_code": "CUST-0009", "actor_phone": "549351000",
                               "inbound_message_id": "w"}}

    sistema = _sistema(conversacion.prompt_clientes(_estado(), config))

    assert DATO not in sistema
    assert "San José" not in sistema


def test_sin_nada_anotado_no_queda_un_encabezado_vacio(redis_real) -> None:
    """Un título con nada debajo el modelo lo lee como una lista de la que ya
    habló, y deja de preguntar por lo que justamente no sabe.

    ESTE TEST NO PODÍA FALLAR y estuvo así hasta que lo cazó una revisión.
    Afirmaba que no aparecía `"LO QUE YA SABÉS DEL NEGOCIO"`, un texto que no
    está en ninguna parte del repo: `memoria.ENCABEZADO` dice *«LO QUE TE FUE
    DICIENDO EL DUEÑO»*. Los dos asserts eran verdaderos pasara lo que pasara, y
    la regresión que el nombre promete cuidar quedó sin cuidar desde el día uno.

    Se afirma contra la CONSTANTE y no contra su texto, que es lo correcto acá:
    lo que se prueba es que el encabezado esté AUSENTE cuando no hay nada, y esa
    propiedad no depende de cómo esté redactado. Renombrarlo mueve las dos
    mitades y el test sigue probando lo mismo.

    MUTACIÓN: sacarle a `memoria.bloque_de_prompt` el `if elegidos:` que rodea
    el `partes.append(...)`. Cae éste y sólo éste.
    """
    sistema = _sistema(conversacion.prompt_gerencia(_estado(), _config()))

    assert memoria.ENCABEZADO not in sistema


def test_con_algo_anotado_el_encabezado_viene_CON_su_cuerpo(redis_real) -> None:
    """La otra mitad, y sin ella la de arriba se cumple sola.

    Un `bloque_de_prompt` que nunca escriba el encabezado pasa el test de
    ausencia con las mejores notas y deja al dueño sin su memoria. Afirmar que
    NO está cuando no hay nada sólo dice algo si además se afirma que SÍ está
    —y con el dato debajo— cuando hay.
    """
    memoria.anotar("pagos", DATO, GERENTE)

    sistema = _sistema(conversacion.prompt_gerencia(_estado(), _config()))

    assert memoria.ENCABEZADO in sistema
    cuerpo = sistema.split(memoria.ENCABEZADO, 1)[1]
    assert DATO in cuerpo


def test_con_redis_caido_el_dueno_igual_recibe_su_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin memoria, no sin agente. Un dueño que pregunta cuánto vendió no puede
    quedarse sin respuesta porque no se pudo leer una nota."""
    monkeypatch.setattr(
        memoria, "bloque_de_prompt", Mock(side_effect=ConnectionError("redis"))
    )

    sistema = _sistema(conversacion.prompt_gerencia(_estado(), _config()))

    assert "Fecha de hoy" in sistema
