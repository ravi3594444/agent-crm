"""HTTP dashboard -> production ERPNext client -> repository's Frappe test double."""
from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import avisos, dashboard, erpnext, limites, locks, outbound_status, reloj
from demo import datos
from demo.falso_erpnext import DEPOSITO, EMPRESA, Almacen, manejar

TOKEN = "integration-dashboard-token-with-enough-entropy"
TODAY = date(2026, 9, 10)


@pytest.fixture
def connected(monkeypatch):
    monkeypatch.setenv("ERPNEXT_COMPANY", EMPRESA)
    monkeypatch.setenv("ERPNEXT_WAREHOUSE", DEPOSITO)
    monkeypatch.setenv("DASHBOARD_API_TOKEN", TOKEN)
    monkeypatch.setenv("ERPNEXT_PUBLIC_URL", "https://crm.example")
    monkeypatch.setattr(reloj, "ahora", lambda: datetime(2026, 9, 10, 12, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires")))
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


@pytest.fixture
def almacen_con_cliente(connected):
    """El cliente habitual, con un pedido de ESTA empresa y su WhatsApp.

    El pedido no es decorado: la pertenencia de un `Customer` —que en ERPNext
    es un maestro global sin campo `company`— se comprueba contra sus pedidos,
    así que sin uno este cliente no es nuestro y la ruta contesta 404.
    """
    client, almacen, _ = connected
    create_order(almacen, docstatus=1)
    return client, {
        "cliente": datos.CLIENTE_HABITUAL,
        "telefono": datos.TELEFONO_HABITUAL,
        "almacen": almacen,
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
    """
    from langchain_core.messages import HumanMessage

    client, registrar = almacen_con_cliente
    # 2026-09-10 21:30 en Buenos Aires = 2026-09-11 00:30 UTC.
    reg = _hilo(monkeypatch, [HumanMessage(content="mandame 10 sachets")],
                sellos=["2026-09-11T00:30:00+00:00"])
    reg(registrar["telefono"])
    monkeypatch.setattr(
        reloj, "ahora",
        lambda: datetime(2026, 9, 10, 21, 45, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires")),
    )

    cuerpo = client.get("/api/dashboard/today").json()

    assert cuerpo["date"] == "2026-09-10"
    assert [c["customerId"] for c in cuerpo["conversations"]] == [registrar["cliente"]]


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
