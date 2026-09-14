"""Banco de pruebas EN UN PROCESO: los mismos dobles, sin Docker.

Por qué existe
--------------
`make demo` (demo/piloto.py) levanta cuatro contenedores en una red
`--internal` y le habla a la imagen real por HTTP. Es la forma correcta de
probar el despliegue —la garantía de aislamiento es la red, no el `.env`— pero
NO CORRE SIN UN DEMONIO DE DOCKER, y ahí muere en el `docker build`, antes del
primer escenario. En una máquina sin Docker el resultado no es «17 escenarios
fallados»: es cero escenarios corridos.

Esto es la misma idea con otra garantía. Todo pasa dentro de un proceso de
Python: los dobles de `demo/` escuchan en 127.0.0.1, la app real de FastAPI
corre en un hilo, y los webhooks entran firmados por el mismo camino de
producción (`POST /webhook/whatsapp` con `X-Hub-Signature-256`). No se parchea
nada de `app/`.

Qué se pierde y qué no
----------------------
Se pierde la prueba de aislamiento por red: acá el proceso SÍ podría salir a
internet. A cambio, `_verificar_destinos()` afirma que todas las URLs que la
app va a usar apuntan a loopback, y las credenciales son las de mentira de
`demo/guardas.py`, que no sirven para nada en ningún lado. Es una garantía más
débil que la de Docker y hay que decirlo: esto es para MIRAR AL AGENTE HABLAR,
no para firmar que un despliegue está aislado. Para eso está `make demo`.

No se pierde nada del camino del mensaje: firma, cola durable en Redis, worker
con lease, idempotencia, el router determinístico de `app/main.py`, las
herramientas reales contra el ERPNext falso y los permisos de las tres
credenciales.

El modelo
---------
Igual que en modo offline de `make demo`: un guión sirve el protocolo de
OpenAI en https://127.0.0.1:<puerto>/v1/ y el `ChatGemini` de producción le
habla sin saber que del otro lado no hay un modelo. La diferencia es que acá
se GUARDA la request entera, así que la transcripción puede mostrar qué
herramientas se llamaron y —lo que importa— QUÉ TEXTO le devolvió cada una al
modelo. Ese texto lo escribe Python y es real; la respuesta final, en modo
guión, la escribe el guión. La transcripción los separa para que nadie
confunda una cosa con la otra.

Uso:
    from sim_banco import Banco
    with Banco(reglas=mis_reglas) as banco:
        turno = banco.mandar(telefono, "hola, tenés leche?")
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

RAIZ = pathlib.Path(__file__).resolve().parent

# Puertos altos y poco usados: si algo contesta en uno de estos sin que lo
# hayamos levantado nosotros, el banco no arranca en vez de hablarle a un
# servicio ajeno.
PUERTO_ERPNEXT = 18000
PUERTO_META = 18443
PUERTO_MODELO = 18444
PUERTO_AGENTE = 18081
PUERTO_REDIS = 16399
PHONE_ID = "sim-phone-id"
ESPERA_TURNO = 90.0


# ------------------------------------------------------------------- guardas


def _puerto_libre(puerto: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", puerto)) != 0


def _verificar_puertos() -> None:
    ocupados = [p for p in (PUERTO_ERPNEXT, PUERTO_META, PUERTO_MODELO,
                            PUERTO_AGENTE, PUERTO_REDIS) if not _puerto_libre(p)]
    if ocupados:
        raise RuntimeError(
            f"los puertos {ocupados} ya están ocupados: hay otra corrida "
            "viva, o un servicio ajeno. No arranco para no hablarle a otro."
        )


def _verificar_destinos(entorno: dict[str, str]) -> None:
    """Toda URL que la app vaya a usar apunta a 127.0.0.1, y nada más.

    Es la guarda que reemplaza a la red `--internal` de `make demo`. Más débil
    —el proceso igual podría abrir un socket a internet— pero afirma lo que
    importa: ninguna credencial de mentira se va a mandar a un servicio real,
    y ningún pedido va a salir de la máquina por una URL mal puesta.
    """
    import re

    problemas = []
    for variable, valor in entorno.items():
        if "://" not in str(valor):
            continue
        for host in re.findall(r"//([^/:\s]+)", str(valor)):
            if host not in {"127.0.0.1", "localhost", "::1"}:
                problemas.append(f"{variable} apunta a {host!r}")
    if problemas:
        raise RuntimeError("el banco no arranca: " + "; ".join(problemas))


# ---------------------------------------------------------------- el entorno


def entorno(datos_mod: Any, extra: dict[str, str] | None = None) -> dict[str, str]:
    """El `.env` del banco. Sale de demo/piloto.py con las URLs en loopback."""
    from demo import guardas

    env = {
        "ERPNEXT_URL": f"http://127.0.0.1:{PUERTO_ERPNEXT}",
        # app/whatsapp.py acepta http SIN TLS en loopback, a propósito: el
        # token no sale de la máquina. Nos ahorra un certificado.
        "META_GRAPH_BASE_URL": f"http://127.0.0.1:{PUERTO_META}",
        # app/modelos.py exige https SIEMPRE, sin excepción por loopback. Por
        # eso el doble del modelo sí lleva certificado.
        "GEMINI_BASE_URL": f"https://127.0.0.1:{PUERTO_MODELO}/v1/",
        "REDIS_URL": f"redis://127.0.0.1:{PUERTO_REDIS}/0",
        "LLM_PROVIDER": "gemini",
        "GEMINI_SALES_MODEL": "gemini-3.5-flash",
        "GEMINI_MANAGER_MODEL": "gemini-3.5-flash",
        "LLM_TIMEOUT_SECONDS": "60",
        **guardas.CREDENCIALES_DE_MENTIRA,
        "WHATSAPP_PHONE_NUMBER_ID": PHONE_ID,
        "ERPNEXT_COMPANY": "Lacteos Demo SA",
        "NOMBRE_NEGOCIO": "Lacteos Demo SA",
        "ERPNEXT_WAREHOUSE": "Principal - LD",
        "TELEFONOS_EQUIPO": f"{datos_mod.TELEFONO_DUENO},{datos_mod.TELEFONO_EQUIPO}",
        "TELEFONO_DUENO": datos_mod.TELEFONO_DUENO,
        "PAIS_TELEFONO": "54",
        "ZONAS_ENTREGA_CP": "5000,5001,5105",
        "ZONAS_ENTREGA_LOCALIDADES": "Cordoba,Villa Allende",
        "BUSINESS_TIMEZONE": "America/Argentina/Buenos_Aires",
        "IDIOMA_DEFAULT": "es",
        "IDIOMA_GERENCIA": "es",
        "LOCALE": "es_AR",
        "STOCK_CONFIABLE": "true",
        "STOCK_CONFIABLE_HORAS": "24",
        "AUTO_CONFIRM_PRICE_LIST": "Standard Selling",
        "AUTO_CONFIRM_CURRENCY": "ARS",
        # La postura de lanzamiento del CLAUDE.md: nada se confirma solo.
        "AUTO_CONFIRM_MAX": "0",
        "ENTREGA_DIAS_REPARTO": "lunes,martes,miercoles,jueves,viernes",
        "ENTREGA_HORA_REPARTO": "09:00",
        "DIGEST_ACTIVO": "false",
        "CONVERSATION_TTL_DAYS": "1",
    }
    env.update(extra or {})
    return env


# ------------------------------------------------------------------- el turno


@dataclass
class Turno:
    """Un mensaje y todo lo que se pudo medir de lo que pasó después."""

    quien: str
    rol: str
    texto: str
    respuestas: list[str] = field(default_factory=list)
    latencia_s: float = 0.0
    cambios: dict[str, dict] = field(default_factory=dict)
    # (herramienta, argumentos) en el orden en que el agente las llamó.
    herramientas: list[tuple[str, dict]] = field(default_factory=list)
    # Lo que cada herramienta le DEVOLVIÓ al modelo. Este texto lo escribe
    # Python: es el único texto del turno que en modo guión no escribí yo.
    resultados: list[str] = field(default_factory=list)

    @property
    def respuesta(self) -> str:
        return "\n---\n".join(self.respuestas)


def _diferencia(antes: dict, despues: dict) -> dict[str, dict]:
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


def _texto(valor: object) -> str:
    return valor.decode() if isinstance(valor, bytes) else str(valor)


def _comando_redis() -> list[str]:
    """Cómo arrancar un Redis CON RediSearch y ReJSON, esté donde esté.

    `redis-stack-server` no siempre está en el PATH —en esta máquina vive en un
    directorio de trabajo—, y el `redis-server` que sí está en el PATH es el
    pelado, que arranca perfecto y después falla en el primer `FT.CREATE`. Ese
    es el peor modo de falla posible: el banco levanta y muere ochenta líneas
    después con un error de LangGraph que no nombra a Redis.

    Por eso: primero el stack; si no, el redis pelado CON los módulos cargados
    a mano; y si tampoco hay módulos, un error que dice exactamente qué falta.
    SIM_REDIS_STACK fuerza un binario concreto.
    """
    forzado = os.getenv("SIM_REDIS_STACK", "").strip()
    if forzado:
        return [forzado]
    stack = shutil.which("redis-stack-server")
    if stack:
        return [stack]
    raices = [pathlib.Path(p) for p in (
        os.getenv("SCRATCHPAD", ""), "/tmp/claude-0", "/opt", "/usr/local",
        str(pathlib.Path.home()),
    ) if p]
    for raiz in raices:
        if not raiz.exists():
            continue
        for candidato in raiz.glob("**/bin/redis-stack-server"):
            return [str(candidato)]
    pelado = shutil.which("redis-server")
    modulos: list[str] = []
    for raiz in raices:
        if not raiz.exists() or modulos:
            continue
        for nombre in ("redisearch.so", "rejson.so"):
            hallados = list(raiz.glob(f"**/lib/{nombre}"))
            if hallados:
                modulos += ["--loadmodule", str(hallados[0])]
    if pelado and len(modulos) == 4:
        return [pelado, *modulos]
    raise RuntimeError(
        "no encontré un Redis con RediSearch + ReJSON. Instalá "
        "redis-stack-server, o apuntá SIM_REDIS_STACK al binario. "
        f"(redis-server pelado: {pelado or 'tampoco'})"
    )


# -------------------------------------------------------------------- el banco


class Banco:
    def __init__(self, reglas: list, *, sembrar=None, extra_env=None,
                 verbose: bool = True) -> None:
        self.reglas = reglas
        self._sembrar = sembrar
        self.extra_env = dict(extra_env or {})
        self.verbose = verbose
        self.n_mensaje = 0
        self._redis: subprocess.Popen | None = None
        self._servidores: list = []
        self._uvicorn = None
        self._dir = pathlib.Path(
            os.getenv("SIM_DIR") or "/tmp/claude-0/sim-banco")
        self.app_secret = ""
        # Las requests completas que recibió el doble del modelo. De acá se
        # leen las herramientas y sus resultados.
        self.payloads: list[dict] = []

    # -- infraestructura

    def _decir(self, mensaje: str) -> None:
        if self.verbose:
            print(f"[banco] {mensaje}", flush=True)

    def _certificado(self) -> tuple[pathlib.Path, pathlib.Path]:
        """Uno nuevo por corrida. Vale para 127.0.0.1 y para nada más."""
        if not shutil.which("openssl"):
            raise RuntimeError("hace falta openssl para el doble del modelo")
        self._dir.mkdir(parents=True, exist_ok=True)
        cert, clave = self._dir / "cert.pem", self._dir / "clave.pem"
        cert.unlink(missing_ok=True)
        clave.unlink(missing_ok=True)
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048",
             "-keyout", str(clave), "-out", str(cert), "-days", "2",
             "-nodes", "-subj", "/CN=127.0.0.1",
             "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"],
            check=True, capture_output=True,
        )
        return cert, clave

    def _levantar_redis(self) -> None:
        """Un Redis Stack PROPIO, en su puerto y su directorio.

        No se usa el Redis de la máquina: el checkpointer de LangGraph exige la
        base 0 (RediSearch se niega en cualquier otra) y un `FLUSHDB` sobre la
        base 0 compartida le borraría el estado a cualquier otra cosa que la
        esté usando. Uno propio se tira al terminar y no se lleva nada puesto.
        """
        trabajo = self._dir / "redis"
        shutil.rmtree(trabajo, ignore_errors=True)
        trabajo.mkdir(parents=True, exist_ok=True)
        self._redis = subprocess.Popen(
            [*_comando_redis(), "--port", str(PUERTO_REDIS),
             "--dir", str(trabajo), "--save", "", "--appendonly", "no"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        import redis as _redis

        cliente = _redis.Redis(host="127.0.0.1", port=PUERTO_REDIS)
        for _ in range(60):
            try:
                cliente.ping()
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("mi Redis no arrancó")
        modulos = {_texto(m.get("name") or m.get(b"name") or "").lower()
                   for m in cliente.module_list()}
        if not {"search", "rejson"} <= modulos:
            raise RuntimeError(
                f"el Redis que levanté no trae ReJSON + search: "
                f"{sorted(modulos)}. El checkpointer de LangGraph "
                "(app/graph.py, al importarse) no arranca sin los dos."
            )
        self._decir(f"redis propio en :{PUERTO_REDIS} ({sorted(modulos)})")

    def _levantar_dobles(self, cert: pathlib.Path, clave: pathlib.Path) -> None:
        from http.server import ThreadingHTTPServer

        from demo import servicios

        banco = self

        class Modelo(servicios._Base):
            """El doble del modelo, guardando la request entera.

            `demo/servicios.py::Modelo` guarda sólo un conteo. Acá hace falta
            el cuerpo: los mensajes con `role="tool"` son lo que las
            herramientas REALES le contestaron al modelo, y ese texto es el
            único del turno que en modo guión no escribió el guión.
            """

            def do_POST(self) -> None:
                from demo import falso_modelo

                cuerpo = self._leer()
                try:
                    payload = json.loads(cuerpo)
                except ValueError:
                    self._json(400, {"error": {"message": "cuerpo no JSON"}})
                    return
                banco.payloads.append(payload)
                self._json(200, falso_modelo.responder(banco.reglas, payload))

        def servir(clase, puerto, tls=False):
            srv = ThreadingHTTPServer(("127.0.0.1", puerto), clase)
            if tls:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(cert, clave)
                srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self._servidores.append(srv)

        servicios.PHONE_ID = PHONE_ID
        servir(servicios.ERPNext, PUERTO_ERPNEXT)
        servir(servicios.Meta, PUERTO_META)
        servir(Modelo, PUERTO_MODELO, tls=True)
        self.almacen = servicios.ALMACEN
        self.buzon = servicios.BUZON
        self._decir(f"dobles: erpnext :{PUERTO_ERPNEXT}  meta :{PUERTO_META}  "
                    f"modelo :{PUERTO_MODELO} (https)")

    def _levantar_agente(self) -> None:
        import uvicorn

        # DESPUÉS del entorno: app/main.py lee META_APP_SECRET al importarse.
        from app import main as app_main

        self.app_secret = app_main.APP_SECRET
        config = uvicorn.Config(app_main.app, host="127.0.0.1",
                                port=PUERTO_AGENTE, log_level="error")
        self._uvicorn = uvicorn.Server(config)
        threading.Thread(target=self._uvicorn.run, daemon=True).start()
        for _ in range(90):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{PUERTO_AGENTE}/health", timeout=3
                ) as r:
                    json.loads(r.read())
                    self._decir(f"agente vivo en :{PUERTO_AGENTE}")
                    return
            except Exception:
                time.sleep(0.5)
        raise RuntimeError("el agente no llegó a /health")

    def levantar(self) -> "Banco":
        _verificar_puertos()
        env = entorno(__import__("demo.datos", fromlist=["datos"]),
                      self.extra_env)
        _verificar_destinos(env)
        cert, clave = self._certificado()
        # SSL_CERT_FILE es de httpx/OpenSSL, no un cambio en la app: así el
        # cliente del modelo confía en el certificado del doble sin que nadie
        # apague la verificación de TLS.
        env["SSL_CERT_FILE"] = str(cert)
        os.environ.update(env)
        self._levantar_redis()
        self._levantar_dobles(cert, clave)
        if self._sembrar:
            self._sembrar(self.almacen)
        self._levantar_agente()
        return self

    def bajar(self) -> None:
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
            time.sleep(1.0)
        for srv in self._servidores:
            srv.shutdown()
        if self._redis is not None:
            self._redis.terminate()
            try:
                self._redis.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._redis.kill()
        self._decir("banco desarmado")

    def __enter__(self) -> "Banco":
        return self.levantar()

    def __exit__(self, *_exc) -> None:
        self.bajar()

    # -- un turno

    def _firmar(self, cuerpo: bytes) -> str:
        return "sha256=" + hmac.new(
            self.app_secret.encode(), cuerpo, hashlib.sha256).hexdigest()

    def _instantanea(self) -> dict[str, dict]:
        from demo import falso_erpnext

        foto: dict[str, dict] = {}
        for doctype, tabla in self.almacen.docs.items():
            if doctype in falso_erpnext.HIJAS:
                continue
            for nombre, doc in tabla.items():
                foto[f"{doctype}/{nombre}"] = {
                    c: doc.get(c) for c in (
                        "docstatus", "status", "customer", "grand_total",
                        "delivery_date", "additional_discount_percentage",
                        "discount_amount", "outstanding_amount", "content",
                        "total_qty",
                    ) if doc.get(c) not in (None, "")
                }
        return foto

    def mandar(self, telefono: str, texto: str, *, rol: str = "",
               espera: bool = True) -> Turno:
        """Un webhook firmado, y todo lo que se puede medir del turno."""
        from demo import datos

        self.n_mensaje += 1
        rol = rol or ("gerencia" if telefono in {
            datos.TELEFONO_DUENO, datos.TELEFONO_EQUIPO} else "cliente")
        turno = Turno(telefono, rol, texto)
        antes = self._instantanea()
        desde_buzon = len(self.buzon.envios)
        desde_payload = len(self.payloads)

        cuerpo = json.dumps({
            "object": "whatsapp_business_account",
            "entry": [{"id": "sim-waba", "changes": [{"field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "+5493510000000",
                                 "phone_number_id": PHONE_ID},
                    "contacts": [{"wa_id": telefono,
                                  "profile": {"name": "Demo"}}],
                    "messages": [{
                        "id": f"wamid.SIM.{self.n_mensaje:04d}",
                        "from": telefono,
                        "timestamp": str(int(time.time())),
                        "type": "text",
                        "text": {"body": texto},
                    }],
                }}]}],
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{PUERTO_AGENTE}/webhook/whatsapp",
            data=cuerpo,
            headers={"Content-Type": "application/json",
                     "X-Hub-Signature-256": self._firmar(cuerpo)},
        )
        inicio = time.monotonic()
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()

        if espera:
            turno.respuestas, turno.latencia_s = self._esperar(
                telefono, desde_buzon)
        else:
            time.sleep(2.0)
        turno.cambios = _diferencia(antes, self._instantanea())
        turno.herramientas, turno.resultados = self._del_modelo(desde_payload)
        turno.latencia_s = turno.latencia_s or (time.monotonic() - inicio)
        return turno

    def _esperar(self, telefono: str, desde: int) -> tuple[list[str], float]:
        """Los textos nuevos para ese número, sin contar el aviso de avance."""
        from demo.piloto import _es_acuse

        inicio = time.monotonic()
        vistos: list[str] = []
        while time.monotonic() - inicio < ESPERA_TURNO:
            nuevos = [e for e in self.buzon.envios[desde:] if e.a == telefono]
            vistos = [e.texto for e in nuevos]
            if [t for t in vistos if not _es_acuse(t)]:
                return vistos, time.monotonic() - inicio
            time.sleep(0.25)
        return vistos, time.monotonic() - inicio

    def _del_modelo(self, desde: int) -> tuple[list[tuple[str, dict]], list[str]]:
        """Qué herramientas se llamaron en este turno y qué devolvieron.

        Se lee de la ÚLTIMA request al modelo, que en el protocolo de OpenAI
        trae la conversación entera: los `tool_calls` que el agente ejecutó y,
        pegados, los mensajes `role="tool"` con lo que cada una contestó.
        """
        nuevos = self.payloads[desde:]
        if not nuevos:
            return [], []
        mensajes = nuevos[-1].get("messages") or []
        herramientas: list[tuple[str, dict]] = []
        resultados: list[str] = []
        # Sólo lo de ESTE turno: desde el último mensaje del usuario.
        corte = max(
            (i for i, m in enumerate(mensajes) if m.get("role") == "user"),
            default=0,
        )
        for m in mensajes[corte:]:
            for llamada in (m.get("tool_calls") or []):
                fn = llamada.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {"<crudo>": fn.get("arguments")}
                herramientas.append((str(fn.get("name")), args))
            if m.get("role") == "tool":
                contenido = m.get("content")
                resultados.append(
                    contenido if isinstance(contenido, str) else json.dumps(contenido)
                )
        return herramientas, resultados
