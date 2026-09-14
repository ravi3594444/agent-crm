"""The manual decision path must stay out of the LLM's reach.

THE BOUNDARY THIS PROTECTS
  Customer Sales Agent  — creates DRAFT orders. Cannot submit anything.
  AI Management Agent   — explains, reports, notifies. Cannot decide.
  Human Manager         — the only role that confirms or rejects by hand,
                          through the signed webhook after es_equipo().

The automatic path is deliberately NOT human-gated: app/policy.py decides
deterministically and app/tools/pedidos.py submits with the policy credential
when every rule passes. These tests assert both halves — that a qualifying
order still auto-confirms with nobody involved, and that no LLM tool can reach
the manual override.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

os.environ.setdefault("ERPNEXT_URL", "http://erpnext.test")
os.environ.setdefault("ERPNEXT_API_KEY", "test-key")
os.environ.setdefault("ERPNEXT_API_SECRET", "test-secret")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test-phone-id")
os.environ.setdefault("WHATSAPP_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import aprobacion, decisiones
from app.graph import TOOLS_CLIENTES, TOOLS_GERENCIA


@pytest.fixture(autouse=True)
def _el_equipo_de_verdad(monkeypatch: pytest.MonkeyPatch):
    """Los teléfonos que estos tests usan de encargado, en la lista DE VERDAD.

    `es_equipo` se comprueba en DOS puertas —`aprobacion.manejar_boton` y
    `decisiones.confirmar`— y cada módulo tiene su propia referencia a la
    función, así que `monkeypatch.setattr(aprobacion, "es_equipo", ...)` tapa
    una sola. Tampoco debería alcanzar: un test que DESACTIVA la autorización
    en una puerta no puede discreparle al código sobre la otra, que es la forma
    exacta del parche inerte que este repo ya se sacó de encima una vez
    (tests/test_frontera_decisiones.py).

    Se parchea `router.STAFF`, que es lo único que las dos puertas leen, y con
    eso corre el `es_equipo` real en las dos. Los tests que afirman un RECHAZO
    siguen parcheando lo suyo y siguen rechazando: un número que no está en
    esta lista no confirma nada.
    """
    from app import router

    monkeypatch.setattr(router, "STAFF", ["5493511111111"])


MANUAL = ("confirmar", "rechazar", "confirmar_pedido", "confirmar_conteo", "preparar", "despachar", "cancelar")


def test_no_llm_tool_exposes_the_manual_decision_functions() -> None:
    """If someone ever registers decisiones.* as a tool, this fails.

    A registered tool is callable by the model, and a model is steerable by the
    customer's own words. Manual override must never be reachable that way.
    """
    for lista, etiqueta in ((TOOLS_CLIENTES, "clientes"), (TOOLS_GERENCIA, "gerencia")):
        nombres = {t.name for t in lista}
        for prohibido in MANUAL:
            assert prohibido not in nombres, (
                f"{prohibido} quedó expuesto como herramienta del agente {etiqueta}"
            )


def test_no_tool_function_is_the_manual_decision_function() -> None:
    """Name-independent: compare the underlying callables, not the labels."""
    prohibidas = {
        decisiones.confirmar,
        decisiones.rechazar,
        decisiones.confirmar_conteo,
        decisiones.preparar,
        decisiones.despachar,
        decisiones.cancelar,
        aprobacion.confirmar_pedido,
    }
    for lista in (TOOLS_CLIENTES, TOOLS_GERENCIA):
        for herramienta in lista:
            fn = getattr(herramienta, "func", None) or getattr(
                herramienta, "coroutine", None
            )
            assert fn not in prohibidas, f"{herramienta.name} envuelve una decisión manual"


def test_a_customer_is_never_offered_the_tools_that_move_a_limit() -> None:
    """The limits decide which orders confirm with nobody watching. A customer
    talking to the sales agent must not be able to read them, let alone move
    one — not even by asking nicely, because the tool is not there."""
    from app import graph

    de_limites = {"ver_limites", "proponer_limite", "historial_limites"}
    de_clientes = {t.name for t in graph.TOOLS_CLIENTES}
    de_gerencia = {t.name for t in graph.TOOLS_GERENCIA}

    assert de_limites & de_clientes == set()
    # And they ARE available to the owner, or the feature does not exist.
    assert de_limites <= de_gerencia


def test_no_agent_can_take_both_halves_of_a_settings_confirmation() -> None:
    """Two steps are only two if different actors take them.

    The management agent proposes a change. It never receives the four-digit
    code — Python sends that straight to the owner's own number — and there is
    no tool that applies one, for EITHER agent. If an agent could call both
    halves, a misread instruction (or one hidden in a message it was asked to
    summarise) would move a limit in a single turn with nobody involved.
    """
    from app import graph
    from app.tools import configuracion

    for lista in (graph.TOOLS_CLIENTES, graph.TOOLS_GERENCIA):
        for herramienta in lista:
            nombre = herramienta.name.lower()
            assert nombre != "confirmar_limite"
            assert not ("confirm" in nombre and "limite" in nombre), nombre
    # Not merely unregistered: the tool does not exist to be registered.
    assert not hasattr(configuracion, "confirmar_limite")
    # And the only thing that applies one is the deterministic router.
    from app import main

    assert callable(main._codigo_de_ajuste)


def test_no_tool_can_submit_or_adjust_anything() -> None:
    """The LLM has no submit/payment/invoice/stock-adjustment verb at all."""
    prohibidos = ("submit", "confirmar_pedido", "aprobar", "pagar", "ajustar_stock", "despachar", "preparar", "cancelar")
    for lista in (TOOLS_CLIENTES, TOOLS_GERENCIA):
        for herramienta in lista:
            for palabra in prohibidos:
                assert palabra not in herramienta.name.lower(), herramienta.name


@pytest.mark.parametrize("accion", ["ok", "no", "ver", "preparar", "despachar", "cancelar"])
def test_unauthorized_phone_cannot_confirm_reject_or_read(
    monkeypatch: pytest.MonkeyPatch, accion: str
) -> None:
    monkeypatch.setattr(aprobacion, "es_equipo", lambda phone: False)
    confirmar = Mock()
    rechazar = Mock()
    monkeypatch.setattr(aprobacion, "confirmar_pedido", confirmar)
    monkeypatch.setattr(decisiones, "rechazar", rechazar)

    result = aprobacion.manejar_boton(f"{accion}:SAL-ORD-0001", "5490000000000")

    assert "permiso" in result
    confirmar.assert_not_called()
    rechazar.assert_not_called()


def test_manual_confirmation_uses_the_policy_credential_not_the_agent_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submitting is only ever erpnext.submit_doc, which is the policy client.

    LO QUE ESTE TEST NO PRUEBA, dicho acá porque parecía que sí: la
    autorización. Acá hubo un `monkeypatch` de `aprobacion.es_equipo` a True que
    este camino NUNCA consultaba —el test pasaba idéntico con la función
    devolviendo False—, así que se leía como cobertura y no lo era. Se sacó.

    Lo que esa nota anunciaba ya pasó: `decisiones.confirmar` SÍ verifica quién
    llama, porque dejó de ser un alias de `confirmar_pedido` y pasó a ser la
    puerta —autorización, bifurcación por solicitud abierta, cierre de la
    revisión y lock, todo adentro—. Quién prueba eso es
    `test_confirmar_le_dice_que_no_a_un_telefono_que_no_es_del_equipo` en
    tests/test_solicitudes.py, con un número que no está en `router.STAFF` en
    vez de una función parcheada. Acá el teléfono ES del equipo (ver
    `_el_equipo_de_verdad`), y lo único que se afirma es la credencial del
    Submit.
    """
    monkeypatch.setattr(
        aprobacion, "_leer_doc", lambda dt, name: {"name": name, "docstatus": 0}
    )
    submit = Mock(return_value={"name": "SAL-ORD-0001", "docstatus": 1})
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", submit)
    monkeypatch.setattr(aprobacion.erpnext, "add_comment", Mock())
    monkeypatch.setattr(aprobacion.avisos, "confirmacion_cliente", lambda so: True)
    monkeypatch.setattr(aprobacion.confirmacion, "registrar", lambda *a, **k: True)
    # La puerta lee el estado de la solicitud ANTES de emitir, y con el lector
    # estricto una lectura que no contesta ya no se lee como «no hay». Sin esta
    # línea el test pasaba a no emitir nada — y el Submit, que es lo único que
    # afirma, no llegaba a ocurrir.
    monkeypatch.setattr(aprobacion.solicitudes, "leer_estricto", lambda nombre: None)

    resultado = decisiones.confirmar(
        "SAL-ORD-0001", "5493511111111", canal=decisiones.CANAL_WHATSAPP
    )

    assert resultado["ok"] is True
    submit.assert_called_once_with("Sales Order", "SAL-ORD-0001")


def test_confirmar_adentro_del_lock_de_acciones_no_se_bloquea_contra_si_mismo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El anidamiento, contra el Redis de VERDAD y no contra los nombres.

    `acciones.py` envuelve TODO `manejar_boton` en `accion:{pedido}` para el
    camino autorizado por código de seis dígitos, y `decisiones.confirmar` toma
    ahora un lock propio adentro. `locks.distributed_lock` arma el lock de redis
    con `thread_local=False`: **no es reentrante**. Si los dos se llamaran
    igual, confirmar con código esperaría 10 s por un lock que ya tiene el mismo
    hilo, levantaría `CoordinationError`, y al encargado que acaba de tipear su
    código le contestaría «no pude coordinar» — o sea que la vía autorizada por
    código dejaría de confirmar, entera.

    Ese peligro no lo ve una afirmación sobre NOMBRES: el doble de locks de
    `test_solicitudes.py` registra nombres y nunca se bloquea, así que ahí los
    dos se pueden llamar igual y el test pasa igual. Acá el lock de afuera se
    toma de verdad, sobre el `REDIS_URL` que la suite ya exige, y lo que se
    afirma es que la confirmación de adentro SALE.
    """
    from app import locks

    pedido = "SAL-ORD-ANIDADO-1"
    monkeypatch.setattr(
        aprobacion, "_leer_doc", lambda dt, name: {"name": name, "docstatus": 0}
    )
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", Mock())
    monkeypatch.setattr(aprobacion.erpnext, "add_comment", Mock())
    monkeypatch.setattr(aprobacion.avisos, "confirmacion_cliente", lambda so: True)
    monkeypatch.setattr(aprobacion.confirmacion, "registrar", lambda *a, **k: True)
    monkeypatch.setattr(decisiones, "cerrar_revision_si_hay", lambda *a, **k: True)
    # `leer_estricto` y no `leer`: la puerta lee ahora con el que DISTINGUE «no
    # hay solicitud» de «no pude leer», porque confundirlos emite el borrador
    # con ERPNext caído (hallazgo 1 de la review de #45). La premisa de este
    # test es la primera: este pedido no tiene solicitud abierta.
    monkeypatch.setattr(aprobacion.solicitudes, "leer_estricto", lambda nombre: None)

    try:
        with locks.distributed_lock(pedido_lock := f"accion:{pedido}", lease_seconds=30, wait_seconds=2):
            assert pedido_lock  # el de afuera está tomado mientras corre lo de adentro
            resultado = decisiones.confirmar(
                pedido, "5493511111111", canal=decisiones.CANAL_WHATSAPP
            )
    except locks.CoordinationError:
        pytest.fail("no se pudo tomar el lock de afuera; ¿hay un Redis en REDIS_URL?")

    assert resultado["ok"] is True, resultado["detalle"]
    assert "coordinar" not in resultado["detalle"]


def test_confirmar_con_solicitud_abierta_no_se_traba_contra_su_propio_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La rama de solicitud abierta corre FUERA del lock del pedido.

    `decisiones.confirmar` mete ahora la lectura y el submit adentro de
    `solicitud:{pedido}`, que es lo que cierra la ventana del hallazgo 2. Pero
    los dos que se llaman DESPUÉS de decidir —`aprobar_solicitud`, que deriva la
    contraoferta, y `cerrar_revision_si_hay`— vuelven a tomar ese mismo lock, y
    `locks.distributed_lock` lo arma con `thread_local=False`: no es reentrante.
    Llamarlos desde adentro del `with` los cuelga 10 s contra un lock que tiene
    este mismo hilo y termina en `CoordinationError`: la rama de solicitud
    abierta —la que evita cobrarle al cliente términos que no aceptó— dejaría de
    funcionar entera, y el arreglo del hallazgo 2 habría roto el hallazgo 2.

    El doble de `aprobar_solicitud` toma el lock DE VERDAD y no finge nada más:
    lo único que tiene que representar es «el que sigue vuelve a pedir este
    lock». Si la puerta todavía lo tuviera tomado, este test espera los 10 s
    reales y se pone en rojo; el doble de locks de test_solicitudes.py no puede
    verlo, porque anota nombres y no bloquea nunca.
    """
    from app import locks, solicitudes

    pedido = "SAL-ORD-DERIVA-1"
    abierta = Mock(abierta=True)
    monkeypatch.setattr(solicitudes, "leer_estricto", lambda nombre: abierta)

    def aprobar(nombre, por):
        with locks.distributed_lock(
            f"solicitud:{nombre}", lease_seconds=30, wait_seconds=10
        ):
            return {"ok": True, "aviso_cliente": False, "detalle": "derivada"}

    monkeypatch.setattr(decisiones, "aprobar_solicitud", aprobar)

    resultado = decisiones.confirmar(
        pedido, "5493511111111", canal=decisiones.CANAL_WHATSAPP
    )

    assert resultado["detalle"] == "derivada"
    assert "coordinar" not in resultado["detalle"]


def test_con_el_lock_del_pedido_tomado_afuera_no_se_emite_nada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La comprobación y el submit están DENTRO de `solicitud:{pedido}`.

    Es la afirmación del hallazgo 2 hecha desde afuera: si alguien más tiene el
    lock del pedido —`solicitudes.crear` abriendo una excepción del cliente, el
    barrido venciéndola, `_decidir` registrando una contraoferta—, confirmar no
    puede pasar por el medio. Con el lock tomado, `decisiones.confirmar` no
    emite NADA y lo dice.

    Las dos mitades, y la segunda es la que importa: que conteste «no pude
    coordinar» lo cumpliría igual un código que contesta eso DESPUÉS de emitir.
    Que `submit_doc` no se haya llamado es lo que prueba que el lock está antes
    del submit y no al lado.

    La mutación que mata a este test y no a otro: sacar el `with
    distributed_lock(f"solicitud:{nombre}")` de `_confirmar_autorizado` dejando
    todo lo demás igual. El de los nombres en test_solicitudes.py sigue verde
    salvo por el orden; éste se pone rojo porque el submit ocurre.
    """
    from app import locks, solicitudes

    pedido = "SAL-ORD-TOMADO-1"
    submit = Mock()
    monkeypatch.setattr(
        aprobacion, "_leer_doc", lambda dt, name: {"name": name, "docstatus": 0}
    )
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", submit)
    monkeypatch.setattr(aprobacion.erpnext, "add_comment", Mock())
    monkeypatch.setattr(aprobacion.avisos, "confirmacion_cliente", lambda so: True)
    monkeypatch.setattr(aprobacion.confirmacion, "registrar", lambda *a, **k: True)
    monkeypatch.setattr(solicitudes, "leer_estricto", lambda nombre: None)

    with locks.distributed_lock(
        f"solicitud:{pedido}", lease_seconds=30, wait_seconds=2
    ):
        resultado = decisiones.confirmar(
            pedido, "5493511111111", canal=decisiones.CANAL_WHATSAPP
        )

    assert resultado["ok"] is False
    submit.assert_not_called()


def test_el_submit_corre_con_el_lock_del_pedido_TOMADO(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El submit pasa con `solicitud:{pedido}` en la mano, y se mide desde adentro.

    ESTE TEST EXISTE PORQUE EL DE ARRIBA NO ALCANZABA.
    `test_con_el_lock_del_pedido_tomado_afuera_no_se_emite_nada` toma el lock
    desde afuera y comprueba que no se emite nada, y eso lo cumple igual un
    código que tiene ADENTRO del `with` solamente la lectura de la solicitud:
    la puerta choca contra el lock ajeno al entrar, contesta «no pude coordinar»
    y nunca llega al submit, así que el test queda verde tenga el submit adentro
    o afuera. Medido: sacar `emitir(...)` del `with` dejaba los 2909 tests en
    verde. Es el mismo defecto que CLAUDE.md describe —un test estructuralmente
    incapaz de fallar sobre lo que dice cubrir— y es la mitad de la sección
    crítica que más caro sale, porque es la irreversible.

    La única forma de afirmarlo es preguntando DESDE ADENTRO: en el momento del
    submit, pedir el mismo lock tiene que fallar. `locks.distributed_lock` lo
    arma con `thread_local=False` y no es reentrante, así que si este hilo ya lo
    tiene, un segundo intento sin espera levanta `CoordinationError`. Si el lock
    estuviera suelto, el intento tendría éxito — que es exactamente el estado
    que le deja la puerta abierta a `solicitudes.crear`.

    La mutación que lo mata y no mata a otro: sacar `emitir(nombre)` afuera del
    `with distributed_lock(f"solicitud:{nombre}")` de `_confirmar_autorizado`.
    """
    from app import locks, solicitudes

    pedido = "SAL-ORD-ADENTRO-1"
    tomado: dict[str, bool] = {}

    def submit(doctype, name):
        try:
            with locks.distributed_lock(
                f"solicitud:{name}", lease_seconds=5, wait_seconds=0
            ):
                tomado["pedido"] = False
        except locks.CoordinationError:
            tomado["pedido"] = True

    monkeypatch.setattr(
        aprobacion, "_leer_doc", lambda dt, name: {"name": name, "docstatus": 0}
    )
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", submit)
    monkeypatch.setattr(aprobacion.erpnext, "add_comment", Mock())
    monkeypatch.setattr(aprobacion.avisos, "confirmacion_cliente", lambda so: True)
    monkeypatch.setattr(aprobacion.confirmacion, "registrar", lambda *a, **k: True)
    monkeypatch.setattr(aprobacion, "_notificar_confirmada", lambda *a, **k: None)
    monkeypatch.setattr(solicitudes, "leer_estricto", lambda nombre: None)
    monkeypatch.setattr(decisiones, "cerrar_revision_si_hay", lambda *a, **k: None)

    resultado = decisiones.confirmar(
        pedido, "5493511111111", canal=decisiones.CANAL_WHATSAPP
    )

    assert resultado["ok"] is True
    assert tomado["pedido"] is True


def test_el_aviso_a_la_persona_sale_con_el_lock_del_pedido_ya_suelto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El anuncio corre AFUERA de `solicitud:{pedido}`, y es la otra mitad.

    El test de acá arriba prueba que el submit está ADENTRO del lock. Éste
    prueba lo contrario sobre la otra mitad de la misma función, y hacen falta
    los dos: son dos consumidores de la misma decisión —dónde corta la sección
    crítica— y una sola mutación no puede matarlos a los dos.

    Por qué importa que esté afuera: el lease son 180 s y adentro del `with`
    había once llamadas a ERPNext (20 y 30 s de timeout) más un POST a Meta de
    15 s, que suman 315. Al vencer el lease, `solicitudes.crear` toma la llave
    libre, relee el borrador todavía en `docstatus=0` y le abre una solicitud a
    un pedido que está por emitirse: la carrera que esta puerta existe para
    cerrar, reabierta por el reloj. Es además la regla que el repo ya escribió
    tres veces —`solicitudes._vencer`, `agenda._despachar` y la baja de
    `agenda`—: el teléfono de nadie va adentro de un lock.

    El doble toma el lock DE VERDAD y no finge nada más, igual que el de la
    rama de solicitud abierta: lo único que tiene que representar es «esto le
    habla a una persona, y mientras tanto el pedido tiene que estar libre». Un
    doble que sólo anotara que lo llamaron no podría discrepar con el código
    sobre lo único que este test afirma.

    La mutación que lo mata y no mata a otro: mover `anunciar(...)` adentro del
    `with distributed_lock(f"solicitud:{nombre}")` de `_confirmar_autorizado`.
    El de arriba sigue verde porque el submit sigue adentro.
    """
    from app import locks, solicitudes

    pedido = "SAL-ORD-SUELTO-1"
    libre: dict[str, bool] = {}

    def avisar(nombre, conocido, *, ventana=True):
        try:
            with locks.distributed_lock(
                f"solicitud:{nombre}", lease_seconds=5, wait_seconds=0
            ):
                libre["pedido"] = True
        except locks.CoordinationError:
            libre["pedido"] = False

    monkeypatch.setattr(
        aprobacion, "_leer_doc", lambda dt, name: {"name": name, "docstatus": 0}
    )
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", Mock())
    monkeypatch.setattr(aprobacion.erpnext, "add_comment", Mock())
    monkeypatch.setattr(aprobacion.avisos, "confirmacion_cliente", lambda so: True)
    monkeypatch.setattr(aprobacion.confirmacion, "registrar", lambda *a, **k: True)
    monkeypatch.setattr(aprobacion, "_notificar_confirmada", avisar)
    monkeypatch.setattr(solicitudes, "leer_estricto", lambda nombre: None)
    monkeypatch.setattr(decisiones, "cerrar_revision_si_hay", lambda *a, **k: None)

    resultado = decisiones.confirmar(
        pedido, "5493511111111", canal=decisiones.CANAL_WHATSAPP
    )

    assert resultado["ok"] is True
    assert libre["pedido"] is True


def test_aprobar_una_contraoferta_no_dice_que_el_pedido_quedo_emitido(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ok` y `emitido` son dos hechos, y con una solicitud abierta no coinciden.

    Con una contraoferta esperando, la puerta NO emite: aprueba la solicitud y
    le manda los términos nuevos al cliente. Eso sale bien, así que `ok` es
    True —la acción que el encargado pidió se hizo— y el Sales Order sigue
    siendo un BORRADOR reservando stock, que el cliente todavía puede rechazar.
    El panel leía `ok` y pintaba la fila como confirmada: una venta cerrada que
    no existe, sobre el único endpoint del sistema que mueve plata.

    Las DOS mitades, y la segunda es la que atrapa el bug: que `emitido` sea
    False acá lo cumpliría igual un código que lo devuelve False SIEMPRE, y
    entonces el panel no podría pintar ninguna confirmación. Por eso se afirma
    también el caso en que sí se emite.

    Mutación dirigida: cambiar `_con_emitido(aprobar_solicitud(...), False)` por
    `True` en `_confirmar_autorizado`. Mata a este test y a ningún otro.
    """
    from app import locks, solicitudes

    pedido = "SAL-ORD-OFERTA-1"
    abierta = Mock(abierta=True)
    monkeypatch.setattr(solicitudes, "leer_estricto", lambda nombre: abierta)

    def aprobar(nombre, por):
        with locks.distributed_lock(
            f"solicitud:{nombre}", lease_seconds=30, wait_seconds=10
        ):
            return {"ok": True, "aviso_cliente": True, "detalle": "derivada"}

    monkeypatch.setattr(decisiones, "aprobar_solicitud", aprobar)

    derivado = decisiones.confirmar(
        pedido, "5493511111111", canal=decisiones.CANAL_WHATSAPP
    )

    assert derivado["ok"] is True
    assert derivado["emitido"] is False

    # Y la otra mitad: sin solicitud abierta, el mismo camino SÍ emite.
    monkeypatch.setattr(solicitudes, "leer_estricto", lambda nombre: None)
    monkeypatch.setattr(
        aprobacion, "_leer_doc", lambda dt, name: {"name": name, "docstatus": 0}
    )
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", Mock())
    monkeypatch.setattr(aprobacion.erpnext, "add_comment", Mock())
    monkeypatch.setattr(aprobacion.avisos, "confirmacion_cliente", lambda so: True)
    monkeypatch.setattr(aprobacion.confirmacion, "registrar", lambda *a, **k: True)
    monkeypatch.setattr(aprobacion, "_notificar_confirmada", lambda *a, **k: None)
    monkeypatch.setattr(decisiones, "cerrar_revision_si_hay", lambda *a, **k: None)

    emitido = decisiones.confirmar(
        pedido, "5493511111111", canal=decisiones.CANAL_WHATSAPP
    )

    assert emitido["ok"] is True
    assert emitido["emitido"] is True


def test_un_estado_que_no_se_puede_confirmar_tampoco_dice_emitido(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El rechazo del mecanismo viaja con `emitido` False, no sin la clave.

    `confirmar` tiene cinco salidas —sin permiso, sin coordinación, estado
    incierto, la contraoferta, el rechazo del mecanismo— y el panel lee la misma
    clave en todas. Una salida que se olvidara de ponerla haría que `.get()`
    contestara None, que es falsy y por eso pasa desapercibido hasta que alguna
    vez signifique otra cosa. Se afirma que la clave ESTÁ y que es False.
    """
    from app import solicitudes

    pedido = "SAL-ORD-CANCELADO-1"
    monkeypatch.setattr(solicitudes, "leer_estricto", lambda nombre: None)
    monkeypatch.setattr(
        aprobacion, "_leer_doc", lambda dt, name: {"name": name, "docstatus": 2}
    )
    submit = Mock()
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", submit)

    resultado = decisiones.confirmar(
        pedido, "5493511111111", canal=decisiones.CANAL_WHATSAPP
    )

    assert resultado["ok"] is False
    assert resultado["emitido"] is False
    submit.assert_not_called()


def test_un_erpnext_que_no_contesta_deja_el_pedido_en_ESTADO_INCIERTO(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«No pude leer» no es «no se emitió», y son dos respuestas distintas.

    `emitir` atrapa `ERPNextError` y contesta un rechazo cuyo texto siempre dijo
    «no pude comprobar la confirmación». Lo que este PR le agregó fue un
    booleano —`emitido`— que sí afirmaba: False. Y el caso en que más duele es
    el que el propio mecanismo documenta, que un Submit puede commitear después
    de que el cliente HTTP se dio por vencido: ahí se emite un `submit_doc`, se
    intenta releer para verificarlo, la relectura tampoco contesta, y el pedido
    puede quedar confirmado en ERPNext mientras el panel dibuja `submitted:
    false`. Un encargado que mira eso confirma de nuevo, o peor, lo da por
    perdido.

    Las DOS mitades, y son dos consumidores del mismo rechazo: el estado LEÍDO
    («está cancelado») sigue siendo un False firme, y sólo la lectura que no
    contestó es `None`. Devolver `None` para todos los rechazos cumpliría la
    primera mitad y dejaría al panel sin poder pintar nunca un rechazo real.

    Mutación dirigida: `incierto=True` -> `incierto=False` en el `except
    ERPNextError` de `emitir`. Mata a este test y a ninguno otro.
    """
    from app import solicitudes

    pedido = "SAL-ORD-INCIERTO-1"
    monkeypatch.setattr(solicitudes, "leer_estricto", lambda nombre: None)

    def no_contesta(doctype, name):
        raise aprobacion.erpnext.ERPNextError("ERPNext no contesta")

    monkeypatch.setattr(aprobacion, "_leer_doc", no_contesta)

    incierto = decisiones.confirmar(
        pedido, "5493511111111", canal=decisiones.CANAL_WHATSAPP
    )

    assert incierto["ok"] is False
    assert incierto["emitido"] is None
    assert "no pude comprobar" in incierto["detalle"].lower()

    # La otra mitad: un estado que SÍ se leyó sigue siendo un False firme.
    monkeypatch.setattr(
        aprobacion, "_leer_doc", lambda dt, name: {"name": name, "docstatus": 2}
    )

    cancelado = decisiones.confirmar(
        pedido, "5493511111111", canal=decisiones.CANAL_WHATSAPP
    )

    assert cancelado["ok"] is False
    assert cancelado["emitido"] is False


def test_el_rastro_en_erpnext_nombra_el_canal_por_el_que_se_confirmo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lo que queda firmado en el pedido dice por dónde entró la persona.

    El historial del pedido y la marca durable decían «mediante WhatsApp» como
    literal, de cuando WhatsApp era el único camino. El panel es el segundo, y
    un rastro de auditoría que nombra un canal por el que nadie pasó es peor que
    no tenerlo: alguien que audite una confirmación discutida va a buscar el
    mensaje de WhatsApp que la respalda y no existe.

    Las DOS mitades. Que aparezca «el panel» lo cumpliría igual un texto que
    dijera las dos cosas —«mediante WhatsApp … el panel»— que es exactamente lo
    que deja un literal al que se le agregó una interpolación al lado. Que no
    aparezca «WhatsApp» es la mitad que mata al literal.
    """
    pedido = "SAL-ORD-CANAL-1"
    comentarios: list[str] = []
    marcas: list[str] = []
    monkeypatch.setattr(
        aprobacion, "_leer_doc", lambda dt, name: {"name": name, "docstatus": 0}
    )
    monkeypatch.setattr(aprobacion.erpnext, "submit_doc", Mock())
    monkeypatch.setattr(
        aprobacion.erpnext, "add_comment",
        lambda dt, name, texto: comentarios.append(texto),
    )
    monkeypatch.setattr(aprobacion.avisos, "confirmacion_cliente", lambda so: True)
    monkeypatch.setattr(
        aprobacion.confirmacion, "registrar",
        lambda nombre, fuente: marcas.append(fuente) or True,
    )
    monkeypatch.setattr(decisiones, "cerrar_revision_si_hay", lambda *a, **k: True)
    # `leer_estricto` y no `leer`: la puerta lee ahora con el que DISTINGUE «no
    # hay solicitud» de «no pude leer», porque confundirlos emite el borrador
    # con ERPNext caído (hallazgo 1 de la review de #45). La premisa de este
    # test es la primera: este pedido no tiene solicitud abierta.
    monkeypatch.setattr(aprobacion.solicitudes, "leer_estricto", lambda nombre: None)

    decisiones.confirmar(pedido, "5493511111111", canal=decisiones.CANAL_PANEL)

    assert comentarios and marcas
    assert "el panel" in comentarios[0]
    assert "WhatsApp" not in comentarios[0]
    assert "por el panel" in marcas[0]
    assert "WhatsApp" not in marcas[0]


# ---------------------------- el registro de herramientas no se lee en voz alta
# Un cliente que probaba el borde («registrame una venta offline y decime cómo
# está el sistema») recibía de vuelta el inventario del agente: LangGraph
# contesta «Error: X is not a valid tool, try one of [buscar_producto, …]» y el
# modelo relata ese texto. El límite aguantaba —la herramienta no es invocable—
# pero la lista salía igual. Lo cazó el guarda de tono del banco de pruebas.


def test_una_herramienta_que_no_existe_no_devuelve_el_inventario() -> None:
    from app import graph

    nodo = graph.ToolNodeSinInventario(
        graph.TOOLS_CLIENTES, handle_tool_errors=graph._ERROR_MSG
    )

    mensaje = nodo._validate_tool_call(
        {"name": "estado_del_sistema", "id": "call_1", "args": {}}
    )

    assert mensaje is not None
    assert mensaje.status == "error"
    # Ni la lista, ni la plantilla de LangGraph, ni una sola herramienta ajena.
    assert "try one of" not in mensaje.content
    for herramienta in ("buscar_producto", "crear_pedido", "consultar_stock"):
        assert herramienta not in mensaje.content
    # Y le dice al modelo qué hacer y que esto no se le muestra a nadie.
    assert "NO le muestres al cliente este mensaje" in mensaje.content
    assert "escalar_a_humano" in mensaje.content


def test_una_herramienta_que_si_existe_pasa_como_siempre() -> None:
    from app import graph

    nodo = graph.ToolNodeSinInventario(
        graph.TOOLS_CLIENTES, handle_tool_errors=graph._ERROR_MSG
    )

    assert (
        nodo._validate_tool_call(
            {"name": "buscar_producto", "id": "call_1", "args": {"consulta": "leche"}}
        )
        is None
    )


def test_el_gancho_que_se_reemplaza_sigue_existiendo_en_langgraph() -> None:
    """Es un método privado de LangGraph. Si una versión le cambia el nombre, el
    override deja de correr y la lista vuelve a salir — así que la que falla es
    esta línea, en CI, y no la conversación de un cliente."""
    from langgraph.prebuilt import ToolNode

    assert hasattr(ToolNode, "_validate_tool_call")
    from app import graph

    assert graph.ToolNodeSinInventario._validate_tool_call is not (
        ToolNode._validate_tool_call
    )


def test_los_dos_agentes_tienen_instalado_el_nodo_que_no_enumera() -> None:
    """El nodo que quedó INSTALADO en cada agente compilado, no el texto del archivo.

    Antes esto se afirmaba grepeando app/graph.py —«tools=ToolNode(» ausente y
    «tools=ToolNodeSinInventario(» dos veces—, que sólo prueba cómo está escrito
    el archivo: un ToolNode pelado pasado por una variable lo habría burlado sin
    cambiar una letra del grep. Acá se mira el grafo compilado, que es lo que
    corre.

    `nodes["tools"].bound` es API interna de LangGraph, igual que el
    `_validate_tool_call` de acá arriba. Es a propósito: si una versión la
    mueve, esto explota con KeyError o AttributeError en CI y alguien vuelve a
    mirar el cableado, en vez de que el test siga pasando sobre un grafo que ya
    no es el que se afirma.
    """
    from app import graph

    for nombre, registro in (
        ("agente_clientes", graph.TOOLS_CLIENTES),
        ("agente_gerencia", graph.TOOLS_GERENCIA),
    ):
        instalado = getattr(graph, nombre).nodes["tools"].bound
        # isinstance contra la SUBCLASE: un ToolNode pelado no la satisface.
        assert isinstance(instalado, graph.ToolNodeSinInventario), (
            f"{nombre}: quedó instalado {type(instalado).__name__}"
        )
        # Y con SU registro. Los dos nodos son ToolNodeSinInventario, así que la
        # clase sola no dice cuál quedó dónde: montar TOOLS_GERENCIA en el
        # agente de clientes es una fuga de privilegios, no un detalle de
        # cableado, y pasaba tanto el grep viejo como el isinstance de arriba.
        assert set(instalado.tools_by_name) == {h.name for h in registro}, (
            f"{nombre}: quedó con el registro del otro agente"
        )

