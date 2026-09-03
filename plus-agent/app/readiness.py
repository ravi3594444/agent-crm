"""¿Está el despliegue listo para una prueba en vivo? Sin mostrar un solo valor.

    make check-env            (python -m app.readiness)
    make check-env-offline    (python -m app.readiness --sin-red)

Cada línea dice OK / AVISO / FALTA / ERROR y qué hacer. Nunca imprime una
clave, un token, un teléfono ni un nombre de usuario: sólo presencia, longitud,
cantidades, regiones, estados y nombres de modelo o de plantilla. Y no inventa
nada: lo que no se pudo verificar se reporta como no verificado.

Las funciones reciben el entorno y los accesos a red como parámetros para que
los tests las ejerciten sin .env, sin Meta, sin ERPNext y sin Redis.
"""
from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import httpx

from app import modelos, telefono

OK, AVISO, FALTA, ERROR = "OK", "AVISO", "FALTA", "ERROR"

# (status, body-json-o-None). Inyectable para los tests.
Http = Callable[..., tuple[int, object]]

PLANTILLAS = (
    "WHATSAPP_STAFF_PENDING_TEMPLATE",
    "WHATSAPP_STAFF_CONFIRMED_TEMPLATE",
    "WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE",
    "WHATSAPP_CUSTOMER_REJECTED_TEMPLATE",
    "WHATSAPP_CUSTOMER_CANCELLED_TEMPLATE",
    "WHATSAPP_STAFF_ALERT_TEMPLATE",
)
ROLES_SUBMIT_PROHIBIDOS = ("agente", "gerencia")
ALCANCES_META = ("whatsapp_business_messaging", "whatsapp_business_management")


@dataclass
class Reporte:
    lineas: list[tuple[str, str, str]] = field(default_factory=list)

    def agregar(self, nivel: str, clave: str, mensaje: str) -> None:
        self.lineas.append((nivel, clave, mensaje))

    def ok(self, clave: str, mensaje: str) -> None:
        self.agregar(OK, clave, mensaje)

    def aviso(self, clave: str, mensaje: str) -> None:
        self.agregar(AVISO, clave, mensaje)

    def falta(self, clave: str, mensaje: str) -> None:
        self.agregar(FALTA, clave, mensaje)

    def error(self, clave: str, mensaje: str) -> None:
        self.agregar(ERROR, clave, mensaje)

    @property
    def listo(self) -> bool:
        return not any(nivel in (FALTA, ERROR) for nivel, _, _ in self.lineas)

    def texto(self) -> str:
        cuerpo = "\n".join(f"{nivel:<7}{clave}: {mensaje}" for nivel, clave, mensaje in self.lineas)
        faltantes = sum(1 for n, _, _ in self.lineas if n in (FALTA, ERROR))
        avisos = sum(1 for n, _, _ in self.lineas if n == AVISO)
        veredicto = (
            f"LISTO para probar en vivo ({avisos} aviso(s))"
            if self.listo
            else f"NO LISTO: {faltantes} problema(s) que bloquean, {avisos} aviso(s)"
        )
        return f"{cuerpo}\n\n{veredicto}"


def _valor(env: Mapping[str, str], clave: str) -> str:
    return str(env.get(clave, "") or "").strip()


def _http_real(url: str, headers: dict | None = None, params: dict | None = None) -> tuple[int, object]:
    try:
        respuesta = httpx.get(url, headers=headers, params=params, timeout=10.0)
    except httpx.HTTPError as exc:
        return 0, {"error": {"message": type(exc).__name__}}
    try:
        return respuesta.status_code, respuesta.json()
    except ValueError:
        return respuesta.status_code, None


# ------------------------------------------------------------------ modelos


def chequear_modelos(env: Mapping[str, str], reporte: Reporte) -> None:
    clave = _valor(env, "DASHSCOPE_API_KEY")
    if not clave:
        reporte.falta("DASHSCOPE_API_KEY", "vacía: los dos agentes usan Qwen y no hay proveedor de respaldo")
    elif len(clave) < 20:
        reporte.error("DASHSCOPE_API_KEY", f"presente pero sospechosamente corta ({len(clave)} caracteres)")
    else:
        reporte.ok("DASHSCOPE_API_KEY", f"presente ({len(clave)} caracteres; no se muestra)")

    base_url = _valor(env, "DASHSCOPE_BASE_URL") or modelos.BASE_URL_DEFAULT
    region = modelos.region(base_url)
    if not base_url.startswith("https://"):
        reporte.error("DASHSCOPE_BASE_URL", "tiene que empezar con https://")
    elif region == "desconocida":
        reporte.aviso("DASHSCOPE_BASE_URL", "host que no es un endpoint conocido de DashScope; verificá la región")
    else:
        origen = "configurada" if _valor(env, "DASHSCOPE_BASE_URL") else "default"
        reporte.ok("DASHSCOPE_BASE_URL", f"región {region} ({origen})")

    for rol, clave_env in (("clientes", "QWEN_SALES_MODEL"), ("gerencia", "QWEN_MANAGER_MODEL")):
        variable, nombre = modelos.nombre_modelo(rol, env)
        origen = "default" if not _valor(env, variable) else variable
        nota = ""
        if rol == "gerencia" and nombre != modelos.MODELO_GERENCIA_DEFAULT:
            nota = " (distinto del documentado; confirmá el endpoint con `make verificar-qwen`)"
        if ":" in nombre:
            reporte.error(clave_env, f"{nombre!r} lleva prefijo de proveedor; va sólo el nombre Qwen")
        else:
            reporte.ok(clave_env, f"{nombre} ({origen}){nota}")
    try:
        cfg_g = modelos.configuracion("gerencia", env)
        cfg_c = modelos.configuracion("clientes", env)
        reporte.ok(
            "QWEN_THINKING",
            f"ventas {'con' if cfg_c['extra_body'].get('enable_thinking') else 'sin'} razonamiento, "
            f"gerencia {'con' if cfg_g['extra_body'].get('enable_thinking') else 'sin'} razonamiento; "
            f"timeout {cfg_g['timeout']:g}s",
        )
    except modelos.ConfiguracionModeloError as exc:
        reporte.error("QWEN", str(exc))
    reporte.aviso("Qwen", "la conexión real se prueba a mano con `make verificar-qwen` (no en CI)")


# ------------------------------------------------------------- equipo/zonas


def chequear_equipo(env: Mapping[str, str], reporte: Reporte) -> None:
    pais = _valor(env, "PAIS_TELEFONO")
    if not pais:
        reporte.aviso("PAIS_TELEFONO", "vacío: se asume 54 (Argentina)")
    elif not pais.isdigit():
        reporte.error("PAIS_TELEFONO", "tiene que ser el código de país en dígitos")
    else:
        reporte.ok("PAIS_TELEFONO", f"configurado ({len(pais)} dígitos)")

    crudos = [t.strip() for t in _valor(env, "TELEFONOS_EQUIPO").split(",") if t.strip()]
    if not crudos:
        reporte.falta(
            "TELEFONOS_EQUIPO",
            "vacío: sin agente de gestión, sin alertas y nadie puede confirmar pedidos",
        )
    else:
        normalizados = [telefono.normalizar(t) for t in crudos]
        invalidos = sum(1 for n in normalizados if not n)
        unicos = {n for n in normalizados if n}
        if invalidos:
            reporte.error("TELEFONOS_EQUIPO", f"{invalidos} número(s) que no se pueden interpretar")
        elif len(unicos) != len(normalizados):
            reporte.aviso("TELEFONOS_EQUIPO", f"{len(normalizados)} número(s), con repetidos")
        else:
            reporte.ok("TELEFONOS_EQUIPO", f"{len(unicos)} número(s) válido(s) (no se muestran)")
        if _valor(env, "NOTIFICAR_SOLO_PRIMERO").lower() != "false" and len(unicos) > 1:
            reporte.aviso("NOTIFICAR_SOLO_PRIMERO", "sólo el primer número recibe los avisos")

    cps = [c for c in _valor(env, "ZONAS_ENTREGA_CP").split(",") if c.strip()]
    locs = [c for c in _valor(env, "ZONAS_ENTREGA_LOCALIDADES").split(",") if c.strip()]
    if cps and locs:
        reporte.ok(
            "ZONAS_ENTREGA", f"{len(cps)} código(s) postal(es) y {len(locs)} localidad(es): la "
            "dirección necesita LOS DOS datos permitidos"
        )
    elif cps:
        reporte.ok("ZONAS_ENTREGA", f"{len(cps)} código(s) postal(es); la localidad no se evalúa")
    elif locs:
        reporte.ok("ZONAS_ENTREGA", f"{len(locs)} localidad(es); el código postal no se evalúa")
    else:
        reporte.falta("ZONAS_ENTREGA", "ninguna lista configurada: ningún pedido se entrega solo")


# --------------------------------------------------------------- WhatsApp


def _graph(env: Mapping[str, str]) -> str:
    version = _valor(env, "META_GRAPH_API_VERSION") or "v21.0"
    return f"https://graph.facebook.com/{version}"


def chequear_whatsapp(env: Mapping[str, str], reporte: Reporte, http: Http | None) -> str:
    """Devuelve el id de la WABA si se pudo deducir (para las plantillas)."""
    token = _valor(env, "WHATSAPP_TOKEN")
    phone_id = _valor(env, "WHATSAPP_PHONE_NUMBER_ID")
    for clave, valor in (
        ("WHATSAPP_TOKEN", token),
        ("WHATSAPP_PHONE_NUMBER_ID", phone_id),
        ("META_APP_SECRET", _valor(env, "META_APP_SECRET")),
        ("META_VERIFY_TOKEN", _valor(env, "META_VERIFY_TOKEN")),
    ):
        if valor:
            reporte.ok(clave, f"presente ({len(valor)} caracteres; no se muestra)")
        else:
            reporte.falta(clave, "vacío")
    waba = _valor(env, "WHATSAPP_BUSINESS_ACCOUNT_ID")
    if not token or not phone_id:
        return waba
    if http is None:
        reporte.aviso("WhatsApp", "sin red: el token y las plantillas no se verificaron")
        return waba

    cabeceras = {"Authorization": f"Bearer {token}"}
    estado, cuerpo = http(f"{_graph(env)}/{phone_id}", headers=cabeceras, params={"fields": "id,quality_rating"})
    if estado == 200 and isinstance(cuerpo, dict):
        reporte.ok("WhatsApp", f"Meta acepta el token para este número (calidad {cuerpo.get('quality_rating') or 'desconocida'})")
    else:
        codigo = (cuerpo or {}).get("error", {}).get("code") if isinstance(cuerpo, dict) else None
        reporte.error("WhatsApp", f"Meta rechaza el token o el número (HTTP {estado}, código {codigo or 'desconocido'})")
        return waba

    estado, cuerpo = http(
        f"{_graph(env)}/debug_token", params={"input_token": token, "access_token": token}
    )
    datos = (cuerpo or {}).get("data") if isinstance(cuerpo, dict) else None
    if estado != 200 or not isinstance(datos, dict):
        reporte.aviso("WHATSAPP_TOKEN", "no pude verificar si es permanente (debug_token no respondió)")
    else:
        tipo = str(datos.get("type") or "desconocido")
        vence = datos.get("expires_at")
        if vence in (0, None) and datos.get("is_valid", True):
            reporte.ok("WHATSAPP_TOKEN", f"permanente (tipo {tipo}, no vence)")
        else:
            horas = max(0.0, (float(vence) - time.time()) / 3600.0) if isinstance(vence, int | float) else 0.0
            reporte.error(
                "WHATSAPP_TOKEN",
                f"TEMPORAL (tipo {tipo}, vence en {horas:.0f} h): usá un token de System User",
            )
        alcances = set(datos.get("scopes") or [])
        faltan = [a for a in ALCANCES_META if a not in alcances]
        if alcances and faltan:
            reporte.error("WHATSAPP_TOKEN", f"le faltan permisos: {', '.join(faltan)}")
        if not waba:
            for granular in datos.get("granular_scopes") or []:
                if granular.get("scope") == "whatsapp_business_management":
                    ids = granular.get("target_ids") or []
                    if len(ids) == 1:
                        waba = str(ids[0])
    return waba


def chequear_plantillas(env: Mapping[str, str], reporte: Reporte, http: Http | None, waba: str) -> None:
    configuradas = {p: _valor(env, p) for p in PLANTILLAS}
    for variable, nombre in configuradas.items():
        if not nombre:
            reporte.aviso(
                variable,
                "vacía (opcional en el piloto): ese aviso sale como texto libre mientras el "
                "destinatario haya escrito en las últimas 24 h",
            )
    con_nombre = {v: n for v, n in configuradas.items() if n}
    if not con_nombre:
        return
    if http is None:
        reporte.aviso("Plantillas", "sin red: no se verificó su aprobación en Meta")
        return
    if not waba:
        reporte.aviso(
            "Plantillas",
            "no pude deducir la cuenta de WhatsApp Business; poné WHATSAPP_BUSINESS_ACCOUNT_ID para verificar la aprobación",
        )
        return
    cabeceras = {"Authorization": f"Bearer {_valor(env, 'WHATSAPP_TOKEN')}"}
    for variable, nombre in con_nombre.items():
        estado, cuerpo = http(
            f"{_graph(env)}/{waba}/message_templates",
            headers=cabeceras,
            params={"name": nombre, "fields": "name,status,language"},
        )
        filas = (cuerpo or {}).get("data") if isinstance(cuerpo, dict) else None
        if estado != 200 or not isinstance(filas, list):
            reporte.aviso(variable, f"'{nombre}': no pude consultar Meta (HTTP {estado})")
            continue
        exactas = [f for f in filas if f.get("name") == nombre]
        if not exactas:
            reporte.error(variable, f"'{nombre}' no existe en la cuenta de WhatsApp Business")
            continue
        estados = {str(f.get("status")) for f in exactas}
        if "APPROVED" in estados:
            reporte.ok(variable, f"'{nombre}' aprobada")
        else:
            reporte.error(variable, f"'{nombre}' en estado {', '.join(sorted(estados))}: no se puede usar")


# ---------------------------------------------------------------- ERPNext


def _pares(env: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    return {
        "agente": (_valor(env, "ERPNEXT_API_KEY"), _valor(env, "ERPNEXT_API_SECRET")),
        "gerencia": (_valor(env, "ERPNEXT_MANAGER_API_KEY"), _valor(env, "ERPNEXT_MANAGER_API_SECRET")),
        "politica": (_valor(env, "ERPNEXT_POLICY_API_KEY"), _valor(env, "ERPNEXT_POLICY_API_SECRET")),
    }


def chequear_erpnext(env: Mapping[str, str], reporte: Reporte, http: Http | None) -> None:
    url = _valor(env, "ERPNEXT_URL").rstrip("/")
    if not url:
        reporte.falta("ERPNEXT_URL", "vacía")
    else:
        reporte.ok("ERPNEXT_URL", "presente")
    for clave in ("ERPNEXT_COMPANY", "ERPNEXT_WAREHOUSE"):
        if _valor(env, clave):
            reporte.ok(clave, "presente")
        else:
            reporte.falta(clave, "vacío: crear un pedido falla cerrado sin los dos nombres exactos")

    pares = _pares(env)
    for rol, (k, s) in pares.items():
        if k and s:
            reporte.ok(f"ERPNext {rol}", "credencial presente")
        else:
            reporte.falta(f"ERPNext {rol}", "credencial incompleta")
    claves = [k for k, _ in pares.values() if k]
    if len(claves) == 3 and len(set(claves)) < 3:
        reporte.falta(
            "ERPNext credenciales",
            "dos de las tres claves son iguales: el LLM tendría Submit o lectura total. Tres usuarios distintos.",
        )
    if http is None or not url or any(not k or not s for k, s in pares.values()):
        if http is None:
            reporte.aviso("ERPNext", "sin red: permisos y depósito no se verificaron")
        return

    # Which roles may submit a Sales Order (standard + custom permissions).
    roles_submit: set[str] = set()
    for doctype in ("DocPerm", "Custom DocPerm"):
        estado, cuerpo = http(
            f"{url}/api/resource/{doctype}",
            headers=_auth(pares["politica"]),
            params={
                "filters": '[["parent","=","Sales Order"],["submit","=",1]]',
                "fields": '["role"]',
                "limit_page_length": 200,
                "parent": "Sales Order",
            },
        )
        if estado == 200 and isinstance(cuerpo, dict):
            roles_submit |= {str(f.get("role")) for f in cuerpo.get("data") or [] if f.get("role")}
    if not roles_submit:
        reporte.aviso("ERPNext permisos", "no pude leer qué roles tienen Submit en Sales Order")

    for rol, par in pares.items():
        estado, cuerpo = http(f"{url}/api/method/frappe.auth.get_logged_user", headers=_auth(par))
        usuario = (cuerpo or {}).get("message") if isinstance(cuerpo, dict) else None
        if estado != 200 or not usuario:
            reporte.error(f"ERPNext {rol}", f"ERPNext rechaza la credencial (HTTP {estado})")
            continue
        if usuario == "Administrator":
            if rol in ROLES_SUBMIT_PROHIBIDOS:
                reporte.error(f"ERPNext {rol}", "es Administrator: el LLM tendría Submit y lectura total")
            else:
                reporte.aviso(f"ERPNext {rol}", "es Administrator: funciona, pero un usuario acotado es más seguro")
            continue
        estado, cuerpo = http(f"{url}/api/resource/User/{usuario}", headers=_auth(par))
        datos = (cuerpo or {}).get("data") if isinstance(cuerpo, dict) else None
        if estado != 200 or not isinstance(datos, dict):
            reporte.aviso(f"ERPNext {rol}", "no pude leer sus roles")
            continue
        roles = {str(r.get("role")) for r in datos.get("roles") or [] if r.get("role")}
        puede_submit = bool(roles & roles_submit) or "System Manager" in roles
        if rol in ROLES_SUBMIT_PROHIBIDOS and puede_submit:
            reporte.error(f"ERPNext {rol}", f"{len(roles)} rol(es), y alguno permite Submit en Sales Order")
        elif rol == "politica" and roles_submit and not puede_submit:
            reporte.error(f"ERPNext {rol}", f"{len(roles)} rol(es) y ninguno permite Submit: nada se confirmaría")
        else:
            reporte.ok(f"ERPNext {rol}", f"{len(roles)} rol(es); Submit: {'sí' if puede_submit else 'no'}")

    deposito = _valor(env, "ERPNEXT_WAREHOUSE")
    empresa = _valor(env, "ERPNEXT_COMPANY")
    if deposito:
        estado, cuerpo = http(f"{url}/api/resource/Warehouse/{deposito}", headers=_auth(pares["politica"]))
        datos = (cuerpo or {}).get("data") if isinstance(cuerpo, dict) else None
        if estado != 200 or not isinstance(datos, dict):
            reporte.error("ERPNEXT_WAREHOUSE", "no existe o no se puede leer")
        elif int(datos.get("is_group") or 0) or int(datos.get("disabled") or 0):
            reporte.error("ERPNEXT_WAREHOUSE", "es un grupo o está deshabilitado")
        elif empresa and str(datos.get("company") or "") != empresa:
            reporte.error("ERPNEXT_WAREHOUSE", "pertenece a otra compañía que ERPNEXT_COMPANY")
        else:
            reporte.ok("ERPNEXT_WAREHOUSE", "existe, habilitado, de la compañía configurada")


def _auth(par: tuple[str, str]) -> dict:
    return {"Authorization": f"token {par[0]}:{par[1]}", "Accept": "application/json"}


# ------------------------------------------------------------ stock/límites


def chequear_stock_y_limites(env: Mapping[str, str], reporte: Reporte, resumen_limites: Callable[[], list[dict]] | None) -> None:
    maestra = _valor(env, "STOCK_CONFIABLE").lower()
    if maestra == "true":
        reporte.ok("STOCK_CONFIABLE", "true: la confianza se gana por producto con conteos confirmados")
    elif maestra in ("", "false"):
        reporte.aviso("STOCK_CONFIABLE", "false: el bot nunca promete stock y nada se auto-confirma")
    else:
        reporte.error("STOCK_CONFIABLE", "tiene que ser true o false")
    horas = _valor(env, "STOCK_CONFIABLE_HORAS")
    try:
        valor = float(horas) if horas else 24.0
        if valor <= 0:
            raise ValueError
        reporte.ok("STOCK_CONFIABLE_HORAS", f"ventana de confianza {'configurada' if horas else 'default'}")
    except ValueError:
        reporte.error("STOCK_CONFIABLE_HORAS", "tiene que ser un número de horas > 0")

    for clave in ("AUTO_CONFIRM_PRICE_LIST", "AUTO_CONFIRM_CURRENCY"):
        if _valor(env, clave):
            reporte.ok(clave, "presente")
        else:
            reporte.aviso(clave, "vacía: el catálogo responde 'precio a confirmar' y nada se auto-confirma")

    if resumen_limites is None:
        reporte.aviso("Límites", "sin Redis: no se verificaron los límites del dueño")
        return
    try:
        filas = resumen_limites()
    except Exception as exc:
        reporte.error("Límites", f"no se pueden leer ({type(exc).__name__}): la política deja TODO pendiente")
        return
    from app import limites as _limites

    for fila in filas:
        nombre = str(fila.get("nombre") or fila.get("alias") or "límite")
        if nombre in _limites.ENTREGA:
            continue  # chequear_entrega reports these, with the fallback line
        origen = {"dueño": "fijado por el dueño", "arranque": "del .env", "default": "default del código"}.get(
            str(fila.get("origen")), str(fila.get("origen"))
        )
        if fila.get("problema"):
            reporte.error(nombre, f"mal configurado ({origen}): {fila['problema']}")
        elif nombre == "AUTO_CONFIRM_MAX" and str(fila.get("valor")) in ("0", "0.0"):
            reporte.aviso(nombre, f"en 0 ({origen}): todo pedido espera al dueño")
        else:
            reporte.ok(nombre, f"válido ({origen})")


def chequear_entrega(env: Mapping[str, str], reporte: Reporte, resumen_limites: Callable[[], list[dict]] | None) -> None:
    """Las reglas de entrega, y sobre todo: ¿hay red de contención o no?

    Read from the owner's own store (through ``resumen_limites``) rather than
    from the environment, because that is where a confirmed change lives — a
    check against the .env would report the bootstrap value and call a
    configured system unconfigured.

    THE LINE THAT MATTERS
    With neither a normal delivery round nor a pickup counter, an expired
    decision request has nothing concrete to offer: the customer gets "no
    answer in time, write to me again" and the order is effectively dropped.
    Nothing is oversold, so this is an AVISO and not a blocker — but the owner
    has to know the fallback is off.
    """
    from app import limites

    if resumen_limites is None:
        reporte.aviso("Entrega", "sin Redis: no se verificaron las reglas de entrega")
        return
    try:
        filas = {
            str(f.get("nombre")): f
            for f in resumen_limites()
            if str(f.get("nombre")) in limites.ENTREGA
        }
    except Exception as exc:
        reporte.error(
            "Entrega",
            f"reglas no legibles ({type(exc).__name__}): no se ofrece ninguna "
            "entrega fuera de día ni retiro",
        )
        return

    def _puesto(nombre: str) -> str:
        fila = filas.get(nombre) or {}
        if fila.get("problema"):
            return ""
        valor = str(fila.get("valor") or "")
        return "" if valor in ("", limites.NINGUNO) else valor

    for nombre, fila in filas.items():
        if fila.get("problema"):
            reporte.error(nombre, f"mal configurado: {fila['problema']}")

    reparto = bool(_puesto("ENTREGA_DIAS") and _puesto("ENTREGA_HORA"))
    retiro = bool(
        _puesto("RETIRO_LOCAL_ACTIVO") == "true"
        and _puesto("RETIRO_LOCAL_DIAS")
        and _puesto("RETIRO_LOCAL_HORA")
    )
    if reparto:
        reporte.ok(
            "ENTREGA_DIAS",
            f"reparto {_puesto('ENTREGA_DIAS')} a las {_puesto('ENTREGA_HORA')}",
        )
    if retiro:
        reporte.ok(
            "RETIRO_LOCAL_DIAS",
            f"retiro {_puesto('RETIRO_LOCAL_DIAS')} a las {_puesto('RETIRO_LOCAL_HORA')}",
        )
    if not reparto and not retiro:
        reporte.aviso(
            "Respaldo de vencimiento",
            "sin días/hora de reparto ni retiro habilitado: una solicitud que "
            "vence no puede ofrecerle nada concreto al cliente y el pedido se "
            "cae. Configurá «días de reparto» y «hora de reparto», o el retiro "
            "por el local",
        )

    if _puesto("ENTREGA_EXCEPCION_ACTIVA") != "true":
        reporte.aviso(
            "ENTREGA_EXCEPCION_ACTIVA",
            "en no: toda entrega fuera de día la decide una persona",
        )
    else:
        faltan = [
            defi.alias[0]
            for clave, defi in limites.ENTREGA.items()
            if clave
            in ("ENTREGA_EXCEPCION_DIAS", "ENTREGA_EXCEPCION_HORA", "ENTREGA_EXCEPCION_CARGO")
            and not _puesto(clave)
        ]
        if faltan:
            reporte.aviso(
                "ENTREGA_EXCEPCION_ACTIVA",
                "en sí pero falta " + ", ".join(faltan) + ": nada queda "
                "pre-autorizado y cada caso lo decide una persona",
            )
        else:
            reporte.ok("ENTREGA_EXCEPCION_ACTIVA", "sí, con días, hora y cargo configurados")
        if not _valor(env, "ENTREGA_CARGO_CUENTA"):
            reporte.aviso(
                "ENTREGA_CARGO_CUENTA",
                "vacía: un cargo de envío no se escribe en el pedido y queda "
                "para que lo agregue una persona (se configura sólo acá, nunca "
                "por WhatsApp)",
            )
        else:
            reporte.ok("ENTREGA_CARGO_CUENTA", "presente")


def chequear_solicitudes(reporte: Reporte) -> None:
    """Drafts the sweep could not get ERPNext to stop reserving.

    An AVISO, not a blocker: nothing is oversold by a draft that holds too much.
    But those units cannot be sold either, and this is the only place the number
    is visible before a customer is told there is no stock.
    """
    from app import solicitudes

    cuantas = solicitudes.trabadas()
    if cuantas is None:
        reporte.aviso(
            "Borradores trabados", "no pude leer el contador (Redis)"
        )
    elif cuantas:
        reporte.aviso(
            "Borradores trabados",
            f"{cuantas}: ERPNext no los deja cerrar y siguen reservando stock. "
            "Hay un ToDo por cada uno; cerralos o confirmalos a mano",
        )
    else:
        reporte.ok("Borradores trabados", "ninguno")


# ------------------------------------------------------------------- entry


def ejecutar(env: Mapping[str, str] | None = None, *, con_red: bool = True) -> Reporte:
    env = os.environ if env is None else env
    reporte = Reporte()
    http = _http_real if con_red else None
    chequear_modelos(env, reporte)
    chequear_equipo(env, reporte)
    waba = chequear_whatsapp(env, reporte, http)
    chequear_plantillas(env, reporte, http, waba)
    chequear_erpnext(env, reporte, http)
    resumen = None
    if _valor(env, "REDIS_URL"):
        try:
            from app import limites

            resumen = limites.resumen
        except Exception as exc:  # pragma: no cover - import-time env problems
            reporte.aviso("Límites", f"módulo de límites no disponible ({type(exc).__name__})")
    chequear_stock_y_limites(env, reporte, resumen)
    chequear_entrega(env, reporte, resumen)
    if _valor(env, "REDIS_URL"):
        chequear_solicitudes(reporte)
    return reporte


def main(argv: list[str]) -> int:
    import app  # noqa: F401  (carga .env)

    reporte = ejecutar(con_red="--sin-red" not in argv)
    print(reporte.texto())
    return 0 if reporte.listo else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
