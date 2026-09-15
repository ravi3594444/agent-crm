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


def test_un_aviso_que_EXPLOTA_tambien_suelta_su_consejo(barrido, monkeypatch) -> None:
    """La rama que faltaba, y este test afirmaba lo contrario.

    Decía «se prueba lo que HACE, no lo que convendría» y afirmaba
    `devolver.assert_not_called()`. Lo que hacía estaba mal: el `SET NX` de
    `tick` YA reclamó el consejo, así que un WhatsApp que tira timeout lo dejaba
    marcado como dicho sin que nadie lo oyera, y sin reintento hasta que venciera
    su TTL —24 horas—. La rama de `False` lo soltaba y la de la excepción no, que
    es la misma falla con dos caminos. Lo cazó una revisión.

    MUTACIÓN: sacar el `consejos.devolver(en_vuelo)` del `except` de
    `_solicitudes_scheduler`. Cae éste y sólo éste.
    """
    monkeypatch.setattr(consejos, "tick", Mock(return_value=[_consejo("a", "T", "C")]))
    monkeypatch.setattr(notificar, "avisar_dueno", Mock(side_effect=OSError("meta")))

    main._solicitudes_scheduler(_UnaVuelta())  # no levanta

    barrido["devolver"].assert_called_once()
    assert barrido["devolver"].call_args.args[0].clave == "a"


def test_el_ULTIMO_que_salio_bien_no_se_suelta_si_el_barrido_explota_despues(
    barrido, monkeypatch
) -> None:
    """El caso que el `en_vuelo = None` protege de verdad, y no era el otro.

    La primera versión de este test usaba dos consejos y el segundo explotando —
    y sacar el `en_vuelo = None` NO lo mataba, porque la vuelta siguiente
    pisaba `en_vuelo` igual. Medía la asignación de la línea de arriba, no la
    limpieza.

    Lo que la limpieza protege es esto: el último consejo SALE BIEN y después
    revienta algo que no es un aviso —`tick` es un generador y puede levantar al
    pedirle el siguiente—. Sin limpiar, se soltaría el que YA salió, y el dueño
    lo recibiría otra vez mañana.

    MUTACIÓN: sacar `en_vuelo = None` tras el aviso. Cae éste y sólo éste.
    """
    def generador():
        yield _consejo("a", "Salió", "C")
        raise RuntimeError("erpnext se cayó pidiendo el siguiente")

    monkeypatch.setattr(consejos, "tick", Mock(return_value=generador()))
    monkeypatch.setattr(notificar, "avisar_dueno", Mock(return_value=True))

    main._solicitudes_scheduler(_UnaVuelta())  # no levanta

    barrido["devolver"].assert_not_called()


def test_el_que_YA_SALIO_no_se_suelta_cuando_el_siguiente_explota(
    barrido, monkeypatch
) -> None:
    """La mitad que el arreglo podía romper, y por eso va su propio test.

    Soltar «el último que vi» en vez de «el que estaba en vuelo» soltaría también
    al que ya salió bien, y el dueño lo recibiría DOS veces mañana. Por eso
    `en_vuelo` se pone en None después de cada aviso exitoso.

    MUTACIÓN: no poner `en_vuelo = None` tras el aviso. Cae éste y sólo éste.
    """
    salio = _consejo("a", "Salió", "C")
    explota = _consejo("b", "Explota", "C")
    monkeypatch.setattr(consejos, "tick", Mock(return_value=[salio, explota]))
    avisar = Mock(side_effect=[True, OSError("meta")])
    monkeypatch.setattr(notificar, "avisar_dueno", avisar)

    main._solicitudes_scheduler(_UnaVuelta())

    barrido["devolver"].assert_called_once()
    assert barrido["devolver"].call_args.args[0].clave == "b"
