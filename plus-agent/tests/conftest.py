"""Test isolation: every test runs against DUMMY credentials, never the real .env.

EL BUG QUE ESTO ARREGLA
app/__init__.py hace load_dotenv(find_dotenv()) al importar. find_dotenv camina
hacia arriba desde el cwd, así que en la máquina de desarrollo encontraba el
.env REAL (con las claves de producción) y los tests "pasaban". En cualquier
checkout limpio —CI, otro developer, un worktree— no hay .env, y la colección
explota con KeyError: 'ERPNEXT_URL' antes de correr un solo test.

Los tests no pueden depender de secretos reales ni de que exista un .env.
Este conftest fija valores de prueba ANTES de que cualquier módulo importe
`app`. Usa setdefault, así que un test que ya fijaba lo suyo sigue igual; y
como load_dotenv corre con override=False, el .env real —si existe— NO pisa
estos valores: los tests quedan aislados de producción en las dos direcciones.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_DUMMY = {
    # obligatorias al importar (os.environ[...])
    "ERPNEXT_URL": "http://erpnext.test",
    "ERPNEXT_API_KEY": "test-key",
    "ERPNEXT_API_SECRET": "test-secret",
    "ERPNEXT_MANAGER_API_KEY": "manager-key",
    "ERPNEXT_MANAGER_API_SECRET": "manager-secret",
    "ERPNEXT_POLICY_API_KEY": "policy-key",
    "ERPNEXT_POLICY_API_SECRET": "policy-secret",
    "META_APP_SECRET": "test-app-secret",
    "META_VERIFY_TOKEN": "test-verify-token",
    # DATABASE 0, and not by preference: RediSearch refuses FT.CREATE on any
    # other one ("Cannot create index on db != 0"), and app/graph.py creates the
    # checkpointer's indices AT IMPORT. This used to say /15, which looked like
    # isolation and worked on any machine that happened to have those indices
    # left on db 15 from before — and failed at COLLECTION on every clean Redis
    # Stack, which is every CI run and every new checkout. Isolation comes from
    # the server being a throwaway one, not from the database number.
    "REDIS_URL": "redis://localhost:6379/0",
    "WHATSAPP_PHONE_NUMBER_ID": "test-phone-id",
    "WHATSAPP_TOKEN": "test-token",
    # app/graph.py builds the chat models at import (app/modelos.py refuses to
    # start without the key). A dummy lets a test import the real tool lists
    # (see tests/test_frontera_decisiones.py) without credentials or network:
    # ChatOpenAI makes no request when constructed.
    "DASHSCOPE_API_KEY": "test-dashscope-key",
    # WHICH provider, pinned. The developer's real .env may say
    # LLM_PROVIDER=gemini, and then the suite would be exercising a different
    # endpoint, model names and key than the one it asserts on — the exact leak
    # this file's docstring exists to describe. Tests that care about the other
    # provider set it themselves (see tests/test_modelos.py).
    "LLM_PROVIDER": "qwen",
    # The developer's real .env may still carry the old "google_genai:…" names;
    # pin the Qwen defaults so the suite never depends on that file.
    "QWEN_SALES_MODEL": "qwen3.7-plus-2026-05-26",
    "QWEN_MANAGER_MODEL": "qwen3.8-max",
    # opcionales que cambian comportamiento: valores deterministas para tests
    # LA QUE DECIDE QUÉ MOMENTO ES, y va con setdefault — como el idioma, y por
    # el mismo motivo. Estuvo FIJA hasta el issue #22, y mientras duró el motivo
    # era cierto: `tests/test_pendientes.py::epoch` armaba sus momentos con
    # Buenos Aires escrito a mano, así que el reloj del negocio tenía que ser el
    # mismo. Medido con el pin afuera y Asia/Kolkata en el entorno: 28 tests
    # —no 26— con los mismos assert que arregló el PR #12 (`assert 13 == 3` en
    # test_the_round_is_capped), porque el borrador del fixture envejecía 8:30 h
    # de golpe.
    #
    # Lo que sacó el pin no fue desfijar la variable: fue que los archivos que
    # dependen del reloj dejaran de heredar la zona. Cada uno declara su día con
    # `RelojDePrueba` (abajo) y arma sus horas DE PARED en la zona que diga esta
    # variable, así que las 09:00 en que se crea el borrador, las 15:00 de la
    # ronda y las 23:30 de la ventana nocturna son las mismas en cualquier zona,
    # y las edades que los tests restan no se mueven. Recién ahí el pin sobraba.
    #
    # Con eso la celda `BUSINESS_TIMEZONE=Asia/Kolkata` de CI (#13, PR #28) pasó
    # de no poder fallar —esta lista la sobreescribía antes del primer test— a
    # probar la zona de verdad.
    "BUSINESS_TIMEZONE": "America/Argentina/Buenos_Aires",
    "ERPNEXT_COMPANY": "Lacteos Test SA",
    "ERPNEXT_WAREHOUSE": "Principal - LT",
    # EL IDIOMA VA CON setdefault, NO FIJO, y ésa es la diferencia entera.
    # Fijarlo hacía pasar la suite, pero por la razón equivocada: con
    # IDIOMA_DEFAULT=en exportado se caían 160 tests con el código funcionando
    # bien, porque afirmaban la salida española sin decir que la esperaban. Un
    # pin tapa eso — y con él la suite no puede probar el segundo idioma, que
    # es el muro contra el que va todo el plan bilingüe.
    #
    # El arreglo es que cada test que afirma texto DIGA su idioma
    # (`pytestmark = pytest.mark.idioma("es")` + el fixture
    # `_idioma_declarado`), y con eso el entorno puede decir lo que quiera: la
    # suite da el mismo resultado con IDIOMA_DEFAULT=en y con IDIOMA_GERENCIA=en.
    # El setdefault se queda para que un checkout limpio sin .env pruebe en el
    # idioma de fábrica, no para impedir el otro.
    "IDIOMA_DEFAULT": "es",
    "IDIOMA_GERENCIA": "",
    # LA FORMA DEL NÚMERO, y va con setdefault por el mismo motivo que el
    # idioma. `$12.000` y `$12,000` son el mismo monto escrito para dos personas
    # distintas, así que un archivo que afirma uno de los dos tiene que DECIRLO
    # (`pytestmark = pytest.mark.locale("es_AR")`) y no heredarlo. Con el
    # locale fijo acá, la suite no podría probar nunca la salida que ve el
    # cliente que habla inglés — que es justo la que este trabajo agregó.
    "LOCALE": "es_AR",
    # DIGEST_ACTIVO sí se queda FIJA (abajo): no elige un idioma equivalente,
    # apaga la sección entera, y con DIGEST_ACTIVO=0 el test que prueba que el
    # resumen sale una vez por día no prueba nada.
    "DIGEST_ACTIVO": "true",
    # LAS DOS QUE DECIDEN QUIÉN ES QUIÉN, y las dos que un .env real cambia.
    # app/telefono.py lee PAIS_TELEFONO AL IMPORTAR y app/router.py arma STAFF
    # igual, así que un .env con otro país o con el número real del dueño no
    # "configura" la suite: la hace probar otra cosa. Con PAIS_TELEFONO=91 se
    # caen veinte tests de identidad que no tienen nada que ver con el país, y
    # con un TELEFONOS_EQUIPO cargado, un test que dice "nadie autoriza" prueba
    # lo contrario de lo que dice. Vacío es lo que hay en un checkout limpio,
    # y los tests que necesitan un equipo lo fijan ellos con router.recargar().
    "PAIS_TELEFONO": "54",
    "TELEFONOS_EQUIPO": "",
}
# Casi todo es setdefault: un test o CI que ya fijó algo (REDIS_URL, sobre
# todo) manda. Pero lo que decide QUIÉN ES QUIÉN, QUÉ PROVEEDOR y QUÉ MOMENTO
# se prueba se fija sin condición: con setdefault, un shell que exporta
# LLM_PROVIDER=gemini, BUSINESS_TIMEZONE=Asia/Kolkata o un TELEFONOS_EQUIPO
# cargado llegaba a la suite, que es justo la fuga que el docstring de arriba
# describe.
#
# NI EL IDIOMA NI LA ZONA ESTÁN EN ESTA LISTA, y hasta el issue #22 la nota que
# había acá decía lo contrario: que fijar una zona ES el arreglo —porque
# `epoch()` tenía Buenos Aires escrito a mano— y que no había que confundir los
# dos casos. Resultó ser el mismo caso, y lo que lo probó fue la cuarta celda de
# CI. El catálogo tiene 129 claves en dos idiomas para que el producto pueda
# hablar los dos, y un pin le sacaba a la suite la capacidad de probar el
# segundo; con la zona pasaba igual, salvo que lo que no se podía probar era un
# negocio que no está en Buenos Aires.
#
# La diferencia estaba en `epoch()`, no en el concepto: mientras el test escribía
# su zona a mano, desfijar la variable rompía 28 tests por la razón equivocada.
# Con el momento declarado (`RelojDePrueba`) el pin sobra, y sin el pin la celda
# `BUSINESS_TIMEZONE=Asia/Kolkata` prueba algo. En los dos casos el arreglo es
# que el test DECLARE lo que supone, no que el conftest se lo imponga a toda la
# suite.
#
# DIGEST_ACTIVO sí se queda fija: no elige un valor equivalente, apaga la
# sección entera.
_FIJAS = {
    "LLM_PROVIDER",
    "QWEN_SALES_MODEL",
    "QWEN_MANAGER_MODEL",
    "PAIS_TELEFONO",
    "TELEFONOS_EQUIPO",
    "DIGEST_ACTIVO",
}
for _k, _v in _DUMMY.items():
    if _k in _FIJAS:
        os.environ[_k] = _v
    else:
        os.environ.setdefault(_k, _v)

from unittest.mock import Mock

import pytest
from redis.exceptions import RedisError

from app import reloj
from tests.fakes import FakeMarcas


class RelojDePrueba:
    """El día que un archivo de test NOMBRA, y las horas de pared que le cuelgan.

    POR QUÉ EXISTE
    Seis archivos definían su propio reloj —`ZONA`, `AHORA`, `HOY`, `epoch()`—
    y todos escribían Buenos Aires a mano: la misma zona que el código resuelve
    desde `BUSINESS_TIMEZONE`. El sello y el reloj salían de la MISMA suposición
    escrita dos veces, así que la resta que mide una edad daba bien POR
    CONSTRUCCIÓN y ningún test podía discreparle al código sobre qué hora de
    pared era. Por eso el bug de zona de #16 pasó inadvertido justamente a los
    tests que tocaban ese campo: un test que no puede estar en desacuerdo con el
    código sobre un concepto no está probando ese concepto.

    LA ZONA SE RESUELVE AL USARLA, NO AL IMPORTAR
    Sale de `reloj.zona()` —el mismo reloj que usa `app/`— en cada llamada. Un
    archivo nombra su día y sus horas de pared, y nada más:

        RELOJ = RelojDePrueba("2026-09-08")
        RELOJ.a_las(9)                 # las 09:00 DEL NEGOCIO, en la zona que haya
        RELOJ.epoch(23, 30)            # el mismo instante, como lo toma tick()
        RELOJ.sello(RELOJ.a_las(9))    # el sello SIN zona que escribe ERPNext

    Con eso las horas de pared no se mueven cuando se mueve la zona, y las edades
    que los tests restan tampoco: son las mismas 6 h en Buenos Aires y en
    Kolkata. Ésa es la propiedad que le sacó el pin a `BUSINESS_TIMEZONE` en
    `_FIJAS` y volvió real la celda `BUSINESS_TIMEZONE=Asia/Kolkata` de CI.

    NO ES «UN LUGAR MENOS»: `en=` ES LA MITAD QUE IMPORTA
    `sello(..., en=otra_zona)` escribe el sello en la hora de OTRO sistema. Es lo
    que le devuelve al test la capacidad de discrepar: un ERPNext en una zona
    distinta de la del negocio —el caso de #16, y lo que
    `readiness.chequear_zona_erpnext` bloquea— ahora se puede DECIR, y antes no.
    Un test que nombra sus dos zonas a propósito es correcto y tiene que
    nombrarlas; lo que se fue es la zona heredada como decorado.
    """

    def __init__(self, dia: str) -> None:
        self._dia = date.fromisoformat(dia)

    @property
    def hoy(self) -> date:
        """El día del negocio que este archivo nombra."""
        return self._dia

    @property
    def zona(self) -> ZoneInfo:
        """La zona del negocio, resuelta ahora. Levanta `reloj.ZonaInvalida`."""
        return reloj.zona()

    def a_las(self, hora: int, minuto: int = 0, *, dia: int | None = None) -> datetime:
        """Esa hora DE PARED, en la zona del negocio. `dia` es el día del mes."""
        base = self._dia if dia is None else self._dia.replace(day=dia)
        return datetime(base.year, base.month, base.day, hora, minuto, tzinfo=self.zona)

    def epoch(self, hora: int, minuto: int = 0, *, dia: int | None = None) -> float:
        """El mismo instante que `a_las`, como epoch — que es lo que toma `tick()`."""
        return self.a_las(hora, minuto, dia=dia).timestamp()

    def sello(self, momento: datetime, *, en: ZoneInfo | None = None) -> str:
        """El sello SIN zona que ERPNext guarda, en la hora de `en`.

        `en=None` es «ERPNext está en la zona del negocio», que es lo que
        `readiness.chequear_zona_erpnext` exige. Pasarle otra zona es cómo un
        test dice que NO coinciden, que es el caso que ningún test podía escribir.
        """
        return self._naive(momento, en).strftime("%Y-%m-%d %H:%M:%S")

    def sello_partido(
        self, momento: datetime, *, en: ZoneInfo | None = None
    ) -> tuple[str, str]:
        """`(posting_date, posting_time)`, los dos campos que ERPNext separa."""
        naive = self._naive(momento, en)
        return naive.date().isoformat(), naive.strftime("%H:%M:%S")

    def _naive(self, momento: datetime, en: ZoneInfo | None) -> datetime:
        return momento.astimezone(en or self.zona).replace(tzinfo=None)


class FakeRedis:
    """Enough Redis for app/limites.py, with no server and no network.

    The limits the owner sets are the numbers that decide whether an order
    confirms with nobody watching, so no test result may depend on what some
    real Redis happens to have left over, and no test should need one running.
    """

    def __init__(self, hashes=None, strings=None, lists=None, zsets=None):
        self.hashes = {k: dict(v) for k, v in (hashes or {}).items()}
        self.strings = dict(strings or {})
        self.lists = {k: list(v) for k, v in (lists or {}).items()}
        self.zsets = {k: dict(v) for k, v in (zsets or {}).items()}
        self.ttls: dict[str, int] = {}
        self.caido = False

    def _vivo(self) -> None:
        if self.caido:
            raise RedisError("redis de prueba caído")

    def hgetall(self, key):
        self._vivo()
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field, value):
        self._vivo()
        self.hashes.setdefault(key, {})[field] = value

    def hincrby(self, key, field, amount=1):
        """Para el contador por día de app/sombra.py."""
        self._vivo()
        campos = self.hashes.setdefault(key, {})
        campos[field] = int(campos.get(field, 0)) + int(amount)
        return campos[field]

    def get(self, key):
        self._vivo()
        return self.strings.get(key)

    def setex(self, key, ttl, value):
        self._vivo()
        self.strings[key] = value
        self.ttls[key] = ttl

    def set(self, key, value, nx=False, ex=None):
        """SET, con NX. app/acciones.py se apoya en que NX sea ATÓMICO.

        Es como se reserva el código de seis dígitos: o la clave no existía y
        queda escrita, o ya había una propuesta viva con ese código y no se
        pisa nada. Un doble que ignorara `nx` dejaría pasar exactamente el bug
        que ese SET existe para hacer imposible.
        """
        self._vivo()
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def exists(self, key):
        self._vivo()
        return 1 if key in self.strings else 0

    def expire(self, key, ttl):
        self._vivo()
        self.ttls[key] = ttl
        return True

    def incr(self, key):
        self._vivo()
        nuevo = int(self.strings.get(key) or 0) + 1
        self.strings[key] = str(nuevo)
        return nuevo

    def delete(self, key):
        self._vivo()
        self.strings.pop(key, None)

    def getdel(self, key):
        """Leer y borrar en una sola operación, como el GETDEL de Redis.

        app/acciones.py lo usa para que un código de confirmación sirva UNA
        vez: dos workers con el mismo mensaje encuentran uno la propuesta y el
        otro nada. Con get + delete por separado los dos la encontrarían.
        """
        self._vivo()
        return self.strings.pop(key, None)

    def rpush(self, key, value):
        self._vivo()
        self.lists.setdefault(key, []).append(value)

    def lrange(self, key, start, end):
        self._vivo()
        datos = self.lists.get(key, [])
        total = len(datos)
        desde = max(0, total + start) if start < 0 else start
        hasta = total + end if end < 0 else end
        return datos[desde : hasta + 1]

    def ltrim(self, key, start, end):
        self._vivo()
        self.lists[key] = self.lrange(key, start, end)

    # Los conjuntos ordenados. app/acciones.py guarda ahí los códigos vivos de
    # un teléfono, con el vencimiento de puntaje, para contestar "¿hay algo
    # esperando?" sin barrer el keyspace.
    @staticmethod
    def _limite(valor, por_defecto):
        if valor in ("-inf", "+inf", "inf"):
            return float("-inf") if valor == "-inf" else float("inf")
        try:
            return float(valor)
        except (TypeError, ValueError):
            return por_defecto

    def zadd(self, key, mapping):
        self._vivo()
        self.zsets.setdefault(key, {}).update(
            {str(m): float(p) for m, p in mapping.items()}
        )
        return len(mapping)

    def zrem(self, key, *members):
        self._vivo()
        datos = self.zsets.get(key, {})
        return sum(1 for m in members if datos.pop(str(m), None) is not None)

    def zcard(self, key):
        self._vivo()
        return len(self.zsets.get(key, {}))

    def zrangebyscore(self, key, minimo, maximo):
        self._vivo()
        desde = self._limite(minimo, float("-inf"))
        hasta = self._limite(maximo, float("inf"))
        return [
            m
            for m, p in sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
            if desde <= p <= hasta
        ]

    def zremrangebyscore(self, key, minimo, maximo):
        self._vivo()
        fuera = self.zrangebyscore(key, minimo, maximo)
        datos = self.zsets.get(key, {})
        for m in fuera:
            datos.pop(m, None)
        return len(fuera)


def inventario_confiable(
    monkeypatch,
    *,
    maestra: bool = True,
    fresco: bool = True,
    motivo: str = "el último conteo de LECHE-1L es de hace 40 h (vale 24 h)",
):
    """Trust the inventory the way a counted-and-confirmed morning does.

    Trust is earned per product now (app/inventario.py), so a test about some
    other rule says "the count is in" here instead of restating it. The tests
    about the counting itself live in tests/test_inventario.py and use the real
    function.
    """
    from app import inventario

    monkeypatch.setenv("STOCK_CONFIABLE", "true" if maestra else "false")
    monkeypatch.setenv("STOCK_CONFIABLE_HORAS", "24")
    monkeypatch.setattr(
        inventario,
        "confiable",
        lambda code, warehouse: (fresco, "" if fresco else motivo),
    )


def entrega_autorizada(
    monkeypatch,
    *,
    autorizada: bool = True,
    motivo: str = "entrega a revisar: Ruta 9 km 300, Villa Rara — el código postal X9999 no está en las zonas de reparto",
):
    """Say the delivery address is settled, without restating how.

    Delivery eligibility is decided in app/entrega.py, deterministically and
    outside the model. A test about some other rule says "the address is fine"
    here; the tests about the decision itself live in tests/test_entrega.py and
    use the real function.
    """
    from app import entrega

    monkeypatch.setattr(
        entrega,
        "autorizada",
        lambda sales_order: (autorizada, "" if autorizada else motivo),
    )


@pytest.fixture(autouse=True)
def limites_sin_redis(monkeypatch):
    """Every test starts with an EMPTY limits store and a clean environment.

    Limits then resolve the way they do in production — store, then the
    bootstrap environment, then the code default — but from nothing, so a test
    that cares sets exactly what it needs. A test about the store itself
    installs its own FakeRedis over this one.
    """
    from app import locks

    for nombre in (
        "AUTO_CONFIRM_MAX",
        "AUTO_CONFIRM_MAX_QTY_POR_PRODUCTO",
        "STOCK_BUFFER_PCT",
        "AUTO_CONFIRM_MAX_CLIENTE_NUEVO",
        "AUTO_CONFIRM_MAX_DEBT",
        "AUTO_CONFIRM_DESCUENTOS_APRUEBAN",
        "AUTO_CONFIRM_MAX_DESCUENTO_PCT",
        "STOCK_CONFIABLE",
        "STOCK_CONFIABLE_HORAS",
        # The two hold deadlines. Without them here, a developer machine with a
        # real .env resolves them from that file and a test that says "the
        # owner's number decides" proves nothing — the exact leak this
        # conftest's docstring exists to describe.
        "APROBACION_TIMEOUT_HORAS",
        "REVISION_TIMEOUT_HORAS",
        # Shadow mode: a developer .env with it on would make the shadow tests
        # pass for the wrong reason, and every other test read one more order.
        "AUTO_CONFIRM_SOMBRA",
        # The plain-draft deadline, its closer and the quiet hours. Same leak
        # as the two timeouts above: a developer .env that sets the closer
        # would have the sweep closing drafts inside an unrelated test.
        "PENDIENTE_AVISO_HORAS",
        "PENDIENTE_CIERRE_HORAS",
        "PENDIENTE_NOCHE_DESDE",
        "PENDIENTE_NOCHE_HASTA",
    ):
        monkeypatch.delenv(nombre, raising=False)
    vacio = FakeRedis()
    monkeypatch.setattr(locks, "conexion", lambda: vacio)
    # An empty store is only "brand new install" if ERPNext has no record of a
    # limit ever being changed. Tests answer that question themselves rather
    # than reaching for ERPNext; the ones about data loss say otherwise.
    from app import limites

    monkeypatch.setattr(limites, "_hubo_cambios_durables", lambda: False)
    monkeypatch.setattr(limites, "_durable_cache", None)
    # The delivery rules have their OWN durable marker, so their own question.
    # Answering it False here is what lets a test use the bootstrap environment
    # for a delivery rule; the tests about a wiped store say otherwise.
    monkeypatch.setattr(limites, "_hubo_cambios_durables_entrega", lambda: False)
    monkeypatch.setattr(limites, "_durable_cache_entrega", None)
    # Applying a limit change writes a durable copy to ERPNext. No test may
    # reach a real one; the tests about that record assert on this mock.
    monkeypatch.setattr(limites.erpnext, "registrar_comentario", Mock())
    return vacio


@pytest.fixture(autouse=True)
def marcas_sin_redis(monkeypatch):
    """Every test starts with an empty, in-memory marker store; tests that need
    their own fake (the webhook harness) override it.

    app/avisos.py and app/confirmacion.py both reach Redis through
    outbound_status.cliente(), so patching this one attribute keeps the notice
    queue and the confirmation cache in memory too.
    """
    from app import outbound_status

    marcas = FakeMarcas()
    monkeypatch.setattr(outbound_status, "_client", marcas)
    return marcas


@pytest.fixture(autouse=True)
def _idioma_declarado(request, monkeypatch):
    """El idioma de un test lo declara el test, no el entorno.

    Un archivo que afirma literales en un idioma lo dice en una línea:

        pytestmark = pytest.mark.idioma("es")

    y este fixture fija las dos variables que resuelven el idioma para esa
    corrida. Sin eso el idioma sale del entorno, y la suite pasa sólo donde el
    entorno diga lo mismo que el test supone: con `IDIOMA_DEFAULT=en`
    exportado se caían 160 tests **con el código funcionando bien**, afirmando
    «necesito revisarlo con una persona» contra un inglés correcto. El issue
    #15 tiene la medición.

    Es la misma forma que `tests/test_fechas_entrega.py`, que fija
    `HOY = date(2026, 9, 1)` y lo pasa en vez de leer el reloj — y ese archivo
    existe por un bug que se vio en producción. El test nombra el valor del
    que depende en vez de heredarlo del mundo.

    ESTO NO REEMPLAZA PASAR `lengua` DONDE EL CONSTRUCTOR LO TOMA. 42 firmas
    del producto ya lo aceptan, y pasarlo es más fuerte: fija el idioma en la
    llamada y no depende del entorno para nada. La marca cubre los caminos que
    resuelven el idioma solos —por teléfono (`idioma.para_destinatario`) o por
    el store del dueño (`idioma.gerencia`)— y que no reciben el idioma por
    parámetro.

    Un archivo SIN la marca queda como estaba: hereda el default. Eso es
    correcto para los que no afirman texto en ningún idioma, y es la razón de
    que la marca sea opt-in y no automática — `grep -rn "pytest.mark.idioma"
    tests/` lista exactamente los archivos que dependen del idioma.
    """
    marca = request.node.get_closest_marker("idioma")
    if marca is None:
        return
    from app import idioma

    lengua = str(marca.args[0]) if marca.args else ""
    if lengua not in idioma.IDIOMAS:
        raise ValueError(
            f"pytest.mark.idioma({lengua!r}): los idiomas son {idioma.IDIOMAS}"
        )
    monkeypatch.setenv("IDIOMA_DEFAULT", lengua)
    monkeypatch.setenv("IDIOMA_GERENCIA", lengua)


@pytest.fixture(autouse=True)
def _locale_declarado(request, monkeypatch):
    """La FORMA DEL NÚMERO también la declara el test, no el entorno.

    Hermano de `_idioma_declarado`, y separado a propósito porque son dos
    decisiones distintas: `IDIOMA_GERENCIA` elige en qué idioma se le habla al
    dueño y `LOCALE` elige si un total se escribe `$12.000` o `$12,000`. Un
    almacén argentino con un dueño que lee en inglés usa los dos valores
    cruzados, así que una sola marca para ambos mentiría sobre el caso real.

        pytestmark = pytest.mark.locale("es_AR")

    Sin la marca, el archivo hereda el default — correcto para los que no
    afirman ningún monto. `grep -rn "pytest.mark.locale" tests/` lista los que
    sí dependen de la forma del número.
    """
    marca = request.node.get_closest_marker("locale")
    if marca is None:
        return
    from app import formato

    codigo = str(marca.args[0]) if marca.args else ""
    if codigo not in formato.LOCALES:
        raise ValueError(
            f"pytest.mark.locale({codigo!r}): los locales son {formato.LOCALES}"
        )
    monkeypatch.setenv("LOCALE", codigo)


@pytest.fixture(autouse=True)
def el_barrido_no_lee_el_reloj_real(monkeypatch):
    """`pendientes.tick()` sin `ahora=` explota, en CUALQUIER archivo.

    Es la regla del PR #12 dejada de ser una convención de un archivo. Ahí, once
    llamadas comparaban un `creation` fijo contra el reloj vivo del negocio y la
    suite pasaba a la mañana y fallaba a la tarde; el guard que lo arregló vive
    dentro de `tests/test_pendientes.py`, así que el mismo error escrito en
    `tests/test_digest.py` o en un archivo nuevo vuelve a costar medio día de CI
    rojo antes de que alguien lo reconozca.

    Acá no hay que distinguir el uso coherente del incoherente: NINGÚN test de
    la suite lee este reloj —las 51 llamadas a `tick()` pasan el momento, y las
    otras funciones de `pendientes` que lo usan (`edad_horas`, `en_silencio`) lo
    reciben de sus llamadores—, así que prohibirlo no le cuesta un test a nadie.
    Un test que de verdad quiera el reloj de verdad lo dice en voz alta con su
    propio `monkeypatch.setattr(pendientes, "_ahora", …)`, que corre después de
    éste y manda.
    """
    from app import pendientes

    def _prohibido():
        # pytest.fail y no assert: `Failed` hereda de BaseException, así que el
        # `except Exception` de la ronda no puede tragárselo y dejar el guard
        # como una ronda que no hizo nada.
        pytest.fail(
            "este test leyó la hora REAL de app/pendientes.py. Pasale el "
            "momento: `pendientes.tick(ahora=…)`, `edad_horas(fila, momento)`, "
            "`en_silencio(momento)`. Ver el guard de tests/conftest.py."
        )

    monkeypatch.setattr(pendientes, "_ahora", _prohibido)
