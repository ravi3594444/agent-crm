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
    # app/graph.py builds the chat model at import, and the provider SDK
    # validates its key eagerly. A dummy lets a test import the real tool lists
    # (see tests/test_frontera_decisiones.py) without credentials or network.
    "GOOGLE_API_KEY": "test-google-key",
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    # opcionales que cambian comportamiento: valores deterministas para tests
    "BUSINESS_TIMEZONE": "America/Argentina/Buenos_Aires",
    "ERPNEXT_COMPANY": "Lacteos Test SA",
    "ERPNEXT_WAREHOUSE": "Principal - LT",
}
for _k, _v in _DUMMY.items():
    os.environ.setdefault(_k, _v)
