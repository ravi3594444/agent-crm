"""Lo que el dueño anotó llega al prompt de gerencia — y a ningún otro.

`app/memoria.py` y sus dos herramientas se prueban solas en
tests/test_memoria.py. Acá se prueba el CABLE: que el bloque entre de verdad en
el mensaje de sistema del agente de gerencia, que no entre en el del agente de
clientes, y que un Redis caído no deje al dueño sin respuesta.

POR QUÉ LO DEL AGENTE DE CLIENTES ES LO MÁS IMPORTANTE DE ESTE ARCHIVO
Estos datos son el conocimiento comercial privado del dueño: «a la panadería no
le fíes», «a éste cobrale antes de cargar». Metidos en el prompt del agente que
atiende a desconocidos, se los cuenta al primero que pregunte bien.

LA FRONTERA SE MOVIÓ, Y NO ES LA MISMA QUE ANTES. Hasta acá era por FUNCIÓN: el
bloque se armaba en `prompt_gerencia` y en ninguna otra. Ahora es por CLAVE.
`prompt_clientes` recibe un bloque propio, `memoria.bloque_para_clientes`, que
cruza sólo las claves marcadas `para_clientes` en `memoria.HUECOS` — las ocho
que son respuestas de mostrador («¿hasta qué hora te puedo pedir?», «¿los
cajones vuelven?»), que el dueño contestaba una vez y se quedaban del lado del
que las escuchó.

Lo que cambió es qué cruza; lo que NO cambió es que las dos frases de arriba no
cruzan. Es una lista de lo PERMITIDO: las cuatro claves privadas siguen
afuera, y una nota bajo una clave inventada por el dueño —que es lo que
`anotar_dato` acepta— tampoco sale, aunque nadie la haya previsto. El test de
abajo usa justamente una de ésas.

Y se puede apagar entero sin tocar código: `MEMORIA_PARA_CLIENTES=false`.

Todo esto se afirma sobre el texto que sale, no sobre dónde está escrita la
llamada.
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
    _limpiar(cliente)
    yield cliente
    _limpiar(cliente)


def _limpiar(cliente) -> None:
    """Las CUATRO claves. El descanso vive cuatro horas y cruza archivos.

    Es un silencio guardado en Redis: un test que lo deje puesto le apaga las
    preguntas al que corra después, que se queda sin la mitad del bloque y no
    tiene cómo saber por qué.
    """
    cliente.delete(
        memoria.CLAVE_DATOS,
        memoria.CLAVE_PREGUNTA,
        memoria.CLAVE_PREGUNTADO,
        memoria.CLAVE_DESCANSO,
    )


def _estado() -> dict:
    from langchain_core.messages import HumanMessage

    return {"messages": [HumanMessage(content="¿cómo venimos?")]}


def _config(scope: str = "management", mensaje: str = "w") -> dict:
    return {"configurable": {"thread_id": f"{scope}:t", "actor_scope": scope,
                             "actor_phone": GERENTE, "customer_code": "",
                             "inbound_message_id": mensaje}}


def _sistema(mensajes) -> str:
    return str(mensajes[0].content)


def test_lo_anotado_aparece_en_el_prompt_del_dueno(redis_real) -> None:
    memoria.anotar("pagos", DATO, GERENTE)

    sistema = _sistema(conversacion.prompt_gerencia(_estado(), _config()))

    assert DATO in sistema, sistema[-800:]


def _config_cliente() -> dict:
    return {"configurable": {"thread_id": "customer:t", "actor_scope": "customer",
                             "customer_code": "CUST-0009", "actor_phone": "549351000",
                             "inbound_message_id": "w"}}


def test_lo_anotado_NO_aparece_en_el_prompt_de_un_cliente(redis_real) -> None:
    """La mitad que importa. Es el conocimiento comercial privado del dueño.

    `pagos` NO es ninguno de los doce huecos: es una clave que se inventó el
    dueño, que es exactamente lo que `anotar_dato` acepta desde WhatsApp. Por
    eso este test sigue siendo el que atrapa la diferencia entre una lista de
    lo PERMITIDO y una de lo prohibido — una lista de claves vedadas sólo tapa
    lo que alguien previó, y nadie previó «pagos».

    MEDIDO: cambiar el filtro de `bloque_para_clientes` por
    `not in {las cuatro claves privadas}` cae éste, con almacén de verdad, y el
    par de primitivo en tests/test_memoria.py. Ningún otro de los 3314.
    """
    memoria.anotar("pagos", DATO, GERENTE)

    sistema = _sistema(conversacion.prompt_clientes(_estado(), _config_cliente()))

    assert DATO not in sistema
    assert "San José" not in sistema


def test_lo_que_el_dueno_contesto_de_mostrador_SI_llega_al_cliente(redis_real) -> None:
    """La otra mitad, con el almacén de verdad y el prompt de verdad.

    `horario_corte` es uno de los ocho marcados y es la pregunta que un almacén
    hace todos los días. Sin esto, el dueño contestaba una vez y el cliente que
    preguntaba lo mismo recibía un «te averiguo».

    Y es el ÚNICO de la pila que pasa por el almacén de verdad: los otros le
    ponen un doble a `activos()`, así que la vuelta completa —guardar el HASH,
    releerlo, pasar el filtro con la clave tal como quedó guardada— sólo se
    recorre acá. Por eso el texto lleva tilde: es lo que escribe un dueño
    argentino.

    DE ESTE TEST NO HAY UNA MUTACIÓN QUE LO MATE SOLO, y se dice en vez de
    inventarle una. Las dos que se corrieron:

      · `json.dumps(..., ensure_ascii=False)` -> `True` en `_linea`: caen TRES
        —éste y los dos del lado del dueño—, porque los tres llevan una nota con
        tilde y `_linea` es de los dos lados.
      · `para_clientes=True` -> `False` en `horario_corte`: cae la pila entera,
        las cinco pruebas que afirman sobre esa clave permitida.

    Es un test de CAPA, y la regla de CLAUDE.md sobre mutaciones dirigidas
    existe para que un test no pueda dejar de fallar nunca, no para prohibir
    que dos capas compartan un defecto. Lo que éste agrega y ninguno de los
    otros cuatro tiene: si alguien cambia `normalizar_clave` —que corre al
    GUARDAR y no al leer—, la clave guardada deja de coincidir con
    `CLAVES_PARA_CLIENTES` y el permiso se apaga en silencio. Con `activos()`
    doblado eso no se ve.
    """
    memoria.anotar("horario_corte", "Hasta las 18 y sale al otro día.", GERENTE)

    sistema = _sistema(conversacion.prompt_clientes(_estado(), _config_cliente()))

    assert "Hasta las 18 y sale al otro día." in sistema


def test_el_dueno_puede_apagar_el_bloque_de_clientes_sin_tocar_codigo(
    redis_real, monkeypatch
) -> None:
    """El interruptor, y que apague SÓLO este lado.

    Esto mueve una frontera que estaba escrita, así que tiene que poder
    volverse atrás sin revertir un commit. Y apagarlo no puede dejar al dueño
    sin su propia memoria: son dos bloques distintos y el interruptor es de uno.

    La tercera vuelta es la dirección en la que el interruptor falla. Un valor
    mal escrito APAGA: es lo único seguro para un interruptor de privacidad.

    MUTACIONES: (a) sacar el `if not memoria_de_clientes_encendida(): return ""`
    -> cae éste y sólo éste; (b) `== "true"` -> `!= "false"`, o sea que el valor
    mal escrito deje el bloque prendido -> cae éste y sólo éste.
    """
    memoria.anotar("horario_corte", "Hasta las 18 y sale al otro día.", GERENTE)

    monkeypatch.setenv("MEMORIA_PARA_CLIENTES", "false")
    apagado = _sistema(conversacion.prompt_clientes(_estado(), _config_cliente()))
    assert "Hasta las 18" not in apagado
    # Y el dueño sigue viendo la suya: el interruptor es de un lado solo.
    assert "Hasta las 18" in _sistema(conversacion.prompt_gerencia(_estado(), _config()))

    monkeypatch.setenv("MEMORIA_PARA_CLIENTES", "true")
    assert "Hasta las 18" in _sistema(
        conversacion.prompt_clientes(_estado(), _config_cliente())
    )

    # Mal escrito: apaga. Nunca al revés.
    monkeypatch.setenv("MEMORIA_PARA_CLIENTES", "treu")
    assert "Hasta las 18" not in _sistema(
        conversacion.prompt_clientes(_estado(), _config_cliente())
    )


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


def test_el_segundo_mensaje_del_dueno_ya_no_trae_la_orden_de_preguntar(
    redis_real,
) -> None:
    """EL CABLE DE LA PREGUNTA, que es lo que estaba roto y no el primitivo.

    `memoria` sabía distinguir un turno de otro; `prompt_gerencia` hacía `del
    config` y no le pasaba cuál era, así que los seis mensajes de una charla
    entraban como si fueran seis vueltas del mismo react loop. El dueño recibía
    la misma pregunta seis veces. Probar `_reclamar` sola no lo habría visto:
    es el mismo punto ciego de «probar el primitivo no es probar el arreglo»
    que este archivo ya documenta para la memoria de clientes.

    Se afirma sobre el TEXTO DEL PROMPT y con dos ids distintos, que es lo
    único que se parece a dos mensajes de WhatsApp.

    MUTACIÓN: en `prompt_gerencia`, volver a `turno = ""`. Cae ésta y sólo ésta.
    """
    primero = _sistema(conversacion.prompt_gerencia(_estado(), _config(mensaje="w1")))
    assert "preguntale ESTO" in primero, primero[-600:]

    segundo = _sistema(conversacion.prompt_gerencia(_estado(), _config(mensaje="w2")))

    assert "preguntale ESTO" not in segundo, segundo[-600:]
    # Pero la clave sigue ahí: la respuesta llega en ESTE mensaje, y sin la
    # sección el modelo no sabe dónde guardarla.
    assert "YA SE LO PREGUNTASTE" in segundo, segundo[-600:]
