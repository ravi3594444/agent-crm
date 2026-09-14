"""El barrido llama a los consejos, y suelta el que no llegó a salir.

`app/consejos.py` no manda nada y no lo llama nadie por su cuenta: es el
barrido de `app/main.py` el que lo hace salir. Este archivo prueba ESE
cableado, que es donde la mitad determinista se conecta con la que habla.

LA MITAD QUE IMPORTA ES `devolver`. `tick()` toma cada consejo con un SET NX
para no decirlo dos veces. Si el aviso no sale y nadie suelta el reclamo, el
consejo queda marcado como dicho sin que el dueño se haya enterado: la falla
del envío se comió el hecho, y para una pérdida de plata eso es peor que no
haber tenido el detector.
"""
from __future__ import annotations

import threading
from unittest.mock import Mock

import pytest

from app import agenda, consejos, main, notificar, pendientes, solicitudes


class _UnaVuelta(threading.Event):
    """Un `stop` que deja pasar exactamente una vuelta del `while`.

    `stop.set()` antes de entrar —como hace el test que ya existía— hace que
    `wait` devuelva True al toque y el cuerpo del bucle NO corra nunca. Eso
    prueba que entrar no explota, y nada más.
    """

    def __init__(self) -> None:
        super().__init__()
        self.vueltas = 0

    def wait(self, timeout=None) -> bool:
        self.vueltas += 1
        return self.vueltas > 1


def _consejo(clave: str, titulo: str, cuerpo: str) -> consejos.Consejo:
    return consejos.Consejo(
        clase="perdida", clave=clave, sobre="SAL-ORD-2026-00001",
        titulo=titulo, cuerpo=cuerpo,
    )


@pytest.fixture
def barrido(monkeypatch: pytest.MonkeyPatch):
    """Los otros tres ticks callados: acá se mira sólo el cuarto."""
    for modulo in (solicitudes, pendientes, agenda):
        monkeypatch.setattr(modulo, "tick", Mock(return_value=None))
    devolver = Mock()
    monkeypatch.setattr(consejos, "devolver", devolver)
    yield {"devolver": devolver}


def test_cada_consejo_le_llega_al_dueno(barrido, monkeypatch) -> None:
    monkeypatch.setattr(consejos, "tick", Mock(return_value=[
        _consejo("a", "Perdiste plata", "En el pedido de la panadería."),
        _consejo("b", "Un cliente dormido", "Hace 40 días que no pide."),
    ]))
    avisar = Mock(return_value=True)
    monkeypatch.setattr(notificar, "avisar_dueno", avisar)

    main._solicitudes_scheduler(_UnaVuelta())

    assert [c.args for c in avisar.call_args_list] == [
        ("Perdiste plata", "En el pedido de la panadería."),
        ("Un cliente dormido", "Hace 40 días que no pide."),
    ]
    # Lo que SÍ salió no se suelta: soltarlo lo repetiría mañana.
    barrido["devolver"].assert_not_called()


def test_el_consejo_que_no_salio_se_suelta_y_SOLO_ESE(barrido, monkeypatch) -> None:
    """Dos consejos, uno sale y el otro no.

    Con los dos fallando o los dos saliendo, un `devolver` que soltara
    cualquiera de los dos —o los dos— pasaría igual. La única forma de que el
    assert pueda estar en desacuerdo con el código sobre CUÁL se suelta es que
    los dos destinos difieran en la misma vuelta.
    """
    salio = _consejo("si", "Este sale", "cuerpo 1")
    no_salio = _consejo("no", "Este no sale", "cuerpo 2")
    monkeypatch.setattr(consejos, "tick", Mock(return_value=[salio, no_salio]))
    monkeypatch.setattr(
        notificar, "avisar_dueno",
        Mock(side_effect=lambda titulo, cuerpo, **kw: titulo == "Este sale"),
    )

    main._solicitudes_scheduler(_UnaVuelta())

    assert barrido["devolver"].call_count == 1
    assert barrido["devolver"].call_args.args[0] is no_salio


def test_los_consejos_salen_con_plantilla(barrido, monkeypatch) -> None:
    """Un consejo sale casi siempre FUERA de la ventana de 24 h del dueño —es
    un barrido, no una respuesta—. Sin plantilla no falla a veces: falla
    siempre, se gasta los ocho reintentos y muere en la cola de descarte."""
    monkeypatch.setattr(consejos, "tick", Mock(return_value=[_consejo("a", "T", "C")]))
    avisar = Mock(return_value=True)
    monkeypatch.setattr(notificar, "avisar_dueno", avisar)

    main._solicitudes_scheduler(_UnaVuelta())

    assert avisar.call_args.kwargs["plantilla_env"] == "WHATSAPP_STAFF_ALERT_TEMPLATE"


def test_si_los_consejos_explotan_el_barrido_sobrevive(barrido, monkeypatch) -> None:
    """Su propio try/except, como los otros tres: un detector que falla no
    puede saltear el vencimiento de una solicitud."""
    monkeypatch.setattr(consejos, "tick", Mock(side_effect=RuntimeError("erpnext")))

    main._solicitudes_scheduler(_UnaVuelta())  # no levanta

    solicitudes.tick.assert_called_once()
    agenda.tick.assert_called_once()


def test_un_aviso_que_explota_no_se_lleva_a_los_demas(barrido, monkeypatch) -> None:
    """El `for` está adentro del try, así que un aviso que levanta corta la
    vuelta. Se prueba lo que HACE, no lo que convendría: el barrido sigue vivo
    y el consejo no queda soltado a medias."""
    monkeypatch.setattr(consejos, "tick", Mock(return_value=[_consejo("a", "T", "C")]))
    monkeypatch.setattr(notificar, "avisar_dueno", Mock(side_effect=OSError("meta")))

    main._solicitudes_scheduler(_UnaVuelta())  # no levanta

    barrido["devolver"].assert_not_called()
