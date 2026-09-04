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
from urllib.parse import quote

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
# El mismo default que app/whatsapp.py, repetido a propósito: readiness no
# importa ese módulo, que exige el token y el phone id al importarse.
GRAPH_HOST_DEFAULT = "https://graph.facebook.com"
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
    """El proveedor elegido, su clave, su endpoint y los dos modelos.

    Nunca muestra la clave: sólo qué variable la trajo y cuántos caracteres
    tiene. Y nombra SIEMPRE las variables del proveedor activo, porque decirle
    a alguien que le falta DASHSCOPE_API_KEY cuando eligió Gemini lo manda a
    cargar la credencial equivocada.
    """
    try:
        prov = modelos.proveedor(env)
    except modelos.ConfiguracionModeloError as exc:
        reporte.error(modelos.VAR_PROVEEDOR, str(exc))
        return
    origen_prov = "explícito" if _valor(env, modelos.VAR_PROVEEDOR) else "default"
    reporte.ok(modelos.VAR_PROVEEDOR, f"{prov.nombre} — {prov.etiqueta} ({origen_prov})")

    variable_clave, clave = modelos.clave_api(prov, env)
    if not clave:
        reporte.falta(
            prov.clave_principal,
            f"vacía: con {modelos.VAR_PROVEEDOR}={prov.nombre} los dos agentes usan "
            f"{prov.etiqueta} y no hay proveedor de respaldo",
        )
    elif len(clave) < 20:
        reporte.error(
            variable_clave, f"presente pero sospechosamente corta ({len(clave)} caracteres)"
        )
    else:
        alias = "" if variable_clave == prov.clave_principal else f" vía {variable_clave}"
        reporte.ok(
            prov.clave_principal,
            f"presente{alias} ({len(clave)} caracteres; no se muestra)",
        )

    # La clave del OTRO proveedor no habilita nada, y tenerla cargada mientras
    # se usa este es exactamente lo que un "fallback" silencioso aprovecharía.
    for otro in modelos.PROVEEDORES.values():
        if otro.nombre == prov.nombre:
            continue
        variable_otra, _ = modelos.clave_api(otro, env)
        if variable_otra:
            reporte.aviso(
                variable_otra,
                f"cargada pero sin uso: el proveedor activo es {prov.nombre} y no hay "
                "respaldo automático entre proveedores",
            )

    base_url = _valor(env, prov.var_base_url) or prov.base_url_default
    region = modelos.region(base_url)
    if not base_url.startswith("https://"):
        reporte.error(prov.var_base_url, "tiene que empezar con https://")
    elif region == "desconocida":
        reporte.aviso(
            prov.var_base_url,
            f"host que no es un endpoint conocido de {prov.nombre}; verificá la región",
        )
    else:
        origen = "configurada" if _valor(env, prov.var_base_url) else "default"
        reporte.ok(prov.var_base_url, f"región {region} ({origen})")

    for rol in modelos.ROLES:
        variable, nombre = modelos.nombre_modelo(rol, env, prov)
        clave_env = prov.var_modelo[rol][0]
        origen = "default" if not _valor(env, variable) else variable
        nota = ""
        if (
            prov.nombre == "qwen"
            and rol == "gerencia"
            and nombre != modelos.MODELO_GERENCIA_DEFAULT
        ):
            nota = " (distinto del documentado; confirmá el endpoint con `make verificar-modelos`)"
        if ":" in nombre:
            reporte.error(
                clave_env, f"{nombre!r} lleva prefijo de proveedor; va sólo el nombre del modelo"
            )
        else:
            reporte.ok(clave_env, f"{nombre} ({origen}){nota}")

    thinking = [v for v in ("QWEN_THINKING_CLIENTES", "QWEN_THINKING_GERENCIA") if _valor(env, v)]
    if thinking and not prov.razona:
        reporte.aviso(
            "QWEN_THINKING",
            f"{', '.join(thinking)} configurada(s) pero {prov.nombre} no usa esos "
            "controles: no se aplican",
        )
    try:
        cfg_g = modelos.configuracion("gerencia", env)
        cfg_c = modelos.configuracion("clientes", env)
        if prov.razona:
            reporte.ok(
                "QWEN_THINKING",
                f"ventas {'con' if cfg_c['extra_body'].get('enable_thinking') else 'sin'} razonamiento, "
                f"gerencia {'con' if cfg_g['extra_body'].get('enable_thinking') else 'sin'} razonamiento; "
                f"timeout {cfg_g['timeout']:g}s",
            )
        else:
            reporte.ok("LLM_TIMEOUT_SECONDS", f"timeout {cfg_g['timeout']:g}s por llamada")
    except modelos.ConfiguracionModeloError as exc:
        reporte.error(prov.nombre.upper(), modelos.enmascarar(exc, env))
    reporte.aviso(
        prov.nombre,
        "la conexión real se prueba a mano con `make verificar-modelos` (no en CI)",
    )


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
    base = (_valor(env, "META_GRAPH_BASE_URL") or GRAPH_HOST_DEFAULT).rstrip("/")
    return f"{base}/{version}"


def chequear_whatsapp(env: Mapping[str, str], reporte: Reporte, http: Http | None) -> str:
    """Devuelve el id de la WABA si se pudo deducir (para las plantillas)."""
    # Si alguien apuntó Graph a otro lado, que se vea antes que cualquier otra
    # cosa: ahí va el token en el header. Vacío es Meta y no se comenta.
    destino = _valor(env, "META_GRAPH_BASE_URL").rstrip("/")
    if destino and destino != GRAPH_HOST_DEFAULT:
        reporte.error(
            "META_GRAPH_BASE_URL",
            f"los envíos NO van a Meta sino a {destino}: sirve para una prueba "
            "local (demo/), pero en producción vaciá la variable",
        )
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


# Topes que en 0 apagan TODA la auto-confirmación, sin que el 0 sea una
# decisión que alguien tomó. app/policy.py lo dice así: "an unconfigured limit
# is not permission", y con la cantidad por producto en 0 el motivo que se
# acumula es "falta configurar la cantidad máxima por producto" — para todo
# pedido, siempre. Reportar eso como OK ("válido (default del código)") era el
# único lugar del sistema donde un valor que apaga la función se leía como si
# la función estuviera lista.
#
# AUTO_CONFIRM_MAX queda aparte a propósito: ahí el 0 SÍ es la forma de decir
# "que todo lo mire una persona", así que sigue siendo un AVISO.
TOPES_QUE_BLOQUEAN_TODO = ("AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO",)


def _es_cero(valor: object) -> bool:
    """Cero, escrito como sea. "", "0", "0.0" y "0,00" son todos cero.

    Ilegible NO es cero: eso lo reporta la fila con `problema`, que es un
    error, y llamarlo cero acá lo taparía con otro mensaje.
    """
    texto = str(valor if valor is not None else "").strip().replace(",", ".")
    if not texto:
        return True
    try:
        return float(texto) == 0.0
    except ValueError:
        return False


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
        elif nombre in TOPES_QUE_BLOQUEAN_TODO and _es_cero(fila.get("valor")):
            reporte.falta(
                nombre,
                f"en 0 ({origen}): NINGÚN pedido se auto-confirma. Es correcto "
                "—un límite sin configurar no es un permiso— pero no es una "
                "configuración lista: hay que fijarle un valor positivo, o "
                "asumir que todo pedido espera al dueño",
            )
        elif nombre == "AUTO_CONFIRM_MAX" and _es_cero(fila.get("valor")):
            reporte.aviso(nombre, f"en 0 ({origen}): todo pedido espera al dueño")
        else:
            reporte.ok(nombre, f"válido ({origen})")


# A delivery charge is written as a Sales Taxes and Charges row of type "Actual"
# against an account head (erpnext.policy_agregar_cargo). ERPNext will not post
# one to a group account, to a disabled or frozen one, or to an account of
# another company — and a charge BILLED TO THE CUSTOMER posted against an asset
# or equity head is a bookkeeping mistake rather than a matter of taste.
#
# Which heads are legitimate is deliberately left wide: "Freight and Forwarding
# Charges" is an EXPENSE head in ERPNext's standard chart and is what most
# businesses use for a shipping charge, while a "Delivery Income" head is
# Income, and a fee treated as a tax is a Liability. So the only root types
# refused are the two that cannot be any of those.
# ponytail: root_type is the check, not account_type. If a build turns out to
# refuse a specific account_type for this row, add that check then — guessing at
# ERPNext's link filters here would fail a valid configuration.
RAICES_IMPOSIBLES_PARA_UN_CARGO = ("Asset", "Equity")


def chequear_cuenta_cargo(
    env: Mapping[str, str],
    reporte: Reporte,
    http: Http | None,
    *,
    con_cargo: bool,
) -> None:
    """Is ENTREGA_CARGO_CUENTA an account ERPNext will actually accept?

    "Present" was the whole check before this, and presence is not the failure
    mode. A wrong name does not break the bot: the charge write fails, and
    app/solicitudes.py sends every accepted off-day delivery to a person instead
    of confirming it. The owner sees orders quietly stop confirming, days after
    typing an account name into the environment.

    ``con_cargo`` is whether a fee is actually enabled. With one enabled a bad
    account BLOCKS readiness; without one it is an AVISO, so a mistake is still
    visible before he turns fees on.
    """
    cuenta = _valor(env, "ENTREGA_CARGO_CUENTA")
    if not cuenta:
        reporte.aviso(
            "ENTREGA_CARGO_CUENTA",
            "vacía: un cargo de envío no se escribe en el pedido y queda "
            "para que lo agregue una persona (se configura sólo acá, nunca "
            "por WhatsApp)",
        )
        return

    def _mal(detalle: str) -> None:
        consecuencia = (
            ": un cargo acordado no se puede escribir y cada entrega fuera de "
            "día que el cliente acepte termina esperando a una persona"
        )
        if con_cargo:
            reporte.error("ENTREGA_CARGO_CUENTA", detalle + consecuencia)
        else:
            reporte.aviso(
                "ENTREGA_CARGO_CUENTA",
                detalle + " (todavía sin cargo configurado, así que no bloquea)",
            )

    url = _valor(env, "ERPNEXT_URL").rstrip("/")
    par = _pares(env)["politica"]
    if http is None or not url or not all(par):
        reporte.aviso(
            "ENTREGA_CARGO_CUENTA",
            "presente, pero sin red no se verificó que la cuenta exista",
        )
        return

    estado, cuerpo = http(
        f"{url}/api/resource/Account/{quote(cuenta, safe='')}", headers=_auth(par)
    )
    datos = (cuerpo or {}).get("data") if isinstance(cuerpo, dict) else None
    if estado != 200 or not isinstance(datos, dict):
        _mal(f"no existe o no se puede leer (HTTP {estado})")
        return
    if int(datos.get("is_group") or 0):
        _mal("es un grupo, y un cargo no se puede imputar a un grupo")
        return
    if int(datos.get("disabled") or 0):
        _mal("está deshabilitada")
        return
    if str(datos.get("freeze_account") or "").strip().lower() in ("yes", "sí", "si"):
        _mal("está congelada, así que no admite asientos")
        return
    empresa = _valor(env, "ERPNEXT_COMPANY")
    if empresa and str(datos.get("company") or "") != empresa:
        _mal("pertenece a otra compañía que ERPNEXT_COMPANY")
        return
    raiz = str(datos.get("root_type") or "").strip()
    if not raiz:
        reporte.aviso(
            "ENTREGA_CARGO_CUENTA",
            "existe y es de la compañía configurada, pero no pude leer su "
            "root_type: revisá a mano que sea una cuenta de cargo",
        )
        return
    if raiz in RAICES_IMPOSIBLES_PARA_UN_CARGO:
        _mal(
            f"es una cuenta de tipo {raiz}: un cargo que se le cobra al cliente "
            "no va contra el patrimonio ni contra un activo"
        )
        return
    reporte.ok(
        "ENTREGA_CARGO_CUENTA",
        f"existe, habilitada, de la compañía configurada y de tipo {raiz}",
    )


def chequear_entrega(
    env: Mapping[str, str],
    reporte: Reporte,
    resumen_limites: Callable[[], list[dict]] | None,
    http: Http | None = None,
) -> None:
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

    # After a Redis loss with [entrega] changes on record, limites.resumen()
    # reports every delivery row as PERDIDO: nothing is in effect, not the .env
    # either, because that is exactly what limites.entrega() decides in that
    # state. One line for the loss rather than ten "mal configurado" — and an
    # ERROR, because the repair is a person's: the owner has to set the rules
    # again, and a live test started before he does offers no delivery at all
    # while the .env looks fully configured.
    perdidas = {n for n, f in filas.items() if str(f.get("origen")) == limites.PERDIDO}
    for nombre, fila in filas.items():
        if fila.get("problema") and nombre not in perdidas:
            reporte.error(nombre, f"mal configurado: {fila['problema']}")
    if perdidas:
        reporte.error(
            "Entrega",
            "las reglas de entrega se PERDIERON del almacén (Redis) y ERPNext "
            "tiene cambios registrados: no rige ningún valor, tampoco el del "
            ".env, y el sistema no ofrece reparto, entrega fuera de día ni "
            "retiro hasta que el dueño las vuelva a fijar por WhatsApp",
        )

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

    # Checked whether or not the exception is on, so a wrong account name is
    # visible BEFORE he turns fees on rather than the first time one is agreed.
    cargo = _puesto("ENTREGA_EXCEPCION_CARGO")
    reporte_con_cargo = bool(
        _puesto("ENTREGA_EXCEPCION_ACTIVA") == "true"
        and cargo
        and cargo not in ("0", "0.0")
    )
    chequear_cuenta_cargo(env, reporte, http, con_cargo=reporte_con_cargo)


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
    chequear_entrega(env, reporte, resumen, http)
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
