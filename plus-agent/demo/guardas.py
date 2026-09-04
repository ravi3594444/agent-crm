"""Las guardas del banco de pruebas. Si alguna no cierra, no arranca nada.

POR QUÉ HAY DOS CAPAS
La capa que de verdad vale es la RED: el agente corre en una red de Docker
creada con --internal, que no tiene ruta a internet ni al host. Ahí no hay
"casi": los paquetes a graph.facebook.com, a Google, al ERPNext de staging del
host (:8080) y al Redis de staging (:6379) no salen. Eso se verifica en la
corrida, no se supone (`verificar_aislamiento`).

Encima va esta capa de configuración, que existe por el mensaje de error: si
alguien apunta el banco de pruebas a algo real, conviene que lo diga con el
nombre de la variable en vez de fallar con un timeout raro diez minutos
después.

NUNCA IMPRIME UN VALOR. Compara y cuenta; lo que muestra es el nombre de la
variable y qué tiene de malo.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
from collections.abc import Mapping

# Lo que jamás puede aparecer en la configuración del banco de pruebas.
HOSTS_PROHIBIDOS = (
    "graph.facebook.com", "facebook.com", "fb.com",
    "generativelanguage.googleapis.com", "googleapis.com",
    "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "aliyuncs.com",
)
# Puertos del host que son los servicios de staging de esta máquina.
PUERTOS_DE_STAGING = {
    8080: "el ERPNext de staging",
    6379: "el Redis de staging",
    8081: "el agente de staging",
}
# Las credenciales que el banco de pruebas usa. Son de mentira y tienen que
# ser EXACTAMENTE éstas: si llegara una real, algo mezcló los entornos.
CREDENCIALES_DE_MENTIRA = {
    "ERPNEXT_API_KEY": "demo-agente-key",
    "ERPNEXT_API_SECRET": "demo-agente-secret",
    "ERPNEXT_MANAGER_API_KEY": "demo-gerencia-key",
    "ERPNEXT_MANAGER_API_SECRET": "demo-gerencia-secret",
    "ERPNEXT_POLICY_API_KEY": "demo-politica-key",
    "ERPNEXT_POLICY_API_SECRET": "demo-politica-secret",
    "WHATSAPP_TOKEN": "demo-whatsapp-token-no-sirve-para-nada",
    "META_APP_SECRET": "demo-app-secret-de-32-caracteres",
    "META_VERIFY_TOKEN": "demo-verify-token",
    "GEMINI_API_KEY": "demo-clave-que-no-sirve",
}


class GuardaError(RuntimeError):
    """Algo apunta a un servicio real. No se arranca."""


def _hosts_de(valor: str) -> list[str]:
    return re.findall(r"//([^/:\s]+)", str(valor))


def _puerto_de(valor: str) -> int | None:
    m = re.search(r"//[^/:\s]+:(\d+)", str(valor))
    return int(m.group(1)) if m else None


def _es_loopback_o_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1", "[::1]",
                    "host.docker.internal", "host-gateway"}


def revisar_entorno(env: Mapping[str, str], *, permitidos: tuple[str, ...]) -> list[str]:
    """Los problemas de la configuración del banco de pruebas, en texto.

    ``permitidos`` son los nombres de host que SÍ puede haber (los dobles).
    """
    problemas: list[str] = []

    for variable, valor in env.items():
        if not isinstance(valor, str) or "//" not in valor:
            continue
        for host in _hosts_de(valor):
            plano = host.lower()
            if any(plano == p or plano.endswith("." + p) for p in HOSTS_PROHIBIDOS):
                problemas.append(
                    f"{variable} apunta a {plano}, que es un servicio real"
                )
            elif plano not in permitidos and not _es_loopback_o_host(plano):
                problemas.append(
                    f"{variable} apunta a {plano}, que no es uno de los dobles "
                    f"({', '.join(permitidos)})"
                )
            if _es_loopback_o_host(plano):
                puerto = _puerto_de(valor)
                if puerto in PUERTOS_DE_STAGING:
                    problemas.append(
                        f"{variable} va al puerto {puerto} del host, que es "
                        f"{PUERTOS_DE_STAGING[puerto]}"
                    )

    for variable, esperado in CREDENCIALES_DE_MENTIRA.items():
        actual = str(env.get(variable, "") or "")
        if not actual:
            problemas.append(f"{variable} vacía: el banco de pruebas la fija a propósito")
        elif actual != esperado:
            problemas.append(
                f"{variable} no es la credencial de mentira del banco de pruebas "
                f"(largo {len(actual)}; el valor no se muestra)"
            )

    redis_url = str(env.get("REDIS_URL", "") or "")
    if not redis_url.rstrip("/").endswith("/0"):
        problemas.append("REDIS_URL no termina en /0: el checkpointer exige la base 0")

    return problemas


def revisar_contra_el_env_real(
    env: Mapping[str, str], ruta_env_real: pathlib.Path
) -> list[str]:
    """Ninguna credencial del .env real puede aparecer en el banco de pruebas.

    Es la guarda que caza el error de verdad peligroso: heredar una credencial
    de producción sin darse cuenta. Lee el .env real sólo para COMPARAR y no
    devuelve ni registra un solo valor.
    """
    if not ruta_env_real.exists():
        return []
    # Sólo las variables que llevan una CREDENCIAL. Compararlo todo daría
    # falsos positivos con la configuración que legítimamente coincide —el
    # nombre de un modelo, la zona horaria, la lista de precios— y el ruido
    # haría que la guarda se ignore, que es peor que no tenerla.
    reales: dict[str, str] = {}
    for linea in ruta_env_real.read_text().splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        clave, valor = clave.strip(), valor.strip().strip('"').strip("'")
        if len(valor) >= 8 and any(
            pista in clave.upper()
            for pista in ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "CLAVE")
        ):
            reales[clave] = valor
    reales_invertido = {v: k for k, v in reales.items()}
    problemas = []
    for variable, valor in env.items():
        if not isinstance(valor, str) or len(valor) < 8:
            continue
        if valor in reales_invertido:
            problemas.append(
                f"{variable} tiene el MISMO valor que {reales_invertido[valor]} "
                f"del .env real: es una credencial de producción"
            )
    return problemas


def verificar_aislamiento(contenedor: str) -> list[str]:
    """Prueba desde ADENTRO que no hay ruta a lo que no debe haberla.

    Abre sockets de verdad hacia los servicios reales y hacia los de staging de
    esta máquina. Cada uno TIENE que fallar. Es la única forma de afirmar el
    aislamiento en vez de confiar en la configuración.

    No tiene excepciones a propósito: un contenedor pasa o no pasa. El relevo a
    Gemini, que por definición necesita salida, es OTRO contenedor y no se
    verifica con esto — se verifica que no lleve ninguna credencial del negocio
    (`relevo_sin_credenciales`).
    """
    destinos = [
        ("graph.facebook.com", 443, "Meta"),
        ("generativelanguage.googleapis.com", 443, "Google"),
        ("dashscope-intl.aliyuncs.com", 443, "DashScope"),
        ("172.17.0.1", 8080, "el ERPNext de staging del host"),
        ("172.17.0.1", 6379, "el Redis de staging del host"),
    ]
    # El script corre DENTRO del contenedor: sin dependencias, sólo stdlib, y
    # el nombre del contenedor no se interpola acá (lo agrega el que lee).
    guion = (
        "import json, socket\n"
        f"destinos = {json.dumps(destinos)}\n"
        "malos = []\n"
        "for h, p, q in destinos:\n"
        "    try:\n"
        "        socket.create_connection((h, p), timeout=4).close()\n"
        "        malos.append(q + ' (' + h + ':' + str(p) + ') es ALCANZABLE')\n"
        "    except Exception:\n"
        "        pass\n"
        "print(json.dumps(malos))\n"
    )
    try:
        salida = subprocess.run(
            ["docker", "exec", contenedor, "python", "-c", guion],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return [f"no pude verificar el aislamiento de {contenedor}: {type(exc).__name__}"]
    if salida.returncode != 0:
        return [
            f"no pude verificar el aislamiento de {contenedor}: "
            f"docker exec salió {salida.returncode}"
        ]
    try:
        malos = json.loads(salida.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return [f"la verificación de aislamiento de {contenedor} no devolvió JSON"]
    return [f"desde {contenedor}: {m}" for m in malos]


def red_es_interna(red: str) -> list[str]:
    """La red de Docker tiene que estar creada con --internal."""
    try:
        salida = subprocess.run(
            ["docker", "network", "inspect", red, "-f", "{{.Internal}}"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except OSError as exc:
        return [f"no pude inspeccionar la red {red}: {type(exc).__name__}"]
    if salida.stdout.strip() != "true":
        return [f"la red {red} NO es --internal: el agente tendría salida a internet"]
    return []


def exigir(problemas: list[str]) -> None:
    if problemas:
        raise GuardaError(
            "el banco de pruebas no arranca:\n  - " + "\n  - ".join(problemas)
        )


def relevo_sin_credenciales(contenedor: str, variable_clave: str) -> list[str]:
    """El contenedor con salida no puede llevar nada más que la clave del modelo.

    Es la contracara de `verificar_aislamiento`: al relevo no se le puede
    exigir que esté aislado —existe para hablar con el proveedor— así que lo
    que se exige es que no tenga nada que perder. Si no lleva credenciales de
    ERPNext ni token de WhatsApp ni datos del negocio, su salida a internet no
    puede filtrar nada de eso.

    Compara NOMBRES de variable, nunca valores, y no imprime ninguno.
    """
    prohibidas = (
        "ERPNEXT_API_KEY", "ERPNEXT_API_SECRET",
        "ERPNEXT_MANAGER_API_KEY", "ERPNEXT_MANAGER_API_SECRET",
        "ERPNEXT_POLICY_API_KEY", "ERPNEXT_POLICY_API_SECRET",
        "WHATSAPP_TOKEN", "META_APP_SECRET", "META_VERIFY_TOKEN",
        "TELEFONOS_EQUIPO", "ERPNEXT_URL", "REDIS_URL",
    )
    try:
        salida = subprocess.run(
            ["docker", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}",
             contenedor],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except OSError as exc:
        return [f"no pude inspeccionar {contenedor}: {type(exc).__name__}"]
    if salida.returncode != 0:
        return [f"no pude inspeccionar {contenedor}: docker inspect falló"]
    nombres = {
        linea.split("=", 1)[0].strip()
        for linea in salida.stdout.splitlines() if "=" in linea
    }
    problemas = [
        f"{contenedor} lleva {v}, y es el contenedor que tiene salida a internet"
        for v in prohibidas if v in nombres
    ]
    if variable_clave not in nombres:
        problemas.append(
            f"{contenedor} no lleva {variable_clave}: el relevo no puede funcionar"
        )
    return problemas
