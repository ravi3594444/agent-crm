"""El piloto: levanta el banco de pruebas, corre los escenarios y cuenta todo.

    python -m demo.piloto --modo offline      (guión determinístico, sin red)
    python -m demo.piloto --modo gemini       (Gemini de verdad, por un relevo)

TOPOLOGÍA (la guarda que importa es la red)

    docker network create --internal plus-demo-net
        |
        +-- plus-demo-redis      redis/redis-stack-server:7.4.0-v1, base 0
        +-- plus-demo-servicios  los dobles: ERPNext, Graph de Meta, modelo
        +-- plus-demo-agente     LA IMAGEN REAL, sin cambios y sin .env

Una red --internal no tiene ruta a internet NI al host, así que desde el
agente no se llega a Meta, ni a Google, ni al ERPNext de staging de esta
máquina (:8080), ni a su Redis (:6379). Se verifica abriendo sockets desde
adentro, no se supone.

El host sí llega a los contenedores por su IP, y por ahí entra el piloto: los
webhooks van firmados con HMAC como los de Meta, así que el agente corre el
mismo camino que en producción.

EN MODO GEMINI la clave real la tiene SÓLO el contenedor de los dobles, que es
el único con salida. El agente lleva una clave de mentira: no la necesita,
porque le habla al relevo, y no podría usarla para nada porque no tiene red.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import pathlib
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from demo import datos, guardas
from demo import escenarios as esc

# plus-agent/, que es la raíz de la app y el contexto del build.
RAIZ = pathlib.Path(__file__).resolve().parent.parent
RED = "plus-demo-net"
REDIS = "plus-demo-redis"
SERVICIOS = "plus-demo-servicios"
AGENTE = "plus-demo-agente"
RELEVO = "plus-demo-relevo"
IMAGEN = "plus-agent:demo"
IMAGEN_REDIS = "redis/redis-stack-server:7.4.0-v1"
IMAGEN_PY = "python:3.12-slim"
PHONE_ID = "demo-phone-id"
APP_SECRET = guardas.CREDENCIALES_DE_MENTIRA["META_APP_SECRET"]
GEMINI_REAL = "https://generativelanguage.googleapis.com/v1beta/openai"
# Cuánto se espera la respuesta final de un turno antes de darlo por perdido.
ESPERA_TURNO = 90.0


def _correr(*args: str, entrada: str = "", verificar: bool = True,
            timeout: float = 300) -> str:
    r = subprocess.run(args, capture_output=True, text=True, input=entrada,
                       timeout=timeout, check=False)
    if verificar and r.returncode != 0:
        raise RuntimeError(
            f"falló {' '.join(args[:3])}…: salida {r.returncode}\n"
            f"{(r.stderr or r.stdout)[-1500:]}"
        )
    return r.stdout.strip()


def _ip(contenedor: str, red: str = "") -> str:
    """La IP del contenedor EN esa red, esperando a que Docker se la asigne.

    Dos cosas que costaron una corrida entera cada una.

    Hay que NOMBRAR la red: el relevo está en dos (la interna y la de salida)
    y recorrerlas todas devuelve las dos IPs pegadas, que no es ninguna.

    Y hay que ESPERAR: `docker run -d` vuelve en cuanto el contenedor existe,
    y la dirección puede no estar puesta todavía. Devolver "" ahí no falla —
    y eso es lo peor que podía hacer: la URL queda «http://:8999/buzon», que
    urllib interpreta como localhost, y el banco de pruebas termina hablándole
    al HOST en vez de al contenedor. Se ve como «connection refused» veinte
    minutos después, en otro lado, sin ninguna pista de la causa.
    """
    plantilla = ("{{(index .NetworkSettings.Networks "
                 + f'"{red or RED}"' + ").IPAddress}}")
    for _ in range(30):
        direccion = _correr("docker", "inspect", "-f", plantilla, contenedor)
        if direccion.strip():
            return direccion.strip()
        time.sleep(1)
    raise RuntimeError(
        f"docker no le asignó una IP a {contenedor} en la red {red or RED}"
    )


def _corriendo() -> list[str]:
    """Los contenedores del banco de pruebas que ya están levantados.

    Existe porque los nombres son FIJOS. Dos corridas en paralelo —dos
    terminales, dos sesiones— se destruyen entre sí en silencio: la segunda
    hace `docker rm -f` sobre los contenedores de la primera, la primera sigue
    andando contra los contenedores de la segunda, y las dos informan
    resultados que no corresponden a lo que midieron. Pasó, y costó entender
    por qué un escenario fallaba con «connection refused» sin motivo.
    """
    vivos = _correr("docker", "ps", "--format", "{{.Names}}", verificar=False)
    nuestros = {AGENTE, RELEVO, SERVICIOS, REDIS}
    return sorted(n for n in vivos.splitlines() if n.strip() in nuestros)


def _sin_host(url: str) -> bool:
    """«http://:8999/x» — urllib lo manda a localhost en vez de fallar."""
    return "://:" in url or url.split("://", 1)[-1].startswith("/")


def _get(url: str, timeout: float = 15) -> dict:
    if _sin_host(url):
        raise RuntimeError(f"URL sin host: {url}")
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _post(url: str, cuerpo: dict | None = None, timeout: float = 30) -> dict:
    if _sin_host(url):
        raise RuntimeError(f"URL sin host: {url}")
    datos_ = json.dumps(cuerpo or {}).encode()
    req = urllib.request.Request(
        url, data=datos_, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


# ------------------------------------------------------------------ entorno


def entorno_del_agente(modo: str = "offline", modelo_llm: str = "") -> dict[str, str]:
    """El .env del banco de pruebas. Ninguna credencial de acá sirve para nada."""
    modelo = RELEVO if modo == "gemini" else SERVICIOS
    nombre_llm = modelo_llm or "gemini-3.5-flash"
    return {
        # --- los dobles
        "ERPNEXT_URL": f"http://{SERVICIOS}:8000",
        "META_GRAPH_BASE_URL": f"https://{SERVICIOS}:8443",
        "GEMINI_BASE_URL": f"https://{modelo}:8444/v1/",
        "REDIS_URL": f"redis://{REDIS}:6379/0",
        "SSL_CERT_FILE": "/demo/cert.pem",
        # --- proveedor: SIGUE SIENDO GEMINI, como pidió el pedido
        "LLM_PROVIDER": "gemini",
        "GEMINI_API_KEY": guardas.CREDENCIALES_DE_MENTIRA["GEMINI_API_KEY"],
        "GEMINI_SALES_MODEL": nombre_llm,
        "GEMINI_MANAGER_MODEL": nombre_llm,
        "LLM_TIMEOUT_SECONDS": "60",
        # --- credenciales de mentira (las tres identidades, distintas)
        **{k: v for k, v in guardas.CREDENCIALES_DE_MENTIRA.items()
           if k != "GEMINI_API_KEY"},
        "WHATSAPP_PHONE_NUMBER_ID": PHONE_ID,
        # --- el negocio
        "ERPNEXT_COMPANY": "Lacteos Demo SA",
        "ERPNEXT_WAREHOUSE": "Principal - LD",
        "TELEFONOS_EQUIPO": f"{datos.TELEFONO_DUENO},{datos.TELEFONO_EQUIPO}",
        "PAIS_TELEFONO": "54",
        "ZONAS_ENTREGA_CP": "5000,5001,5105",
        "ZONAS_ENTREGA_LOCALIDADES": "Cordoba,Villa Allende",
        "BUSINESS_TIMEZONE": "America/Argentina/Buenos_Aires",
        # --- límites: nada se auto-confirma salvo en su propio escenario
        "STOCK_CONFIABLE": "true",
        "STOCK_CONFIABLE_HORAS": "24",
        "AUTO_CONFIRM_PRICE_LIST": "Standard Selling",
        "AUTO_CONFIRM_CURRENCY": "ARS",
        "AUTO_CONFIRM_MAX": "0",
        "ENTREGA_DIAS_REPARTO": "lunes,martes,miercoles,jueves,viernes",
        "ENTREGA_HORA_REPARTO": "09:00",
        # --- el digest de las 18 no tiene por qué dispararse en una demo
        "DIGEST_ACTIVO": "false",
        "CONVERSATION_TTL_DAYS": "1",
    }


# -------------------------------------------------------------- transcripción


@dataclass
class Turno:
    escenario: str
    paso: int
    quien: str
    rol: str
    texto: str
    respuestas: list[str] = field(default_factory=list)
    latencia_s: float = 0.0
    cambios: dict[str, dict] = field(default_factory=dict)
    problemas: list[str] = field(default_factory=list)
    # Diferencias que no son fallas: la redacción de un modelo libre.
    avisos: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problemas


def _diferencia(antes: dict, despues: dict) -> dict[str, dict]:
    """Qué documentos aparecieron o cambiaron. Es lo que se informa."""
    cambios: dict[str, dict] = {}
    for clave, ahora in despues.items():
        entonces = antes.get(clave)
        if entonces is None:
            cambios[clave] = {"nuevo": ahora}
        elif entonces != ahora:
            cambios[clave] = {
                "de": {k: v for k, v in entonces.items() if ahora.get(k) != v},
                "a": {k: v for k, v in ahora.items() if entonces.get(k) != v},
            }
    return cambios


_RE_PEDIDO = re.compile(r"\b[A-Z]{2,6}(?:-[A-Z]{2,6})?-\d{2,}[0-9-]*\b")


class Piloto:
    def __init__(self, modo: str, salida: pathlib.Path,
                 modelo_llm: str = "") -> None:
        self.modo = modo
        self.modelo_llm = modelo_llm
        self.salida = salida
        self.turnos: list[Turno] = []
        self.ultimo_pedido = ""
        self.n_mensaje = 0
        self.ip_servicios = ""
        self.ip_agente = ""
        self.entorno_cambiado = False
        self._cert_de_esta_corrida: tuple[pathlib.Path, pathlib.Path] | None = None

    # -- infraestructura

    @property
    def control(self) -> str:
        return f"http://{self.ip_servicios}:8999"

    def _control(self, ruta: str) -> dict:
        """Lee del control de los dobles, y si no está dice POR QUÉ.

        Un URLError pelado a mitad de una corrida no dice nada: no se sabe si
        el contenedor se cayó, si lo borró otra cosa o si fue un hipo de red.
        Un reintento cubre el hipo; si no vuelve, se informa el estado del
        contenedor y sus últimas líneas, que es lo que hace falta para
        entender qué pasó.
        """
        ultimo: Exception | None = None
        for intento in range(3):
            try:
                return _get(f"{self.control}{ruta}")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                ultimo = exc
                if intento < 2:
                    time.sleep(2)
        estado = _correr("docker", "inspect", "-f", "{{.State.Status}} "
                         "(exitcode {{.State.ExitCode}}, oomkilled "
                         "{{.State.OOMKilled}})", SERVICIOS, verificar=False)
        raise RuntimeError(
            f"los dobles no contestan en {ruta} ({type(ultimo).__name__}).\n"
            f"contenedor {SERVICIOS}: {estado or 'no existe'}\n"
            f"--- últimas líneas ---\n{self.registros(SERVICIOS, 25)}"
        )

    def firmar(self, cuerpo: bytes) -> str:
        return "sha256=" + hmac.new(
            APP_SECRET.encode(), cuerpo, hashlib.sha256
        ).hexdigest()

    def mandar_whatsapp(self, telefono: str, texto: str) -> None:
        """Un webhook firmado, igual que el que manda Meta."""
        self.n_mensaje += 1
        cuerpo = json.dumps({
            "object": "whatsapp_business_account",
            "entry": [{"id": "demo-waba", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "+5493510000000",
                             "phone_number_id": PHONE_ID},
                "contacts": [{"wa_id": telefono,
                              "profile": {"name": "Demo"}}],
                "messages": [{
                    "id": f"wamid.DEMO.{self.modo}.{self.n_mensaje:04d}",
                    "from": telefono,
                    "timestamp": str(int(time.time())),
                    "type": "text",
                    "text": {"body": texto},
                }],
            }}]}],
        }, ensure_ascii=False).encode("utf-8")
        destino = f"http://{self.ip_agente}:8081/webhook/whatsapp"
        if _sin_host(destino):
            raise RuntimeError("no sé la IP del agente: no mando el webhook")
        req = urllib.request.Request(
            destino,
            data=cuerpo,
            headers={"Content-Type": "application/json",
                     "X-Hub-Signature-256": self.firmar(cuerpo)},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()

    def esperar_respuesta(self, telefono: str, desde: int) -> tuple[list[str], float]:
        """Los textos nuevos para ese número, sin contar el acuse.

        Todo turno de texto manda DOS cosas: el acuse ("Recibido, dame un
        momento…") y después la respuesta. Se espera la segunda.
        """
        inicio = time.monotonic()
        mientras = ESPERA_TURNO
        vistos: list[str] = []
        while time.monotonic() - inicio < mientras:
            envios = self._control("/buzon")["envios"]
            nuevos = [e for e in envios if e["n"] > desde and e["a"] == telefono]
            vistos = [e["texto"] for e in nuevos]
            sustanciales = [t for t in vistos if "dame un momento" not in t.lower()]
            if sustanciales:
                return vistos, time.monotonic() - inicio
            time.sleep(0.5)
        return vistos, time.monotonic() - inicio

    # -- un paso

    def correr_paso(self, escenario: esc.Escenario, i: int, paso: esc.Paso) -> Turno:
        texto = paso.texto.replace(esc.ULTIMO_PEDIDO, self.ultimo_pedido)
        rol = "gerencia" if paso.quien in {
            datos.TELEFONO_DUENO, datos.TELEFONO_EQUIPO} else "cliente"
        turno = Turno(escenario.clave, i + 1, paso.quien, rol, texto)

        antes = self._control("/instantanea")["documentos"]
        cuantos = len(self._control("/buzon")["envios"])
        try:
            self.mandar_whatsapp(paso.quien, texto)
        except (urllib.error.URLError, TimeoutError) as exc:
            turno.problemas.append(f"el webhook no entró: {type(exc).__name__}")
            return turno

        if paso.espera_respuesta:
            turno.respuestas, turno.latencia_s = self.esperar_respuesta(
                paso.quien, cuantos)
        else:
            time.sleep(2)
        despues = self._control("/instantanea")["documentos"]
        turno.cambios = _diferencia(antes, despues)

        # el número de pedido más nuevo, para los pasos que lo nombran
        for clave in turno.cambios:
            if clave.startswith("Sales Order/"):
                self.ultimo_pedido = clave.split("/", 1)[1]
        if not self.ultimo_pedido:
            for t in turno.respuestas:
                hallados = _RE_PEDIDO.findall(t)
                if hallados:
                    self.ultimo_pedido = hallados[-1]

        turno.problemas.extend(self._revisar(paso, turno, despues))
        return turno

    def _revisar(self, paso: esc.Paso, turno: Turno, foto: dict) -> list[str]:
        problemas: list[str] = []
        todo = " ".join(turno.respuestas).lower()
        if paso.espera_respuesta and not turno.respuestas:
            problemas.append(
                f"no llegó ninguna respuesta en {ESPERA_TURNO:.0f}s")
        sustanciales = [t for t in turno.respuestas
                        if "dame un momento" not in t.lower()]
        if paso.espera_respuesta and not sustanciales:
            problemas.append("sólo llegó el acuse, nunca la respuesta")
        # Un fallo técnico es SIEMPRE un fallo del escenario, diga lo que diga
        # el paso. app/main.py convierte cualquier excepción en una disculpa,
        # así que sin este chequeo un escenario cuyas condiciones son sólo
        # "prohibe" pasaría con el agente completamente roto.
        for disculpa in ("problema técnico", "problema tecnico",
                         "error tecnico", "error técnico"):
            if disculpa in todo:
                problemas.append(
                    "el agente contestó con una disculpa técnica: se rompió algo")
                break
        # Los fragmentos de texto son EXACTOS contra un guión y sólo
        # orientativos contra un modelo libre: "tengo leche entera" es una
        # respuesta correcta que no contiene "LECHE-ENT-1L". Así que en modo
        # gemini se informan pero no fallan. Lo que sí falla en los dos modos
        # es el estado de los documentos y una disculpa técnica: eso no
        # depende de cómo el modelo eligió redactar.
        for fragmento in paso.espera:
            if fragmento.lower() not in todo:
                queja = f"la respuesta no dice {fragmento!r}"
                if self.modo == "gemini":
                    turno.avisos.append(queja + " (redacción del modelo)")
                else:
                    problemas.append(queja)
        # Lo PROHIBIDO falla en los dos modos: que el modelo prometa un
        # descuento o diga "confirmado" cuando no lo está es justo lo que hay
        # que cazar, y no es una cuestión de redacción.
        for fragmento in paso.prohibe:
            if fragmento.lower() in todo:
                problemas.append(f"la respuesta dice {fragmento!r} y no debería")
        for patron, exigido in paso.documentos.items():
            doctype = patron.split("/", 1)[0]
            candidatos = {k: v for k, v in foto.items()
                          if k.startswith(doctype + "/")}
            if not candidatos:
                problemas.append(f"no quedó ningún {doctype}")
                continue
            if not any(
                all(str(doc.get(c)) == str(v) for c, v in exigido.items())
                for doc in candidatos.values()
            ):
                problemas.append(
                    f"ningún {doctype} quedó en {exigido} "
                    f"(hay {len(candidatos)})"
                )
        return problemas

    # -- un escenario

    def calentar(self) -> None:
        """El dueño escribe una vez, para abrir su ventana de 24 h.

        No es un truco del banco de pruebas: con las plantillas de WhatsApp
        vacías —como están hoy— un aviso al equipo sale como texto libre y
        Meta sólo lo permite si el destinatario escribió en las últimas 24 h.
        Sin esto, la primera alerta de cada corrida se anota en ERPNext como
        "no enviada", que es exactamente lo que pasaría en el piloto real el
        día que el dueño no le hubiera escrito al bot.
        """
        print("[piloto] el dueño escribe una vez (abre su ventana de 24 h)",
              flush=True)
        cuantos = len(self._control("/buzon")["envios"])
        self.mandar_whatsapp(datos.TELEFONO_DUENO, "hola")
        self.esperar_respuesta(datos.TELEFONO_DUENO, cuantos)

    def correr_escenario(self, escenario: esc.Escenario) -> list[Turno]:
        print(f"\n=== {escenario.clave}: {escenario.titulo}", flush=True)
        self.ultimo_pedido = ""
        if escenario.entorno:
            self.recrear_agente(escenario.entorno)
        elif self.entorno_cambiado:
            self.recrear_agente({})
        self.entorno_cambiado = bool(escenario.entorno)
        turnos = []
        for i, paso in enumerate(escenario.pasos):
            if escenario.reiniciar_antes_de == i:
                self.reiniciar_agente()
            turno = self.correr_paso(escenario, i, paso)
            turnos.append(turno)
            self.turnos.append(turno)
            estado = "ok " if turno.ok else "FALLA"
            print(f"  [{estado}] {turno.rol:8} {turno.texto[:58]!r} "
                  f"({turno.latencia_s:.1f}s)", flush=True)
            for p in turno.problemas:
                print(f"          -> {p}", flush=True)
            for a in turno.avisos:
                print(f"          ·  {a}", flush=True)
        return turnos

    def recrear_agente(self, cambios: dict[str, str]) -> None:
        """El agente de nuevo, con el entorno cambiado. Redis se conserva.

        Los topes del dueño se cambian por WhatsApp (proponer_limite + código
        de cuatro dígitos), no por entorno; pero esa herramienta es de gerencia
        y hoy no se puede usar (ver el informe). Así que el escenario de
        auto-confirmación arranca el agente con el tope ya puesto, que es lo
        mismo que ve app/policy.py cuando el dueño no lo cambió nunca.
        """
        etiquetas = ", ".join(f"{k}={v}" for k, v in cambios.items())
        print(f"  ·· recreando el agente con {etiquetas}", flush=True)
        _correr("docker", "rm", "-f", AGENTE, verificar=False)
        env = {**entorno_del_agente(self.modo, self.modelo_llm), **cambios}
        guardas.exigir(
            guardas.revisar_entorno(env, permitidos=(SERVICIOS, REDIS, RELEVO))
            + guardas.revisar_contra_el_env_real(env, RAIZ / ".env")
        )
        argumentos: list[str] = []
        for k, v in env.items():
            argumentos += ["-e", f"{k}={v}"]
        cert, _clave = self._certificado()
        _correr("docker", "run", "-d", "--name", AGENTE, "--network", RED,
                "-v", f"{cert}:/demo/cert.pem:ro", *argumentos, IMAGEN)
        self.ip_agente = _ip(AGENTE)
        self.esperar_salud()

    def reiniciar_agente(self) -> None:
        """Redis vacío y proceso nuevo: el estado tiene que venir de ERPNext."""
        print("  ·· reiniciando: FLUSHALL en Redis y reinicio del agente",
              flush=True)
        _correr("docker", "exec", REDIS, "redis-cli", "FLUSHALL")
        _correr("docker", "restart", AGENTE)
        self.esperar_salud()

    def esperar_salud(self, intentos: int = 60) -> dict:
        for _ in range(intentos):
            try:
                return _get(f"http://{self.ip_agente}:8081/health", timeout=5)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                time.sleep(2)
        raise RuntimeError("el agente nunca llegó a /health")

    # -- levantar y bajar

    def levantar(self) -> None:
        cert, clave = self._certificado()
        print("[piloto] construyendo la imagen real…", flush=True)
        _correr("docker", "build", "-q", "-t", IMAGEN, str(RAIZ),
                timeout=900)

        ajenos = _corriendo()
        if ajenos:
            raise RuntimeError(
                "ya hay un banco de pruebas levantado: "
                + ", ".join(ajenos)
                + ".\nLos nombres de los contenedores son fijos, así que arrancar "
                "otra corrida BORRARÍA esa: si son de otra sesión o de otra "
                "terminal, la dejarías hablando con contenedores nuevos y su "
                "resultado no querría decir nada. Esperá a que termine, o "
                "bajala a mano:\n"
                "  python -m demo.piloto --bajar"
            )
        self.bajar(silencioso=True)
        _correr("docker", "network", "create", "--internal", RED)
        guardas.exigir(guardas.red_es_interna(RED))

        _correr("docker", "run", "-d", "--name", REDIS, "--network", RED,
                "-e", "REDIS_ARGS=--appendonly yes --maxmemory-policy noeviction",
                IMAGEN_REDIS)

        _correr(
            "docker", "run", "-d", "--name", SERVICIOS, "--network", RED,
            "-v", f"{RAIZ / 'demo'}:/srv/demo:ro",
            "-v", f"{cert}:/demo/cert.pem:ro", "-v", f"{clave}:/demo/clave.pem:ro",
            "-e", f"DEMO_PHONE_ID={PHONE_ID}",
            "--entrypoint", "python", IMAGEN,
            "-m", "demo.servicios", "--cert", "/demo/cert.pem",
            "--clave", "/demo/clave.pem", "--sembrar",
        )
        self.ip_servicios = _ip(SERVICIOS)
        self.ip_redis = _ip(REDIS)

        if self.modo == "gemini":
            self._levantar_relevo(cert, clave)

        for _ in range(40):
            try:
                _get(f"{self.control}/salud", timeout=4)
                break
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(1)
        else:
            raise RuntimeError(f"los dobles no respondieron:\n{self.registros(SERVICIOS)}")

        env = entorno_del_agente(self.modo, self.modelo_llm)
        guardas.exigir(
            guardas.revisar_entorno(env, permitidos=(SERVICIOS, REDIS, RELEVO))
            + guardas.revisar_contra_el_env_real(
                env, RAIZ / ".env")
        )
        argumentos_env: list[str] = []
        for k, v in env.items():
            argumentos_env += ["-e", f"{k}={v}"]
        _correr(
            "docker", "run", "-d", "--name", AGENTE, "--network", RED,
            "-v", f"{cert}:/demo/cert.pem:ro", *argumentos_env, IMAGEN,
        )
        self.ip_agente = _ip(AGENTE)
        try:
            salud = self.esperar_salud()
        except RuntimeError:
            raise RuntimeError(
                f"el agente no arrancó:\n{self.registros(AGENTE)}") from None
        print(f"[piloto] /health del agente: {salud}", flush=True)

        # LA guarda que vale: desde adentro, nada de lo real es alcanzable.
        print("[piloto] verificando el aislamiento desde adentro…", flush=True)
        problemas = guardas.verificar_aislamiento(AGENTE)
        problemas += guardas.verificar_aislamiento(SERVICIOS)
        guardas.exigir(problemas)
        print("[piloto] aislamiento confirmado: desde el agente y desde los "
              "dobles no se llega a Meta, ni a Google, ni al ERPNext/Redis de "
              "staging de esta máquina", flush=True)

    def _levantar_relevo(self, cert: pathlib.Path, clave: pathlib.Path) -> None:
        """El ÚNICO contenedor con salida, y lo más chico posible.

        Alguien tiene que poder hablar con Google: el agente corre en una red
        sin ruta a internet, y ésa es la garantía que no se negocia. Así que la
        salida se concentra acá, en un proceso cuyo código sólo sabe reenviar
        al endpoint que se le pasó por parámetro.

        Lo que este contenedor NO tiene: ninguna credencial de ERPNext, ningún
        token de WhatsApp, ningún dato del negocio. Sólo la clave de Gemini.
        Se verifica antes de arrancarlo.
        """
        variable, valor = self._clave_real()
        _correr(
            "docker", "run", "-d", "--name", RELEVO, "--network", RED,
            "--cap-drop", "ALL",
            "-v", f"{RAIZ / 'demo'}:/srv/demo:ro",
            "-v", f"{cert}:/demo/cert.pem:ro", "-v", f"{clave}:/demo/clave.pem:ro",
            "-e", f"{variable}={valor}",
            "--entrypoint", "python", IMAGEN,
            "-m", "demo.servicios", "--cert", "/demo/cert.pem",
            "--clave", "/demo/clave.pem",
            "--relevar-a", GEMINI_REAL, "--clave-en", variable,
        )
        _correr("docker", "network", "connect", "bridge", RELEVO)
        problemas = guardas.relevo_sin_credenciales(RELEVO, variable)
        guardas.exigir(problemas)
        for _ in range(40):
            try:
                _get(f"http://{_ip(RELEVO)}:8999/salud", timeout=4)
                break
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(1)
        else:
            raise RuntimeError(f"el relevo no arrancó:\n{self.registros(RELEVO)}")
        print("[piloto] relevo a Gemini levantado: es el ÚNICO contenedor con "
              "salida, y lleva sólo la clave del modelo", flush=True)

    def _certificado(self) -> tuple[pathlib.Path, pathlib.Path]:
        destino = pathlib.Path(
            os.getenv("DEMO_DIR_CERT") or "/tmp/plus-demo-cert")
        destino.mkdir(parents=True, exist_ok=True)
        cert, clave = destino / "cert.pem", destino / "clave.pem"
        # Se regenera SIEMPRE. Un certificado guardado de una corrida anterior
        # puede no tener en su subjectAltName un host que se agregó después, y
        # entonces el agente falla con un error de conexión que no dice
        # "certificado" en ninguna parte. Generarlo cuesta medio segundo.
        if self._cert_de_esta_corrida is None:
            cert.unlink(missing_ok=True)
            clave.unlink(missing_ok=True)
            if not shutil.which("openssl"):
                raise RuntimeError("hace falta openssl para el certificado del doble")
            _correr(
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(clave), "-out", str(cert), "-days", "2",
                "-nodes", "-subj", "/CN=plus-demo-servicios",
                "-addext",
                f"subjectAltName=DNS:{SERVICIOS},DNS:{RELEVO},DNS:localhost,IP:127.0.0.1",
            )
            clave.chmod(0o644)  # lo lee el usuario no-root de la imagen
            self._cert_de_esta_corrida = (cert, clave)
        return self._cert_de_esta_corrida

    def _clave_real(self) -> tuple[str, str]:
        """La clave de Gemini. No se imprime, no se guarda, no se commitea.

        Primero el entorno del proceso, después el .env local. El entorno gana
        para poder usar OTRA clave —la del .env agotó su cuota diaria, por
        ejemplo— sin escribirla en ningún archivo:

            GOOGLE_API_KEY=… python -m demo.piloto --modo gemini
        """
        for variable in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            valor = os.environ.get(variable, "").strip()
            if valor:
                print(f"[piloto] clave real tomada de {variable} del ENTORNO "
                      f"({len(valor)} caracteres; no se muestra) y entregada "
                      f"SÓLO al contenedor del relevo", flush=True)
                return variable, valor
        env_real = RAIZ / ".env"
        if not env_real.exists():
            raise RuntimeError(
                f"modo gemini sin {env_real} y sin clave en el entorno")
        for linea in env_real.read_text().splitlines():
            linea = linea.strip()
            if "=" not in linea or linea.startswith("#"):
                continue
            k, v = linea.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k in ("GEMINI_API_KEY", "GOOGLE_API_KEY") and v:
                print(f"[piloto] clave real tomada de {k} del .env local "
                      f"({len(v)} caracteres; no se muestra) y entregada SÓLO "
                      f"al contenedor del relevo", flush=True)
                return k, v
        raise RuntimeError(
            "no encontré GEMINI_API_KEY ni GOOGLE_API_KEY en el .env local")

    def registros(self, contenedor: str, lineas: int = 40) -> str:
        return _correr("docker", "logs", "--tail", str(lineas), contenedor,
                       verificar=False)

    def bajar(self, silencioso: bool = False) -> None:
        nombres = (AGENTE, RELEVO, SERVICIOS, REDIS)
        for c in nombres:
            _correr("docker", "rm", "-f", c, verificar=False)
        # `docker rm -f` vuelve antes de que el nombre quede libre si el
        # contenedor estaba reiniciándose, y el `docker run` siguiente falla
        # con "name already in use" — que se lee como un bug del banco de
        # pruebas y no como lo que es: una corrida anterior que no terminó.
        for _ in range(30):
            quedan = [
                c for c in nombres
                if _correr("docker", "ps", "-aq", "-f", f"name=^{c}$",
                           verificar=False)
            ]
            if not quedan:
                break
            time.sleep(1)
            for c in quedan:
                _correr("docker", "rm", "-f", c, verificar=False)
        else:
            raise RuntimeError(
                f"no pude liberar los nombres {', '.join(quedan)}: "
                "borralos a mano con `docker rm -f`"
            )
        _correr("docker", "network", "rm", RED, verificar=False)
        if not silencioso:
            print("[piloto] banco de pruebas desarmado", flush=True)

    # -- el informe

    def informe(self) -> str:
        lineas = [
            "# Transcripción del banco de pruebas",
            "",
            f"Modo: **{self.modo}** "
            + ("(guión determinístico, sin ningún modelo)" if self.modo == "offline"
               else "(Gemini de verdad, por el relevo de los dobles)"),
            "",
            f"Turnos: {len(self.turnos)} · "
            f"ok {sum(1 for t in self.turnos if t.ok)} · "
            f"fallados {sum(1 for t in self.turnos if not t.ok)}",
            "",
        ]
        latencias = [t.latencia_s for t in self.turnos if t.latencia_s > 0]
        if latencias:
            ordenadas = sorted(latencias)
            lineas += [
                f"Latencia por turno: mínima {ordenadas[0]:.1f}s · "
                f"mediana {ordenadas[len(ordenadas) // 2]:.1f}s · "
                f"máxima {ordenadas[-1]:.1f}s",
                "",
            ]
        actual = ""
        for t in self.turnos:
            if t.escenario != actual:
                actual = t.escenario
                titulo = next(
                    (e.titulo for e in esc.escenarios() if e.clave == actual), actual)
                lineas += ["", f"## {actual} — {titulo}", ""]
            lineas.append(
                f"**{t.paso}. {t.rol}** ({'ok' if t.ok else 'FALLA'}, "
                f"{t.latencia_s:.1f}s)"
            )
            lineas.append(f"- dice: {t.texto!r}")
            for r in t.respuestas:
                lineas.append(f"- recibe: {r!r}")
            for clave, cambio in t.cambios.items():
                lineas.append(f"- documento `{clave}`: {json.dumps(cambio, ensure_ascii=False)}")
            for p in t.problemas:
                lineas.append(f"- **problema**: {p}")
            for a in t.avisos:
                lineas.append(f"- nota: {a}")
            lineas.append("")
        return "\n".join(lineas)

    def guardar(self) -> None:
        self.salida.mkdir(parents=True, exist_ok=True)
        (self.salida / f"transcripcion-{self.modo}.md").write_text(
            self.informe(), encoding="utf-8")
        (self.salida / f"turnos-{self.modo}.json").write_text(
            json.dumps([{
                "escenario": t.escenario, "paso": t.paso, "rol": t.rol,
                "texto": t.texto, "respuestas": t.respuestas,
                "latencia_s": round(t.latencia_s, 3), "cambios": t.cambios,
                "problemas": t.problemas, "avisos": t.avisos,
            } for t in self.turnos], ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"[piloto] transcripción en {self.salida}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Banco de pruebas del agente")
    ap.add_argument("--modo", choices=("offline", "gemini"), default="offline")
    ap.add_argument("--salida", default=str(RAIZ / "demo-resultados"))
    ap.add_argument("--dejar-levantado", action="store_true",
                    help="no desarmar al terminar, para mirar los logs")
    ap.add_argument("--solo", default="",
                    help="correr uno o varios escenarios, separados por coma")
    ap.add_argument("--modelo", default="",
                    help="otro modelo de Gemini (la cuota gratuita es por modelo)")
    ap.add_argument("--bajar", action="store_true",
                    help="sólo desarmar lo que quedó de una corrida anterior")
    args = ap.parse_args()

    piloto = Piloto(args.modo, pathlib.Path(args.salida), args.modelo)
    if args.bajar:
        piloto.bajar()
        return 0
    try:
        piloto.levantar()
        piloto.calentar()
        pedidos = [c.strip() for c in args.solo.split(",") if c.strip()]
        elegidos = [
            e for e in esc.escenarios() if not pedidos or e.clave in pedidos
        ]
        if not elegidos:
            raise SystemExit(f"no hay un escenario llamado {args.solo!r}")
        for escenario in elegidos:
            piloto.correr_escenario(escenario)
        piloto.guardar()
    finally:
        if not args.dejar_levantado:
            piloto.bajar()

    fallados = [t for t in piloto.turnos if not t.ok]
    print(f"\n{'=' * 70}")
    print(f"{len(piloto.turnos)} turnos, {len(fallados)} con problemas")
    for t in fallados:
        print(f"  {t.escenario} paso {t.paso}: {'; '.join(t.problemas)}")
    return 1 if fallados else 0


if __name__ == "__main__":
    raise SystemExit(main())
