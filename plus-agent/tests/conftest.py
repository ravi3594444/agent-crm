"""Arranque de los tests: nada de ERPNext, Redis ni LLM de verdad.

TODO ESTE ARCHIVO EXISTE POR UNA RAZÓN
`app/erpnext.py`, `app/whatsapp.py` y `app/main.py` leen variables de entorno
obligatorias EN EL IMPORT (`os.environ[...]`). Sin estos valores, importar
cualquier cosa explota antes de llegar al test. Se setean acá, antes de que
pytest importe los módulos de la app.

Los tests son 100% offline y deterministas: se pueden correr en cualquier
máquina, sin docker, sin credenciales, en menos de un segundo. Esa es la
única forma de que se corran de verdad.
"""

from __future__ import annotations

import os

# --- variables obligatorias, antes de cualquier import de la app -----------
os.environ.setdefault("ERPNEXT_URL", "http://erpnext.test")
os.environ.setdefault("ERPNEXT_API_KEY", "k")
os.environ.setdefault("ERPNEXT_API_SECRET", "s")
os.environ.setdefault("ERPNEXT_POLICY_API_KEY", "pk")
os.environ.setdefault("ERPNEXT_POLICY_API_SECRET", "ps")
os.environ.setdefault("META_APP_SECRET", "secreto-de-prueba")
os.environ.setdefault("META_VERIFY_TOKEN", "token-de-prueba")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "123456")
os.environ.setdefault("WHATSAPP_TOKEN", "t")
os.environ.setdefault("ERPNEXT_COMPANY", "Lacteos Test SA")
os.environ.setdefault("ERPNEXT_WAREHOUSE", "Principal - LT")
os.environ.setdefault("TELEFONOS_EQUIPO", "+5493511111111")

import pytest

from app import erpnext, router


class ERPNextFalso:
    """Un ERPNext de mentira, en memoria.

    Guarda lo que se creó para poder afirmar sobre los payloads — que es
    donde estaban la mitad de los bugs (is_pos sin perfil, falta de
    valuation_rate, docstatus).
    """

    # El código de la app hace `except erpnext.ERPNextError`, así que el
    # doble tiene que exponer la misma clase de excepción que el módulo real.
    ERPNextError = erpnext.ERPNextError

    def __init__(self) -> None:
        self.listas: dict[str, list[dict]] = {}
        self.docs: dict[tuple[str, str], dict] = {}
        self.reportes: dict[str, list] = {}
        self.creados: list[tuple[str, dict]] = []
        self.enviados_submit: list[tuple[str, str]] = []
        self.comentarios: list[tuple[str, str, str]] = []
        self.fallar_en: set[str] = set()
        self.consultas: list[tuple[str, list | None]] = []
        self._contador = 0

    # --- lo que el código de la app llama ---------------------------------
    def get_list(self, doctype, filters=None, fields=None, limit=20, order_by=None):
        self.consultas.append((doctype, filters))
        if doctype in self.fallar_en:
            raise erpnext.ERPNextError(f"{doctype} falla (test)")
        filas = self.listas.get(doctype, [])
        return [dict(f) for f in filas[:limit]]

    def get_doc(self, doctype, name):
        if doctype in self.fallar_en:
            raise erpnext.ERPNextError(f"{doctype} falla (test)")
        doc = self.docs.get((doctype, name))
        if doc is None:
            raise erpnext.ERPNextError(f"{doctype} {name} no existe (test)")
        return dict(doc)

    def create_doc(self, doctype, payload):
        if doctype in self.fallar_en:
            raise erpnext.ERPNextError(f"{doctype} falla (test)")
        self._contador += 1
        nombre = f"{doctype[:3].upper()}-{self._contador:04d}"
        guardado = {**payload, "docstatus": 0, "name": nombre}
        self.creados.append((doctype, guardado))
        self.docs[(doctype, nombre)] = guardado
        return guardado

    def submit_doc(self, doctype, name):
        if f"submit:{doctype}" in self.fallar_en:
            raise erpnext.ERPNextError(f"submit {doctype} falla (test)")
        self.enviados_submit.append((doctype, name))
        doc = self.docs.get((doctype, name))
        if doc:
            doc["docstatus"] = 1
        return doc or {}

    def add_comment(self, doctype, name, text):
        self.comentarios.append((doctype, name, text))

    def run_report(self, report_name, filters=None):
        if f"report:{report_name}" in self.fallar_en:
            raise erpnext.ERPNextError(f"reporte {report_name} falla (test)")
        self.consultas.append((f"report:{report_name}", filters))
        return self.reportes.get(report_name, [])

    def default_company(self):
        return "Lacteos Test SA"

    def default_warehouse(self):
        return "Principal - LT"

    # --- helpers para los tests -------------------------------------------
    def creados_de(self, doctype: str) -> list[dict]:
        return [p for d, p in self.creados if d == doctype]

    def ultimo_creado(self, doctype: str) -> dict:
        docs = self.creados_de(doctype)
        assert docs, f"no se creó ningún {doctype}"
        return docs[-1]


@pytest.fixture
def erp(monkeypatch):
    """Reemplaza el módulo erpnext donde sea que se haya importado."""
    falso = ERPNextFalso()
    for modulo in (
        "app.erpnext",
        "app.clientes",
        "app.policy",
        "app.aprobacion",
        "app.tools.catalogo",
        "app.tools.pedidos",
        "app.tools.captura",
        "app.tools.gerencia",
    ):
        try:
            __import__(modulo)
        except Exception:
            continue
    import sys

    for nombre in list(sys.modules):
        mod = sys.modules[nombre]
        if not nombre.startswith("app.") and nombre != "app":
            continue
        if getattr(mod, "erpnext", None) is not None:
            monkeypatch.setattr(mod, "erpnext", falso, raising=False)
    # También en el módulo real, para quien lo use por atributo.
    for fn in (
        "get_list",
        "get_doc",
        "create_doc",
        "submit_doc",
        "add_comment",
        "run_report",
        "default_company",
        "default_warehouse",
    ):
        monkeypatch.setattr(erpnext, fn, getattr(falso, fn), raising=False)
    return falso


class WhatsAppFalso:
    def __init__(self) -> None:
        self.mensajes: list[tuple[str, str]] = []
        self.botones: list[tuple[str, str, list]] = []
        self.fallar = False

    def enviar_mensaje(self, numero, texto):
        if self.fallar:
            return False
        self.mensajes.append((numero, texto))
        return True

    def enviar_botones(self, numero, texto, botones):
        if self.fallar:
            return False
        self.botones.append((numero, texto, botones))
        return True

    def textos_a(self, numero: str) -> list[str]:
        from app import telefono as t

        objetivo = t.normalizar(numero)
        return [txt for n, txt in self.mensajes if t.normalizar(n) == objetivo]


@pytest.fixture
def wa(monkeypatch):
    """Intercepta todo envío de WhatsApp."""
    falso = WhatsAppFalso()
    import sys

    for nombre in list(sys.modules):
        if not (nombre == "app" or nombre.startswith("app.")):
            continue
        mod = sys.modules[nombre]
        if hasattr(mod, "enviar_mensaje"):
            monkeypatch.setattr(mod, "enviar_mensaje", falso.enviar_mensaje, raising=False)
        if hasattr(mod, "enviar_botones"):
            monkeypatch.setattr(mod, "enviar_botones", falso.enviar_botones, raising=False)
    return falso


@pytest.fixture
def lock_libre(monkeypatch):
    """El lock de auto-confirmación siempre se consigue."""
    import contextlib

    from app import lock

    @contextlib.contextmanager
    def tomar(nombre, ttl=None, espera=None):
        yield True

    monkeypatch.setattr(lock, "tomar", tomar)
    import app.tools.pedidos as pedidos_mod

    monkeypatch.setattr(pedidos_mod.lock, "tomar", tomar, raising=False)
    return True


@pytest.fixture
def lock_ocupado(monkeypatch):
    """El lock NUNCA se consigue: verifica que se cae a revisión humana."""
    import contextlib

    from app import lock

    @contextlib.contextmanager
    def tomar(nombre, ttl=None, espera=None):
        yield False

    monkeypatch.setattr(lock, "tomar", tomar)
    import app.tools.pedidos as pedidos_mod

    monkeypatch.setattr(pedidos_mod.lock, "tomar", tomar, raising=False)
    return True


@pytest.fixture
def equipo(monkeypatch):
    """Lista de staff conocida y determinística."""
    monkeypatch.setenv("TELEFONOS_EQUIPO", "+54 9 351 111-1111, 03514 15 22-2222")
    router.recargar()
    yield router.STAFF
    monkeypatch.setenv("TELEFONOS_EQUIPO", "+5493511111111")
    router.recargar()


@pytest.fixture
def auto_confirm_on(monkeypatch):
    """Auto-confirmación habilitada con valores conocidos."""
    monkeypatch.setenv("AUTO_CONFIRM_MAX", "100000")
    monkeypatch.setenv("AUTO_CONFIRM_MULT", "2.0")
    monkeypatch.setenv("AUTO_CONFIRM_MIN_ORDERS", "3")
    monkeypatch.setenv("AUTO_CONFIRM_MAX_DEBT", "0")
    monkeypatch.setenv("STOCK_BUFFER_PCT", "20")
