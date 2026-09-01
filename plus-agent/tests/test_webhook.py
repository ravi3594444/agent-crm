"""El webhook: firma, idempotencia, ruteo y —sobre todo— nunca silencio.

Los tests corren contra la app real con TestClient. Lo único falso son el
agente (no queremos gastar tokens ni depender de la red), Redis y WhatsApp.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

import pytest
from fastapi.testclient import TestClient

SECRETO = "secreto-de-prueba"


class RedisFalso:
    """Lo justo para probar idempotencia: SET NX EX, GET, DELETE."""

    def __init__(self) -> None:
        self.datos: dict[str, str] = {}
        self.caido = False

    def set(self, clave, valor, nx=False, ex=None):
        if self.caido:
            raise ConnectionError("redis caído (test)")
        if nx and clave in self.datos:
            return None
        self.datos[clave] = valor
        return True

    def get(self, clave):
        if self.caido:
            raise ConnectionError("redis caído (test)")
        v = self.datos.get(clave)
        return v.encode() if isinstance(v, str) else v

    def delete(self, clave):
        self.datos.pop(clave, None)
        return 1

    def ping(self):
        if self.caido:
            raise ConnectionError("redis caído (test)")
        return True


class AgenteFalso:
    """Registra lo que se le pidió y devuelve una respuesta fija."""

    def __init__(self) -> None:
        self.llamadas_cliente: list[dict] = []
        self.llamadas_gerencia: list[dict] = []
        self.explotar = False
        self.respuesta = "Listo, te lo cargo."

    def responder_cliente(self, mensaje, thread_id, contexto_cliente, cliente_code, telefono):
        if self.explotar:
            raise RuntimeError("el LLM explotó (test)")
        self.llamadas_cliente.append(
            {
                "mensaje": mensaje,
                "thread_id": thread_id,
                "contexto": contexto_cliente,
                "cliente_code": cliente_code,
                "telefono": telefono,
            }
        )
        return self.respuesta

    def responder_gerencia(self, mensaje, thread_id, usuario):
        if self.explotar:
            raise RuntimeError("el LLM explotó (test)")
        self.llamadas_gerencia.append(
            {"mensaje": mensaje, "thread_id": thread_id, "usuario": usuario}
        )
        return self.respuesta


@pytest.fixture
def app_test(monkeypatch, wa, erp):
    """La app real, con Redis y agente falsos."""
    from app import main

    redis_falso = RedisFalso()
    agente = AgenteFalso()
    monkeypatch.setattr(main, "r", redis_falso)
    monkeypatch.setattr(main, "responder_cliente", agente.responder_cliente)
    monkeypatch.setattr(main, "responder_gerencia", agente.responder_gerencia)
    monkeypatch.setenv("TELEFONOS_EQUIPO", "+5493511111111")

    from app import router

    router.recargar()

    cliente_http = TestClient(main.app)
    cliente_http.redis = redis_falso
    cliente_http.agente = agente
    cliente_http.wa = wa
    cliente_http.erp = erp
    return cliente_http


def firmar(cuerpo: bytes) -> str:
    return "sha256=" + hmac.new(SECRETO.encode(), cuerpo, hashlib.sha256).hexdigest()


def entrante(
    tipo="text", texto="hola, tenés queso cremoso?", de="5493519999999", mid="wamid.1", **extra
):
    msg = {"id": mid, "from": de, "type": tipo}
    if tipo == "text":
        msg["text"] = {"body": texto}
    msg.update(extra)
    return {"entry": [{"changes": [{"value": {"messages": [msg]}}]}]}


def postear(cliente_http, payload, firma=True):
    cuerpo = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if firma:
        headers["X-Hub-Signature-256"] = firmar(cuerpo)
    return cliente_http.post("/webhook/whatsapp", content=cuerpo, headers=headers)


# --------------------------------------------------------------------------
# Firma
# --------------------------------------------------------------------------


def test_sin_firma_es_403(app_test):
    assert postear(app_test, entrante(), firma=False).status_code == 403
    assert not app_test.agente.llamadas_cliente


def test_firma_invalida_es_403(app_test):
    cuerpo = json.dumps(entrante()).encode()
    r = app_test.post(
        "/webhook/whatsapp",
        content=cuerpo,
        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    assert r.status_code == 403
    assert not app_test.agente.llamadas_cliente


def test_firma_de_otro_cuerpo_es_403(app_test):
    """Firma válida pero de un payload distinto: no sirve."""
    otro = json.dumps(entrante(texto="otra cosa")).encode()
    cuerpo = json.dumps(entrante()).encode()
    r = app_test.post(
        "/webhook/whatsapp",
        content=cuerpo,
        headers={"X-Hub-Signature-256": firmar(otro)},
    )
    assert r.status_code == 403


def test_firma_valida_procesa(app_test):
    assert postear(app_test, entrante()).status_code == 200
    assert len(app_test.agente.llamadas_cliente) == 1


def test_verificacion_de_meta(app_test):
    r = app_test.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": os.environ["META_VERIFY_TOKEN"],
            "hub.challenge": "desafio-123",
        },
    )
    assert r.status_code == 200
    assert r.text == "desafio-123"


def test_verificacion_con_token_malo(app_test):
    r = app_test.get(
        "/webhook/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "x"},
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------
# Idempotencia: Meta REINTENTA
# --------------------------------------------------------------------------


def test_el_reintento_de_meta_no_duplica(app_test):
    payload = entrante(mid="wamid.repetido")
    postear(app_test, payload)
    postear(app_test, payload)
    postear(app_test, payload)
    assert len(app_test.agente.llamadas_cliente) == 1


def test_mensajes_distintos_se_procesan_los_dos(app_test):
    postear(app_test, entrante(mid="wamid.a", texto="uno"))
    postear(app_test, entrante(mid="wamid.b", texto="dos"))
    assert len(app_test.agente.llamadas_cliente) == 2


def test_si_falla_el_proceso_el_mensaje_se_puede_reintentar(app_test):
    """EL BUG VIEJO: la marca se ponía con 24h ANTES de procesar. Si el
    proceso moría en medio (deploy, OOM), el reintento de Meta veía la marca
    y el mensaje del cliente se perdía PARA SIEMPRE.

    Ahora un fallo libera la marca y el reintento vuelve a intentar.
    """
    app_test.agente.explotar = True
    postear(app_test, entrante(mid="wamid.falla"))
    assert "wa:seen:wamid.falla" not in app_test.redis.datos

    app_test.agente.explotar = False
    postear(app_test, entrante(mid="wamid.falla"))
    assert len(app_test.agente.llamadas_cliente) == 1


def test_con_redis_caido_no_se_procesa(app_test):
    """Sin idempotencia los reintentos duplican pedidos. Un pedido duplicado
    es peor que uno demorado: preferimos no procesar."""
    app_test.redis.caido = True
    r = postear(app_test, entrante())
    assert r.status_code == 200  # Meta igual recibe 200 y reintenta
    assert not app_test.agente.llamadas_cliente


# --------------------------------------------------------------------------
# Ruteo: el límite de seguridad entre los dos agentes
# --------------------------------------------------------------------------


def test_el_equipo_va_al_agente_de_gerencia(app_test):
    postear(app_test, entrante(de="5493511111111", texto="cómo van las ventas?"))
    assert len(app_test.agente.llamadas_gerencia) == 1
    assert not app_test.agente.llamadas_cliente


def test_un_desconocido_va_al_agente_de_clientes(app_test):
    postear(app_test, entrante(de="5493519999999"))
    assert len(app_test.agente.llamadas_cliente) == 1
    assert not app_test.agente.llamadas_gerencia


def test_el_equipo_se_reconoce_en_cualquier_formato(app_test):
    """El dueño escribe desde su teléfono y Meta manda el número sin +.
    Si esto falla, el dueño queda ruteado como cliente."""
    for formato in ("5493511111111", "+5493511111111"):
        app_test.agente.llamadas_gerencia.clear()
        postear(app_test, entrante(de=formato, mid=f"wamid.{formato}"))
        assert app_test.agente.llamadas_gerencia, f"no reconoció {formato}"


def test_el_codigo_de_cliente_sale_del_telefono_no_del_modelo(app_test):
    """El webhook resuelve el cliente y lo pasa por config. Es el límite de
    autorización."""
    app_test.erp.listas["Customer"] = [
        {
            "name": "CUST-0007",
            "customer_name": "Almacen Don Jose",
            "customer_group": "Comercio",
            "mobile_no": "+54 9 351 999-9999",
        }
    ]
    postear(app_test, entrante(de="5493519999999"))
    llamada = app_test.agente.llamadas_cliente[0]
    assert llamada["cliente_code"] == "CUST-0007"
    assert "Almacen Don Jose" in llamada["contexto"]


def test_cliente_no_registrado_no_trae_codigo(app_test):
    app_test.erp.listas["Customer"] = []
    postear(app_test, entrante(de="5493519999999"))
    llamada = app_test.agente.llamadas_cliente[0]
    assert llamada["cliente_code"] == ""
    assert "no registrado" in llamada["contexto"]


def test_un_telefono_que_termina_igual_no_es_el_mismo_cliente(app_test):
    """El `like` por sufijo puede traer falsos positivos. Si se colara uno,
    le cargaríamos el pedido a otra persona."""
    app_test.erp.listas["Customer"] = [
        {
            "name": "CUST-OTRO",
            "customer_name": "Otro Cliente",
            "customer_group": "Comercio",
            "mobile_no": "+54 9 11 4599-9999",  # mismo final, otro número
        }
    ]
    postear(app_test, entrante(de="5493519999999"))
    assert app_test.agente.llamadas_cliente[0]["cliente_code"] == ""


# --------------------------------------------------------------------------
# NUNCA SILENCIO — el fallo que el README declara como el peor
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tipo", ["audio", "image", "video", "document", "sticker", "location"])
def test_los_mensajes_que_no_son_texto_reciben_respuesta(app_test, tipo):
    """EL BUG: `if msg.get("type") != "text": continue`. El cliente mandaba un
    audio —cosa constante en Argentina— y no recibía NADA. Quedaba esperando
    una respuesta que no iba a llegar nunca.
    """
    postear(app_test, entrante(tipo=tipo, mid=f"wamid.{tipo}", de="5493519999999"))
    respuestas = app_test.wa.textos_a("5493519999999")
    assert respuestas, f"un mensaje de tipo {tipo} quedó sin respuesta"
    assert len(respuestas[0]) > 10


def test_el_audio_pide_que_lo_escriban(app_test):
    postear(app_test, entrante(tipo="audio", mid="wamid.audio"))
    texto = app_test.wa.textos_a("5493519999999")[0]
    assert "audio" in texto.lower()
    assert "escrib" in texto.lower()


def test_si_el_agente_explota_el_cliente_igual_recibe_algo(app_test):
    app_test.agente.explotar = True
    postear(app_test, entrante(de="5493519999999"))
    respuestas = app_test.wa.textos_a("5493519999999")
    assert respuestas
    assert "problema técnico" in respuestas[0]


def test_si_el_agente_explota_el_equipo_SE_ENTERA(app_test):
    """El mensaje de error le dice al cliente "ya avisé al equipo". Antes eso
    era mentira: no se avisaba a nadie. Ahora tiene que ser verdad."""
    app_test.agente.explotar = True
    postear(app_test, entrante(de="5493519999999"))
    avisos = app_test.wa.textos_a("5493511111111")
    assert avisos, "el equipo no se enteró de la falla"
    assert "Falló" in avisos[0] or "falló" in avisos[0]


def test_respuesta_vacia_del_agente_no_deja_silencio(app_test):
    app_test.agente.respuesta = ""
    postear(app_test, entrante(de="5493519999999"))
    respuestas = app_test.wa.textos_a("5493519999999")
    assert respuestas and respuestas[0].strip()


def test_mensaje_de_texto_vacio_no_llama_al_agente(app_test):
    postear(app_test, entrante(texto="   ", mid="wamid.vacio"))
    assert not app_test.agente.llamadas_cliente


# --------------------------------------------------------------------------
# Botones (aprobación desde la pantalla de bloqueo)
# --------------------------------------------------------------------------


def boton(reply_id, de="5493511111111", mid="wamid.btn"):
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": mid,
                                    "from": de,
                                    "type": "interactive",
                                    "interactive": {"button_reply": {"id": reply_id}},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


def test_el_dueno_confirma_desde_el_boton(app_test):
    app_test.erp.docs[("Sales Order", "SO-0001")] = {
        "name": "SO-0001",
        "customer": "CUST-0007",
        "docstatus": 0,
        "grand_total": 12000,
        "delivery_date": "2026-09-02",
        "items": [],
    }
    app_test.erp.docs[("Customer", "CUST-0007")] = {
        "name": "CUST-0007",
        "mobile_no": "+5493519999999",
    }
    postear(app_test, boton("ok:SO-0001"))
    assert ("Sales Order", "SO-0001") in app_test.erp.enviados_submit
    # y el cliente se entera
    assert app_test.wa.textos_a("5493519999999")


def test_un_extrano_no_puede_aprobar(app_test):
    """Si alguien adivina el payload del botón, no consigue nada."""
    app_test.erp.docs[("Sales Order", "SO-0001")] = {
        "name": "SO-0001",
        "customer": "CUST-0007",
        "docstatus": 0,
        "items": [],
    }
    postear(app_test, boton("ok:SO-0001", de="5493519999999"))
    assert not app_test.erp.enviados_submit
    assert "permiso" in app_test.wa.textos_a("5493519999999")[0]


def test_un_error_en_el_boton_no_es_un_500(app_test):
    """EL BUG: la rama de botones estaba fuera de todo try/except. Un fallo
    era un 500 al webhook y, como la marca de idempotencia ya estaba puesta,
    el reintento se descartaba: el toque del dueño no hacía nada y nadie se
    enteraba."""
    r = postear(app_test, boton("ok:NO-EXISTE"))
    assert r.status_code == 200
    assert app_test.wa.textos_a("5493511111111"), "el dueño tiene que recibir algo"


def test_boton_con_payload_basura(app_test):
    for payload in ("", "sinseparador", "accion_rara:SO-1", ":SO-1", "ok:"):
        app_test.wa.mensajes.clear()
        postear(app_test, boton(payload, mid=f"wamid.{payload or 'vacio'}"))
        assert not app_test.erp.enviados_submit
        assert app_test.wa.textos_a("5493511111111")


# --------------------------------------------------------------------------
# Robustez del payload
# --------------------------------------------------------------------------


def test_payloads_raros_no_tiran_la_app(app_test):
    """Meta manda status updates y estructuras que no son mensajes. Nada de
    esto puede ser un 500: un 500 hace que Meta reintente y al final
    deshabilite el webhook."""
    for payload in (
        {},
        {"entry": []},
        {"entry": [{}]},
        {"entry": [{"changes": []}]},
        {"entry": [{"changes": [{}]}]},
        {"entry": [{"changes": [{"value": {}}]}]},
        {"entry": [{"changes": [{"value": {"statuses": [{"status": "read"}]}}]}]},
        {"entry": [{"changes": [{"value": {"messages": []}}]}]},
        {"entry": [{"changes": [{"value": {"messages": [{"id": "x"}]}}]}]},
        {"entry": None},
    ):
        r = postear(app_test, payload)
        assert r.status_code == 200, payload


def test_body_que_no_es_json(app_test):
    cuerpo = b"esto no es json"
    r = app_test.post(
        "/webhook/whatsapp",
        content=cuerpo,
        headers={"X-Hub-Signature-256": firmar(cuerpo)},
    )
    assert r.status_code == 200


def test_varios_mensajes_en_un_payload(app_test):
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.1",
                                    "from": "5493519999999",
                                    "type": "text",
                                    "text": {"body": "uno"},
                                },
                                {
                                    "id": "wamid.2",
                                    "from": "5493518888888",
                                    "type": "text",
                                    "text": {"body": "dos"},
                                },
                            ]
                        }
                    }
                ]
            }
        ]
    }
    postear(app_test, payload)
    assert len(app_test.agente.llamadas_cliente) == 2


def test_health_no_depende_de_nada(app_test):
    app_test.redis.caido = True
    assert app_test.get("/health").status_code == 200


def test_ready_avisa_cuando_redis_esta_caido(app_test):
    app_test.redis.caido = True
    r = app_test.get("/ready")
    assert r.status_code == 503
    assert r.json()["redis"] is False
