"""El modo sombra dice lo mismo que la política, y no puede confirmar nada.

Dos propiedades, y son las únicas que importan:

1. ES EL MISMO CÓDIGO. Con las puertas de postura abiertas (tope > 0 y stock
   encendido), ``evaluar_sombra`` tiene que coincidir con ``evaluar`` pedido por
   pedido y motivo por motivo. Un registro de sombra que dijera «habría pasado»
   sobre un pedido que la política rechaza es peor que no tener registro: el
   dueño levantaría su tope con un número inventado.

2. NO SOMETE. La sombra corre justamente cuando la auto-confirmación está
   apagada. Si pudiera someter, sería la postura apagándose sola.

El resto del archivo son los casos que hacen que la sombra sirva de algo: con
tope 0 tiene que seguir leyendo y contestar «las reglas pasan, te frena la
postura», que es exactamente lo que ``evaluar`` no puede decir.
"""
from __future__ import annotations

import pytest
from conftest import inventario_confiable
from test_policy_reglas import _fijar, _order, green  # noqa: F401

from app import erpnext, policy

# --------------------------------------------------------- 1. el mismo código


def test_shadow_and_the_policy_agree_on_a_green_order(green) -> None:
    """La propiedad, en el caso base: si no coinciden acá, no coinciden nunca."""
    decision = policy.evaluar(_order())
    sombra = policy.evaluar_sombra(_order())

    assert decision.auto is True
    assert sombra.pasa_reglas is True
    assert sombra.motivos_reglas == decision.motivos == []
    assert sombra.motivos_postura == []


# Cada tupla es un giro de UNA sola regla, con el mock que hay que mover. Son
# los mismos giros de test_policy_reglas.py: si allá se agrega una regla y acá
# no, esta lista queda corta y la propiedad se prueba sobre menos casos — por
# eso el test de abajo exige que TODOS fallen de verdad.
GIROS = (
    ("stock", lambda mocks: mocks["stock"].configure_mock(return_value=False)),
    ("precio", lambda mocks: mocks["price"].configure_mock(return_value=False)),
    ("deuda", lambda mocks: mocks["debt"].configure_mock(return_value=5_000.0)),
    (
        "deuda ilegible",
        lambda mocks: mocks["debt"].configure_mock(return_value=None),
    ),
    (
        "historial corto",
        lambda mocks: mocks["history"].configure_mock(
            return_value=[{"grand_total": 100}]
        ),
    ),
    (
        "historial ilegible",
        lambda mocks: mocks["history"].configure_mock(
            side_effect=erpnext.ERPNextError("boom")
        ),
    ),
    (
        "muy por encima del promedio",
        lambda mocks: mocks["history"].configure_mock(
            return_value=[{"grand_total": 1}] * 3
        ),
    ),
)


@pytest.mark.parametrize("nombre,girar", GIROS, ids=[g[0] for g in GIROS])
def test_shadow_and_the_policy_agree_on_every_single_rule_flip(
    green, nombre: str, girar
) -> None:
    """Una regla movida por vez: los dos tienen que decir lo mismo, con los mismos motivos."""
    girar(green)

    decision = policy.evaluar(_order())
    sombra = policy.evaluar_sombra(_order())

    assert decision.auto is False, f"{nombre}: el giro no hizo fallar a la política"
    assert sombra.pasa_reglas == decision.auto
    assert sombra.motivos_reglas == decision.motivos
    # Las puertas de postura están abiertas en el fixture green, así que no hay
    # nada que apartar: todo motivo es de las reglas.
    assert sombra.motivos_postura == []


def test_the_order_of_the_reasons_is_the_same_in_both(green) -> None:
    """Mismo orden, no sólo mismo conjunto: el resumen agrupa por el primero."""
    green["stock"].return_value = False
    green["debt"].return_value = 5_000.0

    decision = policy.evaluar(_order())
    sombra = policy.evaluar_sombra(_order())

    assert len(decision.motivos) > 1
    assert sombra.motivos_reglas == decision.motivos


# ------------------------------------------------------------- 2. no somete


def test_shadow_never_submits_not_even_a_fully_green_order(
    green, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El caso peligroso: reglas perfectas, tope en 0. No se toca el documento."""
    _fijar(monkeypatch, AUTO_CONFIRM_MAX=0)

    def explota(*args, **kwargs):
        raise AssertionError("la sombra sometió un pedido")

    monkeypatch.setattr(erpnext, "submit_doc", explota)
    monkeypatch.setattr(
        erpnext, "policy_update_status", lambda *a, **k: explota()
    )

    pedido = _order()
    sombra = policy.evaluar_sombra(pedido)

    assert sombra.pasa_reglas is True  # habrían pasado todas las reglas
    assert pedido["docstatus"] == 0  # y sigue siendo un borrador


def test_shadow_takes_no_lock(green, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ni el lock de submit: la sombra no puede serializarse contra una venta."""
    monkeypatch.setattr(
        policy,
        "auto_submit_lock",
        lambda: (_ for _ in ()).throw(AssertionError("la sombra tomó el lock")),
    )

    assert policy.evaluar_sombra(_order()).pasa_reglas is True


# ------------------------------------------- 3. para qué sirve: el tope en 0


def test_with_the_ceiling_at_zero_the_policy_says_nothing_and_shadow_says_everything(
    green, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El hallazgo entero de W1, en un test.

    ``evaluar`` vuelve en su segunda línea con un motivo que es el mismo para
    todos los pedidos del día. ``evaluar_sombra`` corre las reglas y separa lo
    que frena de verdad de lo que frena por decisión del dueño.
    """
    _fijar(monkeypatch, AUTO_CONFIRM_MAX=0)

    decision = policy.evaluar(_order())
    assert decision.auto is False
    assert decision.motivos == ["auto-confirmación desactivada"]

    sombra = policy.evaluar_sombra(_order())
    assert sombra.pasa_reglas is True
    assert sombra.motivos_reglas == []
    assert sombra.motivos_postura == [policy.POSTURA_TOPE]
    assert sombra.tope_vigente == 0
    assert sombra.total == 100.0


def test_with_the_ceiling_at_zero_the_policy_reads_nothing_but_shadow_does(
    green, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La contracara del early return: es justamente lo que la sombra viene a pagar."""
    _fijar(monkeypatch, AUTO_CONFIRM_MAX=0)

    policy.evaluar(_order())
    green["history"].assert_not_called()
    green["debt"].assert_not_called()

    policy.evaluar_sombra(_order())
    assert green["history"].called
    assert green["debt"].called


def test_a_real_rule_failure_is_not_hidden_behind_the_posture(
    green, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tope 0 Y sin stock: la sombra tiene que decir las dos cosas, separadas."""
    _fijar(monkeypatch, AUTO_CONFIRM_MAX=0)
    green["stock"].return_value = False

    sombra = policy.evaluar_sombra(_order())

    assert sombra.pasa_reglas is False
    assert "stock insuficiente de LECHE-1L" in sombra.motivos_reglas
    assert sombra.motivos_postura == [policy.POSTURA_TOPE]


# ----------------------------- 4. la confianza por producto NO es una postura


def test_the_master_switch_is_posture_but_per_product_trust_is_not(
    green, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con el interruptor maestro apagado y nadie que haya contado, la sombra lo dice.

    Es el argumento para empezar a contar a la mañana: el resumen puede
    separar «te frena la postura» de «no lo contó nadie».
    """
    _fijar(monkeypatch, AUTO_CONFIRM_MAX=0)
    inventario_confiable(monkeypatch, maestra=False, fresco=False, motivo="nadie confirmó un conteo de LECHE-1L")

    sombra = policy.evaluar_sombra(_order())

    assert policy.POSTURA_STOCK in sombra.motivos_postura
    assert "nadie confirmó un conteo de LECHE-1L" in sombra.motivos_reglas
    assert sombra.pasa_reglas is False


def test_the_master_switch_off_alone_does_not_fail_the_rules(
    green, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apagado el interruptor pero con conteo fresco, las reglas pasan igual."""
    _fijar(monkeypatch, AUTO_CONFIRM_MAX=0)
    inventario_confiable(monkeypatch, maestra=False, fresco=True)

    sombra = policy.evaluar_sombra(_order())

    # Orden exacto, en el mismo que las aparta _evaluar: el stock primero
    # (policy.py:277), el tope después (policy.py:299).
    assert sombra.motivos_postura == [policy.POSTURA_STOCK, policy.POSTURA_TOPE]
    assert sombra.motivos_reglas == []
    assert sombra.pasa_reglas is True


# ------------------------------------------------------ 5. límites ilegibles


def test_unreadable_limits_judge_nothing(green, monkeypatch: pytest.MonkeyPatch) -> None:
    """Misma dirección que evaluar: sin límites verificados no se juzga el pedido."""
    from app import limites

    def explota() -> None:
        raise limites.LimiteError("no pude leer los límites configurados")

    monkeypatch.setattr(limites, "configuracion", explota)

    sombra = policy.evaluar_sombra(_order())

    assert sombra.pasa_reglas is False
    assert sombra.motivos_reglas == []
    # Y NO en motivos_postura: el informe rinde esa lista como «frenados sólo
    # por la postura que elegiste», así que una caída ahí le diría al dueño que
    # decidió algo que no decidió. Una falla nunca es una decisión.
    assert sombra.motivos_postura == []
    assert sombra.ilegible == (
        "límites sin verificar: no pude leer los límites configurados"
    )


def test_an_unreadable_total_still_produces_a_record(
    green, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El registro lleva un número siempre; la regla ya rechazó el pedido."""
    _fijar(monkeypatch, AUTO_CONFIRM_MAX=0)

    sombra = policy.evaluar_sombra(_order(grand_total="ocho mil"))

    assert sombra.total == 0.0
    assert "total inválido" in sombra.motivos_reglas
    assert sombra.pasa_reglas is False


# --------------------------------------------------- 6. habitual llega en W5


def test_habitual_is_none_until_the_question_exists(green) -> None:
    """None no es False: significa que nadie preguntó todavía (W5)."""
    assert policy.evaluar_sombra(_order()).habitual is None
