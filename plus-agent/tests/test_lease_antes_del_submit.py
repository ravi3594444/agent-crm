"""Nadie emite un pedido sin poder probar que todavía tiene el lock.

EL DEFECTO QUE ESTO FIJA
------------------------
Un lease vence solo y nadie lo renueva —no hay un `extend()` en este repo—. En
`aprobacion.emitir` hay hasta cuatro llamadas a ERPNext con `timeout` de 20 y
30 s adentro de un lease de 180, y en `solicitudes.aceptar_cliente` hay una
relectura, `revalidar`, la escritura de los términos, `policy.evaluar` entero y
una relectura durable. Cuando el lease se vence a mitad de camino la llamada NO
falla: sigue caminando hacia el Submit sin exclusión mutua, mientras otro worker
toma la llave libre y decide otra cosa sobre el mismo pedido — la carrera exacta
que `decisiones.confirmar` existe para cerrar, reabierta por el reloj.

`locks.distributed_lock` hacía `yield None`, así que la pregunta «¿lo sigo
teniendo?» era literalmente imposible de hacer. Los dos lugares que emiten lo
decían en un comentario.

POR QUÉ `lease` ES OBLIGATORIO Y NO UN PARÁMETRO CON DEFAULT
------------------------------------------------------------
Un default convierte «me olvidé de pasarlo» en «corrió sin la comprobación», en
silencio y sobre el único Submit del sistema. El test de abajo lo fija: `emitir`
sin lease no compila la llamada.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes import LeaseDoble

from app import aprobacion, erpnext

pytestmark = pytest.mark.idioma("es")

SO = "SAL-ORD-2026-00099"


@pytest.fixture
def borrador(monkeypatch):
    """Un Sales Order en borrador, y la lista de lo que se emitió."""
    emitidos: list[str] = []
    doc = {"name": SO, "docstatus": 0, "status": "To Deliver and Bill"}

    monkeypatch.setattr(aprobacion, "_leer_doc", lambda dt, name: dict(doc))

    def submit(dt, name):
        emitidos.append(name)
        doc["docstatus"] = 1
        return dict(doc)

    monkeypatch.setattr(erpnext, "submit_doc", submit)
    return emitidos


def test_sin_el_lease_no_se_emite_nada(borrador):
    """El lease se venció entre la lectura y el Submit."""
    lease = LeaseDoble(vigente=False)

    emision = aprobacion.emitir(SO, lease=lease)

    assert borrador == [], "emitió sin tener el lock"
    assert emision.rechazo is not None
    # NO es «incierto»: no se mandó nada, así que el pedido sigue siendo un
    # borrador conocido. Decir «no pude comprobar» acá mandaría al encargado a
    # mirar ERPNext por un pedido que está exactamente donde lo dejó.
    assert emision.incierto is False
    assert emision.rechazo["ok"] is False
    assert emision.rechazo["aviso_cliente"] is False
    assert lease.preguntas == 1


def test_con_el_lease_entero_se_emite_igual_que_siempre(borrador):
    """La otra mitad: la comprobación no puede frenar el camino normal."""
    lease = LeaseDoble()

    emision = aprobacion.emitir(SO, lease=lease)

    assert borrador == [SO]
    assert emision.rechazo is None
    assert lease.preguntas == 1


def test_un_pedido_ya_emitido_no_gasta_la_pregunta(borrador):
    """Si ya estaba en `docstatus=1` no se emite nada, así que no hay qué
    proteger: preguntar igual sería pagarle una lectura a Redis por un camino
    que no escribe."""
    monkeypatch_doc = {"name": SO, "docstatus": 1, "status": "To Deliver and Bill"}
    from unittest.mock import patch

    lease = LeaseDoble(vigente=False)
    with patch.object(aprobacion, "_leer_doc", lambda dt, name: dict(monkeypatch_doc)):
        emision = aprobacion.emitir(SO, lease=lease)

    assert borrador == []
    assert emision.rechazo is None
    assert emision.ya_estaba is True
    assert lease.preguntas == 0


def test_emitir_no_se_puede_llamar_sin_lease(borrador):
    """Un default habría dejado que un llamador futuro se olvidara la
    comprobación sin que nada se lo dijera. Es el único Submit del sistema."""
    with pytest.raises(TypeError):
        aprobacion.emitir(SO)  # type: ignore[call-arg]
    assert borrador == []
