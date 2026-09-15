"""El sha del relay está en dos archivos y tiene que ser el mismo.

`requirements-voz.txt` lo instala con pip y `Dockerfile.voz` lo clona para
quedarse con `static/`. Si se separan, la imagen sirve una interfaz de una
versión y un relay de otra — y el síntoma es una página que carga y un botón
que no hace nada, que es de lo peor que hay para diagnosticar.
"""
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
SHA = re.compile(r"[0-9a-f]{40}")


def _sha(archivo: str) -> str:
    encontrados = SHA.findall((RAIZ / archivo).read_text())
    assert encontrados, f"{archivo} no tiene un sha de 40 caracteres"
    assert len(set(encontrados)) == 1, f"{archivo} nombra varios shas: {set(encontrados)}"
    return encontrados[0]


def test_el_pin_del_relay_es_el_mismo_en_los_dos_lados():
    assert _sha("requirements-voz.txt") == _sha("Dockerfile.voz")
