"""The readiness check validates everything the owner must configure — and
never prints a secret, a phone number or a user name while doing it."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import readiness

TOKEN = "EAAG" + "x" * 180
KEY = "sk-" + "a" * 30
STAFF = "5493511111111"
BASE = {
    "DASHSCOPE_API_KEY": KEY,
    "DASHSCOPE_BASE_URL": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "QWEN_SALES_MODEL": "qwen3.7-plus-2026-05-26",
    "TELEFONOS_EQUIPO": STAFF,
    "PAIS_TELEFONO": "54",
    "ZONAS_ENTREGA_CP": "5000,5001",
    "ZONAS_ENTREGA_LOCALIDADES": "Córdoba",
    "WHATSAPP_TOKEN": TOKEN,
    "WHATSAPP_PHONE_NUMBER_ID": "1357924680",
    "WHATSAPP_BUSINESS_ACCOUNT_ID": "2468013579",
    "META_APP_SECRET": "b" * 32,
    "META_VERIFY_TOKEN": "plus-verify-secret",
    "WHATSAPP_STAFF_PENDING_TEMPLATE": "pedido_pendiente_equipo",
    "ERPNEXT_URL": "http://backend:8000",
    "ERPNEXT_COMPANY": "Lacteos Test SA",
    "ERPNEXT_WAREHOUSE": "Principal - LT",
    "ERPNEXT_API_KEY": "agentkey000000000",
    "ERPNEXT_API_SECRET": "agentsec000000000",
    "ERPNEXT_MANAGER_API_KEY": "mgrkey00000000000",
    "ERPNEXT_MANAGER_API_SECRET": "mgrsec00000000000",
    "ERPNEXT_POLICY_API_KEY": "polkey00000000000",
    "ERPNEXT_POLICY_API_SECRET": "polsec00000000000",
    "STOCK_CONFIABLE": "true",
    "STOCK_CONFIABLE_HORAS": "24",
    "AUTO_CONFIRM_PRICE_LIST": "Standard Selling",
    "AUTO_CONFIRM_CURRENCY": "ARS",
    "REDIS_URL": "redis://redis:6379/0",
}
SECRETOS = (KEY, TOKEN, STAFF, "b" * 32, "plus-verify-secret", "agentkey000000000", "polsec00000000000")


def _http_sano(url, headers=None, params=None):
    if "/debug_token" in url:
        return 200, {"data": {"type": "SYSTEM_USER", "expires_at": 0, "is_valid": True,
                              "scopes": ["whatsapp_business_messaging", "whatsapp_business_management"]}}
    if "/message_templates" in url:
        return 200, {"data": [{"name": params["name"], "status": "APPROVED", "language": "es_AR"}]}
    if url.endswith("/1357924680"):
        return 200, {"id": "1357924680", "quality_rating": "GREEN"}
    if "frappe.auth.get_logged_user" in url:
        auth = headers["Authorization"]
        return 200, {"message": {"agentkey000000000": "agente@x", "mgrkey00000000000": "gerencia@x", "polkey00000000000": "politica@x"}[auth.split()[1].split(":")[0]]}
    if "/api/resource/DocPerm" in url or "/api/resource/Custom%20DocPerm" in url or "Custom DocPerm" in url:
        return 200, {"data": [{"role": "Sales Manager"}]}
    if "/api/resource/User/" in url:
        usuario = url.rsplit("/", 1)[1]
        roles = {"agente@x": ["Agente IA"], "gerencia@x": ["Gerencia IA"], "politica@x": ["Sales Manager"]}[usuario]
        return 200, {"data": {"roles": [{"role": r} for r in roles]}}
    if "/api/resource/Warehouse/" in url:
        return 200, {"data": {"is_group": 0, "disabled": 0, "company": "Lacteos Test SA"}}
    raise AssertionError(f"URL inesperada {url}")


# The delivery rules live in the owner's store like every other setting, so the
# stub answers for them too. "-" is limites.NINGUNO: set, and set to nothing.
def _entrega(**cambios):
    filas = {
        "ENTREGA_DIAS": "martes,viernes",
        "ENTREGA_HORA": "08:00",
        "ENTREGA_EXCEPCION_ACTIVA": "false",
        "ENTREGA_EXCEPCION_DIAS": "-",
        "ENTREGA_EXCEPCION_HORA": "-",
        "ENTREGA_EXCEPCION_CARGO": "-",
        "ENTREGA_EXCEPCION_MIN_TOTAL": "0",
        "RETIRO_LOCAL_ACTIVO": "false",
        "RETIRO_LOCAL_DIAS": "-",
        "RETIRO_LOCAL_HORA": "-",
    }
    filas.update(cambios)
    from app import limites

    return [
        {
            "nombre": nombre,
            "alias": limites.ENTREGA[nombre].alias[0],
            "unidad": limites.ENTREGA[nombre].unidad,
            "valor": valor,
            "origen": "dueño",
            "problema": "",
        }
        for nombre, valor in filas.items()
    ]


def _limites_ok():
    return [
        {"nombre": "AUTO_CONFIRM_MAX", "alias": "tope", "valor": "30000", "origen": "dueño", "problema": ""},
        {"nombre": "STOCK_BUFFER_PCT", "alias": "colchón", "valor": "20", "origen": "default", "problema": ""},
        *_entrega(),
    ]


def _correr(env, http=_http_sano, limites=_limites_ok):
    reporte = readiness.Reporte()
    readiness.chequear_modelos(env, reporte)
    readiness.chequear_equipo(env, reporte)
    waba = readiness.chequear_whatsapp(env, reporte, http)
    readiness.chequear_plantillas(env, reporte, http, waba)
    readiness.chequear_erpnext(env, reporte, http)
    readiness.chequear_stock_y_limites(env, reporte, limites)
    readiness.chequear_entrega(env, reporte, limites)
    return reporte


def test_a_complete_environment_is_ready_and_the_report_exposes_no_value() -> None:
    reporte = _correr(BASE)
    texto = reporte.texto()
    assert reporte.listo, texto
    assert "LISTO para probar en vivo" in texto
    for secreto in SECRETOS:
        assert secreto not in texto
    assert "agente@x" not in texto and "politica@x" not in texto  # no user names either
    assert "región internacional (Singapur)" in texto
    assert "qwen3.7-plus-2026-05-26 (QWEN_SALES_MODEL)" in texto and "qwen3.8-max (default)" in texto
    assert "1 número(s) válido(s) (no se muestran)" in texto
    assert "LOS DOS datos permitidos" in texto
    assert "permanente (tipo SYSTEM_USER" in texto
    assert "'pedido_pendiente_equipo' aprobada" in texto
    assert "ERPNext politica: 1 rol(es); Submit: sí" in texto
    assert "ERPNext agente: 1 rol(es); Submit: no" in texto


def test_missing_configuration_is_reported_not_fabricated() -> None:
    env = {k: v for k, v in BASE.items() if k not in ("DASHSCOPE_API_KEY", "TELEFONOS_EQUIPO", "ZONAS_ENTREGA_CP", "ZONAS_ENTREGA_LOCALIDADES", "ERPNEXT_WAREHOUSE")}
    reporte = _correr(env)
    texto = reporte.texto()
    assert not reporte.listo
    assert "FALTA  DASHSCOPE_API_KEY" in texto
    assert "FALTA  TELEFONOS_EQUIPO" in texto
    assert "FALTA  ZONAS_ENTREGA: ninguna lista" in texto
    assert "FALTA  ERPNEXT_WAREHOUSE" in texto
    assert "NO LISTO" in texto


def test_a_temporary_dashboard_token_is_flagged() -> None:
    def http(url, headers=None, params=None):
        if "/debug_token" in url:
            return 200, {"data": {"type": "USER", "expires_at": int(time.time()) + 3600 * 5, "is_valid": True,
                                  "scopes": ["whatsapp_business_messaging"]}}
        return _http_sano(url, headers, params)

    texto = _correr(BASE, http=http).texto()
    assert "ERROR  WHATSAPP_TOKEN: TEMPORAL (tipo USER, vence en 5 h)" in texto
    assert "le faltan permisos: whatsapp_business_management" in texto
    assert TOKEN not in texto


def test_templates_missing_or_unapproved_in_meta_are_errors_and_empty_ones_are_notices() -> None:
    def http(url, headers=None, params=None):
        if "/message_templates" in url:
            if params["name"] == "pedido_pendiente_equipo":
                return 200, {"data": [{"name": "pedido_pendiente_equipo", "status": "PENDING"}]}
            return 200, {"data": []}
        return _http_sano(url, headers, params)

    env = {**BASE, "WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE": "no_existe"}
    texto = _correr(env, http=http).texto()
    assert "ERROR  WHATSAPP_STAFF_PENDING_TEMPLATE: 'pedido_pendiente_equipo' en estado PENDING" in texto
    assert "ERROR  WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE: 'no_existe' no existe" in texto
    assert "AVISO  WHATSAPP_STAFF_ALERT_TEMPLATE: vacía" in texto


def test_without_a_business_account_id_template_approval_is_reported_as_unverified() -> None:
    def http(url, headers=None, params=None):
        if "/debug_token" in url:
            return 200, {"data": {"type": "SYSTEM_USER", "expires_at": 0, "scopes": list(readiness.ALCANCES_META)}}
        return _http_sano(url, headers, params)

    env = {k: v for k, v in BASE.items() if k != "WHATSAPP_BUSINESS_ACCOUNT_ID"}
    texto = _correr(env, http=http).texto()
    assert "no pude deducir la cuenta de WhatsApp Business" in texto


def test_identical_erpnext_keys_and_administrator_are_blocking() -> None:
    env = {**BASE, "ERPNEXT_MANAGER_API_KEY": BASE["ERPNEXT_API_KEY"]}

    def http(url, headers=None, params=None):
        if "frappe.auth.get_logged_user" in url:
            return 200, {"message": "Administrator"}
        return _http_sano(url, headers, params)

    texto = _correr(env, http=http).texto()
    assert "FALTA  ERPNext credenciales: dos de las tres claves son iguales" in texto
    assert "ERROR  ERPNext agente: es Administrator" in texto
    assert "AVISO  ERPNext politica: es Administrator" in texto


def test_an_agent_role_with_submit_rights_is_an_error() -> None:
    def http(url, headers=None, params=None):
        if "/api/resource/User/agente@x" in url:
            return 200, {"data": {"roles": [{"role": "Agente IA"}, {"role": "Sales Manager"}]}}
        return _http_sano(url, headers, params)

    texto = _correr(BASE, http=http).texto()
    assert "ERROR  ERPNext agente: 2 rol(es), y alguno permite Submit" in texto


def test_limits_are_reported_by_origin_and_validity_without_values() -> None:
    def limites():
        return [
            {"nombre": "AUTO_CONFIRM_MAX", "valor": "0", "origen": "arranque", "problema": ""},
            {"nombre": "STOCK_BUFFER_PCT", "valor": "150", "origen": "dueño", "problema": "fuera de rango 0-95"},
        ]

    texto = _correr(BASE, limites=limites).texto()
    assert "AVISO  AUTO_CONFIRM_MAX: en 0 (del .env)" in texto
    assert "ERROR  STOCK_BUFFER_PCT: mal configurado (fijado por el dueño): fuera de rango 0-95" in texto
    assert "150" not in texto.split("STOCK_BUFFER_PCT")[1].split("\n")[0].replace("fuera de rango 0-95", "")


def test_unreadable_limits_block_the_release() -> None:
    def limites():
        raise RuntimeError("redis caído")

    texto = _correr(BASE, limites=limites).texto()
    assert "ERROR  Límites: no se pueden leer (RuntimeError): la política deja TODO pendiente" in texto


def test_offline_mode_makes_no_network_call_and_says_what_was_not_verified() -> None:
    def nunca(*args, **kwargs):
        raise AssertionError("sin red no se llama a nadie")

    reporte = readiness.Reporte()
    readiness.chequear_whatsapp(BASE, reporte, None)
    readiness.chequear_plantillas(BASE, reporte, None, "")
    readiness.chequear_erpnext(BASE, reporte, None)
    texto = reporte.texto()
    assert "sin red: el token y las plantillas no se verificaron" in texto
    assert "sin red: permisos y depósito no se verificaron" in texto


def test_zone_rule_mode_is_described(monkeypatch) -> None:
    solo_cp = {**BASE, "ZONAS_ENTREGA_LOCALIDADES": ""}
    assert "la localidad no se evalúa" in _correr(solo_cp).texto()
    solo_loc = {**BASE, "ZONAS_ENTREGA_CP": ""}
    assert "el código postal no se evalúa" in _correr(solo_loc).texto()


def test_unparsable_staff_numbers_and_bad_country_code_are_errors() -> None:
    texto = _correr({**BASE, "TELEFONOS_EQUIPO": "abc", "PAIS_TELEFONO": "AR"}).texto()
    assert "ERROR  TELEFONOS_EQUIPO: 1 número(s) que no se pueden interpretar" in texto
    assert "ERROR  PAIS_TELEFONO" in texto


def test_the_manager_snapshot_override_is_noted_for_verification() -> None:
    texto = _correr({**BASE, "QWEN_MANAGER_MODEL": "qwen3.8-max-0902"}).texto()
    assert "qwen3.8-max-0902 (QWEN_MANAGER_MODEL) (distinto del documentado" in texto
    assert "make verificar-qwen" in texto


def test_the_command_exits_nonzero_when_not_ready(monkeypatch, capsys) -> None:
    monkeypatch.setattr(readiness, "ejecutar", lambda con_red=True: _correr({k: v for k, v in BASE.items() if k != "DASHSCOPE_API_KEY"}))
    assert readiness.main(["--sin-red"]) == 1
    assert "NO LISTO" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The delivery rules, and the line that actually matters: is the expiry
# fallback on? Without it an expired decision request has nothing concrete to
# offer, the customer is told "write to me again", and the order is dropped.
# ---------------------------------------------------------------------------


def test_a_configured_round_is_reported_as_the_fallback_being_on() -> None:
    reporte = _correr(BASE)
    texto = reporte.texto()

    assert reporte.listo, texto
    assert "reparto martes,viernes a las 08:00" in texto
    assert "Respaldo de vencimiento" not in texto


def test_with_neither_a_round_nor_a_pickup_the_owner_is_warned() -> None:
    """The requirement: at least an AVISO that the fallback is disabled."""
    def sin_nada():
        return _entrega(ENTREGA_DIAS="-", ENTREGA_HORA="-")

    reporte = _correr(BASE, limites=sin_nada)
    texto = reporte.texto()

    assert "Respaldo de vencimiento" in texto
    assert "el pedido se cae" in texto
    assert [n for n, c, _ in reporte.lineas if c == "Respaldo de vencimiento"] == [
        readiness.AVISO
    ]
    # An AVISO, not a blocker: nothing is oversold by having no fallback.
    assert reporte.listo, texto


def test_a_pickup_counter_alone_is_enough_of_a_fallback() -> None:
    def solo_retiro():
        return _entrega(
            ENTREGA_DIAS="-",
            ENTREGA_HORA="-",
            RETIRO_LOCAL_ACTIVO="true",
            RETIRO_LOCAL_DIAS="sabado",
            RETIRO_LOCAL_HORA="10:00",
        )

    texto = _correr(BASE, limites=solo_retiro).texto()

    assert "retiro sabado a las 10:00" in texto
    assert "Respaldo de vencimiento" not in texto


def test_half_a_round_is_not_a_round() -> None:
    """Days with no time cannot produce an offer, so it is not configured."""
    def media():
        return _entrega(ENTREGA_HORA="-")

    assert "Respaldo de vencimiento" in _correr(BASE, limites=media).texto()


def test_a_malformed_delivery_rule_is_an_error_naming_the_setting() -> None:
    def roto():
        return [
            {
                "nombre": "ENTREGA_DIAS",
                "alias": "días de reparto",
                "unidad": "días",
                "valor": "lunez",
                "origen": "arranque",
                "problema": "«lunez» no es un día de la semana",
            }
        ]

    reporte = _correr(BASE, limites=roto)
    texto = reporte.texto()

    assert not reporte.listo
    assert "ENTREGA_DIAS" in texto and "no es un día de la semana" in texto


def test_an_enabled_exception_missing_its_terms_is_called_out() -> None:
    def a_medias():
        return _entrega(ENTREGA_EXCEPCION_ACTIVA="true")

    texto = _correr(BASE, limites=a_medias).texto()

    assert "en sí pero falta" in texto
    assert "nada queda pre-autorizado" in texto


def test_a_fee_with_no_account_says_a_person_has_to_add_the_charge() -> None:
    def completa():
        return _entrega(
            ENTREGA_EXCEPCION_ACTIVA="true",
            ENTREGA_EXCEPCION_DIAS="jueves",
            ENTREGA_EXCEPCION_HORA="19:00",
            ENTREGA_EXCEPCION_CARGO="1500",
        )

    texto = _correr(BASE, limites=completa).texto()

    assert "ENTREGA_CARGO_CUENTA" in texto
    assert "nunca por WhatsApp" in texto


def test_without_redis_the_delivery_rules_are_not_guessed_at() -> None:
    reporte = readiness.Reporte()
    readiness.chequear_entrega(BASE, reporte, None)

    assert "no se verificaron las reglas de entrega" in reporte.texto()


def test_an_unreadable_store_is_an_error_not_an_empty_configuration() -> None:
    def explota():
        raise RuntimeError("redis caído")

    reporte = readiness.Reporte()
    readiness.chequear_entrega(BASE, reporte, explota)

    assert not reporte.listo
    assert "reglas no legibles" in reporte.texto()


def test_the_end_to_end_report_includes_the_delivery_check(monkeypatch) -> None:
    """chequear_entrega is wired into ejecutar(), not only into this file's
    own harness — otherwise it would have zero real coverage."""
    monkeypatch.setattr(readiness, "_http_real", _http_sano)
    from app import limites

    monkeypatch.setattr(limites, "resumen", lambda: list(_limites_ok()))

    reporte = readiness.ejecutar(BASE, con_red=False)

    assert "reparto martes,viernes" in reporte.texto()
