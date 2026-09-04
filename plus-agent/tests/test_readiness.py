"""The readiness check validates everything the owner must configure — and
never prints a secret, a phone number or a user name while doing it."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import limites, readiness

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
    if "/api/resource/Account/" in url:
        return 200, {"data": CUENTA_SANA}
    raise AssertionError(f"URL inesperada {url}")


# What a usable delivery-charge head looks like: a real ledger of the configured
# company. "Freight and Forwarding Charges" is an Expense head in ERPNext's
# standard chart, which is what most businesses actually use for this.
CUENTA_SANA = {
    "name": "Fletes - LT",
    "is_group": 0,
    "disabled": 0,
    "freeze_account": "No",
    "company": "Lacteos Test SA",
    "root_type": "Expense",
    "account_type": "Chargeable",
}


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
    readiness.chequear_entrega(env, reporte, limites, http)
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
    assert "make verificar-modelos" in texto


def test_the_report_names_the_chosen_provider_and_defaults_to_qwen() -> None:
    """A .env that never heard of LLM_PROVIDER keeps reporting Qwen."""
    texto = _correr(BASE).texto()
    assert "LLM_PROVIDER: qwen" in texto and "(default)" in texto


def test_with_gemini_the_report_names_the_gemini_variables_only() -> None:
    """Telling somebody who chose Gemini that DASHSCOPE_API_KEY is missing sends
    them to load the wrong credential."""
    env = {k: v for k, v in BASE.items() if k not in ("DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL", "QWEN_SALES_MODEL")}
    env = {**env, "LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "AIza" + "z" * 30}

    reporte = _correr(env)
    texto = reporte.texto()

    assert reporte.listo, texto
    assert "LLM_PROVIDER: gemini" in texto and "(explícito)" in texto
    assert "OK     GEMINI_API_KEY: presente (34 caracteres; no se muestra)" in texto
    assert "región global (Google) (default)" in texto
    assert "gemini-3.5-flash (default)" in texto
    assert "DASHSCOPE" not in texto
    assert "AIzazzz" not in texto  # never the value


def test_with_gemini_a_missing_gemini_key_is_what_blocks() -> None:
    env = {k: v for k, v in BASE.items() if k != "DASHSCOPE_API_KEY"}
    reporte = _correr({**env, "LLM_PROVIDER": "gemini"})
    texto = reporte.texto()

    assert not reporte.listo
    assert "FALTA  GEMINI_API_KEY" in texto
    assert "no hay proveedor de respaldo" in texto
    assert "FALTA  DASHSCOPE_API_KEY" not in texto


def test_a_leftover_key_from_the_other_provider_is_flagged_as_unused() -> None:
    """It is not an error, but he has to know it buys him nothing: there is no
    automatic fallback, so the process runs on the chosen provider or not at all."""
    env = {**BASE, "LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "AIza" + "z" * 30}
    texto = _correr(env).texto()

    assert "AVISO  DASHSCOPE_API_KEY: cargada pero sin uso" in texto
    assert "no hay respaldo automático entre proveedores" in texto


def test_an_unknown_provider_blocks_the_report() -> None:
    reporte = _correr({**BASE, "LLM_PROVIDER": "openai"})

    assert not reporte.listo
    assert "ERROR  LLM_PROVIDER" in reporte.texto()


def test_the_qwen_thinking_switches_are_reported_as_inert_under_gemini() -> None:
    env = {
        **BASE,
        "LLM_PROVIDER": "gemini",
        "GEMINI_API_KEY": "AIza" + "z" * 30,
        "QWEN_THINKING_GERENCIA": "true",
    }
    texto = _correr(env).texto()

    assert "QWEN_THINKING_GERENCIA configurada(s) pero gemini no usa esos controles" in texto


def test_a_provider_prefixed_model_name_is_an_error_under_gemini_too() -> None:
    env = {
        **BASE,
        "LLM_PROVIDER": "gemini",
        "GEMINI_API_KEY": "AIza" + "z" * 30,
        "GEMINI_SALES_MODEL": "google_genai:gemini-3.5-flash",
    }
    reporte = _correr(env)

    assert not reporte.listo
    assert "ERROR  GEMINI_SALES_MODEL" in reporte.texto()


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


def test_lost_delivery_rules_are_a_failure_and_the_env_is_not_shown_as_active() -> None:
    """After a Redis loss with [entrega] history on record, limites.resumen()
    reports every delivery row as PERDIDO — nothing is in effect, not the .env
    either, because that is what limites.entrega() decides in that state. The
    report has to say the loss, once, as a failure — never OK for a round that
    will not run, and never one "mal configurado" per row."""
    from app import limites

    def perdidas():
        return [
            {
                **fila,
                "valor": "",
                "origen": limites.PERDIDO,
                "problema": limites.PROBLEMA_ENTREGA_PERDIDA,
            }
            for fila in _entrega()
        ]

    reporte = _correr(BASE, limites=perdidas)
    texto = reporte.texto()

    assert not reporte.listo
    assert texto.count("se PERDIERON") == 1
    assert "vuelva a fijar" in texto
    assert "reparto martes" not in texto
    assert "mal configurado" not in texto
    assert "Respaldo de vencimiento" in texto


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


# ---------------------------------------------------------------------------
# ENTREGA_CARGO_CUENTA. "Present" was the whole check, and presence is not the
# failure mode: a wrong account name does not break the bot, it makes every
# accepted off-day delivery with a fee wait for a person, days later.
# ---------------------------------------------------------------------------

CON_CARGO = dict(BASE, ENTREGA_CARGO_CUENTA="Fletes - LT")


def _con_fee(**cambios):
    """The owner's store with off-day delivery ON and a fee configured."""

    def limites():
        return _entrega(
            ENTREGA_EXCEPCION_ACTIVA="true",
            ENTREGA_EXCEPCION_DIAS="jueves",
            ENTREGA_EXCEPCION_HORA="19:00",
            ENTREGA_EXCEPCION_CARGO="1500",
            **cambios,
        )

    return limites


def _http_con_cuenta(cuenta: dict | None, estado: int = 200):
    def http(url, headers=None, params=None):
        if "/api/resource/Account/" in url:
            return estado, ({"data": cuenta} if cuenta is not None else {})
        return _http_sano(url, headers, params)

    return http


def _linea(reporte, clave: str) -> tuple[str, str]:
    for nivel, k, mensaje in reporte.lineas:
        if k == clave:
            return nivel, mensaje
    raise AssertionError(f"{clave} no está en el reporte")


def test_a_usable_charge_account_is_verified_not_just_present() -> None:
    reporte = _correr(CON_CARGO, limites=_con_fee())

    nivel, mensaje = _linea(reporte, "ENTREGA_CARGO_CUENTA")
    assert nivel == readiness.OK
    assert "existe, habilitada, de la compañía configurada" in mensaje
    assert reporte.listo


@pytest.mark.parametrize(
    "cuenta, estado, esperado",
    [
        (None, 404, "no existe o no se puede leer"),
        (dict(CUENTA_SANA, is_group=1), 200, "es un grupo"),
        (dict(CUENTA_SANA, disabled=1), 200, "está deshabilitada"),
        (dict(CUENTA_SANA, freeze_account="Yes"), 200, "está congelada"),
        (dict(CUENTA_SANA, company="Otra SA"), 200, "pertenece a otra compañía"),
        (dict(CUENTA_SANA, root_type="Asset"), 200, "cuenta de tipo Asset"),
        (dict(CUENTA_SANA, root_type="Equity"), 200, "cuenta de tipo Equity"),
    ],
    ids=["no existe", "grupo", "deshabilitada", "congelada", "otra compañía", "activo", "patrimonio"],
)
def test_a_fee_with_an_unusable_account_blocks_readiness(
    cuenta, estado, esperado
) -> None:
    """The charge write would fail, and then a customer who said yes waits for
    a person instead of getting their order confirmed."""
    reporte = _correr(
        CON_CARGO, http=_http_con_cuenta(cuenta, estado), limites=_con_fee()
    )

    nivel, mensaje = _linea(reporte, "ENTREGA_CARGO_CUENTA")
    assert nivel == readiness.ERROR
    assert esperado in mensaje
    assert "termina esperando a una persona" in mensaje
    assert not reporte.listo


@pytest.mark.parametrize(
    "raiz", ["Income", "Expense", "Liability"], ids=["ingreso", "gasto", "pasivo"]
)
def test_the_heads_a_business_actually_uses_for_a_delivery_fee_are_accepted(
    raiz,
) -> None:
    """Freight is an Expense head in ERPNext's standard chart, a delivery-income
    head is Income, and a fee treated as a tax is a Liability. Refusing any of
    those would fail a correct configuration."""
    reporte = _correr(
        CON_CARGO,
        http=_http_con_cuenta(dict(CUENTA_SANA, root_type=raiz)),
        limites=_con_fee(),
    )

    assert _linea(reporte, "ENTREGA_CARGO_CUENTA")[0] == readiness.OK
    assert reporte.listo


def test_a_bad_account_is_visible_before_fees_are_turned_on_but_does_not_block() -> None:
    """Nothing is broken while no fee is configured — but he should not find out
    about the typo the day he starts charging for delivery."""
    reporte = _correr(
        CON_CARGO, http=_http_con_cuenta(dict(CUENTA_SANA, is_group=1))
    )

    nivel, mensaje = _linea(reporte, "ENTREGA_CARGO_CUENTA")
    assert nivel == readiness.AVISO
    assert "es un grupo" in mensaje
    assert "todavía sin cargo configurado" in mensaje
    assert reporte.listo


def test_an_exception_enabled_with_a_zero_fee_does_not_block_either() -> None:
    """A fee of 0 is a real answer: no charge is ever written, so no account is
    ever needed."""
    reporte = _correr(
        CON_CARGO,
        http=_http_con_cuenta(dict(CUENTA_SANA, disabled=1)),
        limites=lambda: _entrega(
            ENTREGA_EXCEPCION_ACTIVA="true",
            ENTREGA_EXCEPCION_DIAS="jueves",
            ENTREGA_EXCEPCION_HORA="19:00",
            ENTREGA_EXCEPCION_CARGO="0",
        ),
    )

    assert _linea(reporte, "ENTREGA_CARGO_CUENTA")[0] == readiness.AVISO
    assert reporte.listo


def test_without_the_network_the_account_is_reported_as_unverified_not_as_fine() -> None:
    reporte = readiness.Reporte()
    readiness.chequear_entrega(CON_CARGO, reporte, _con_fee(), None)

    nivel, mensaje = _linea(reporte, "ENTREGA_CARGO_CUENTA")
    assert nivel == readiness.AVISO
    assert "sin red no se verificó" in mensaje


def test_an_account_whose_type_cannot_be_read_is_not_declared_fine() -> None:
    sin_raiz = {k: v for k, v in CUENTA_SANA.items() if k != "root_type"}
    reporte = _correr(
        CON_CARGO, http=_http_con_cuenta(sin_raiz), limites=_con_fee()
    )

    nivel, mensaje = _linea(reporte, "ENTREGA_CARGO_CUENTA")
    assert nivel == readiness.AVISO
    assert "no pude leer su root_type" in mensaje


def test_the_account_name_is_never_printed_by_itself_as_a_secret() -> None:
    """It is not a secret — but the report must not leak the credential used to
    read it, and the check must survive a name with spaces and a dash."""
    reporte = _correr(CON_CARGO, limites=_con_fee())

    texto = reporte.texto()
    for secreto in SECRETOS:
        assert secreto not in texto


def test_the_account_check_is_wired_into_ejecutar(monkeypatch) -> None:
    """Not only into this file: `make check-env` has to run it, WITH the
    network. Wiring it in but not passing the network through would report
    every account as unverified for ever, which reads like a pass."""
    monkeypatch.setattr(readiness, "_http_real", _http_sano)
    monkeypatch.setattr(
        limites, "resumen", _con_fee()
    )

    reporte = readiness.ejecutar(dict(CON_CARGO), con_red=True)

    nivel, mensaje = _linea(reporte, "ENTREGA_CARGO_CUENTA")
    assert nivel == readiness.OK, mensaje
    assert "existe, habilitada" in mensaje


def test_ejecutar_without_the_network_still_reports_the_account_honestly(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        limites, "resumen", _con_fee()
    )

    reporte = readiness.ejecutar(dict(CON_CARGO), con_red=False)

    assert _linea(reporte, "ENTREGA_CARGO_CUENTA")[0] == readiness.AVISO
