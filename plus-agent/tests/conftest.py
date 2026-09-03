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
from pathlib import Path

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
    "REDIS_URL": "redis://localhost:6379/15",
    "WHATSAPP_PHONE_NUMBER_ID": "test-phone-id",
    "WHATSAPP_TOKEN": "test-token",
    # app/graph.py builds the chat models at import (app/modelos.py refuses to
    # start without the key). A dummy lets a test import the real tool lists
    # (see tests/test_frontera_decisiones.py) without credentials or network:
    # ChatOpenAI makes no request when constructed.
    "DASHSCOPE_API_KEY": "test-dashscope-key",
    # The developer's real .env may still carry the old "google_genai:…" names;
    # pin the Qwen defaults so the suite never depends on that file.
    "QWEN_SALES_MODEL": "qwen3.7-plus-2026-05-26",
    "QWEN_MANAGER_MODEL": "qwen3.8-max",
    # opcionales que cambian comportamiento: valores deterministas para tests
    "BUSINESS_TIMEZONE": "America/Argentina/Buenos_Aires",
    "ERPNEXT_COMPANY": "Lacteos Test SA",
    "ERPNEXT_WAREHOUSE": "Principal - LT",
}
for _k, _v in _DUMMY.items():
    os.environ.setdefault(_k, _v)

from unittest.mock import Mock

import pytest
from redis.exceptions import RedisError


class FakeRedis:
    """Enough Redis for app/limites.py, with no server and no network.

    The limits the owner sets are the numbers that decide whether an order
    confirms with nobody watching, so no test result may depend on what some
    real Redis happens to have left over, and no test should need one running.
    """

    def __init__(self, hashes=None, strings=None, lists=None):
        self.hashes = {k: dict(v) for k, v in (hashes or {}).items()}
        self.strings = dict(strings or {})
        self.lists = {k: list(v) for k, v in (lists or {}).items()}
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

    def get(self, key):
        self._vivo()
        return self.strings.get(key)

    def setex(self, key, ttl, value):
        self._vivo()
        self.strings[key] = value
        self.ttls[key] = ttl

    def delete(self, key):
        self._vivo()
        self.strings.pop(key, None)

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
    # Applying a limit change writes a durable copy to ERPNext. No test may
    # reach a real one; the tests about that record assert on this mock.
    monkeypatch.setattr(limites.erpnext, "registrar_comentario", Mock())
    return vacio


class _MarcasSinRedis:
    """Enough of Redis for app/outbound_status.py's markers and claims."""

    def __init__(self) -> None:
        self.values: dict = {}
        self.lists: dict = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        self.values.pop(key, None)
        return 1

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def llen(self, key):
        return len(self.lists.get(key, []))

    def scan_iter(self, match="*", count=100):
        return iter([k for k in self.values if k.startswith(match.rstrip("*"))])

    def eval(self, *args):
        return "accepted_by_meta"


@pytest.fixture(autouse=True)
def marcas_sin_redis(monkeypatch):
    """Every test starts with an empty, in-memory marker store; tests that need
    their own fake (the webhook harness) override it."""
    from app import outbound_status

    marcas = _MarcasSinRedis()
    monkeypatch.setattr(outbound_status, "_client", marcas)
    return marcas
