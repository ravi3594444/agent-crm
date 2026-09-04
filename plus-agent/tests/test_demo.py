"""El banco de pruebas también se prueba.

Un doble que miente es peor que no tener doble: un escenario pasa, alguien
concluye que el sistema está bien y el bug sale a producción. Así que acá se
verifica lo que el doble PROMETE parecerse a ERPNext y a Meta, y sobre todo se
verifica que las guardas realmente frenen algo.

Nada de esto necesita Docker ni red.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from demo import datos, escenarios, guardas, piloto
from demo import falso_erpnext as fe
from demo import falso_meta as fm
from demo import falso_modelo as md

AGENTE = "token demo-agente-key:demo-agente-secret"
GERENCIA = "token demo-gerencia-key:demo-gerencia-secret"
POLITICA = "token demo-politica-key:demo-politica-secret"


@pytest.fixture
def almacen() -> fe.Almacen:
    a = fe.Almacen()
    datos.sembrar(a)
    return a


def _pedir(a, metodo, ruta, consulta=None, cuerpo=None, auth=AGENTE):
    crudo = json.dumps(cuerpo).encode() if cuerpo is not None else b""
    listas = {k: (v if isinstance(v, list) else [v])
              for k, v in (consulta or {}).items()}
    return fe.manejar(a, metodo, ruta, listas, crudo, auth)


def _listar(a, doctype, filtros=None, campos=None, auth=AGENTE, **extra):
    consulta = {"limit_page_length": "100"}
    if filtros is not None:
        consulta["filters"] = json.dumps(filtros)
    if campos is not None:
        consulta["fields"] = json.dumps(campos)
    consulta.update({k: str(v) for k, v in extra.items()})
    estado, cuerpo = _pedir(a, "GET", f"/api/resource/{doctype}", consulta, auth=auth)
    assert estado == 200, cuerpo
    return cuerpo["data"]


# ------------------------------------------------------- los filtros de Frappe


def test_like_matches_the_way_sql_does(almacen: fe.Almacen) -> None:
    """Un `like` roto devuelve cero filas, que se lee como «no existe»."""
    nombres = [f["item_code"] for f in _listar(
        almacen, "Item", [["item_name", "like", "%leche%"]], ["item_code"])]
    assert "LECHE-ENT-1L" in nombres
    # "Dulce de leche" también contiene "leche": el LIKE no ancla.
    assert "DDL-400" in nombres
    assert "MANTECA-200" not in nombres


def test_like_is_case_and_position_insensitive(almacen: fe.Almacen) -> None:
    assert _listar(almacen, "Item", [["item_name", "like", "%MANTECA%"]], ["name"])
    assert _listar(almacen, "Item", [["item_name", "like", "Manteca%"]], ["name"])
    assert not _listar(almacen, "Item", [["item_name", "like", "anteca%"]], ["name"])


def test_the_interleaved_phone_pattern_finds_the_customer(almacen: fe.Almacen) -> None:
    """app/clientes.py busca así: %5%4%9%3%…%. Si falla, no reconoce a nadie."""
    tel = datos.TELEFONO_HABITUAL
    patron = "%" + "%".join(tel[-8:]) + "%"
    filas = _listar(almacen, "Customer", [["mobile_no", "like", patron]], ["name"])
    assert [f["name"] for f in filas] == [datos.CLIENTE_HABITUAL]


def test_every_operator_the_app_uses_is_supported(almacen: fe.Almacen) -> None:
    codigos = ["LECHE-ENT-1L", "MANTECA-200"]
    assert len(_listar(almacen, "Item", [["name", "in", codigos]], ["name"])) == 2
    assert len(_listar(almacen, "Item", [["name", "not in", codigos]], ["name"])) == 4
    assert len(_listar(almacen, "Item", [["item_code", "=", "DDL-400"]], ["name"])) == 1
    assert len(_listar(almacen, "Item", [["item_code", "!=", "DDL-400"]], ["name"])) == 5
    caros = _listar(almacen, "Bin", [["actual_qty", ">", 100]], ["item_code"])
    assert {f["item_code"] for f in caros} == {"LECHE-ENT-1L", "LECHE-DESC-1L",
                                              "YOG-FRUT-190", "DDL-400"}
    assert _listar(almacen, "Bin", [["actual_qty", ">=", 3], ["actual_qty", "<", 4]],
                   ["item_code"])[0]["item_code"] == datos.ITEM_SIN_STOCK


def test_an_unknown_operator_is_an_error_not_a_silent_empty(almacen: fe.Almacen) -> None:
    """Devolver [] ante un operador que no se entiende es mentir."""
    estado, cuerpo = _pedir(almacen, "GET", "/api/resource/Item",
                            {"filters": json.dumps([["name", "regex", "x"]])})
    assert estado == 417
    assert "regex" in cuerpo["exception"]


# ------------------------------------------------------------ orden y páginas


def test_comments_page_in_a_stable_newest_first_order(almacen: fe.Almacen) -> None:
    """La reconstrucción del índice de solicitudes pagina por creation desc.

    Sin un orden estable, paginar saltea y repite eventos, que es justo el bug
    que app/solicitudes.py evita.
    """
    for i in range(10):
        _pedir(almacen, "POST", "/api/resource/Comment", cuerpo={
            "comment_type": "Comment", "reference_doctype": "Sales Order",
            "reference_name": "SAL-ORD-0001", "content": f"[solicitud] n={i}"})
    paginas = []
    for pagina in range(4):
        filas = _listar(
            almacen, "Comment",
            [["reference_doctype", "=", "Sales Order"],
             ["content", "like", "%[solicitud]%"]],
            ["content", "creation"], auth=POLITICA,
            order_by="creation desc", limit_page_length=3,
            limit_start=pagina * 3)
        paginas.append([f["content"] for f in filas])
    vistos = [c for p in paginas for c in p]
    assert len(vistos) == len(set(vistos)), "una página repitió un evento"
    assert vistos[0] == "[solicitud] n=9", "el más nuevo no vino primero"
    assert len(vistos) == 10


def test_every_document_gets_a_distinct_creation_instant(almacen: fe.Almacen) -> None:
    """Dos eventos en el mismo microsegundo hacen que el orden sea azar."""
    for i in range(25):
        _pedir(almacen, "POST", "/api/resource/Comment", cuerpo={
            "reference_doctype": "Company", "reference_name": "x",
            "content": f"c{i}"})
    momentos = [d["creation"] for d in almacen.tabla("Comment").values()]
    assert len(set(momentos)) == len(momentos)


# ------------------------------------------------------------- tablas hijas


def test_a_child_table_cannot_be_listed_without_its_parent(almacen: fe.Almacen) -> None:
    """Frappe se niega igual; un doble permisivo esconde una llamada mal hecha."""
    estado, cuerpo = _pedir(almacen, "GET", "/api/resource/Sales Order Item",
                            {"limit_page_length": "5"}, auth=POLITICA)
    assert estado == 403
    assert "parent" in cuerpo["exception"]


def test_child_rows_inherit_the_parent_docstatus(almacen: fe.Almacen) -> None:
    """app/inventario.py sólo le cree a un conteo CONFIRMADO (docstatus 1)."""
    filas = _listar(almacen, "Stock Reconciliation Item",
                    [["item_code", "=", "LECHE-ENT-1L"], ["docstatus", "=", 1]],
                    ["parent", "docstatus"], auth=POLITICA,
                    parent="Stock Reconciliation")
    assert filas and all(f["docstatus"] == 1 for f in filas)

    _e, cuerpo = _pedir(almacen, "POST", "/api/resource/Stock Reconciliation",
                        cuerpo={"posting_date": "2026-09-01",
                                "items": [{"item_code": "DDL-400",
                                           "warehouse": fe.DEPOSITO, "qty": 1}]},
                        auth=POLITICA)
    borrador = cuerpo["data"]["name"]
    hijas = _listar(almacen, "Stock Reconciliation Item",
                    [["parent", "=", borrador]], ["docstatus"], auth=POLITICA,
                    parent="Stock Reconciliation")
    assert [f["docstatus"] for f in hijas] == [0], "un borrador no puede dar confianza"


def test_child_rows_carry_the_stock_uom_erpnext_would_copy(almacen: fe.Almacen) -> None:
    """app/policy.py::_precio_estandar exige uom == stock_uom en el renglón.

    ERPNext lo copia del Item al guardar. Sin ese campo NADA se auto-confirma
    nunca, y el banco de pruebas informaría un sistema roto que no lo está.
    """
    _estado, cuerpo = _pedir(almacen, "POST", "/api/resource/Sales Order", cuerpo={
        "customer": datos.CLIENTE_HABITUAL, "delivery_date": "2026-09-10",
        "items": [{"item_code": "LECHE-ENT-1L", "qty": 2, "uom": "Unidad",
                   "warehouse": fe.DEPOSITO}]})
    fila = cuerpo["data"]["items"][0]
    assert fila["stock_uom"] == "Unidad" == fila["uom"]
    assert fila["conversion_factor"] == 1


# -------------------------------------------------------- la frontera de permisos


@pytest.mark.parametrize("auth,quien", [(AGENTE, "agente"), (GERENCIA, "gerencia")])
def test_only_the_policy_credential_can_submit(almacen: fe.Almacen, auth, quien) -> None:
    """La frontera que el sistema real delega en ERPNext. Si el doble no la
    tiene, el banco de pruebas no prueba nada sobre permisos."""
    _e, cuerpo = _pedir(almacen, "POST", "/api/resource/Sales Order", cuerpo={
        "customer": datos.CLIENTE_HABITUAL, "delivery_date": "2026-09-10",
        "items": [{"item_code": "DDL-400", "qty": 1, "uom": "Unidad",
                   "warehouse": fe.DEPOSITO}]})
    nombre = cuerpo["data"]["name"]
    estado, cuerpo = _pedir(almacen, "PUT", f"/api/resource/Sales Order/{nombre}",
                            cuerpo={"docstatus": 1}, auth=auth)
    assert estado == 403, f"{quien} pudo confirmar"
    assert almacen.leer("Sales Order", nombre)["docstatus"] == 0

    estado, _c = _pedir(almacen, "PUT", f"/api/resource/Sales Order/{nombre}",
                        cuerpo={"docstatus": 1}, auth=POLITICA)
    assert estado == 200
    assert almacen.leer("Sales Order", nombre)["docstatus"] == 1


def test_an_unknown_credential_is_refused_without_echoing_it(almacen: fe.Almacen) -> None:
    estado, cuerpo = _pedir(almacen, "GET", "/api/resource/Item",
                            auth="token robada-key:robada-secret")
    assert estado == 401
    assert "robada" not in json.dumps(cuerpo)


def test_a_submitted_document_cannot_go_back_to_draft(almacen: fe.Almacen) -> None:
    _e, cuerpo = _pedir(almacen, "POST", "/api/resource/Sales Order", cuerpo={
        "customer": datos.CLIENTE_HABITUAL, "delivery_date": "2026-09-10",
        "items": [{"item_code": "DDL-400", "qty": 1, "uom": "Unidad",
                   "warehouse": fe.DEPOSITO}]})
    nombre = cuerpo["data"]["name"]
    _pedir(almacen, "PUT", f"/api/resource/Sales Order/{nombre}",
           cuerpo={"docstatus": 1}, auth=POLITICA)
    estado, _c = _pedir(almacen, "PUT", f"/api/resource/Sales Order/{nombre}",
                        cuerpo={"docstatus": 0}, auth=POLITICA)
    assert estado == 417


def test_only_a_draft_can_be_deleted(almacen: fe.Almacen) -> None:
    """app/erpnext.py::policy_delete_doc ya lo verifica; el doble también."""
    _e, cuerpo = _pedir(almacen, "POST", "/api/resource/Delivery Note", cuerpo={
        "customer": datos.CLIENTE_HABITUAL,
        "items": [{"item_code": "DDL-400", "qty": 1, "uom": "Unidad",
                   "warehouse": fe.DEPOSITO}]}, auth=POLITICA)
    nombre = cuerpo["data"]["name"]
    estado, _c = _pedir(almacen, "DELETE", f"/api/resource/Delivery Note/{nombre}",
                        auth=POLITICA)
    assert estado == 202
    _pedir(almacen, "POST", "/api/resource/Delivery Note", cuerpo={
        "customer": datos.CLIENTE_HABITUAL, "name": "MAT-DN-FIJO",
        "items": [{"item_code": "DDL-400", "qty": 1, "uom": "Unidad",
                   "warehouse": fe.DEPOSITO}]}, auth=POLITICA)
    _pedir(almacen, "PUT", "/api/resource/Delivery Note/MAT-DN-FIJO",
           cuerpo={"docstatus": 1}, auth=POLITICA)
    estado, _c = _pedir(almacen, "DELETE", "/api/resource/Delivery Note/MAT-DN-FIJO",
                        auth=POLITICA)
    assert estado == 417


# ----------------------------------------------------------------- los totales


def test_a_line_with_no_rate_is_priced_off_the_selling_list(almacen: fe.Almacen) -> None:
    """app/tools/pedidos.py manda item_code, qty y uom y NADA MÁS, a propósito.

    Si el doble dejara la línea en cero, todo pedido valdría cero y los topes
    del dueño se evaluarían contra la nada: pasarían sin probar ningún límite.
    """
    _e, cuerpo = _pedir(almacen, "POST", "/api/resource/Sales Order", cuerpo={
        "customer": datos.CLIENTE_HABITUAL, "delivery_date": "2026-09-10",
        "selling_price_list": fe.LISTA_PRECIOS,
        "items": [{"item_code": "LECHE-ENT-1L", "qty": 10, "uom": "Unidad",
                   "warehouse": fe.DEPOSITO}]})
    doc = cuerpo["data"]
    assert doc["items"][0]["rate"] == 1250.0
    assert doc["items"][0]["price_list_rate"] == 1250.0
    assert doc["grand_total"] == 12500.0


def test_adding_a_charge_raises_the_grand_total_by_exactly_that_amount(
    almacen: fe.Almacen,
) -> None:
    """Es lo único que app/erpnext.py::policy_agregar_cargo lee de vuelta y
    exige. Si el doble no lo cumple, el cargo se reporta como no aplicado."""
    _e, cuerpo = _pedir(almacen, "POST", "/api/resource/Sales Order", cuerpo={
        "customer": datos.CLIENTE_HABITUAL, "delivery_date": "2026-09-10",
        "items": [{"item_code": "DDL-400", "qty": 4, "uom": "Unidad",
                   "warehouse": fe.DEPOSITO}]})
    nombre, antes = cuerpo["data"]["name"], cuerpo["data"]["grand_total"]
    _e, cuerpo = _pedir(almacen, "PUT", f"/api/resource/Sales Order/{nombre}",
                        cuerpo={"taxes": [{"charge_type": "Actual",
                                           "account_head": "Fletes - LD",
                                           "description": "Envío",
                                           "tax_amount": 1500.0}]},
                        auth=POLITICA)
    assert cuerpo["data"]["grand_total"] == pytest.approx(antes + 1500.0)


def test_a_document_discount_lowers_the_total(almacen: fe.Almacen) -> None:
    _e, cuerpo = _pedir(almacen, "POST", "/api/resource/Sales Order", cuerpo={
        "customer": datos.CLIENTE_HABITUAL, "delivery_date": "2026-09-10",
        "items": [{"item_code": "LECHE-ENT-1L", "qty": 10, "uom": "Unidad",
                   "warehouse": fe.DEPOSITO}]})
    nombre = cuerpo["data"]["name"]
    _e, cuerpo = _pedir(almacen, "PUT", f"/api/resource/Sales Order/{nombre}",
                        cuerpo={"additional_discount_percentage": 10.0},
                        auth=POLITICA)
    assert cuerpo["data"]["grand_total"] == pytest.approx(11250.0)


def test_the_receivables_report_always_carries_a_due_date(almacen: fe.Almacen) -> None:
    """app/policy.py levanta ERPNextError si falta: sin fecha no puede saber
    si la deuda está vencida, y no lo adivina."""
    estado, cuerpo = _pedir(
        almacen, "GET", "/api/method/frappe.desk.query_report.run",
        {"report_name": "Accounts Receivable", "filters": json.dumps({})},
        auth=POLITICA)
    assert estado == 200
    filas = cuerpo["message"]["result"]
    assert filas
    for fila in filas:
        assert fila["due_date"]
        assert fila["outstanding_amount"] > 0


def test_the_seeded_debt_belongs_only_to_the_delinquent_customer(
    almacen: fe.Almacen,
) -> None:
    """El cliente habitual no puede tener deuda: bloquearía la auto-confirmación."""
    _e, cuerpo = _pedir(
        almacen, "GET", "/api/method/frappe.desk.query_report.run",
        {"report_name": "Accounts Receivable",
         "filters": json.dumps({"customer": [datos.CLIENTE_HABITUAL]})},
        auth=POLITICA)
    assert cuerpo["message"]["result"] == []


def test_the_seeded_history_lets_auto_confirmation_be_possible(
    almacen: fe.Almacen,
) -> None:
    """app/policy.py exige 3 pedidos confirmados y total <= 2x el promedio."""
    filas = _listar(almacen, "Sales Order",
                    [["customer", "=", datos.CLIENTE_HABITUAL],
                     ["docstatus", "=", 1]], ["grand_total"])
    importes = [f["grand_total"] for f in filas]
    assert len(importes) >= 3
    assert (sum(importes) / len(importes)) * 2.0 >= 12500.0


# --------------------------------------------------------------- el doble de Meta


def test_a_send_is_only_accepted_with_a_non_empty_message_id() -> None:
    """Es TODO lo que app/whatsapp.py inspecciona para dar un envío por bueno."""
    buzon = fm.Buzon()
    estado, cuerpo, _cab = fm.manejar(
        buzon, "POST", "/v21.0/demo-phone/messages", {},
        {"messaging_product": "whatsapp", "to": "5493511110001", "type": "text",
         "text": {"body": "hola"}}, "demo-phone")
    assert estado == 200
    assert isinstance(cuerpo["messages"][0]["id"], str)
    assert cuerpo["messages"][0]["id"].strip()
    assert buzon.textos("5493511110001") == ["hola"]


def test_a_send_to_another_phone_id_is_not_captured() -> None:
    buzon = fm.Buzon()
    estado, _c, _cab = fm.manejar(buzon, "POST", "/v21.0/otro/messages", {},
                                  {"messaging_product": "whatsapp", "to": "1"},
                                  "demo-phone")
    assert estado == 404
    assert not buzon.envios


def test_a_programmed_failure_looks_like_a_real_meta_rejection() -> None:
    """app/whatsapp.py::es_permanente decide con el status y el código."""
    buzon = fm.Buzon()
    buzon.programar_falla(400, 131047)   # ventana de 24 h cerrada: permanente
    buzon.programar_falla(429, 130429)   # límite de tasa: transitorio
    primero, cuerpo, _cab = fm.manejar(
        buzon, "POST", "/v21.0/demo-phone/messages", {},
        {"messaging_product": "whatsapp", "to": "549", "type": "text",
         "text": {"body": "x"}}, "demo-phone")
    assert primero == 400
    assert cuerpo["error"]["code"] == 131047
    segundo, _c, cabeceras = fm.manejar(
        buzon, "POST", "/v21.0/demo-phone/messages", {},
        {"messaging_product": "whatsapp", "to": "549", "type": "text",
         "text": {"body": "x"}}, "demo-phone")
    assert segundo == 429
    assert cabeceras["retry-after"] == "2"
    # gastadas las dos, el tercero pasa
    tercero, _c, _cab = fm.manejar(
        buzon, "POST", "/v21.0/demo-phone/messages", {},
        {"messaging_product": "whatsapp", "to": "549", "type": "text",
         "text": {"body": "x"}}, "demo-phone")
    assert tercero == 200


def test_a_template_send_is_recorded_with_its_name_and_parameters() -> None:
    buzon = fm.Buzon()
    fm.manejar(buzon, "POST", "/v21.0/demo-phone/messages", {}, {
        "messaging_product": "whatsapp", "to": "549", "type": "template",
        "template": {"name": "pedido_pendiente_equipo", "language": {"code": "es_AR"},
                     "components": [
                         {"type": "body", "parameters": [
                             {"type": "text", "text": "SAL-ORD-1"},
                             {"type": "text", "text": "$1.000"}]},
                         {"type": "button", "sub_type": "quick_reply", "index": "0",
                          "parameters": [{"type": "payload", "payload": "ok:SAL-ORD-1"}]}]},
    }, "demo-phone")
    envio = buzon.envios[0]
    assert envio.plantilla == "pedido_pendiente_equipo"
    assert envio.parametros == ["SAL-ORD-1", "$1.000"]
    assert envio.botones == ["ok:SAL-ORD-1"]


# -------------------------------------------------------------- el doble del modelo


def _payload(mensajes: list[dict], modelo: str = "gemini-3.5-flash") -> dict:
    return {"model": modelo, "messages": mensajes}


def test_the_script_walks_its_steps_one_request_at_a_time() -> None:
    """El protocolo de OpenAI es sin estado: el paso se deduce de la request."""
    reglas = [(md.contiene("dame leche"),
               [md.Llamada("buscar_producto", {"consulta": "leche"}),
                md.Texto("listo")])]
    sistema = {"role": "system", "content": "Sos el bot de ventas"}
    usuario = {"role": "user", "content": "dame leche"}

    primero = md.responder(reglas, _payload([sistema, usuario]))
    llamada = primero["choices"][0]["message"]["tool_calls"][0]
    assert llamada["function"]["name"] == "buscar_producto"
    assert primero["choices"][0]["finish_reason"] == "tool_calls"

    segundo = md.responder(reglas, _payload([
        sistema, usuario,
        {"role": "assistant", "content": None, "tool_calls": [llamada]},
        {"role": "tool", "tool_call_id": llamada["id"], "content": "LECHE-ENT-1L"}]))
    assert segundo["choices"][0]["message"]["content"] == "listo"
    assert segundo["choices"][0]["finish_reason"] == "stop"


def test_a_message_with_no_rule_says_so_instead_of_inventing() -> None:
    respuesta = md.responder([], _payload([{"role": "user", "content": "qué tal"}]))
    assert "no tengo un guion" in respuesta["choices"][0]["message"]["content"].lower()


def test_the_script_can_tell_the_two_agents_apart() -> None:
    """El mismo texto significa otra cosa según quién escribe."""
    reglas = [
        (lambda t, rol: rol == "gerencia", [md.Texto("hola jefe")]),
        (lambda t, rol: rol == "clientes", [md.Texto("hola cliente")]),
    ]
    ger = md.responder(reglas, _payload([
        {"role": "system", "content": "Asistente de GERENCIA del negocio"},
        {"role": "user", "content": "hola"}]))
    cli = md.responder(reglas, _payload([
        {"role": "system", "content": "Atendés clientes por WhatsApp"},
        {"role": "user", "content": "hola"}]))
    assert ger["choices"][0]["message"]["content"] == "hola jefe"
    assert cli["choices"][0]["message"]["content"] == "hola cliente"


def test_the_order_placeholder_is_resolved_from_the_tool_output() -> None:
    """En el guión no se puede escribir un número que todavía no existe."""
    reglas = [(md.contiene("el domingo"),
               [md.Llamada("pedir_excepcion_de_entrega",
                           {"numero_de_pedido": md.ULTIMO_PEDIDO,
                            "lo_que_pidio_el_cliente": "el domingo"})])]
    respuesta = md.responder(reglas, _payload([
        {"role": "user", "content": "el domingo"},
        {"role": "tool", "content": "PEDIDO_PENDIENTE SAL-ORD-2026-00042 creado "
                                    "con 10 x YOG-FRUT-190"}]))
    argumentos = json.loads(
        respuesta["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
    assert argumentos["numero_de_pedido"] == "SAL-ORD-2026-00042", (
        "confundió el código de producto con el número de pedido")


def test_the_reply_can_echo_the_tool_result_verbatim() -> None:
    """Un texto propio taparía que la herramienta falló."""
    reglas = [(md.contiene("estado"),
               [md.Llamada("estado_del_sistema", {}),
                md.Texto(f"Estado:\n{md.ULTIMO_RESULTADO}")])]
    llamada = {"id": "c1", "type": "function",
               "function": {"name": "estado_del_sistema", "arguments": "{}"}}
    respuesta = md.responder(reglas, _payload([
        {"role": "user", "content": "estado"},
        {"role": "assistant", "content": None, "tool_calls": [llamada]},
        {"role": "tool", "tool_call_id": "c1",
         "content": "Ese número no está autorizado"}]))
    assert "no está autorizado" in respuesta["choices"][0]["message"]["content"]


def test_the_relay_never_forwards_the_callers_authorization() -> None:
    """La clave real vive SÓLO en el relevo; la del agente es de mentira."""
    relevo = md.Relevo("https://proveedor.invalid/v1", clave="la-real")
    enviados: dict = {}

    class _Cliente:
        def post(self, url, content, headers):
            enviados.update({"url": url, "headers": headers})
            class _R:
                status_code = 200
                content = b"{}"
                text = "{}"

                def __init__(self):
                    self.headers = {"content-type": "application/json"}
            return _R()

    relevo.cliente = _Cliente()
    relevo("/v1/chat/completions", b"{}",
           {"Authorization": "Bearer la-de-mentira", "Content-Type": "application/json",
            "Host": "otro"})
    assert enviados["headers"]["Authorization"] == "Bearer la-real"
    assert "Host" not in enviados["headers"]
    assert enviados["url"] == "https://proveedor.invalid/v1/chat/completions"


def test_the_relay_waits_the_delay_the_provider_asked_for() -> None:
    """La cuota del tier gratuito se mide por minuto y el error dice cuánto falta."""
    relevo = md.Relevo("https://p.invalid/v1", reintentos=2)
    esperas: list[float] = []
    respuestas = iter([429, 429, 200])

    class _R:
        def __init__(self, estado):
            self.status_code = estado
            self.content = b"{}"
            self.headers = {"content-type": "application/json"}
            self.text = ('{"error":{"message":"Quota exceeded. '
                         'Please retry in 3.5s."}}')

    class _Cliente:
        def post(self, url, content, headers):
            return _R(next(respuestas))

    relevo.cliente = _Cliente()
    md.time.sleep, original = esperas.append, md.time.sleep
    try:
        estado, _c, _t = relevo("/v1/chat/completions", b"{}", {})
    finally:
        md.time.sleep = original
    assert estado == 200
    assert esperas == [4.0, 4.0], "no respetó el retraso que pidió el proveedor"


# ------------------------------------------------------------------- las guardas


def _env_demo() -> dict[str, str]:
    return piloto.entorno_del_agente("offline")


def test_the_demo_environment_passes_its_own_guards() -> None:
    assert guardas.revisar_entorno(
        _env_demo(), permitidos=(piloto.SERVICIOS, piloto.REDIS, piloto.RELEVO)) == []


@pytest.mark.parametrize("variable,valor", [
    ("META_GRAPH_BASE_URL", "https://graph.facebook.com"),
    ("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"),
    ("DASHSCOPE_BASE_URL", "https://dashscope-intl.aliyuncs.com/v1"),
])
def test_a_url_pointing_at_a_known_real_service_stops_the_harness(
    variable, valor
) -> None:
    problemas = guardas.revisar_entorno(
        {**_env_demo(), variable: valor},
        permitidos=(piloto.SERVICIOS, piloto.REDIS, piloto.RELEVO))
    assert any(variable in p and "servicio real" in p for p in problemas), problemas


@pytest.mark.parametrize("variable,valor", [
    ("ERPNEXT_URL", "https://erp.de-la-empresa.example"),
    ("REDIS_URL", "redis://un-redis-cualquiera:6379/0"),
])
def test_any_host_that_is_not_one_of_the_doubles_stops_the_harness(
    variable, valor
) -> None:
    """No hace falta que el host esté en una lista negra: lo que no es un doble
    no se usa. Una lista negra sola dejaría pasar el ERPNext de un cliente."""
    problemas = guardas.revisar_entorno(
        {**_env_demo(), variable: valor},
        permitidos=(piloto.SERVICIOS, piloto.REDIS, piloto.RELEVO))
    assert any(variable in p and "no es uno de los dobles" in p
               for p in problemas), problemas


@pytest.mark.parametrize("variable,puerto,que", [
    ("ERPNEXT_URL", 8080, "ERPNext de staging"),
    ("REDIS_URL", 6379, "Redis de staging"),
])
def test_the_staging_services_of_this_machine_stop_the_harness(
    variable, puerto, que
) -> None:
    """Son loopback, así que ninguna regla sobre hosts reales las atrapa."""
    esquema = "redis" if variable == "REDIS_URL" else "http"
    problemas = guardas.revisar_entorno(
        {**_env_demo(), variable: f"{esquema}://127.0.0.1:{puerto}/0"},
        permitidos=(piloto.SERVICIOS, piloto.REDIS, piloto.RELEVO))
    assert any(que in p for p in problemas), problemas


def test_a_credential_that_is_not_the_fake_one_stops_the_harness() -> None:
    problemas = guardas.revisar_entorno(
        {**_env_demo(), "WHATSAPP_TOKEN": "EAAG" + "x" * 180},
        permitidos=(piloto.SERVICIOS, piloto.REDIS, piloto.RELEVO))
    assert any("WHATSAPP_TOKEN" in p for p in problemas)
    # y nunca muestra el valor
    assert not any("EAAG" in p for p in problemas)


def test_a_real_credential_from_the_env_file_stops_the_harness(
    tmp_path: pathlib.Path,
) -> None:
    """La guarda que caza el error peligroso: heredar algo de producción."""
    env_real = tmp_path / ".env"
    env_real.write_text(
        "WHATSAPP_TOKEN=EAAGsecretodeproduccion123456\n"
        "GOOGLE_API_KEY=AIzaSyDclaveDeProduccion123\n"
        "BUSINESS_TIMEZONE=America/Argentina/Buenos_Aires\n"
    )
    problemas = guardas.revisar_contra_el_env_real(
        {**_env_demo(), "GEMINI_API_KEY": "AIzaSyDclaveDeProduccion123"}, env_real)
    assert len(problemas) == 1
    assert "GEMINI_API_KEY" in problemas[0]
    assert "GOOGLE_API_KEY" in problemas[0]
    assert "AIza" not in problemas[0], "la guarda mostró la credencial"


def test_shared_harmless_configuration_is_not_flagged(
    tmp_path: pathlib.Path,
) -> None:
    """Si la guarda grita por la zona horaria, se la empieza a ignorar."""
    env_real = tmp_path / ".env"
    env_real.write_text(
        "BUSINESS_TIMEZONE=America/Argentina/Buenos_Aires\n"
        "AUTO_CONFIRM_PRICE_LIST=Standard Selling\n"
        "GEMINI_SALES_MODEL=gemini-3.5-flash\n"
    )
    assert guardas.revisar_contra_el_env_real(_env_demo(), env_real) == []


def test_a_missing_env_file_is_not_an_excuse_to_skip_the_other_guards(
    tmp_path: pathlib.Path,
) -> None:
    assert guardas.revisar_contra_el_env_real(
        _env_demo(), tmp_path / "no-existe") == []


def test_redis_must_be_database_zero() -> None:
    """RediSearch se niega a crear su índice en otra base."""
    problemas = guardas.revisar_entorno(
        {**_env_demo(), "REDIS_URL": f"redis://{piloto.REDIS}:6379/3"},
        permitidos=(piloto.SERVICIOS, piloto.REDIS, piloto.RELEVO))
    assert any("base 0" in p for p in problemas)


def test_exigir_raises_with_every_problem_listed() -> None:
    with pytest.raises(guardas.GuardaError) as exc:
        guardas.exigir(["uno", "dos"])
    assert "uno" in str(exc.value) and "dos" in str(exc.value)
    guardas.exigir([])  # sin problemas no levanta


def test_the_relay_may_not_carry_any_business_credential() -> None:
    """Es el único contenedor con salida: no puede tener nada que perder."""
    prohibidas = ("ERPNEXT_API_KEY", "WHATSAPP_TOKEN", "META_APP_SECRET",
                  "REDIS_URL", "TELEFONOS_EQUIPO")
    env_relevo = piloto.entorno_del_agente("gemini")
    for variable in prohibidas:
        assert variable in env_relevo, (
            f"{variable} dejó de estar en el entorno del agente: "
            "actualizá relevo_sin_credenciales")


# ---------------------------------------------------------------- los escenarios


def test_every_release_scenario_the_task_asked_for_exists() -> None:
    claves = {e.clave for e in escenarios.escenarios()}
    esperados = {
        "pedido_cliente_existente", "alta_y_pedido", "confirmacion_automatica",
        "stock_insuficiente", "pedido_descuento", "entrega_excepcional",
        "gerente_aprueba", "gerente_rechaza", "cliente_acepta",
        "preparar_despachar_cancelar", "cancelacion_sin_remito",
        "reinicio_con_pendiente",
    }
    assert esperados <= claves, esperados - claves


def test_every_scenario_says_why_it_exists() -> None:
    for e in escenarios.escenarios():
        assert e.porque.strip(), f"{e.clave} no dice qué prueba"
        assert e.pasos, f"{e.clave} no tiene pasos"


def test_a_step_that_names_an_order_comes_after_one_that_creates_it() -> None:
    """El marcador se resuelve con el pedido del paso anterior; si no hay
    ninguno, el mensaje sale con el número vacío y el escenario miente."""
    for e in escenarios.escenarios():
        creado = False
        for i, paso in enumerate(e.pasos):
            if escenarios.ULTIMO_PEDIDO in paso.texto:
                assert creado, (
                    f"{e.clave} paso {i + 1} nombra un pedido que nadie creó")
            if "SAL-ORD" in " ".join(paso.espera):
                creado = True
            if any(k.startswith("Sales Order") for k in paso.documentos):
                creado = True


def test_every_customer_message_in_a_scenario_has_a_rule_in_the_script() -> None:
    """En modo offline, un mensaje sin regla contesta «no tengo un guion» y el
    escenario falla por el banco de pruebas, no por el sistema."""
    reglas = escenarios.reglas()
    sin_regla = []
    for e in escenarios.escenarios():
        for paso in e.pasos:
            texto = paso.texto.replace(escenarios.ULTIMO_PEDIDO, "SAL-ORD-2026-00001")
            rol = ("gerencia" if paso.quien in
                   {datos.TELEFONO_DUENO, datos.TELEFONO_EQUIPO} else "clientes")
            # Los comandos de gerencia y la aceptación del cliente los resuelve
            # app/main.py sin modelo: no necesitan regla.
            if rol == "gerencia" or texto.lower().startswith(
                    ("acepto", "no acepto", "rechazo")):
                continue
            if not any(matchea(texto, rol) for matchea, _pasos in reglas):
                sin_regla.append(f"{e.clave}: {texto!r}")
    assert not sin_regla, sin_regla


def test_the_fake_phones_are_obviously_invented() -> None:
    """Ningún dato del banco de pruebas puede parecer de una persona real."""
    telefonos = (datos.TELEFONO_DUENO, datos.TELEFONO_EQUIPO,
                 datos.TELEFONO_HABITUAL, datos.TELEFONO_MOROSO,
                 datos.TELEFONO_NUEVO)
    for t in telefonos:
        assert t.startswith("54935"), t
        cuerpo = t[5:]
        assert len(set(cuerpo)) <= 4, f"{t} no parece inventado"
    assert len(set(telefonos)) == len(telefonos)


# ------------------------------------------- qué se exige en cada modo


def _turno_con(modo: str, paso, respuestas: list[str], foto: dict | None = None):
    """Corre sólo la revisión de un paso, sin Docker ni red."""
    piloto_ = piloto.Piloto(modo, pathlib.Path("/tmp/no-se-escribe"))
    turno = piloto.Turno("x", 1, "549", "cliente", paso.texto,
                         respuestas=list(respuestas))
    turno.problemas.extend(piloto_._revisar(paso, turno, foto or {}))
    return turno


def test_expected_wording_is_hard_offline_and_advisory_against_a_real_model() -> None:
    """Contra un guión el texto es exacto; contra un modelo libre, no.

    "tengo leche entera" es una respuesta correcta que no contiene la cadena
    LECHE-ENT-1L. Fallar por eso convertiría cada corrida con Gemini en un
    muro de rojo que no dice nada sobre el sistema.
    """
    paso = escenarios.Paso("549", "tenés leche?", espera=["LECHE-ENT-1L"])
    respuestas = ["Sí, tengo leche entera y descremada."]

    offline = _turno_con("offline", paso, respuestas)
    assert not offline.ok
    assert any("LECHE-ENT-1L" in p for p in offline.problemas)

    gemini = _turno_con("gemini", paso, respuestas)
    assert gemini.ok, gemini.problemas
    assert any("LECHE-ENT-1L" in a for a in gemini.avisos)


@pytest.mark.parametrize("modo", ["offline", "gemini"])
def test_a_forbidden_phrase_fails_in_both_modes(modo: str) -> None:
    """Que el modelo diga «confirmado» cuando no lo está es lo que hay que
    cazar, y no es una cuestión de redacción."""
    paso = escenarios.Paso("549", "dame 10", prohibe=["confirmado"])
    turno = _turno_con(modo, paso, ["Tu pedido quedó confirmado."])
    assert not turno.ok
    assert any("confirmado" in p for p in turno.problemas)


@pytest.mark.parametrize("modo", ["offline", "gemini"])
def test_a_technical_apology_fails_in_both_modes(modo: str) -> None:
    """app/main.py convierte cualquier excepción en una disculpa. Sin este
    chequeo, un escenario cuyas condiciones son sólo «prohibe» pasaría con el
    agente completamente roto."""
    paso = escenarios.Paso("549", "hola", prohibe=["confirmado"])
    turno = _turno_con(
        modo, paso, ["Perdón, tuve un problema técnico. Ya avisé al equipo."])
    assert not turno.ok
    assert any("disculpa técnica" in p for p in turno.problemas)


@pytest.mark.parametrize("modo", ["offline", "gemini"])
def test_the_document_state_is_demanded_in_both_modes(modo: str) -> None:
    """El estado de los documentos no depende de cómo redactó nadie."""
    paso = escenarios.Paso("549", "dame 10",
                           documentos={"Sales Order/*": {"docstatus": 1}})
    sin_confirmar = _turno_con(modo, paso, ["listo"],
                               {"Sales Order/SO-1": {"docstatus": 0}})
    assert not sin_confirmar.ok
    confirmado = _turno_con(modo, paso, ["listo"],
                            {"Sales Order/SO-1": {"docstatus": 1}})
    assert confirmado.ok, confirmado.problemas


@pytest.mark.parametrize("modo", ["offline", "gemini"])
def test_only_the_acknowledgement_is_never_a_pass(modo: str) -> None:
    """Todo turno de texto manda el acuse; la respuesta es la segunda."""
    paso = escenarios.Paso("549", "hola")
    turno = _turno_con(modo, paso, [
        "Recibido, dame un momento mientras lo verifico."])
    assert not turno.ok
    assert any("sólo llegó el acuse" in p for p in turno.problemas)
