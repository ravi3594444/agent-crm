"""HTTP dashboard -> production ERPNext client -> repository's Frappe test double."""
from __future__ import annotations

import importlib.util
import json
import threading
from datetime import UTC, date, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from conftest import RelojDePrueba
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import (
    avisos,
    dashboard,
    erpnext,
    limites,
    locks,
    outbound_status,
    reloj,
    router,
)
from demo import datos
from demo.falso_erpnext import DEPOSITO, EMPRESA, Almacen, manejar

TOKEN = "integration-dashboard-token-with-enough-entropy"
TODAY = date(2026, 9, 10)
# El día que este archivo NOMBRA, con sus horas de pared en la zona que el
# negocio tenga configurada. Escribir la zona a mano acá es lo que hacía que
# la celda `BUSINESS_TIMEZONE=Asia/Kolkata` de CI probara otra cosa que el
# código: ver `RelojDePrueba` en tests/conftest.py.
RELOJ = RelojDePrueba(TODAY.isoformat())
# Un token POR PERSONA y el teléfono al que pertenece. El largo no es
# decorativo: `dashboard.TOKEN_MINIMO` descarta cualquier entrada más corta,
# así que un token de juguete daría 401 y el test mediría eso y no el permiso.
TOKEN_PERSONA = "t" * 44
GERENTE = "5493511111111"


@pytest.fixture
def connected(monkeypatch):
    monkeypatch.setenv("ERPNEXT_COMPANY", EMPRESA)
    monkeypatch.setenv("ERPNEXT_WAREHOUSE", DEPOSITO)
    monkeypatch.setenv("DASHBOARD_API_TOKEN", TOKEN)
    monkeypatch.setenv("ERPNEXT_PUBLIC_URL", "https://crm.example")
    monkeypatch.setattr(reloj, "ahora", lambda: RELOJ.a_las(12))
    almacen = Almacen()
    datos.sembrar(almacen, hoy=TODAY)
    calls = []

    def transport(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "token demo-gerencia-key:demo-gerencia-secret"
        code, body = manejar(
            almacen, request.method, request.url.path,
            parse_qs(request.url.query.decode()), request.content,
            request.headers["authorization"],
        )
        return httpx.Response(code, json=body)

    manager = httpx.Client(
        base_url="https://erpnext.test",
        headers={"Authorization": "token demo-gerencia-key:demo-gerencia-secret"},
        transport=httpx.MockTransport(transport),
    )
    monkeypatch.setattr(erpnext, "_manager_client", manager)

    def denied(*args, **kwargs):
        raise AssertionError("Dashboard record reads must not use customer/policy identity")

    monkeypatch.setattr(erpnext, "_client", type("Denied", (), {"request": denied})())
    monkeypatch.setattr(erpnext, "_policy", denied)
    app = FastAPI()
    dashboard.install_dashboard(app)
    with TestClient(app, base_url="https://agent.example") as client:
        client.headers["Authorization"] = "Bearer " + TOKEN
        yield client, almacen, calls
    manager.close()


def create_order(almacen, *, days=0, company=EMPRESA, docstatus=0, customer=None):
    customer = customer or datos.CLIENTE_HABITUAL
    return almacen.crear("Sales Order", {
        "company": company, "customer": customer,
        "customer_name": customer,
        "transaction_date": (TODAY - timedelta(days=days)).isoformat(),
        "delivery_date": (TODAY + timedelta(days=1)).isoformat(),
        "docstatus": docstatus, "status": "Draft" if not docstatus else "To Deliver and Bill",
        "currency": "ARS", "selling_price_list": "Standard Selling",
        "shipping_address": "<p>Belgrano 1200</p><p>Córdoba</p>",
        "items": [{"item_code": "LECHE-ENT-1L", "qty": 10, "rate": 1250, "uom": "Unidad"}],
    }, puede_confirmar=True)


def test_installed_dashboard_and_api_enforce_the_http_boundary(connected):
    client, _, calls = connected
    client.headers.pop("Authorization")
    page = client.get("/dashboard/")
    assert page.status_code == 200
    assert 'src="./app.js"' in page.text
    script = client.get("/dashboard/app.js")
    assert script.status_code == 200
    assert "disconnectedData()" in script.text
    config = client.get("/api/dashboard/config")
    assert config.json() == {"service": "plus-agent", "apiVersion": 1, "configured": True}
    for route in ("snapshot", "controls", "operations", "orders/SAL-ORD-2026-00001"):
        assert client.get("/api/dashboard/" + route).status_code == 401
    assert calls == []


def test_live_snapshot_reads_real_repository_contract_and_old_pending(connected):
    client, almacen, calls = connected
    recent = create_order(almacen, docstatus=1)
    old = create_order(almacen, days=60)
    other = create_order(almacen, company="Other Company")
    # Un cliente que SÓLO le compra a la otra empresa. `Customer` es un maestro
    # global en ERPNext —no tiene campo `company`—, así que preguntar por
    # clientes a secas lo devolvía igual.
    ajeno = create_order(
        almacen, company="Other Company", customer=datos.CLIENTE_MOROSO
    )
    response = client.get("/api/dashboard/snapshot")
    assert response.status_code == 200
    result = response.json()
    assert result["mode"] == "live"
    assert result["company"] == EMPRESA
    assert result["currency"] == "ARS"
    assert result["errors"] == []
    assert recent["name"] in {o["id"] for o in result["orders"]}
    assert old["name"] not in {o["id"] for o in result["orders"]}
    assert old["name"] in {o["id"] for o in result["pendingOrders"]}
    assert other["name"] not in {o["id"] for o in result["pendingOrders"]}
    milk = next(p for p in result["products"] if p["id"] == "LECHE-ENT-1L")
    assert milk["stock"] == 400 and milk["available"] == 400
    assert milk["name"] == "Leche entera sachet 1 L"
    # Los clientes salen de los pedidos de ESTA empresa, así que el que sólo le
    # compra a la otra no está — y el que sí nos compra, está. Las dos mitades:
    # afirmar sólo la ausencia lo cumple también una lista vacía.
    nombres = {c["id"] for c in result["customers"]}
    assert datos.CLIENTE_HABITUAL in nombres
    assert datos.CLIENTE_MOROSO not in nombres
    assert ajeno["name"] not in {o["id"] for o in result["pendingOrders"]}
    assert all(r.extensions["timeout"]["read"] == 3 for r in calls)
    assert response.headers["cache-control"] == "no-store"


def test_order_details_include_erp_items_and_enforce_company(connected):
    client, almacen, _ = connected
    own = create_order(almacen, docstatus=1)
    other = create_order(almacen, company="Other Company")
    result = client.get("/api/dashboard/orders/" + own["name"]).json()
    assert result["total"] == 12500
    assert result["items"][0]["qty"] == 10
    assert result["items"][0]["rate"] == 1250
    assert result["address"] == "Belgrano 1200 Córdoba"
    assert result["erpUrl"] == "https://crm.example/app/sales-order/" + own["name"]
    assert client.get("/api/dashboard/orders/" + other["name"]).status_code == 404
    assert client.get("/api/dashboard/orders/does-not-exist").status_code == 404
    assert client.post("/api/dashboard/orders/" + own["name"]).status_code == 405


def test_order_outage_is_retryable_and_not_reported_as_missing(connected, monkeypatch):
    client, _, _ = connected

    def outage(*args, **kwargs):
        raise erpnext.ERPNextError("private upstream error", status_code=503)

    monkeypatch.setattr(erpnext, "get_doc", outage)
    response = client.get("/api/dashboard/orders/SO-1")
    assert response.status_code == 502
    assert "private" not in response.text


def test_operations_reads_real_queue_keys_and_reports_outages(connected, monkeypatch):
    client, _, calls = connected
    counts = {
        "wa:{inbound}:queue": 3, "wa:{inbound}:dead": 2,
        outbound_status.DEAD_NOTIFY_KEY: 1,
    }
    redis = SimpleNamespace(
        ping=lambda: True,
        exists=lambda key: key == "wa:{inbound}:worker-lock",
        llen=lambda key: counts[key],
    )
    monkeypatch.setattr(outbound_status, "cliente", lambda: redis)
    monkeypatch.setattr(avisos, "pendientes", lambda: 4)
    response = client.get("/api/dashboard/operations")
    assert response.status_code == 200
    assert response.json() == {
        "redis": "Connected", "worker": "Active lease", "queuedMessages": 3,
        "failedReplies": 2, "failedNotices": 1, "queuedNotices": 4,
        "providerHealth": "Not probed",
    }
    monkeypatch.setattr(avisos, "pendientes", lambda: -1)
    assert client.get("/api/dashboard/operations").json()["queuedNotices"] is None

    def offline():
        raise ConnectionError("private Redis address")

    redis.ping = offline
    response = client.get("/api/dashboard/operations")
    assert response.status_code == 502
    assert "private" not in response.text
    assert calls == []


def test_record_limit_is_reported_and_does_not_hide_truncation(connected, monkeypatch):
    client, almacen, _ = connected
    for _ in range(3):
        create_order(almacen)
    monkeypatch.setattr(dashboard, "LIMIT", 2)
    result = client.get("/api/dashboard/snapshot").json()
    assert len(result["orders"]) == 2
    assert len(result["pendingOrders"]) == 2
    assert "orders" in result["truncated"]
    assert "pending orders" in result["truncated"]


def test_failed_dataset_is_unknown_and_not_empty(connected, monkeypatch):
    client, _, _ = connected
    original = erpnext.get_list

    def fail_inventory(doctype, **kwargs):
        if doctype == "Bin":
            raise erpnext.ERPNextError("private upstream text")
        return original(doctype, **kwargs)

    monkeypatch.setattr(erpnext, "get_list", fail_inventory)
    result = client.get("/api/dashboard/snapshot").json()
    assert result["products"] is None
    assert "inventory" in result["errors"]
    assert "private" not in str(result)


def test_controls_use_owner_values_and_preserve_lost_state_guard(connected, monkeypatch):
    client, _, _ = connected
    store = locks.conexion()
    store.hset(limites.CLAVE_VALORES, "AUTO_CONFIRM_MAX", "75000")
    response = client.get("/api/dashboard/controls")
    assert response.status_code == 200
    ceiling = next(p for p in response.json()["policies"] if p["id"] == "AUTO_CONFIRM_MAX")
    assert ceiling["value"] == "75000"
    assert ceiling["source"] == "dueño"
    store.hashes.clear()
    monkeypatch.setattr(limites, "_hubo_cambios_durables", lambda: True)
    response = client.get("/api/dashboard/controls")
    assert response.status_code == 502
    assert "75000" not in response.text


def test_snapshot_resolves_actual_model_alias(connected, monkeypatch):
    client, _, _ = connected
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    monkeypatch.delenv("QWEN_SALES_MODEL", raising=False)
    monkeypatch.setenv("LLM_MODEL_CLIENTES", "custom-sales-model")
    result = client.get("/api/dashboard/snapshot").json()
    assert result["agents"][0]["model"] == "custom-sales-model"
    assert result["agents"][0]["status"] == "Configured"


def test_setup_preserves_agent_configuration_and_does_not_rotate_token(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "dashboard_setup", Path(__file__).parents[1] / "deploy" / "configurar_dashboard.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / ".env"
    original = "ERPNEXT_COMPANY=Existing Company\nWHATSAPP_TOKEN=untouched\nDASHBOARD_API_TOKEN= # enable access with a dedicated random token\n"
    path.write_text(original)
    token = module.configure(path)
    assert len(token) >= 32
    assert "WHATSAPP_TOKEN=untouched\n" in path.read_text()
    assert "ERPNEXT_COMPANY=Existing Company\n" in path.read_text()
    assert module.configure(path) is None
    assert token in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text(f'DASHBOARD_API_TOKEN="{token}" # preserve this existing token\n')
    assert module.configure(path) is None


def _confirmable(monkeypatch, resultado=None, registro=None):
    """`decisiones.confirmar` de mentira, que ANOTA cómo la llamaron.

    Anota porque los tres argumentos son el contrato entero del endpoint: el
    pedido, QUIÉN decide —el teléfono que sale del token, no el token— y el
    canal que va a quedar firmado en ERPNext. Un doble que devolviera siempre lo
    mismo sin mirar los argumentos no podría discreparle al código sobre ninguno
    de los tres.
    """
    from app import decisiones

    llamadas = registro if registro is not None else []

    def falso(nombre, por, *, canal):
        llamadas.append({"pedido": nombre, "por": por, "canal": canal})
        return resultado or {"ok": True, "aviso_cliente": True, "detalle": f"✅ {nombre} confirmado."}

    monkeypatch.setattr(decisiones, "confirmar", falso)
    return llamadas


def test_el_panel_confirma_a_nombre_del_telefono_del_token(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """El endpoint pasa el TELÉFONO, no el token, y dice que viene del panel.

    Las tres mitades del contrato, y ninguna es decorativa: el pedido, la
    persona a cuyo nombre queda la decisión firmada en el historial de ERPNext,
    y el canal —sin el cual el rastro diría «mediante WhatsApp» sobre algo que
    pasó por una pantalla.
    """
    from app import decisiones

    client, registrar = almacen_con_cliente
    monkeypatch.setenv("DASHBOARD_TOKENS", f"{TOKEN_PERSONA}:{GERENTE}")
    # `router.STAFF` y no `TELEFONOS_EQUIPO` + `recargar()`: `recargar` escribe un
    # global que `monkeypatch` no deshace, así que el equipo se le quedaría puesto
    # al archivo siguiente. Es además lo único que leen las DOS puertas de
    # `es_equipo`, así que acá corre la autorización de verdad.
    monkeypatch.setattr(router, "STAFF", [GERENTE])
    llamadas = _confirmable(monkeypatch)

    respuesta = client.post(
        f"/api/dashboard/orders/{registrar['pedido']}/confirm",
        headers={"Authorization": f"Bearer {TOKEN_PERSONA}"},
    )

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["ok"] is True
    assert cuerpo["orderId"] == registrar["pedido"]
    assert cuerpo["customerNotified"] is True
    assert llamadas == [{
        "pedido": registrar["pedido"], "por": GERENTE, "canal": decisiones.CANAL_PANEL,
    }]


def test_el_pedido_se_confirma_con_el_nombre_del_documento_y_no_con_el_de_la_url(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """El nombre que sale del DOCUMENTO, no el texto que vino en la URL.

    ERPNext resuelve el nombre sin mirar mayúsculas —MariaDB colaciona así por
    defecto—, así que `POST /orders/so-ord-0001/confirm` encuentra el pedido y
    sigue de largo. Redis no colaciona: con ese texto crudo, la puerta arma
    `solicitud:so-ord-0001`, que NO es la misma llave que el
    `solicitud:SO-ORD-0001` que toma `solicitudes.crear` (lo saca de
    `so["name"]`) ni la que toma el botón de WhatsApp (`acciones.pedido_valido`
    termina en `.upper()`). Dos llaves para el mismo pedido es no tener
    exclusión mutua entre los dos canales, que es exactamente la carrera que ese
    lock cierra. El panel era el único camino que no canonizaba, y la lectura
    que trae el nombre bueno ya se hacía: se tiraba el resultado.

    El doble DERIVA DE LO QUE RECIBE y se afirma que lo recibió: si ignorara el
    `order_id` no podría discreparle al código sobre lo único que este test
    mide. Y se comprueba que la URL y el canónico difieren de verdad, porque con
    un pedido cuyo nombre ya está en mayúsculas el test pasaría solo.

    Mutación dirigida: volver a pasarle `order_id` a `decisiones.confirmar` en
    `confirmar_desde_el_panel`. Mata a éste y a ningún otro.
    """
    client, registrar = almacen_con_cliente
    canonico = registrar["pedido"]
    en_minuscula = canonico.lower()
    assert en_minuscula != canonico

    monkeypatch.setenv("DASHBOARD_TOKENS", f"{TOKEN_PERSONA}:{GERENTE}")
    monkeypatch.setattr(router, "STAFF", [GERENTE])

    buscados: list[str] = []

    def buscar(order_id):
        buscados.append(order_id)
        return {"name": canonico, "company": EMPRESA}

    monkeypatch.setattr(dashboard, "_pedido_de_la_empresa", buscar)
    llamadas = _confirmable(monkeypatch)

    respuesta = client.post(
        f"/api/dashboard/orders/{en_minuscula}/confirm",
        headers={"Authorization": f"Bearer {TOKEN_PERSONA}"},
    )

    assert respuesta.status_code == 200
    assert buscados == [en_minuscula]
    assert llamadas[0]["pedido"] == canonico
    assert respuesta.json()["orderId"] == canonico


def test_una_contraoferta_aprobada_no_se_informa_como_confirmada(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """`ok` no es «se confirmó», y el panel necesita los dos hechos separados.

    Con una contraoferta abierta la puerta aprueba la solicitud y le manda los
    términos al cliente: no hay Submit, el pedido sigue siendo un borrador
    reservando stock y el cliente todavía puede rechazarlo. Eso sale bien, así
    que `ok` es True. El panel leía esa clave y pintaba la fila como confirmada
    —`docs/prompt-panel.md` dice que `ok:false` es el caso de rechazo, o sea que
    no dejaba lugar para un tercer estado—, y mostraba una venta cerrada que no
    existe sobre el único endpoint que mueve plata.

    Las DOS mitades en el mismo test: que `submitted` sea False acá lo cumpliría
    igual un código que lo devuelve False siempre, y entonces ninguna
    confirmación real se podría pintar nunca.

    Mutación dirigida: `"submitted": bool(resultado.get("ok"))` en
    `confirmar_desde_el_panel`. Mata la primera mitad y deja la segunda verde.
    """
    client, registrar = almacen_con_cliente
    monkeypatch.setenv("DASHBOARD_TOKENS", f"{TOKEN_PERSONA}:{GERENTE}")
    monkeypatch.setattr(router, "STAFF", [GERENTE])

    _confirmable(monkeypatch, resultado={
        "ok": True,
        "emitido": False,
        "aviso_cliente": True,
        "detalle": "✅ registré «aprobada». Le mandé la oferta al cliente.",
    })
    derivada = client.post(
        f"/api/dashboard/orders/{registrar['pedido']}/confirm",
        headers={"Authorization": f"Bearer {TOKEN_PERSONA}"},
    ).json()

    assert derivada["ok"] is True
    assert derivada["submitted"] is False

    _confirmable(monkeypatch, resultado={
        "ok": True,
        "emitido": True,
        "aviso_cliente": True,
        "detalle": "✅ confirmado.",
    })
    emitida = client.post(
        f"/api/dashboard/orders/{registrar['pedido']}/confirm",
        headers={"Authorization": f"Bearer {TOKEN_PERSONA}"},
    ).json()

    assert emitida["ok"] is True
    assert emitida["submitted"] is True


def test_confirmar_no_ocupa_los_hilos_con_los_que_se_lee_el_panel(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """La escritura tiene su propio pool, y por eso una lectura no espera detrás.

    Una lectura son unos segundos; confirmar espera hasta 10 s por `confirmar:`,
    otros 10 por `solicitud:`, y después la cadena del submit contra timeouts de
    20 y 30 s — minutos, medido. Con los cuatro hilos del panel compartidos, dos
    confirmaciones lentas dejan a `/snapshot`, `/today` y `/queue` en la cola:
    `run_in_executor` encola sin límite y el cliente que se cansa no cancela
    nada, así que el panel se ve muerto para todo el mundo mientras el trabajo
    sigue ahí adentro.

    Se afirman los DOS lados, porque el nombre del pool de escritura solo no
    dice nada: la lectura tiene que seguir estando en el de lectura. Un cambio
    que mandara todo al pool nuevo cumple la primera mitad y rompe la razón de
    ser del pool de lectura, que es no compartirlo con el webhook de WhatsApp.

    Mutación dirigida: sacar `pool=_HILOS_ESCRITURA` de la llamada del confirm.
    Mata a este test y a ningún otro.
    """
    from app import decisiones

    client, registrar = almacen_con_cliente
    monkeypatch.setenv("DASHBOARD_TOKENS", f"{TOKEN_PERSONA}:{GERENTE}")
    monkeypatch.setattr(router, "STAFF", [GERENTE])

    hilos: dict[str, str] = {}

    def falso(nombre, por, *, canal):
        hilos["escritura"] = threading.current_thread().name
        return {"ok": True, "emitido": True, "aviso_cliente": True, "detalle": "ok"}

    monkeypatch.setattr(decisiones, "confirmar", falso)

    def leer():
        hilos["lectura"] = threading.current_thread().name
        return {"policies": []}

    monkeypatch.setattr(dashboard, "controls", leer)

    cabecera = {"Authorization": f"Bearer {TOKEN_PERSONA}"}
    assert client.post(
        f"/api/dashboard/orders/{registrar['pedido']}/confirm", headers=cabecera
    ).status_code == 200
    assert client.get("/api/dashboard/controls", headers=cabecera).status_code == 200

    assert hilos["escritura"].startswith("dashboard-write")
    assert hilos["lectura"].startswith("dashboard")
    assert not hilos["lectura"].startswith("dashboard-write")


def test_el_preflight_de_un_post_cross_origin_permite_el_content_type(
    connected, monkeypatch
) -> None:
    """Un `fetch` que manda JSON tiene que poder pasar el preflight.

    `Content-Type: application/json` no está en la lista segura de CORS, así
    que un POST escrito de la forma natural dispara un OPTIONS y el navegador
    lo rechaza si el header no está permitido. Lo que se ve entonces es que el
    botón Confirmar no hace nada: la petición no sale, no llega nada al
    servidor, no hay log en ninguna parte. La lista decía `Authorization` sola.

    Se afirma también `Authorization`, porque una lista que lo perdiera dejaría
    sin panel a todos los despliegues cross-origin y este test seguiría verde
    mirando sólo el header nuevo.
    """
    client, _, _ = connected
    monkeypatch.setenv("DASHBOARD_ALLOWED_ORIGINS", "https://panel.example")

    respuesta = client.options(
        "/api/dashboard/orders/SAL-ORD-0001/confirm",
        headers={
            "origin": "https://panel.example",
            "access-control-request-method": "POST",
            "access-control-request-headers": "authorization,content-type",
        },
    )

    assert respuesta.status_code == 200
    permitidos = respuesta.headers["access-control-allow-headers"].lower()
    assert "content-type" in permitidos
    assert "authorization" in permitidos
    assert "POST" in respuesta.headers["access-control-allow-methods"]


def test_un_confirm_del_mismo_host_pasa_con_el_proxy_terminando_TLS(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """El ASCENSO se acepta: el proceso en http, el navegador en https.

    El navegador omite `Origin` en un GET del mismo origen pero SIEMPRE lo manda
    en un POST, así que el botón Confirmar del propio panel pasa por esta
    comparación. Comparando el esquema a secas, pasaba sólo si el proceso veía
    el mismo que el navegador — y eso depende de que el proxy mande
    `X-Forwarded-Proto`. Donde no lo mande, el confirm da 403 y todas las
    lecturas siguen andando: «el botón no hace nada», sin configurar
    DASHBOARD_ALLOWED_ORIGINS.

    La URL absoluta en http es lo que hace que `scope["scheme"]` sea `http`, que
    es el despliegue que se está representando; el `Origin` en https es lo que
    ve el navegador del otro lado del proxy.
    """
    client, registrar = almacen_con_cliente
    monkeypatch.setenv("DASHBOARD_TOKENS", f"{TOKEN_PERSONA}:{GERENTE}")
    monkeypatch.setattr(router, "STAFF", [GERENTE])
    _confirmable(monkeypatch)

    respuesta = client.post(
        f"http://agent.example/api/dashboard/orders/{registrar['pedido']}/confirm",
        headers={
            "Authorization": f"Bearer {TOKEN_PERSONA}",
            "origin": "https://agent.example",
        },
    )

    assert respuesta.status_code == 200


def test_una_pagina_http_no_entra_al_panel_https_por_tener_el_mismo_host(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """La BAJADA no: `http://` y `https://` son dos orígenes, no uno.

    La otra mitad, y sin ella el arreglo de arriba se escribe como «el esquema
    no importa» — que es aceptar los dos sentidos y dejar que una página servida
    en http sobre este mismo host le hable al servicio https salteándose la
    lista exacta de `allowed_origin`. El panel lleva bearer y tiene una ruta que
    escribe, así que la lista existe por algo.

    El ascenso tiene un caso legítimo (el proxy termina TLS y el proceso se ve
    en http); la bajada no tiene ninguno. Hallazgo 3 de la review de Qodo.

    Mutación dirigida: sacar la condición `esquema == "http"` del `or`. Mata a
    este test y deja verde al de arriba.
    """
    client, registrar = almacen_con_cliente
    monkeypatch.setenv("DASHBOARD_TOKENS", f"{TOKEN_PERSONA}:{GERENTE}")
    monkeypatch.setattr(router, "STAFF", [GERENTE])
    llamadas = _confirmable(monkeypatch)

    respuesta = client.post(
        f"https://agent.example/api/dashboard/orders/{registrar['pedido']}/confirm",
        headers={
            "Authorization": f"Bearer {TOKEN_PERSONA}",
            "origin": "http://agent.example",
        },
    )

    assert respuesta.status_code == 403
    assert llamadas == []


def test_un_submit_que_no_se_pudo_verificar_no_se_informa_como_no_emitido(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """`submitted` viaja como `null` cuando el estado quedó sin comprobar.

    Un Submit puede commitear DESPUÉS de que el cliente HTTP se dio por vencido
    —el mecanismo tiene un `except` escrito para justamente eso— y si la
    relectura que lo verificaría tampoco contesta, nadie sabe en qué estado
    quedó el pedido. El texto para el encargado siempre dijo «no pude
    comprobar»; lo que no podía era viajar como un booleano, porque
    `bool(None)` es False y el panel dibuja un pedido posiblemente confirmado
    como borrador. `null` es la única respuesta que no afirma nada.

    Las DOS mitades: que lo incierto sea `null` Y que lo conocido siga siendo
    `false`, porque devolver `null` siempre cumpliría la primera y dejaría al
    panel sin poder distinguir nunca un rechazo de verdad.

    Hallazgo 2 de la review de Qodo — introducido por este mismo PR, que le
    agregó a un resultado honesto un booleano que no lo era.
    """
    client, registrar = almacen_con_cliente
    monkeypatch.setenv("DASHBOARD_TOKENS", f"{TOKEN_PERSONA}:{GERENTE}")
    monkeypatch.setattr(router, "STAFF", [GERENTE])
    cabecera = {"Authorization": f"Bearer {TOKEN_PERSONA}"}

    _confirmable(monkeypatch, resultado={
        "ok": False,
        "emitido": None,
        "aviso_cliente": False,
        "detalle": "No pude comprobar la confirmación. Revisalo en ERPNext.",
    })
    incierto = client.post(
        f"/api/dashboard/orders/{registrar['pedido']}/confirm", headers=cabecera
    ).json()

    assert incierto["ok"] is False
    assert incierto["submitted"] is None

    _confirmable(monkeypatch, resultado={
        "ok": False,
        "emitido": False,
        "aviso_cliente": False,
        "detalle": "Está Closed — se rechazó antes y ya no reserva stock.",
    })
    rechazado = client.post(
        f"/api/dashboard/orders/{registrar['pedido']}/confirm", headers=cabecera
    ).json()

    assert rechazado["ok"] is False
    assert rechazado["submitted"] is False


def test_el_token_compartido_puede_mirar_y_no_puede_confirmar(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """`ANONIMO` es `""`: falsy y VÁLIDO. Entra al panel y no decide.

    Es la trampa que `quien()` documenta. Una guarda escrita `if not mirando`
    trataría al token compartido como «no autenticado» y devolvería 401, que
    parece seguro y esconde el caso; lo que hace falta es que entre —porque es
    un token válido— y que se le diga que no en la línea de decidir. Y el
    endpoint no puede haber llamado a `confirmar` ni una vez.
    """
    client, registrar = almacen_con_cliente
    llamadas = _confirmable(monkeypatch)

    respuesta = client.post(
        f"/api/dashboard/orders/{registrar['pedido']}/confirm",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert respuesta.status_code == 403
    assert "cannot confirm" in respuesta.json()["error"]
    assert llamadas == []
    # La otra mitad: el MISMO token sigue leyendo, así que el 403 es de decidir
    # y no de estar mal autenticado.
    assert client.get("/api/dashboard/snapshot").status_code == 200


def test_un_token_de_alguien_que_no_es_del_equipo_tampoco_confirma(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """Tener nombre no alcanza: el nombre tiene que estar en el equipo.

    `puede_decidir` son DOS condiciones —un token con nombre Y `es_equipo`— y
    afirmar sólo la primera dejaría pasar a cualquiera con token propio.
    """
    client, registrar = almacen_con_cliente
    monkeypatch.setenv("DASHBOARD_TOKENS", f"{TOKEN_PERSONA}:5490000000000")
    # El equipo NO está vacío: con la lista vacía este test pasaría porque no
    # hay equipo, no porque este número quede afuera de uno que existe.
    monkeypatch.setattr(router, "STAFF", [GERENTE])
    llamadas = _confirmable(monkeypatch)

    respuesta = client.post(
        f"/api/dashboard/orders/{registrar['pedido']}/confirm",
        headers={"Authorization": f"Bearer {TOKEN_PERSONA}"},
    )

    assert respuesta.status_code == 403
    assert llamadas == []


def test_no_se_confirma_un_pedido_de_otra_empresa(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """404, y sin llamar a `confirmar`: un token válido no emite lo que no ve.

    Es la misma comprobación de pertenencia que la lectura, y tiene que correr
    ANTES de decidir nada: si corriera después, el pedido ya estaría emitido
    cuando el panel contesta que no existe.

    ESTE TEST NACIÓ ROTO Y LA MUTACIÓN LO CAZÓ. La primera versión usaba el
    token COMPARTIDO, así que moría en el 403 de `puede_decidir` y no llegaba
    nunca a la comprobación de empresa: borrar esa comprobación entera dejaba
    las 2889 pruebas en verde. Va con un token que SÍ puede decidir, que es la
    única forma de que lo único que quede entre él y el Submit sea la
    pertenencia.
    """
    client, _ = almacen_con_cliente
    monkeypatch.setenv("DASHBOARD_TOKENS", f"{TOKEN_PERSONA}:{GERENTE}")
    monkeypatch.setattr(router, "STAFF", [GERENTE])
    llamadas = _confirmable(monkeypatch)

    respuesta = client.post(
        "/api/dashboard/orders/SAL-ORD-DE-OTRA-EMPRESA/confirm",
        headers={"Authorization": f"Bearer {TOKEN_PERSONA}"},
    )

    assert respuesta.status_code == 404
    assert llamadas == []


def test_el_resto_del_panel_sigue_siendo_de_solo_lectura(
    connected, almacen_con_cliente
) -> None:
    """Se abrió UNA ruta, no el método.

    Un POST a cualquier otra cosa sigue siendo 405, y un GET a la ruta de
    confirmar también: la excepción es (ruta, método), no una de las dos.
    """
    client, registrar = almacen_con_cliente

    assert client.post("/api/dashboard/snapshot").status_code == 405
    assert client.post(f"/api/dashboard/orders/{registrar['pedido']}").status_code == 405
    assert client.get(
        f"/api/dashboard/orders/{registrar['pedido']}/confirm"
    ).status_code == 405


def _herramienta():
    spec = importlib.util.spec_from_file_location(
        "dashboard_setup", Path(__file__).parents[1] / "deploy" / "configurar_dashboard.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_el_token_que_escribe_la_herramienta_es_el_que_el_panel_acepta(
    tmp_path, monkeypatch
) -> None:
    """De punta a punta: lo que el script deja en el `.env` lo LEE `quien()`.

    Es la mitad que ningún test de la herramienta sola puede dar. El script
    escribe `<token>:<teléfono>` y el panel lo parsea con
    `dashboard.entradas_de_tokens`; si el formato se separara —una coma de más,
    el teléfono escrito de otra forma, un largo mínimo distinto— el token
    quedaría prolijo en el archivo y no entraría, sin que nada avise. Acá se
    escribe con uno y se entra con el otro.
    """
    modulo = _herramienta()
    numero = "+54 9 351 111 1111"
    ruta = tmp_path / ".env"
    ruta.write_text(f"TELEFONOS_EQUIPO={datos.TELEFONO_HABITUAL},5493511111111\n")

    token, normalizado = modulo.agregar_persona(ruta, numero)

    assert normalizado == "5493511111111", "lo guarda como lo busca el webhook"
    escrito = ruta.read_text()
    assert f"{token}:{normalizado}" in escrito
    assert ruta.stat().st_mode & 0o777 == 0o600

    # Y ahora el panel, leyendo lo mismo.
    monkeypatch.setenv(
        "DASHBOARD_TOKENS",
        escrito.split("DASHBOARD_TOKENS=", 1)[1].strip(),
    )
    assert dashboard.quien(f"Bearer {token}") == normalizado


def test_la_herramienta_no_mina_un_token_para_alguien_que_no_es_del_equipo(
    tmp_path,
) -> None:
    """Un dígito mal tipeado da un token que entra y no puede confirmar nada.

    `puede_decidir` exige `router.es_equipo`, así que un número que no está en
    `TELEFONOS_EQUIPO` produce un panel que «no anda» sin que nada lo explique.
    Se puede hacer a propósito —alguien que sólo mira— y para eso está la
    bandera; lo que no se puede es hacerlo sin querer.
    """
    modulo = _herramienta()
    ruta = tmp_path / ".env"
    ruta.write_text("TELEFONOS_EQUIPO=5493511111111\n")

    with pytest.raises(ValueError, match="no está en TELEFONOS_EQUIPO"):
        modulo.agregar_persona(ruta, "5493512222222")
    assert "DASHBOARD_TOKENS" not in ruta.read_text()

    token, numero = modulo.agregar_persona(ruta, "5493512222222", solo_lectura=True)
    assert f"{token}:{numero}" in ruta.read_text()


def test_la_herramienta_no_le_da_un_segundo_token_a_la_misma_persona(tmp_path) -> None:
    """Dos tokens vivos para una persona es uno que nadie sabe que existe.

    Y se comprueba contra el teléfono NORMALIZADO: pedirlo la segunda vez
    escrito distinto —con `+`, con espacios— es la misma persona, así que
    comparar los textos crudos dejaría pasar exactamente el caso que más se da.
    """
    modulo = _herramienta()
    ruta = tmp_path / ".env"
    ruta.write_text("TELEFONOS_EQUIPO=5493511111111\n")
    modulo.agregar_persona(ruta, "5493511111111")
    antes = ruta.read_text()

    with pytest.raises(ValueError, match="ya tiene un token"):
        modulo.agregar_persona(ruta, "+54 9 351 111-1111")

    assert ruta.read_text() == antes


def test_la_herramienta_agrega_sin_pisar_a_los_que_ya_estaban(tmp_path) -> None:
    """El segundo token no borra al primero: se agrega a la lista."""
    modulo = _herramienta()
    ruta = tmp_path / ".env"
    ruta.write_text(
        "TELEFONOS_EQUIPO=5493511111111,5493512222222\nWHATSAPP_TOKEN=intacto\n"
    )

    primero, _ = modulo.agregar_persona(ruta, "5493511111111")
    segundo, _ = modulo.agregar_persona(ruta, "5493512222222")

    escrito = ruta.read_text()
    assert primero in escrito and segundo in escrito
    assert escrito.count("DASHBOARD_TOKENS=") == 1
    assert "WHATSAPP_TOKEN=intacto\n" in escrito


@pytest.fixture
def almacen_con_cliente(connected):
    """El cliente habitual, con un pedido de ESTA empresa y su WhatsApp.

    El pedido no es decorado: la pertenencia de un `Customer` —que en ERPNext
    es un maestro global sin campo `company`— se comprueba contra sus pedidos,
    así que sin uno este cliente no es nuestro y la ruta contesta 404.
    """
    client, almacen, _ = connected
    # BORRADOR, no confirmado: los tests de lectura sólo necesitan que exista
    # para probar pertenencia, y los de confirmar necesitan algo confirmable.
    pedido = create_order(almacen, docstatus=0)
    return client, {
        "cliente": datos.CLIENTE_HABITUAL,
        "telefono": datos.TELEFONO_HABITUAL,
        "almacen": almacen,
        "pedido": pedido if isinstance(pedido, str) else str(pedido.get("name")),
    }


def _hilo(monkeypatch, mensajes, sellos=None):
    """Un checkpointer de mentira que HONRA el thread_id que le pasan.

    Devuelve mensajes sólo para el hilo del teléfono que se le declara, así que
    un código que buscara otro hilo —o que se armara el sha por su cuenta y se
    le fuera un byte— recibe vacío y el test cae. Un doble que contestara lo
    mismo para cualquier id no podría discrepar con el código sobre la única
    cosa que esta función hace: encontrar el hilo correcto.
    """
    from types import SimpleNamespace

    from app import graph

    sellos = sellos or [
        f"2026-09-10T12:0{i}:00+00:00" for i in range(len(mensajes))
    ]
    esperado = {}

    def registrar(telefono):
        from app.main import _thread_tag

        esperado["id"] = f"cli:{_thread_tag(telefono)}"

    def listar(config, limit=None):
        """El historial como lo devuelve LangGraph: del más NUEVO al más viejo.

        Un checkpoint por mensaje, con la lista creciendo, que es la forma real
        —y es la que hace que la fecha de cada mensaje sea la del checkpoint
        donde aparece por primera vez—. Un doble que devolviera un solo
        checkpoint con todo adentro no podría discrepar con el código sobre
        ese fechado, que es justo lo que el código hace.

        Y HONRA `limit`, que es lo que hace posible el caso del corte: sin eso
        el doble devolvía el hilo entero por mucho que el código pidiera una
        página, así que la rama que decide si la historia se cortó no se podía
        ejercitar. El mismo defecto que este archivo viene arreglando.
        """
        if config["configurable"]["thread_id"] != esperado.get("id"):
            return []
        historia = [
            SimpleNamespace(checkpoint={
                "channel_values": {"messages": mensajes[: i + 1]},
                "ts": sellos[i],
            })
            for i in range(len(mensajes))
        ]
        recientes = list(reversed(historia))
        return recientes if limit is None else recientes[:limit]

    def get_tuple(config):
        """El checkpoint MÁS NUEVO, que es lo que usa la lectura barata.

        El doble tiene que modelar los DOS accesos: `conversation` recorre el
        historial con `list` para fechar cada mensaje y `today` pide sólo el
        último con `get_tuple`. Un doble que implementara uno solo dejaría la
        mitad del código sin poder discrepar con él.
        """
        historia = listar(config)
        return historia[0] if historia else None

    monkeypatch.setattr(
        graph, "checkpointer",
        lambda: SimpleNamespace(list=listar, get_tuple=get_tuple),
    )
    return registrar


def test_una_transcripcion_no_muestra_lo_que_el_agente_hace_por_dentro(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """Las dos mitades: la conversación SE VE, y lo de adentro NO.

    Un `ToolMessage` trae la salida cruda de la herramienta y un `AIMessage`
    vacío es el turno en que el modelo la llamó. Devolver la lista tal cual le
    mostraba al dueño el stock interno y globos en blanco. Afirmar sólo que el
    texto crudo no está lo cumpliría también una respuesta vacía, que es la
    otra forma de estar roto.
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    client, registrar = almacen_con_cliente
    registrar_hilo = _hilo(monkeypatch, [
        HumanMessage(content="cuánta leche hay?"),
        AIMessage(content=""),
        ToolMessage(content="stock de leche: 12", tool_call_id="t1"),
        AIMessage(content="Hay 12 de leche."),
    ])
    registrar_hilo(registrar["telefono"])

    cuerpo = client.get(
        f"/api/dashboard/customers/{registrar['cliente']}/conversation"
    ).json()

    assert cuerpo["reachable"] is True
    assert [m["role"] for m in cuerpo["messages"]] == ["customer", "note", "agent"]
    assert cuerpo["messages"][0]["text"] == "cuánta leche hay?"
    assert cuerpo["messages"][2]["text"] == "Hay 12 de leche."
    # Y CADA MENSAJE TRAE SU HORA, que sale del checkpoint donde apareció por
    # primera vez: el `AIMessage` vacío se saltea, así que la nota hereda la
    # hora del ToolMessage —la tercera— y no la del globo que no se muestra.
    assert [m["at"] for m in cuerpo["messages"]] == [
        "2026-09-10T12:00:00+00:00",
        "2026-09-10T12:02:00+00:00",
        "2026-09-10T12:03:00+00:00",
    ]
    # Lo de adentro no sale, ni el número del stock ni el globo vacío.
    assert "stock de leche" not in json.dumps(cuerpo)
    assert all(m["text"].strip() for m in cuerpo["messages"])
    # Y el teléfono tampoco: se usa para encontrar el hilo y se descarta.
    assert registrar["telefono"] not in json.dumps(cuerpo)


def test_la_conversacion_de_un_cliente_de_otra_empresa_no_se_lee(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """`Customer` es un maestro GLOBAL: existe aunque no sea nuestro.

    La pertenencia se comprueba contra los pedidos de ESTA empresa, igual que
    en `snapshot`. Sin eso, un token de este panel leería las conversaciones de
    los clientes de otra empresa del mismo ERPNext — que es peor que ver sus
    nombres, porque son sus mensajes.
    """
    client, _ = almacen_con_cliente
    _hilo(monkeypatch, [])

    respuesta = client.get(
        f"/api/dashboard/customers/{datos.CLIENTE_MOROSO}/conversation"
    )

    assert respuesta.status_code == 404


def test_un_cliente_sin_whatsapp_no_es_un_error(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """Un cliente cargado a mano puede no tener número, y eso es un estado."""
    client, registrar = almacen_con_cliente
    _hilo(monkeypatch, [])
    registrar["almacen"].docs["Customer"][registrar["cliente"]]["mobile_no"] = ""

    cuerpo = client.get(
        f"/api/dashboard/customers/{registrar['cliente']}/conversation"
    ).json()

    assert cuerpo["reachable"] is False
    assert cuerpo["messages"] == []
    assert cuerpo["retentionDays"] >= 1


def test_la_cola_no_muestra_trabajo_de_otra_empresa(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """La consulta por empresa AUTORIZA la fila, no la decora.

    El índice de agenda es UNO SOLO para todo el sitio y no sabe de empresas.
    Usarlo para listar y después pedir los nombres sólo a los pedidos de ESTA
    empresa dejaba pasar las filas ajenas enteras —id de pedido, tipo de
    acción, hora y descripción— con el nombre vacío.

    Las dos mitades: la fila propia SÍ sale con su cliente —si no, un endpoint
    que devuelva siempre vacío pasaría igual— y la ajena no sale en absoluto.
    """
    from app import agenda, outbound_status

    client, registrar = almacen_con_cliente
    ajeno = create_order(registrar["almacen"], company="Other Company")
    propio = create_order(registrar["almacen"], docstatus=1)
    cliente_redis = outbound_status.cliente()
    ahora = 1_000_000.0
    monkeypatch.setattr(agenda, "_ahora", lambda: ahora)
    for pedido in (propio["name"], ajeno["name"]):
        fila = agenda.crear(
            pedido, agenda.SEGUIMIENTO, ahora + 3600,
            params={"por_que": "x"}, ahora=ahora,
        )
        assert fila is not None
    assert cliente_redis.zcard(agenda.CLAVE_INDICE) == 2, "las dos filas están en el índice"

    cuerpo = client.get("/api/dashboard/queue").json()

    pedidos = {p["orderId"] for p in cuerpo["upcoming"]}
    assert propio["name"] in pedidos
    assert ajeno["name"] not in pedidos, cuerpo["upcoming"]
    assert all(p["customer"] for p in cuerpo["upcoming"])


def test_una_charla_de_la_noche_sigue_siendo_de_hoy(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """El sello del checkpoint es UTC y el día del panel es el del negocio.

    A las 21:00 de Buenos Aires ya son las 00:00 del día siguiente en UTC, así
    que comparar los textos borraba de «Hoy» toda la franja en que un almacén
    cierra la caja y encarga para mañana. Se compara el día LOCAL.

    LA FRANJA SALE DE LA ZONA, NO ESTÁ ESCRITA A MANO, y ésa es la mitad que
    importa. Al oeste de Greenwich la charla que discrepa es la de la noche
    —23:30 locales ya son de mañana en UTC— y al este es la de la madrugada
    —00:30 locales todavía son de ayer—. Con Buenos Aires escrito acá el test
    fijaba el sentido occidental, y la celda `BUSINESS_TIMEZONE=Asia/Kolkata`
    de CI lo hacía caer con el código correcto: el sello caía en el día
    siguiente en las dos zonas, pero en Kolkata ese día siguiente también es el
    local. El momento se arma con `RELOJ`, que resuelve la zona por
    `reloj.zona()` —el mismo reloj que usa el panel—, así que lo que se afirma
    es la regla y no una zona.
    """
    from langchain_core.messages import HumanMessage

    client, registrar = almacen_con_cliente
    # El instante DEL NEGOCIO cuyo sello UTC cae en el otro día del calendario.
    charla = RELOJ.a_las(0, 30) if RELOJ.a_las(12).utcoffset() > timedelta(0) else RELOJ.a_las(23, 30)
    if charla.astimezone(UTC).date() == charla.date():
        pytest.skip("con el negocio en UTC no hay día local del que discrepar")
    reg = _hilo(monkeypatch, [HumanMessage(content="mandame 10 sachets")],
                sellos=[charla.astimezone(UTC).isoformat()])
    reg(registrar["telefono"])
    monkeypatch.setattr(reloj, "ahora", lambda: charla + timedelta(minutes=15))

    cuerpo = client.get("/api/dashboard/today").json()

    assert cuerpo["date"] == "2026-09-10"
    assert [c["customerId"] for c in cuerpo["conversations"]] == [registrar["cliente"]]


def test_una_charla_de_ayer_no_entra_en_hoy(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """La otra mitad de «Hoy»: el hilo existe y NO se movió hoy, así que no sale.

    La mitad que faltaba, y se midió: con el filtro del día borrado entero
    —`if _dia_local(sello) != hoy` cambiado por `if False`— la suite entera
    pasaba en las dos celdas de zona. O sea que nada impedía que «Hoy» listara
    a un cliente cuya última charla fue hace tres semanas, que es la pantalla
    que el dueño abre a la mañana para saber con quién habló el agente HOY.

    Y las dos fases son una sola cosa: afirmar nada más que la lista viene
    vacía lo cumple igual un hilo que no se encontró —teléfono mal normalizado,
    sha distinto, doble sin registrar—, que es la otra forma de estar roto y la
    que deja el vacío pareciendo correcto. Primero se ve al MISMO cliente con
    el MISMO doble apareciendo con el sello de hoy; recién entonces el vacío
    con el sello de ayer dice lo que el test dice.
    """
    from langchain_core.messages import HumanMessage

    client, registrar = almacen_con_cliente
    mensaje = [HumanMessage(content="ayer te pedí dos cajones")]

    reg = _hilo(monkeypatch, mensaje,
                sellos=[RELOJ.a_las(12).astimezone(UTC).isoformat()])
    reg(registrar["telefono"])
    de_hoy = client.get("/api/dashboard/today").json()
    assert [c["customerId"] for c in de_hoy["conversations"]] == [registrar["cliente"]]

    reg = _hilo(monkeypatch, mensaje,
                sellos=[RELOJ.a_las(12, dia=TODAY.day - 1).astimezone(UTC).isoformat()])
    reg(registrar["telefono"])

    cuerpo = client.get("/api/dashboard/today").json()

    assert cuerpo["date"] == "2026-09-10"
    assert cuerpo["conversations"] == []


def test_si_los_pedidos_no_se_pueden_leer_no_se_inventan_ventas_perdidas(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """La lista vacía convertía a TODOS en «habló y no compró».

    Esa fila es la que el dueño va a mirar —una venta perdida—, así que
    fabricarla por una lectura que falló es la peor forma de equivocarse acá.
    La ruta contesta 502 y la UI reintenta, en vez de devolver media pantalla
    que se lee como un hecho.
    """
    client, _ = almacen_con_cliente
    original = erpnext.get_list

    def falla_los_de_hoy(doctype, **kwargs):
        filtros = kwargs.get("filters") or []
        if doctype == "Sales Order" and any(
            f[0] == "transaction_date" and f[1] == "=" for f in filtros
        ):
            raise erpnext.ERPNextError("private upstream text")
        return original(doctype, **kwargs)

    monkeypatch.setattr(erpnext, "get_list", falla_los_de_hoy)

    respuesta = client.get("/api/dashboard/today")

    assert respuesta.status_code == 502
    assert "private" not in respuesta.text


def test_un_cliente_nuevo_sin_conversacion_igual_aparece(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """«Primer pedido hoy» es un hecho de ERPNext, no de Redis.

    Salir de `conversaciones` exigía además teléfono, hilo legible y un mensaje
    de hoy, así que el cliente nuevo cuyo pedido se cargó a mano no aparecía
    nunca — justo el que el dueño quiere ver.

    Y sin conversación no se le inventa una: cero turnos y sin última línea.
    """
    client, registrar = almacen_con_cliente
    _hilo(monkeypatch, [])
    nuevo = create_order(registrar["almacen"], customer=datos.CLIENTE_MOROSO)

    cuerpo = client.get("/api/dashboard/today").json()

    fila = next(c for c in cuerpo["newCustomers"] if c["customerId"] == datos.CLIENTE_MOROSO)
    assert fila["orderId"] == nuevo["name"]
    assert fila["turns"] == 0
    assert fila["lastLine"] == ""


def test_una_conversacion_mas_larga_que_la_historia_no_inventa_horas(
    connected, almacen_con_cliente, monkeypatch
) -> None:
    """El checkpoint más viejo que vuelve NO es el principio del hilo.

    Trae mensajes ya acumulados de antes de la página, y fecharlos con SU
    sello es exactamente la hora inventada que este endpoint existe para no
    dar — y encima dejaba `truncated` en falso, así que ni siquiera avisaba.
    Se pide UNO de más para poder distinguir «éste es el principio» de «acá se
    cortó».

    Las dos mitades: los viejos NO salen, y los nuevos SÍ con su hora — si
    saliera vacío también pasaría la primera.
    """
    from langchain_core.messages import HumanMessage

    client, registrar = almacen_con_cliente
    monkeypatch.setattr(dashboard, "HISTORIA_MAX", 3)
    mensajes = [HumanMessage(content=f"m{i}") for i in range(5)]
    sellos = [f"2026-09-10T12:0{i}:00+00:00" for i in range(5)]
    reg = _hilo(monkeypatch, mensajes, sellos=sellos)
    reg(registrar["telefono"])

    cuerpo = client.get(
        f"/api/dashboard/customers/{registrar['cliente']}/conversation"
    ).json()

    # Con la página en 3 se retienen los checkpoints de m2, m3 y m4; el más
    # viejo de ellos YA TRAE m0, m1 y m2 acumulados, así que los tres quedan
    # sin fecha propia y no salen. Sólo m3 y m4 aparecen por primera vez
    # adentro de la página, y son los únicos que se pueden fechar.
    assert [m["text"] for m in cuerpo["messages"]] == ["m3", "m4"]
    assert cuerpo["truncated"] is True
    assert all(m["at"] for m in cuerpo["messages"])


def test_el_tope_de_clientes_corta_por_recencia_y_no_por_alfabeto(
    connected, monkeypatch
) -> None:
    """ERPNext los devuelve por fecha descendente; un `sorted(set(...))` lo tiraba.

    Con más clientes que el tope, el corte pasaba a ser alfabético: el que
    compró HOY y se llama «Kiosco» quedaba afuera y entraba uno que no compra
    desde hace dos meses y se llama «Almacén». Justo al revés de lo que la
    pantalla dice ser.

    El tope se baja a 1 para que el corte sea lo único que decide, y las fechas
    son distintas a propósito: si el test dependiera del desempate entre dos
    pedidos del mismo día, estaría probando el orden del doble y no la regla.
    """
    from langchain_core.messages import HumanMessage

    client, almacen, _ = connected
    monkeypatch.setattr(dashboard, "HOY_MAX_CLIENTES", 1)
    # El habitual («Almacen…», alfabéticamente primero) compró hace 60 días; el
    # moroso («Kiosco…», alfabéticamente después) compró hoy.
    create_order(almacen, days=60)
    create_order(almacen, customer=datos.CLIENTE_MOROSO)
    almacen.docs["Customer"][datos.CLIENTE_MOROSO]["mobile_no"] = "5493511111333"
    reg = _hilo(monkeypatch, [HumanMessage(content="mandame 20")])
    reg("5493511111333")

    cuerpo = client.get("/api/dashboard/today").json()

    assert [c["customerId"] for c in cuerpo["conversations"]] == [datos.CLIENTE_MOROSO]
    assert "conversations" in cuerpo["truncated"]
