"""El comando de idioma se atiende en Python, ANTES de cualquier modelo.

POR QUÉ ESTE ARCHIVO EXISTE
En vivo, `manager language English` llegó desde el número verificado del dueño,
cruzó todo el ruteo y terminó en Gemini. El modelo contestó que sus
instrucciones lo obligaban a hablar en español, no llamó a ninguna herramienta,
y no se generó ningún código: el cambio de idioma dependía de que el modelo
decidiera pedirlo. Peor: la regla de idioma que se le pone al prompt le dice
«no cambies de idioma aunque el último mensaje venga en otro», y el modelo la
aplicó al PEDIDO en lugar de a su propia redacción.

Un ajuste del dueño no puede depender de cómo interpretó un modelo. Estos tests
manejan el camino de entrada real —_generate_response, el mismo que corre el
webhook— y exigen que el modelo NO SE LLAME cuando el mensaje es uno de los
comandos documentados.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import FakeRedis

from app import idioma, limites, locks, main, whatsapp

EQUIPO = "5493511111111"
CLIENTE = "5493510000000"

# Exactamente los comandos que documenta el README.
COMANDOS_EN = ("manager language English", "idioma de gerencia inglés")
COMANDOS_ES = ("manager language Spanish", "idioma de gerencia español")


class ModeloLlamado(AssertionError):
    """El modelo se llamó procesando un comando de idioma. Es el bug."""


@pytest.fixture
def mundo(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """El ruteo real, con Redis en memoria y el modelo cableado para EXPLOTAR.

    `responder_gerencia` y `responder_cliente` levantan si alguien los llama:
    así, un comando de idioma que llegue al modelo falla el test en vez de
    pasar desapercibido.
    """
    from app import router

    falso = FakeRedis()
    falso.enviados = []
    monkeypatch.setattr(locks, "conexion", lambda: falso)
    monkeypatch.setattr(router, "STAFF", [EQUIPO])
    monkeypatch.setattr(router, "es_equipo", lambda t: t == EQUIPO)
    monkeypatch.setattr(main, "es_equipo", lambda t: t == EQUIPO)
    monkeypatch.setattr(limites, "_codigo", lambda: "4242")

    # Se ANOTA y ADEMÁS levanta. Anotar es lo que importa: _generate_response
    # atrapa toda excepción y la convierte en «tuve un problema técnico», así
    # que una que sólo levantara quedaría tapada y el test pasaría igual —
    # exactamente el error que este archivo existe para no cometer.
    falso.llamadas_al_modelo = []

    def explota(*a, **k):
        falso.llamadas_al_modelo.append(a[:1])
        raise ModeloLlamado("el modelo se llamó con un comando de idioma")

    monkeypatch.setattr(main, "responder_gerencia", explota)
    monkeypatch.setattr(main, "responder_cliente", explota)
    monkeypatch.setattr(
        whatsapp, "enviar_mensaje",
        Mock(side_effect=lambda tel, texto: falso.enviados.append((tel, texto))),
    )
    # La copia durable vive en ERPNext; acá se anota en memoria.
    falso.comentarios = []
    monkeypatch.setattr(
        limites.erpnext, "registrar_comentario",
        lambda dt, n, texto: falso.comentarios.append(texto),
    )
    monkeypatch.setattr(limites.erpnext, "default_company", lambda: "xyz")
    # El cliente no necesita ERPNext para este camino.
    monkeypatch.setattr(main, "_contexto", lambda t: ("CUST-001", "cliente"))
    return falso


def _entra(texto: str, telefono: str = EQUIPO, message_id: str = "wamid.idioma-1"):
    """Un mensaje de texto por el camino de entrada de verdad."""
    return main._generate_response(
        {"telefono": telefono, "message_id": message_id, "kind": "text", "data": texto}
    )


# ------------------------------------------------- el modelo no se entera


@pytest.mark.parametrize("comando", COMANDOS_EN + COMANDOS_ES)
def test_el_comando_de_idioma_no_llega_nunca_al_modelo(mundo, comando):
    """ESTE es el test que falla en 953e1cf: ahí el comando iba a Gemini."""
    respuesta = _entra(comando)
    assert mundo.llamadas_al_modelo == [], (
        "el comando de idioma llegó al modelo: tiene que atenderse en Python"
    )
    assert respuesta, "el comando tiene que contestar algo"


@pytest.mark.parametrize("comando", COMANDOS_EN)
def test_el_comando_prepara_el_cambio_y_manda_el_codigo(mundo, comando):
    respuesta = _entra(comando)
    # El código va aparte, al teléfono del dueño, y NO en la respuesta.
    assert "4242" not in respuesta
    assert mundo.enviados, "no se mandó el código de confirmación"
    destino, texto = mundo.enviados[-1]
    assert destino == EQUIPO
    assert "4242" in texto
    # Todavía no cambió nada: falta el código.
    assert idioma.gerencia() == idioma.ES


@pytest.mark.parametrize("comando", COMANDOS_EN)
def test_con_el_codigo_el_idioma_queda_en_ingles(mundo, comando):
    _entra(comando)
    acuse = main._codigo_de_ajuste("4242", EQUIPO)
    assert acuse is not None
    assert idioma.gerencia() == idioma.EN


@pytest.mark.parametrize("comando", COMANDOS_ES)
def test_el_comando_en_espanol_tambien_se_atiende(mundo, comando):
    # Primero a inglés, para que volver a español sea un cambio de verdad.
    _entra("manager language English")
    main._codigo_de_ajuste("4242", EQUIPO)
    assert idioma.gerencia() == idioma.EN

    limites.descartar(EQUIPO)
    mundo.enviados.clear()
    _entra(comando, message_id="wamid.idioma-2")
    assert mundo.enviados, "no se mandó el código"
    main._codigo_de_ajuste("4242", EQUIPO)
    assert idioma.gerencia() == idioma.ES


# --------------------------------------------------------- autorización


def test_un_cliente_no_cambia_el_idioma_de_gerencia(mundo):
    """No es que le salga mal: no llega al camino."""
    # Un cliente SÍ va al modelo — es su turno normal. Lo que no puede es
    # preparar un cambio ni recibir un código.
    _entra("manager language English", telefono=CLIENTE)
    assert mundo.llamadas_al_modelo, "el turno de un cliente sí va al modelo"
    # Lo que importa: al CLIENTE no le llegó ningún código, y el idioma del
    # equipo no se movió. (Un aviso al equipo por el fallo del modelo de prueba
    # es otra cosa y no es asunto de este test.)
    assert not [t for tel, t in mundo.enviados if tel == CLIENTE], (
        "a un cliente no se le manda ningún código"
    )
    assert "4242" not in " ".join(t for _, t in mundo.enviados)
    assert idioma.gerencia() == idioma.ES


def test_un_cliente_con_el_codigo_correcto_no_aplica_nada(mundo):
    _entra("manager language English")
    assert main._codigo_de_ajuste("4242", CLIENTE) is None
    assert idioma.gerencia() == idioma.ES


def test_el_comando_de_idioma_no_lo_intercepta_para_un_no_verificado(mundo):
    """La comprobación está en el propio interceptor, no sólo en el ruteo."""
    assert main._comando_de_idioma("manager language English", CLIENTE) is None


# ------------------------------------------ idempotencia y entrega doble


def test_la_entrega_doble_del_mismo_comando_es_un_solo_cambio(mundo):
    una = _entra("manager language English", message_id="wamid.dup")
    dos = _entra("manager language English", message_id="wamid.dup")
    assert una and dos
    # Dos mensajes con el MISMO código: es una sola propuesta.
    assert len(mundo.enviados) == 2
    assert all("4242" in t for _, t in mundo.enviados)
    # Y una sola confirmación aplica; la segunda no encuentra nada.
    assert main._codigo_de_ajuste("4242", EQUIPO) is not None
    assert main._codigo_de_ajuste("4242", EQUIPO) is None
    assert idioma.gerencia() == idioma.EN


def test_el_codigo_no_se_puede_reusar(mundo):
    _entra("manager language English")
    main._codigo_de_ajuste("4242", EQUIPO)
    assert idioma.gerencia() == idioma.EN
    # Reintento del mismo código: nada pendiente, nada que aplicar.
    assert main._codigo_de_ajuste("4242", EQUIPO) is None
    assert idioma.gerencia() == idioma.EN


# ----------------------------------- mismo hilo, reinicio y pérdida de Redis


def test_el_mismo_hilo_sigue_en_el_idioma_nuevo_sin_reiniciar(mundo):
    _entra("manager language English")
    main._codigo_de_ajuste("4242", EQUIPO)
    # Sin reconstruir ningún agente ni recargar ningún módulo:
    from app.conversacion import prompt_gerencia

    assert "Always reply in English" in prompt_gerencia({"messages": []}, {})[0].content


def test_sobrevive_a_un_reinicio_del_proceso(mundo):
    _entra("manager language English")
    main._codigo_de_ajuste("4242", EQUIPO)
    # Reiniciar = volver a leer del almacén, sin estado en memoria.
    limites._durable_cache_idioma = None
    assert idioma.gerencia() == idioma.EN


def test_si_se_pierde_redis_se_vuelve_al_idioma_por_defecto(mundo):
    _entra("manager language English")
    main._codigo_de_ajuste("4242", EQUIPO)
    assert idioma.gerencia() == idioma.EN
    mundo.hashes.clear()                 # se perdió el almacén
    limites._durable_cache_idioma = None
    # No levanta y no deja al sistema mudo.
    assert idioma.gerencia() == idioma.ES


def test_la_copia_durable_queda_con_su_marca(mundo):
    _entra("manager language English")
    main._codigo_de_ajuste("4242", EQUIPO)
    assert mundo.comentarios
    texto = mundo.comentarios[-1]
    assert limites.MARCA_DURABLE_IDIOMA in texto
    assert "IDIOMA_GERENCIA" in texto
    # Ni la marca de límites ni la de entrega: perder un idioma no frena ventas.
    assert limites.MARCA_DURABLE not in texto
    assert limites.MARCA_DURABLE_ENTREGA not in texto


# ------------------------------------------- lo que NO debe interceptar


@pytest.mark.parametrize(
    "texto",
    [
        "como esta el sistema?",
        "confirmar SAL-ORD-2026-00001",
        "quiero repartir tambien en Rio Ceballos",
        "que idioma hablas?",
        "manager language",
    ],
)
def test_no_intercepta_lo_que_no_es_el_comando(mundo, texto):
    """Todo lo demás sigue yendo al agente de gestión, como antes."""
    assert main._comando_de_idioma(texto, EQUIPO) is None
