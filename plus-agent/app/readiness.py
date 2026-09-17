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

from app import modelos, reloj, telefono

OK, AVISO, FALTA, ERROR = "OK", "AVISO", "FALTA", "ERROR"

# (status, body-json-o-None). Inyectable para los tests.
Http = Callable[..., tuple[int, object]]

UMBRAL_BORRADORES_PCT = 80.0

PLANTILLAS = (
    "WHATSAPP_STAFF_PENDING_TEMPLATE",
    "WHATSAPP_STAFF_CONFIRMED_TEMPLATE",
    "WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE",
    "WHATSAPP_CUSTOMER_REJECTED_TEMPLATE",
    "WHATSAPP_CUSTOMER_CANCELLED_TEMPLATE",
    "WHATSAPP_STAFF_ALERT_TEMPLATE",
    # Los dos del barrido de vencimientos (app/solicitudes.py). Se chequean acá
    # para que una plantilla que falta o está mal escrita se vea en el
    # preflight y no en el primer vencimiento — que es cuando el cliente se
    # queda sin enterarse de que su solicitud venció.
    "WHATSAPP_CUSTOMER_EXPIRED_TEMPLATE",
    "WHATSAPP_CUSTOMER_FALLBACK_TEMPLATE",
    "WHATSAPP_CUSTOMER_REVIEW_EXPIRED_TEMPLATE",
    "WHATSAPP_CUSTOMER_PENDING_TEMPLATE",
    "WHATSAPP_CUSTOMER_PENDING_CLOSED_TEMPLATE",
    # El aviso antes de la entrega (una fila de app/agenda.py), por lo mismo:
    # sale horas antes de una entrega prometida, así que la ventana de 24 h ya
    # está cerrada casi siempre.
    "WHATSAPP_CUSTOMER_DELIVERY_LEAD_TEMPLATE",
)
# Las que NO pueden contar con la ventana de 24 h, porque las dispara un barrido
# y no una respuesta a un mensaje del cliente. El resto son opcionales en el
# piloto de verdad; estas no.
#
# Y se parten en dos, porque el NIVEL que corresponde no es el mismo. El
# criterio de este archivo es el de `chequear_solicitudes`: FALTA bloquea
# cuando el sistema haría algo MAL, no cuando es menos útil.
#
# Estas tres las dispara `solicitudes.tick()`, que corre en el hilo del barrido
# SIEMPRE, sin límite que lo apague. Si falta la plantilla, el aviso se aparca y
# el cliente nunca se entera de que su solicitud venció — con readiness diciendo
# «LISTO para probar en vivo». Eso es el sistema haciendo algo mal, así que
# bloquea.
PLANTILLAS_BARRIDO_SIEMPRE = (
    "WHATSAPP_CUSTOMER_EXPIRED_TEMPLATE",
    "WHATSAPP_CUSTOMER_FALLBACK_TEMPLATE",
    "WHATSAPP_CUSTOMER_REVIEW_EXPIRED_TEMPLATE",
)
# Estas tres sólo salen si el dueño encendió el límite que las gatea, y los tres
# arrancan en NINGUNO. Apagado el flujo, no se manda nada y no hay nada mal:
# AVISO. Encendido, es exactamente el mismo problema que arriba: FALTA.
PLANTILLAS_BARRIDO_OPCIONAL = {
    "WHATSAPP_CUSTOMER_PENDING_TEMPLATE": "PENDIENTE_AVISO_HORAS",
    "WHATSAPP_CUSTOMER_PENDING_CLOSED_TEMPLATE": "PENDIENTE_CIERRE_HORAS",
    # El aviso antes de la entrega (una fila de app/agenda.py). Sale horas
    # antes de una entrega prometida, o sea casi siempre fuera de la ventana de
    # 24 h, así que sin plantilla no falla a veces: falla SIEMPRE. Arranca en
    # NINGUNO como los otros dos.
    "WHATSAPP_CUSTOMER_DELIVERY_LEAD_TEMPLATE": "AVISO_ANTES_DE_ENTREGA_HORAS",
}
PLANTILLAS_FUERA_DE_VENTANA = PLANTILLAS_BARRIDO_SIEMPRE + tuple(
    PLANTILLAS_BARRIDO_OPCIONAL
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


def _codigo_pais(env: Mapping[str, str]) -> str:
    """El código de discado de ESTE mapping, con la precedencia del runtime.

    `app/telefono.py` congela `PAIS` y `REGION` cuando se importa, o sea con el
    entorno del PROCESO. Un preflight de un .env candidato que declara otro país
    normalizaba sus teléfonos con el país que está corriendo, y con eso contesta
    mal sobre duplicados y sobre si el dueño está en el equipo. Acá se rehace la
    misma cadena —explícito, derivado, default— pero leyendo `env`.
    """
    from app import pais as _pais_mod

    explicito = _valor(env, "PAIS_TELEFONO")
    if explicito:
        return explicito
    return _pais_mod.codigo_de(_valor(env, "PAIS_NEGOCIO")) or _pais_mod.CODIGO_POR_DEFECTO


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
    # EL TECHO DE PASOS, que es lo que acota la cuenta del modelo. Se lee desde
    # `env` y no desde `pasos.PASOS_*` a propósito: readiness tiene que poder
    # mirar un `.env` que todavía no es el del proceso — es el chequeo que se
    # corre ANTES de recrear el contenedor. Un valor inválido no se reporta acá
    # como aviso: `app/pasos.py` revienta al importar, o sea que el proceso no
    # arranca, que es más fuerte que una línea en un informe.
    for clave, por_defecto, quien in (
        ("PASOS_MAX_CLIENTES", "8", "ventas"),
        ("PASOS_MAX_GERENCIA", "14", "gerencia"),
    ):
        crudo = _valor(env, clave) or por_defecto
        try:
            techo = int(crudo)
            if techo <= 0:
                raise ValueError
        except ValueError:
            reporte.error(clave, f"{crudo!r} no es un entero > 0: el agente no arranca")
            continue
        reporte.ok(
            clave,
            f"hasta {techo} llamadas al modelo por mensaje de {quien}"
            + ("" if _valor(env, clave) else " (default)"),
        )


# ------------------------------------------------------------- equipo/zonas


def chequear_equipo(env: Mapping[str, str], reporte: Reporte) -> None:
    from app import pais as _pais_mod

    territorio = str(_valor(env, "PAIS_NEGOCIO")).strip().upper()
    if not territorio:
        reporte.aviso(
            "PAIS_NEGOCIO",
            "vacío: el país no está declarado, así que el teléfono y los montos "
            "salen de PAIS_TELEFONO y LOCALE por separado (y pueden contradecirse)",
        )
    elif len(territorio) != 2 or not territorio.isalpha():
        reporte.error(
            "PAIS_NEGOCIO",
            f"{territorio!r} no es un código ISO de dos letras (AR, US, IN, BR)",
        )
    else:
        # NO ALCANZA CON LA FORMA, y esto es lo que dejaba pasar un `ZZ`: dos
        # letras, alfabético, y ni libphonenumber ni CLDR lo conocen. El runtime
        # entonces no deriva nada y se cae al default argentino —+54 y es_AR—
        # mientras el informe decía OK, o sea que un typo en el .env sale a
        # normalizar teléfonos y a escribir montos con reglas de otro país sin
        # una línea en ningún lado.
        #
        # Se le pregunta a los MISMOS datos que usa el runtime, y se le pregunta
        # por el territorio DE ESTE mapping, sin escribir en `os.environ`: la
        # versión anterior lo pisaba y lo restauraba, lo que es una variable
        # global compartida con todo el proceso para responder una pregunta que
        # no necesitaba ninguna.
        codigo = _pais_mod.codigo_de(territorio)
        idioma = _pais_mod.locale_de(territorio)
        if not codigo or not idioma:
            falta = " ni ".join(
                nombre
                for nombre, dato in (
                    ("código de discado (libphonenumber)", codigo),
                    ("idioma oficial (CLDR)", idioma),
                )
                if not dato
            )
            reporte.error(
                "PAIS_NEGOCIO",
                f"{territorio!r} no tiene {falta}: no es un país que se pueda "
                f"derivar, y todo saldría con el default "
                f"(+{_pais_mod.CODIGO_POR_DEFECTO} · {_pais_mod.LOCALE_POR_DEFECTO}) "
                "sin avisar",
            )
        else:
            reporte.ok("PAIS_NEGOCIO", f"{territorio}: +{codigo} · {idioma}")

    pais = _valor(env, "PAIS_TELEFONO")
    if not pais:
        # Se dice el código que va a salir DE VERDAD, no de dónde sale. Con un
        # `PAIS_NEGOCIO` que no deriva, «sale de PAIS_NEGOCIO (ZZ)» es una línea
        # OK que contradice al ERROR de arriba y deja creyendo que el país está
        # puesto cuando lo que se va a usar es el default.
        reporte.ok(
            "PAIS_TELEFONO",
            f"vacío: se usa +{_codigo_pais(env)} "
            + (f"(de PAIS_NEGOCIO={territorio})" if _pais_mod.codigo_de(territorio)
               else "(el default: PAIS_NEGOCIO no está puesto o no deriva)"),
        )
    elif not pais.isdigit():
        reporte.error("PAIS_TELEFONO", "tiene que ser el código de país en dígitos")
    else:
        derivado = _pais_mod.codigo_de(territorio)
        if derivado and derivado != pais:
            # LA CONTRADICCIÓN, DICHA. Arreglar `.env.example` sirve para la
            # próxima instalación y no hace nada por las que ya existen: un
            # .env escrito con la plantilla vieja tiene `PAIS_TELEFONO=54`
            # adentro, gana sobre `PAIS_NEGOCIO`, y cambiar el país que la
            # plantilla dice que es «lo único que hay que poner» deja los
            # teléfonos normalizados como argentinos. Callado, eso son clientes
            # que dejan de matchear.
            reporte.aviso(
                "PAIS_TELEFONO",
                f"+{pais} puesto a mano GANA sobre PAIS_NEGOCIO={territorio} "
                f"(+{derivado}): los teléfonos se normalizan con reglas de "
                f"+{pais} y los montos con los de {territorio}. Si no es a "
                "propósito, dejala vacía",
            )
        else:
            reporte.ok("PAIS_TELEFONO", f"configurado ({len(pais)} dígitos)")

    crudos = [t.strip() for t in _valor(env, "TELEFONOS_EQUIPO").split(",") if t.strip()]
    if not crudos:
        reporte.falta(
            "TELEFONOS_EQUIPO",
            "vacío: sin agente de gestión, sin alertas y nadie puede confirmar pedidos",
        )
    else:
        codigo_pais = _codigo_pais(env)
        normalizados = [telefono.normalizar(t, codigo_pais) for t in crudos]
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
        # El resumen del día va al DUEÑO, explícito, no al primero de la lista.
        dueno_crudo = _valor(env, "TELEFONO_DUENO")
        if dueno_crudo:
            dueno = telefono.normalizar(dueno_crudo, codigo_pais)
            if not dueno:
                reporte.error("TELEFONO_DUENO", "no se puede interpretar")
            elif dueno not in unicos:
                reporte.error(
                    "TELEFONO_DUENO",
                    "no está en TELEFONOS_EQUIPO: el resumen del día no se manda",
                )
            else:
                reporte.ok("TELEFONO_DUENO", "configurado y es uno del equipo (no se muestra)")
        elif len(unicos) == 1:
            reporte.ok("TELEFONO_DUENO", "vacío; el único número del equipo recibe el resumen del día")
        else:
            reporte.error(
                "TELEFONO_DUENO",
                f"vacío con {len(unicos)} números en el equipo: el resumen del día no sabe "
                "a quién ir y no se manda",
            )


# ------------------------------------------------------------------- Panel


def chequear_idioma(env: Mapping[str, str], reporte: Reporte) -> None:
    """En qué idioma va a hablar cada agente, dicho en voz alta.

    El interruptor existe, anda y está probado en CI —hay una celda entera con
    `IDIOMA_DEFAULT=en`—, pero no se veía por ningún lado: `.env.example` lo
    nombraba de refilón adentro del comentario de LOCALE y el preflight no lo
    mencionaba. O sea que el que instalaba para un dueño que lee en inglés no
    tenía cómo enterarse de que existía, y lo único que veía era un agente
    contestando en castellano.

    Esto no valida nada que pueda fallar: informa una decisión. Por eso es `ok`
    y nunca `error`, salvo un valor escrito mal, que sí es un error porque el
    sistema lo ignora en silencio y se va al default.
    """
    from app import idioma

    crudo = _valor(env, "IDIOMA_DEFAULT")
    if crudo and crudo.strip().lower() not in idioma.IDIOMAS:
        reporte.error(
            "IDIOMA_DEFAULT",
            f"«{crudo}» no es un idioma conocido ({', '.join(sorted(idioma.IDIOMAS))}): "
            "se ignora y todo sale en el idioma por defecto",
        )
        return
    por_defecto = crudo.strip().lower() if crudo else idioma.ES

    # CON LA MISMA REGLA QUE EL RUNTIME, no con una más estricta. Acá se
    # comparaba el valor crudo contra `IDIOMA_GERENCIA`, pero
    # `limites.idioma_gerencia()` lo pasa por `idioma.normalizar`, que acepta
    # cómo lo escribe una persona: «english», «inglés», «eng». O sea que un
    # `.env` que FUNCIONA —el sistema lo entiende y contesta en inglés— no
    # pasaba el preflight, y el preflight existe para decir qué va a hacer el
    # sistema, no para inventar un contrato más angosto.
    #
    # `IDIOMA_DEFAULT` se queda estricto arriba a propósito: `idioma.por_defecto`
    # sí compara contra los códigos, así que ahí el estricto ES el runtime.
    crudo_gerencia = _valor(env, "IDIOMA_GERENCIA").strip()
    fijado = idioma.normalizar(crudo_gerencia) or ""
    if crudo_gerencia and not fijado:
        reporte.error(
            "IDIOMA_GERENCIA",
            f"«{crudo_gerencia}» no es un idioma conocido: se ignora",
        )
        return

    # El del dueño puede estar guardado —él lo cambia por WhatsApp— y eso le
    # GANA al `.env`. Se informa lo que el sistema va a hacer de verdad, no lo
    # que dice el archivo, que es la diferencia entre un preflight y un `cat`.
    #
    # SE PREGUNTA POR LO GUARDADO Y NO POR LA RESOLUCIÓN YA HECHA. `idioma.
    # gerencia()` cae a `os.environ` cuando no hay nada guardado, o sea al `.env`
    # del proceso que está corriendo ESTE chequeo — que es justamente el viejo,
    # el que se quiere reemplazar. Con `IDIOMA_GERENCIA=en` en el archivo
    # candidato y `es` exportado, el preflight decía «el dueño recibe ES» sobre
    # un archivo que dice lo contrario. Y `guardado` se deducía comparando dos
    # valores que venían de fuentes distintas, así que también mentía.
    from app import limites

    try:
        del_almacen = limites.idioma_gerencia_guardado()
    except Exception as exc:
        # `idioma_gerencia_guardado` YA convierte a `None` lo que se espera que
        # falle (Redis caído, coordinación). Lo que llegue acá es lo que no se
        # esperaba, y tragarlo como `None` hacía que el reporte siguiera y
        # terminara en `ok`: un preflight en verde sobre un error de programa.
        # No se propaga —`ejecutar()` llama a esto sin handler— pero se anota.
        reporte.error(
            "IDIOMA_GERENCIA",
            f"no pude leer el idioma guardado ({type(exc).__name__})",
        )
        return
    # LAS TRES RESPUESTAS, Y LAS TRES SEPARADAS. Acá decía `if del_almacen:`, que
    # mete el `None` en la misma rama que el `""` — o sea que inventé el contrato
    # de tres estados y lo colapsé una línea después. Con Redis caído
    # `limites.idioma_gerencia()` se va al DEFAULT sin mirar el entorno, así que
    # informar `fijado or por_defecto` hace que el preflight y el runtime
    # contesten distinto justo cuando algo ya está roto, que es cuando más se
    # mira el preflight.
    if del_almacen is None:
        del_dueno, guardado = por_defecto, False
    elif del_almacen:
        del_dueno, guardado = del_almacen, True
    else:
        del_dueno, guardado = fijado or por_defecto, False

    reporte.ok(
        "IDIOMA_DEFAULT",
        f"el dueño recibe {del_dueno.upper()}"
        + (" (lo cambió él desde su teléfono)" if guardado else "")
        + f"; a un cliente se le espeja el idioma y, si no se sabe, {por_defecto.upper()}",
    )


def chequear_panel(env: Mapping[str, str], reporte: Reporte) -> None:
    """El panel: quién entra, y quién de los que entran puede decidir.

    El panel es OPCIONAL —un despliegue que no lo usa está bien— así que «no hay
    ninguno» es un aviso y no un bloqueo. Lo que sí bloquea es un panel
    configurado MAL, que es peor que no tenerlo: hasta ahora una entrada rota de
    `DASHBOARD_TOKENS` simplemente se descartaba en silencio, o sea que el dueño
    pegaba un token en el `.env`, no funcionaba, y no había ni un log donde
    enterarse.

    Se parsea con `dashboard.entradas_de_tokens`, el MISMO parser que usa el
    panel, así que este chequeo no puede aprobar una entrada que el panel
    después rechaza ni al revés.

    Ningún token se imprime, ni entero ni cortado, igual que con
    TELEFONOS_EQUIPO: esta salida es lo que la gente pega en un chat.
    """
    from app import dashboard

    # `dashboard.normalizar_token` y no `_valor`: los dos recortan espacios, pero
    # sólo uno de los dos es LA MISMA FUNCIÓN que usa `quien()` para comparar. Con
    # `_valor` el chequeo validaba una cosa y el panel comparaba otra, y un
    # `DASHBOARD_API_TOKEN=" … "` entrecomillado daba preflight en verde y 401 en
    # todas las peticiones. Ver `dashboard.normalizar_token`.
    compartido = dashboard.normalizar_token(env.get("DASHBOARD_API_TOKEN", ""))
    validos, problemas = dashboard.entradas_de_tokens(_valor(env, "DASHBOARD_TOKENS"))

    for motivo in problemas:
        reporte.error("DASHBOARD_TOKENS", f"{motivo}: esa persona no entra al panel")

    if compartido and len(compartido) < dashboard.TOKEN_MINIMO:
        reporte.error(
            "DASHBOARD_API_TOKEN",
            f"tiene {len(compartido)} caracteres y el mínimo es "
            f"{dashboard.TOKEN_MINIMO}: no lo acepta nadie",
        )
    elif compartido and compartido in validos:
        # `quien()` recorre los tokens POR PERSONA antes que el compartido, así
        # que con este choque el token que comparte todo el equipo pasa a
        # resolver a esa persona — y con ella, al derecho de confirmar pedidos.
        # Un copy-paste sube privilegios sin que nada lo diga.
        reporte.error(
            "DASHBOARD_API_TOKEN",
            "es igual a uno de DASHBOARD_TOKENS: el token compartido pasaría a ser "
            "esa persona y podría confirmar pedidos",
        )

    if not compartido and not validos:
        reporte.aviso(
            "Panel",
            "sin DASHBOARD_API_TOKEN ni DASHBOARD_TOKENS: contesta 503 y no "
            "muestra ningún dato",
        )
        return

    # SIN el país del mapping, a propósito, y esto NO es un olvido del arreglo
    # de `chequear_equipo`. Acá los dos lados se comparan entre sí: la otra
    # mitad sale de `dashboard.entradas_de_tokens`, que normaliza con el país
    # del proceso y es el mismo parser que usa el panel en vivo. Pasarle el país
    # candidato a una sola de las dos mitades las despega y empieza a contar
    # como «mirón» a alguien del equipo. Lo que se chequea acá es si un token
    # pertenece a alguien del equipo, y eso sólo tiene sentido con los dos lados
    # escritos igual.
    del_equipo = {
        numero
        for numero in (
            telefono.normalizar(t) for t in _valor(env, "TELEFONOS_EQUIPO").split(",")
        )
        if numero
    }
    mirones = {n for n in validos.values() if n not in del_equipo}
    deciden = {n for n in validos.values() if n in del_equipo}

    if validos:
        reporte.ok(
            "DASHBOARD_TOKENS",
            f"{len(validos)} token(s) por persona, {len(deciden)} de ellos del "
            "equipo (no se muestran)",
        )
    if mirones:
        reporte.aviso(
            "DASHBOARD_TOKENS",
            f"{len(mirones)} número(s) con token no están en TELEFONOS_EQUIPO: "
            "entran y miran, pero no pueden confirmar nada",
        )
    if compartido and not deciden:
        reporte.aviso(
            "Panel",
            "sólo hay token compartido: alcanza para mirar, y ninguna decisión se "
            "puede tomar desde el panel (un token sin nombre no se puede auditar)",
        )


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


def _limite_encendido(
    nombre: str, resumen_limites: Callable[[], list[dict]] | None
) -> bool | None:
    """¿El dueño encendió este límite? None = no se pudo saber.

    Tres estados y no dos: aplastar el «no sé» en False diría que el flujo está
    apagado sin haberlo mirado, que es la clase de afirmación que este archivo
    existe para no hacer.
    """
    if resumen_limites is None:
        return None
    from app import limites

    try:
        filas = {str(f.get("nombre")): f for f in resumen_limites()}
    except Exception:
        return None
    fila = filas.get(nombre)
    if fila is None or fila.get("problema"):
        return None
    if str(fila.get("origen")) == limites.PERDIDO:
        return None
    return str(fila.get("valor") or "") not in ("", limites.NINGUNO)


def _plantillas_del_dueno(
    resumen_limites: Callable[[], list[dict]] | None,
) -> dict[str, str]:
    """Los nombres de plantilla que FIJÓ el dueño. Vacío si no se pudo leer.

    Sólo `origen == dueño`: lo que viene del `.env` o del default del código no
    entra, porque el `.env` que importa acá es el CANDIDATO que se está
    validando y no el del proceso que corre el chequeo. `NINGUNO` se mapea a ""
    —el dueño la borró— y eso tiene que ganarle al archivo igual que un nombre.
    """
    if resumen_limites is None:
        return {}
    from app import limites

    try:
        filas = resumen_limites()
    except Exception:
        return {}
    puestas = {}
    for fila in filas:
        nombre = str(fila.get("nombre") or "")
        if nombre not in PLANTILLAS or fila.get("problema"):
            continue
        if str(fila.get("origen")) != limites.ORIGEN_DUENO:
            continue
        valor = str(fila.get("valor") or "")
        puestas[nombre] = "" if valor == limites.NINGUNO else valor
    return puestas


def chequear_plantillas(
    env: Mapping[str, str],
    reporte: Reporte,
    http: Http | None,
    waba: str,
    resumen_limites: Callable[[], list[dict]] | None = None,
) -> None:
    _FUERA = (
        "vacía, y este aviso lo dispara un barrido HORAS después del último "
        "mensaje del cliente: la ventana de 24 h ya está cerrada, no hay texto "
        "libre posible, y el aviso se aparca sin que el cliente se entere. "
        "Registrá la plantilla en Meta"
    )
    # EL NOMBRE QUE VA A USAR EL ENVÍO, no el que dice el archivo. Desde que las
    # plantillas son ajustes, `notificar.plantilla_vigente` resuelve el almacén
    # del dueño ANTES que el `.env`; leer sólo el archivo acá dejaba a readiness
    # verificando en Meta un nombre distinto del que se manda — y contestando
    # «falta» sobre una plantilla que el dueño cargó desde el panel, o «aprobada»
    # sobre una que reemplazó. Es el mismo defecto que documenta
    # `limites.idioma_gerencia_guardado`: un preflight que contesta sobre otra
    # configuración es peor que no tenerlo.
    #
    # Se usa `resumen_limites`, que YA está inyectado para los límites, así que
    # `--sin-red` y los tests siguen pudiendo no pasarlo: sin él manda el `.env`,
    # que es exactamente lo que pasaba antes.
    guardadas = _plantillas_del_dueno(resumen_limites)
    configuradas = {p: guardadas.get(p) or _valor(env, p) for p in PLANTILLAS}
    for variable, nombre in configuradas.items():
        if nombre:
            continue
        if variable in PLANTILLAS_BARRIDO_SIEMPRE:
            # BLOQUEA. El barrido de vencimientos corre siempre, así que sin
            # esta plantilla el despliegue no está listo para una prueba en
            # vivo: se le va a vencer la solicitud a alguien y no se le va a
            # poder decir. Decir «LISTO» ahí sería el informe mintiendo.
            reporte.falta(variable, _FUERA)
        elif variable in PLANTILLAS_BARRIDO_OPCIONAL:
            limite = PLANTILLAS_BARRIDO_OPCIONAL[variable]
            encendido = _limite_encendido(limite, resumen_limites)
            if encendido is True:
                reporte.falta(variable, f"{_FUERA} (el dueño encendió {limite})")
            elif encendido is None:
                # Ni sí ni no: no se pudo leer el límite. AVISO y no FALTA,
                # porque `--sin-red` es la puerta de `deploy.yml` y tampoco
                # puede verificar la aprobación en Meta — bloquear el
                # despliegue por algo que no se pudo mirar deja el check en
                # rojo para siempre, que es peor que no tenerlo.
                reporte.aviso(
                    variable,
                    f"vacía, y no pude leer {limite} para saber si el flujo está "
                    f"encendido. Si lo está, este aviso sale fuera de la ventana "
                    f"de 24 h y no le llega a nadie",
                )
            else:
                reporte.aviso(
                    variable,
                    f"vacía (el flujo está apagado: {limite} en NINGUNO). Antes "
                    f"de encenderlo, registrá la plantilla en Meta: el aviso sale "
                    f"fuera de la ventana de 24 h y sin plantilla no llega",
                )
        else:
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
    # SE CUENTAN LAS LECTURAS, NO SE MIRA SI EL CONJUNTO QUEDÓ VACÍO.
    #
    # Un 403 no agrega nada al set, así que «vacío» y «una de las dos falló»
    # daban lo mismo: con DocPerm devolviendo un rol y Custom DocPerm en 403,
    # `roles_submit` queda no-vacío, el guardia de abajo no salta, y la
    # conclusión sale de MEDIA lectura —justo el defecto que ese guardia vino
    # a arreglar, una capa más adentro—. Los permisos custom son los que un
    # ERPNext real usa para acotar un rol, o sea la mitad que más importa.
    permisos_leidos = 0
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
            permisos_leidos += 1
            roles_submit |= {str(f.get("role")) for f in cuerpo.get("data") or [] if f.get("role")}
    if permisos_leidos < 2:
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
        # VACÍO NO ES LO MISMO QUE NO VINO, y Frappe distingue las dos cosas:
        # una credencial sin permiso sobre la tabla hija devuelve el User SIN la
        # clave `roles`, mientras que un usuario que de verdad no tiene ninguno
        # la devuelve como lista vacía. Colapsarlas hacía que «este usuario no
        # tiene roles» —que en política significa que NADA se confirma nunca—
        # saliera como un aviso de lectura en vez del error que es.
        tabla_de_roles = datos.get("roles")
        if tabla_de_roles is None:
            reporte.aviso(
                f"ERPNext {rol}",
                "el User se lee pero su tabla de roles no viene en la respuesta, así "
                "que no puedo comprobar si tiene Submit en Sales Order: dar lectura "
                f"de User a la credencial de {rol}",
            )
            continue
        roles = {str(r.get("role")) for r in tabla_de_roles if r.get("role")}
        # DOS LECTURAS TIENEN QUE HABER SALIDO BIEN PARA PODER DECIR «Submit: no».
        #
        # Sin ellas `roles & roles_submit` es vacío ∩ vacío, así que `puede_submit`
        # sale False por no haber medido nada —no por no tener el permiso—, y las
        # tres identidades se imprimían como `OK ... 0 rol(es); Submit: no`.
        # Incluida política, para la que «Submit: no» sería la PEOR configuración
        # posible del sistema: nada se confirmaría nunca. El `elif` de abajo existe
        # para cazar exactamente eso y está guardado por `roles_submit`, así que
        # cuando la lectura falla tampoco corre: el único chequeo de la regla de
        # las tres identidades quedaba en verde por no haber podido mirar.
        #
        # Medido en vivo el 2026-09-15 contra agentcrm4: la credencial de política
        # tiene un solo rol («Politica IA») y ese rol no lee DocPerm ni System
        # Settings, así que las dos lecturas daban 403 y el reporte decía OK tres
        # veces. La separación estaba bien —se verificó a mano con `bench`—, pero
        # el reporte habría dicho lo mismo si hubiera estado mal.
        #
        # Mismo criterio que «ERPNext zona» más abajo: lo que no se pudo mirar es
        # AVISO con el permiso que falta, nunca FALTA (rojo para siempre) ni un OK
        # afirmando lo que no se vio.
        if permisos_leidos < 2:
            reporte.aviso(
                f"ERPNext {rol}",
                "no pude comprobar si tiene Submit en Sales Order porque no pude leer "
                "qué roles lo permiten: dar lectura de DocPerm y Custom DocPerm a la "
                "credencial de política",
            )
            continue
        puede_submit = bool(roles & roles_submit) or "System Manager" in roles
        if rol in ROLES_SUBMIT_PROHIBIDOS and puede_submit:
            reporte.error(f"ERPNext {rol}", f"{len(roles)} rol(es), y alguno permite Submit en Sales Order")
        elif rol == "politica" and not puede_submit:
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

    chequear_zona_erpnext(env, reporte, http, url, pares["politica"])


def chequear_zona_erpnext(
    env: Mapping[str, str],
    reporte: Reporte,
    http: Http,
    url: str,
    par: tuple[str, str],
) -> None:
    """Que ERPNext y el agente estén en la MISMA zona, porque se supone y no se mira.

    ERPNext guarda `creation` SIN zona, en la hora de su propio sistema —
    `System Settings.time_zone`, que el asistente de instalación pone según el
    país elegido—. Y TRES lugares interpretan un sello así en
    BUSINESS_TIMEZONE, porque es el único reloj con el que decide todo el resto
    del código: `pendientes.edad_horas`, `autonomia._creacion` y
    `inventario._momento` (que lee `posting_date` + `posting_time` de la Stock
    Reconciliation). Son DOS zonas configuradas por separado y supuestas
    iguales, y hasta acá nada las comparaba. Una comparación cubre los tres.

    Cuando no coinciden, TODA edad sale corrida por el offset, en silencio, y
    ningún test lo puede ver porque los tests comparten la suposición del
    código. Medido con un borrador creado hace 2 h:

        ERPNext en la zona del negocio ->  2.00 h   (bien)
        ERPNext en UTC                 -> -1.00 h   (error -3 h)
        ERPNext en Asia/Kolkata        -> -6.50 h   (error -8.5 h)

    Y en la dirección de un cliente configurado sin cuidado no es «tarde» sino
    «nunca, hasta más tarde»: la edad sale NEGATIVA, el borrador se lee como
    creado en el futuro, y no empieza a envejecer hacia las 48 h del
    recordatorio ni hacia las 168 h del cierre hasta que pasa el offset.

    POR QUÉ BLOQUEA
    El criterio de este archivo es el de `chequear_solicitudes`: FALTA bloquea
    cuando el sistema haría algo MAL. Estas edades gatean el recordatorio al
    cliente y el cierre que le suelta el stock, así que una zona distinta es el
    sistema mandando esos dos a la hora equivocada. Y se arregla con una línea
    en ERPNext, así que bloquear no deja a nadie trabado sin salida.

    El tercer lugar sube la apuesta: la edad que calcula `inventario._momento`
    decide `confiable`, que es un freno de `policy.evaluar`. Con la zona del
    agente al OESTE de la de ERPNext las edades salen más chicas, la ventana de
    STOCK_CONFIABLE_HORAS se ensancha por el offset y un conteo de hace 30 h
    pasa como fresco con la ventana en 24. Ahí una zona mal puesta no informa
    mal: auto-confirma un pedido sobre stock que nadie contó recién.

    POR QUÉ NO BLOQUEA CUANDO NO SE PUDO LEER
    Mismo motivo que las plantillas opcionales de más arriba: `--sin-red` es la
    puerta de `deploy.yml` y no puede verificar nada remoto, y `System
    Settings` es un Single que puede necesitar un permiso que la credencial de
    política no tenga. Bloquear por algo que no se pudo mirar deja el check en
    rojo para siempre, que es peor que no tenerlo. AVISO, con el motivo y qué
    permiso falta.
    """

    # El default sale de `pendientes`, que es quien lo usa para decidir: dos
    # copias del nombre de la zona es exactamente la clase de deriva que este
    # chequeo vino a cerrar.
    esperada = _valor(env, "BUSINESS_TIMEZONE") or reloj.ZONA_DEFAULT
    # `System Settings` es un Single: el nombre del doc es el del doctype, y
    # lleva un espacio que hay que escapar.
    unico = quote("System Settings", safe="")
    estado, cuerpo = http(f"{url}/api/resource/{unico}/{unico}", headers=_auth(par))
    datos = (cuerpo or {}).get("data") if isinstance(cuerpo, dict) else None
    declarada = str((datos or {}).get("time_zone") or "").strip() if isinstance(datos, dict) else ""
    if not declarada:
        reporte.aviso(
            "ERPNext zona",
            f"no pude leer System Settings.time_zone (HTTP {estado}): sin eso no puedo "
            f"confirmar que ERPNext esté en {esperada}, y una zona distinta corre TODA "
            "edad por el offset. Dar lectura de System Settings a la credencial de política.",
        )
    elif declarada != esperada:
        reporte.falta(
            "ERPNext zona",
            f"ERPNext dice {declarada} y BUSINESS_TIMEZONE dice {esperada}: `creation` "
            "viene sin zona y se interpreta en la del negocio, así que toda edad sale "
            "corrida por el offset — el recordatorio y el cierre salen a la hora "
            f"equivocada. Poner System Settings.time_zone en {esperada}.",
        )
    else:
        reporte.ok("ERPNext zona", f"{declarada}, igual que BUSINESS_TIMEZONE")


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


def chequear_auto_confirmacion(reporte: Reporte) -> None:
    """¿Se va a confirmar algún pedido solo, con lo que hay configurado?

    ES OTRA PREGUNTA QUE EL RESTO DE ESTE ARCHIVO. Todo lo demás valida que la
    configuración sea VÁLIDA; esto pregunta si SIRVE. Un `.env` impecable puede
    no confirmar un solo pedido nunca y hasta hoy salía en verde — el dueño
    leía «todo OK», mandaba un pedido de prueba, lo veía frenado, arreglaba un
    ajuste, mandaba otro. Cinco veces el 17/09, con clientes reales del otro
    lado. Los motivos existían y eran correctos: lo que no existía era una
    forma de verlos TODOS JUNTOS antes de que hubiera un cliente.

    AVISO Y NO ERROR, incluso con la automatización apagada: `AUTO_CONFIRM_MAX`
    en 0 es la postura de lanzamiento que `CLAUDE.md` pide, o sea un despliegue
    CORRECTO en el que nada se confirma solo. Un error acá haría fallar el
    preflight de una instalación bien hecha, y un preflight que grita sobre lo
    normal se deja de leer — que es exactamente cómo se pierden los avisos que
    sí importan. Por eso se nombra el MODO: «los revisa una persona» es una
    decisión, no una falla.
    """
    from app import modos

    diagnostico = modos.diagnosticar()
    if diagnostico.problema:
        reporte.aviso(
            "Auto-confirmación", f"no pude saberlo: {diagnostico.problema}"
        )
        return
    if diagnostico.confirma_algo:
        reporte.ok(
            "Auto-confirmación",
            "hay pedidos que pueden confirmarse solos; cada uno sigue pasando "
            "por las reglas de policy (deuda, stock, precio, zona)",
        )
        return
    titulo = next(
        (m.titulo for m in modos.MODOS if m.clave == diagnostico.modo), ""
    )
    encabezado = (
        f"NINGÚN pedido se confirma solo — modo «{titulo}»"
        if titulo
        else "NINGÚN pedido se confirma solo"
    )
    reporte.aviso("Auto-confirmación", encabezado)
    for freno in diagnostico.frenos:
        reporte.aviso(freno.nombre, freno.consecuencia)


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

    # LOCALE se fija en el onboarding al lado de la moneda, y un valor mal
    # escrito no rompe nada: `formato.pesos` cae al de por defecto para no dejar
    # un mensaje sin salir. Por eso lo dice acá, una vez y en el arranque, en vez
    # de que un total con forma argentina aparezca en la pantalla de alguien que
    # lo lee como otra cifra y nadie se entere. Se valida el valor de `env` —no
    # `formato.locale_configurado()`— porque este chequeo tiene que poder mirar
    # un .env que todavía no es el del proceso.
    from app import formato as _formato

    crudo_locale = _valor(env, "LOCALE")
    # CUALQUIER LOCALE QUE CONOZCA CLDR, no los dos de la lista: el producto se
    # despliega fuera de Argentina y `en_IN` o `pt_BR` son configuraciones
    # legítimas. `formato._babel` es el mismo juez que usa el módulo, así que
    # el informe no puede decir que algo vale y el formateador tratarlo como
    # basura. Los dos de `LOCALES` siguen siendo los que tienen moneda escrita
    # a mano y cobertura de tests, y el aviso lo dice.
    normal_locale = None
    if crudo_locale:
        candidato = crudo_locale.replace("-", "_")
        conocidos = {codigo.lower(): codigo for codigo in _formato.LOCALES}
        normal_locale = conocidos.get(candidato.lower()) or (
            candidato if _formato._babel(candidato) is not None else None
        )
    if not crudo_locale:
        reporte.ok(
            "LOCALE",
            f"vacía: los montos se escriben {_formato.LOCALE_POR_DEFECTO} "
            "($12.000), como siempre",
        )
    elif normal_locale is None:
        reporte.error(
            "LOCALE",
            f"{crudo_locale!r} no es un locale que CLDR reconozca: los montos "
            f"se van a escribir {_formato.LOCALE_POR_DEFECTO} igual, que puede "
            "no ser lo que lee este cliente",
        )
    elif normal_locale in _formato.LOCALES:
        reporte.ok("LOCALE", f"montos con forma {normal_locale}")
    else:
        reporte.aviso(
            "LOCALE",
            f"{normal_locale}: válido, y la moneda sale del territorio según "
            f"CLDR ({_formato.moneda_del_locale(normal_locale)}). Los que este "
            f"producto trae probados son {', '.join(_formato.LOCALES)}: mirá un "
            "monto antes de mostrárselo a un cliente",
        )

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
        # Las plantillas ya las reporta `chequear_plantillas`, y con el
        # significado que importa: si está vacía, si el barrido que la dispara
        # corre fuera de la ventana de 24 h, si Meta la tiene aprobada. Desde
        # que son ajustes (`limites.PLANTILLAS`) también caen en este bucle, y
        # el reporte imprimía DOS renglones por plantilla con veredictos
        # opuestos: `FALTA ..._TEMPLATE: vacía` arriba y `OK ..._TEMPLATE:
        # válido (default del código)` abajo. Los dos son ciertos —uno habla
        # del negocio, el otro del tipo— y juntos no se pueden leer.
        #
        # Pero el `problema` NO se puede saltear: `_plantillas_del_dueno`
        # descarta las filas que lo traen y se cae al `.env` en silencio, así
        # que un nombre mal guardado por el dueño no aparece allá. Este bucle
        # es el único lugar donde se ve. Por eso la excepción es condicional y
        # no un `continue` a secas.
        #
        # Se filtra contra `PLANTILLAS` (las doce que chequea la otra función),
        # no contra `limites.PLANTILLAS`, que incluye además
        # WHATSAPP_TEMPLATE_LANGUAGE — ese no lo reporta nadie más.
        if nombre in PLANTILLAS and not fila.get("problema"):
            continue
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
        todas = list(resumen_limites())
        filas = {
            str(f.get("nombre")): f
            for f in todas
            if str(f.get("nombre")) in limites.ENTREGA
        }
        # El tope NO es una regla de entrega, y por eso no está en `filas`. Se
        # lee aparte porque la excepción pre-autorizada EMITE el pedido sola, y
        # eso depende del tope: los dos tienen que estar de acuerdo o el cliente
        # recibe una oferta que después no se puede cumplir.
        tope = next(
            (f for f in todas if str(f.get("nombre")) == "AUTO_CONFIRM_MAX"), None
        )
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

    # Las zonas se leen de ACÁ y no del entorno desde que son un límite: el
    # dueño las cambia por WhatsApp, así que un chequeo contra el .env
    # reportaría el valor de arranque y llamaría "sin configurar" a un sistema
    # que sí lo está — o al revés. Es el mismo desacuerdo entre el reporte y el
    # runtime que teníamos con la cantidad por producto.
    if not perdidas:
        cps = [c for c in _puesto("ZONAS_ENTREGA_CP").split(",") if c.strip()]
        locs = [c for c in _puesto("ZONAS_ENTREGA_LOCALIDADES").split(",") if c.strip()]
        if cps and locs:
            reporte.ok(
                "ZONAS_ENTREGA",
                f"{len(cps)} código(s) postal(es) y {len(locs)} localidad(es): "
                "la dirección necesita LOS DOS datos permitidos",
            )
        elif cps:
            reporte.ok(
                "ZONAS_ENTREGA",
                f"{len(cps)} código(s) postal(es); la localidad no se evalúa",
            )
        elif locs:
            reporte.ok(
                "ZONAS_ENTREGA",
                f"{len(locs)} localidad(es); el código postal no se evalúa",
            )
        else:
            reporte.falta(
                "ZONAS_ENTREGA",
                "ninguna lista configurada: ningún pedido se entrega solo. El "
                "dueño puede cargarlas por WhatsApp: «localidades de reparto» "
                "o «códigos postales»",
            )

    reparto = bool(_puesto("ENTREGA_DIAS") and _puesto("ENTREGA_HORA"))
    retiro = bool(
        _puesto("RETIRO_LOCAL_ACTIVO") == "true"
        and _puesto("RETIRO_LOCAL_DIAS")
        and _puesto("RETIRO_LOCAL_HORA")
    )
    def _dicho(nombre: str) -> str:
        """El valor como se MUESTRA, no como está guardado.

        Los días se guardan siempre en español —"lunes,viernes"— y quién decide
        cómo se escriben para una persona es `limites.mostrar`, en un solo
        lugar. Interpolar el valor crudo acá era una segunda ortografía del
        mismo dato: hoy se ve casi igual, y el día que la forma guardada cambie,
        este informe va a decir otra cosa que el resto del producto.

        El idioma se pide EXPLÍCITO y es el español, como toda la prosa de este
        informe: lo lee quien opera el sistema en una consola, no un cliente en
        WhatsApp (ver la sección 1 de tests/idioma_allowlist.py). Dejarlo al
        default lo ataría a `IDIOMA_DEFAULT`, y un despliegue en inglés
        imprimiría «reparto Monday,Friday» adentro de una frase en español.
        """
        from app import idioma as _idioma

        return limites.mostrar(nombre, _puesto(nombre), _idioma.ES)

    if reparto:
        reporte.ok(
            "ENTREGA_DIAS",
            f"reparto {_dicho('ENTREGA_DIAS')} a las {_dicho('ENTREGA_HORA')}",
        )
    if retiro:
        reporte.ok(
            "RETIRO_LOCAL_DIAS",
            f"retiro {_dicho('RETIRO_LOCAL_DIAS')} a las {_dicho('RETIRO_LOCAL_HORA')}",
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
        elif tope is not None and not tope.get("problema") and _es_cero(
            tope.get("valor")
        ):
            # LOS DOS INTERRUPTORES TIENEN QUE ESTAR DE ACUERDO, y este es el
            # único lugar donde se puede ver que no lo están.
            #
            # Una excepción pre-autorizada termina EMITIENDO el pedido sola: el
            # cliente pide un día de fuera, la regla del dueño lo autoriza, la
            # oferta sale sin que nadie la mire, el cliente contesta «acepto» y
            # `solicitudes` emite. Con el tope en 0 —que es el dueño diciendo
            # «ningún pedido se emite sin mí»— esa emisión se rechaza al final
            # del camino, y para entonces al cliente ya se le prometieron
            # condiciones y ya contestó que sí. Lo que ve es que le ofrecen algo
            # y después le dicen que espere a una persona.
            #
            # No es un error: las dos configuraciones son válidas por separado
            # y ninguna está rota. Es que juntas no hacen lo que parecen.
            reporte.aviso(
                "ENTREGA_EXCEPCION_ACTIVA",
                "en sí, pero el tope de auto-confirmación está en 0: la oferta "
                "sale sola y después NO se puede emitir, así que al cliente se "
                "le ofrece algo que termina esperando a una persona. Poné un "
                "tope, o dejá la excepción en no",
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


def chequear_borradores(reporte: Reporte, *, con_red: bool = True) -> None:
    """Cuántos borradores compiten por stock, contra el techo de app/policy.py.

    Es la falla más grande que este sistema puede tener y la que menos se ve:
    pasados policy.MAX_BORRADORES borradores vivos,
    `_borradores_que_reservan` LEVANTA y no se auto-confirma nada, de ningún
    producto — con un motivo («no se pudo verificar stock de X») que se lee
    exactamente igual que una caída de ERPNext. Sin este chequeo el número no
    existe en ninguna parte hasta después de que la auto-confirmación murió.

    SIEMPRE AVISO, NUNCA BLOQUEA, aunque ya se haya pasado el techo. Mismo
    criterio que `chequear_solicitudes`: en este archivo FALTA/ERROR significa
    que el sistema haría algo MAL. Pasado el techo no se hace nada mal — deja
    de auto-confirmar y todo pedido espera a una persona, que es exactamente la
    postura de lanzamiento: nada se sobrevende, a nadie se le promete de más.
    Es el sistema siendo menos útil, en la dirección segura, y eso no es un eje
    que estos niveles midan.

    La urgencia creciente va en el resumen de las 18:00, que el dueño lee todos
    los días; esto lo lee un desarrollador de vez en cuando. Pero es el AVISO
    más fuerte del archivo, y la consecuencia va en la línea misma.

    Necesita ERPNext, así que se auto-protege con `con_red`: `deploy.yml` corre
    `make check-env-offline` como puerta de despliegue y `main()` sale 1 con
    cualquier bloqueante, así que un chequeo que saliera a la red bajo
    `--sin-red` podría frenar justamente el despliegue que trae la limpieza.

    Cuenta los cargados a mano también, porque ocupan lugar en el mismo techo:
    `_borradores_que_reservan` filtra por docstatus y status, no por origen.
    """
    if not con_red:
        # La regla de la casa (ver el docstring del módulo): lo que no se pudo
        # verificar se reporta como no verificado, no se calla ni se inventa.
        reporte.aviso("Borradores vivos", "sin red: no se verificó el techo")
        return

    from app import autonomia

    datos = autonomia.borradores_vivos()
    if datos is None:
        reporte.aviso("Borradores vivos", "no pude contarlos en ERPNext")
        return
    # El `+` cuando la cuenta quedó cortada: acá el número también se leía como
    # exacto, y es el mismo número.
    mas = "+" if datos.get("truncado") else ""
    detalle = (
        f"{datos['vivos']}{mas} de {datos['tope']} "
        f"({datos['del_bot']}{mas} del bot + {datos['a_mano']}{mas} cargados a mano)"
    )
    if datos["pasado"]:
        reporte.aviso(
            "Borradores vivos",
            f"{detalle}: PASASTE EL TECHO — auto-confirmación apagada para "
            f"TODOS los productos hasta bajar de {datos['tope']}, y el motivo "
            "que ve el equipo se lee igual que una caída de ERPNext. Cerrá o "
            "confirmá los que sobran",
        )
    elif datos["pct"] >= UMBRAL_BORRADORES_PCT:
        reporte.aviso(
            "Borradores vivos",
            f"{detalle}, {datos['pct']:.0f} % del techo: pasado el techo queda "
            "la auto-confirmación apagada para todos los productos",
        )
    else:
        reporte.ok("Borradores vivos", detalle)


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


def chequear_memoria_de_clientes(env: Mapping[str, str], reporte: Reporte) -> None:
    """Si el agente de clientes puede usar lo que el dueño ya contestó.

    NO BLOQUEA: las dos posturas son válidas y el default es la que pidió el
    dueño. Sale igual por el mismo motivo que la línea de la credencial de MCP:
    es una decisión sobre QUÉ SE LE CUENTA A UN DESCONOCIDO, y el momento de
    verla es antes de salir en vivo, no después de que un cliente repita algo
    que no tenía que escuchar.

    El interruptor se consulta donde vive, pasándole el mapa candidato, y no se
    vuelve a escribir acá: dos lecturas de la misma variable son dos cosas que
    tienen que coincidir y nada que las obligue —y la que se equivocaría es
    ésta, que es la que le dice al dueño qué va a pasar—. La cuenta sale de
    `memoria.HUECOS`, así que un hueco nuevo mal marcado mueve el número.
    """
    try:
        from app import conversacion, memoria
    except Exception as exc:  # pragma: no cover - problemas de import
        reporte.aviso(
            "Memoria para clientes", f"módulo no disponible ({type(exc).__name__})"
        )
        return
    if not conversacion.memoria_de_clientes_encendida(env):
        reporte.ok(
            "Memoria para clientes",
            "apagada: el agente de clientes no usa ninguna nota del dueño",
        )
        return
    cruzan = sorted(memoria.CLAVES_PARA_CLIENTES)
    reporte.ok(
        "Memoria para clientes",
        f"encendida: cruzan {len(cruzan)} de {len(memoria.HUECOS)} respuestas "
        f"del dueño ({', '.join(cruzan)}). Lo que no está en esa lista —las "
        "notas privadas y cualquier clave que él invente— no sale",
    )


def chequear_mcp_externos(env: Mapping[str, str], reporte: Reporte) -> None:
    """Los servidores MCP de terceros: qué se conectó y QUIÉN decide qué pueden.

    NO BLOQUEA, y el criterio es el del módulo: bloquear es para cuando el
    sistema haría algo MAL. Esto es opcional —`MCP_EXTERNOS` vacío es el
    default y deja al agente con sus herramientas— y el dueño pidió
    explícitamente que gerencia pueda hacer de todo. Lo que sí hace falta es
    que la postura se VEA antes de salir en vivo, porque es la única parte del
    sistema donde el alcance no lo decide este repo.

    LO QUE ESTE ARCHIVO NO PUEDE VERIFICAR, y por eso lo dice en vez de
    callarlo: un servidor MCP de terceros actúa con UNA credencial de ERPNext
    —la que tiene en SU entorno, adentro de SU contenedor— y no tiene permisos
    por herramienta. Las tres identidades de este repo no aplican del otro
    lado. Así que la pregunta operativa no es «¿qué herramientas cargué?» sino
    «¿con qué usuario de ERPNext arrancó ese contenedor?», y la respuesta no
    está en ninguna variable que readiness pueda leer.
    """
    # `MCP_EXTERNOS` se lee del `env` que recibe esta función y el PARSEO se
    # delega al módulo que lo define, inyectado: duplicar acá el formato
    # `nombre=destino` sería un segundo parser que puede discrepar con el que
    # de verdad arma los clientes, y entonces readiness diría «dos servidores»
    # de una línea que el agente lee distinto.
    if not _valor(env, "MCP_EXTERNOS").strip():
        reporte.ok("MCP externos", "ninguno: el agente usa sólo sus herramientas")
        return

    try:
        from app import mcp_cliente
    except Exception as exc:  # pragma: no cover - problemas de import
        reporte.aviso("MCP externos", f"módulo no disponible ({type(exc).__name__})")
        return

    try:
        # `servidores(env)` y no `servidores()`: `ejecutar` recibe un `.env`
        # CANDIDATO —el que todavía no está puesto— y lo baja a cada chequeo.
        # Con `os.environ`, readiness aprobaba los servidores del proceso que lo
        # corre en vez de los del archivo que le pidieron revisar, y los avisos
        # de token faltante iban con ellos.
        configuracion = mcp_cliente.servidores(env)
    except Exception as exc:
        # DOS CAUSAS, UN MENSAJE, y el mensaje dice la consecuencia y después
        # la causa. `servidores()` levanta por `MCP_EXTERNOS` mal escrito Y por
        # un token que saldría en claro hacia un destino que sale a la red — y
        # el texto viejo («MCP_EXTERNOS no se puede interpretar») leía el
        # segundo como un error de tipeo, que es justo lo que hace que nadie
        # mire el rechazo. Lo que las dos comparten es que NO SE CARGÓ NINGÚN
        # SERVIDOR; el `exc` que se adjunta ya dice cuál de las dos fue, y en el
        # caso del bearer nombra las dos salidas.
        #
        # AVISO y no FALTA por el criterio del módulo: el agente arranca con sus
        # herramientas, que es el default, y el token justamente NO salió.
        reporte.aviso(
            "MCP externos", f"no se cargó ningún servidor externo ({exc})"
        )
        return

    if not configuracion:
        # `MCP_EXTERNOS` tiene algo y el parser no sacó ningún servidor: una
        # línea de comas sueltas. No es lo mismo que no haber configurado nada.
        reporte.aviso(
            "MCP externos", "MCP_EXTERNOS tiene un valor del que no sale ningún servidor"
        )
        return

    nombres = ", ".join(
        f"{n} ({a['transporte']})" for n, a in sorted(configuracion.items())
    )
    reporte.ok("MCP externos", f"{len(configuracion)}: {nombres}")

    # Un servidor HTTP sin token es un servidor al que le puede pedir cualquiera
    # que llegue a su puerto. En la compose está en la red interna, así que no
    # bloquea; pero no se asume.
    sin_token = sorted(
        n for n, a in configuracion.items()
        if a["transporte"] == "http"
        and not _valor(env, f"MCP_EXTERNO_TOKEN_{n.upper()}").strip()
    )
    if sin_token:
        reporte.aviso(
            "MCP externos sin token",
            f"{', '.join(sin_token)}: sin MCP_EXTERNO_TOKEN_<NOMBRE>, le contesta "
            "a cualquiera que llegue a ese puerto",
        )

    if not _valor(env, "MCP_EXTERNOS_BLOQUEAR").strip():
        reporte.aviso(
            "MCP_EXTERNOS_BLOQUEAR",
            "vacío: se carga TODO lo que publiquen, incluidos submit, cancel y "
            "delete. Ver .env.example para los dos dials",
        )

    # SIEMPRE, con lista de bloqueo o sin ella: el filtro de este lado decide
    # qué VE el modelo, y la credencial del otro lado decide qué PUEDE. Las dos
    # cosas hacen falta y sólo una está acá.
    reporte.aviso(
        "MCP externos: la credencial",
        "lo que pueden hacer lo decide el usuario de ERPNext con el que arrancó "
        "cada contenedor, no este archivo. Revisalo a mano",
    )


# ------------------------------------------------------------------- entry


def ejecutar(env: Mapping[str, str] | None = None, *, con_red: bool = True) -> Reporte:
    env = os.environ if env is None else env
    reporte = Reporte()
    http = _http_real if con_red else None
    chequear_modelos(env, reporte)
    chequear_equipo(env, reporte)
    chequear_idioma(env, reporte)
    chequear_panel(env, reporte)
    waba = chequear_whatsapp(env, reporte, http)
    # El resumen de límites se resuelve ANTES de las plantillas: dos de ellas
    # sólo bloquean si el dueño encendió el límite que las gatea.
    resumen = None
    if _valor(env, "REDIS_URL"):
        try:
            from app import limites

            resumen = limites.resumen
        except Exception as exc:  # pragma: no cover - import-time env problems
            reporte.aviso("Límites", f"módulo de límites no disponible ({type(exc).__name__})")
    chequear_plantillas(env, reporte, http, waba, resumen)
    chequear_erpnext(env, reporte, http)
    chequear_stock_y_limites(env, reporte, resumen)
    # Después de los límites y ANTES de entrega: usa los mismos valores que
    # acaba de reportar `chequear_stock_y_limites`, y leerlo pegado a ellos es
    # leer la consecuencia justo debajo de la causa.
    if _valor(env, "REDIS_URL"):
        chequear_auto_confirmacion(reporte)
    chequear_entrega(env, reporte, resumen, http)
    if _valor(env, "REDIS_URL"):
        chequear_solicitudes(reporte)
    # Se pasa con_red en vez de gatear acá: la función se auto-protege y
    # reporta «no verificado», que es la regla del módulo.
    chequear_borradores(reporte, con_red=con_red)
    # Al final: es lo único que habla de un alcance que este repo no controla,
    # y leerlo último es leerlo justo antes del veredicto.
    chequear_memoria_de_clientes(env, reporte)
    chequear_mcp_externos(env, reporte)
    return reporte


def main(argv: list[str]) -> int:
    import app  # noqa: F401  (carga .env)

    reporte = ejecutar(con_red="--sin-red" not in argv)
    print(reporte.texto())
    return 0 if reporte.listo else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
