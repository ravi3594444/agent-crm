"""El manager le manda un WhatsApp a un cliente, con el visto bueno del dueño.

LO QUE ESTO REEMPLAZA
`redactar_mensaje_cliente` devolvía un borrador con un hueco literal
—«[Redactá el mensaje acá…]»— para que una persona lo copiara y lo mandara
desde su propio WhatsApp. La frase del dueño «yo le digo cualquier cosa al
manager y él lo hace» terminaba, para todo lo que sale hacia un cliente, en
copiar y pegar.

LAS DOS COSAS QUE NO PUEDEN FALLAR
1. Nada sale sin que el dueño toque el botón, y lo que sale es EXACTAMENTE el
   texto que él vio. Un resumen mostrado y otro texto mandado sería aprobar
   una cosa y mandar otra.
2. Nada sale dos veces. Es una promesa comercial a un cliente, no un aviso
   interno.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from app import aprobacion, erpnext, idioma, notificar, outbound_status, salidas
from app.runtime_context import SIN_PERMISO
from app.tools.captura import avisar_al_cliente

pytestmark = pytest.mark.idioma("es")

GERENTE = "5493511234567"
CLIENTE_TEL = "5493516667777"
FICHA = {
    "name": "CUST-0009",
    "customer_name": "Panadería San José",
    "mobile_no": CLIENTE_TEL,
}
TEXTO = "Llegó el queso cremoso, ¿te mando 2 hormas mañana?"


def _config() -> dict:
    return {
        "configurable": {
            "thread_id": "ger:t",
            "actor_scope": "management",
            "actor_phone": GERENTE,
            "inbound_message_id": "wamid.avisar-001",
        }
    }


@pytest.fixture(autouse=True)
def equipo(monkeypatch: pytest.MonkeyPatch):
    from app import router, telefono

    monkeypatch.setattr(router, "STAFF", [telefono.normalizar(GERENTE)])
    yield


@pytest.fixture
def mundo(monkeypatch: pytest.MonkeyPatch):
    """Un cliente que existe, dentro de la ventana, y nada que salga de verdad."""

    def get_list(doctype, filters=None, fields=None, limit=None, **kw):
        if doctype != "Customer":
            return []
        for campo, operador, valor in filters or []:
            if campo == "name" and operador == "=" and valor == FICHA["name"]:
                return [dict(FICHA)]
            if campo == "customer_name" and operador == "like":
                aguja = str(valor).strip("%").casefold()
                if aguja and aguja in FICHA["customer_name"].casefold():
                    return [dict(FICHA)]
        return []

    monkeypatch.setattr(erpnext, "get_list", get_list)
    monkeypatch.setattr(outbound_status, "window_open", Mock(return_value=True))
    botones = Mock(return_value=True)
    monkeypatch.setattr(notificar, "pedir_visto_bueno_de_envio", botones)
    encolar = Mock(return_value=True)
    from app import avisos

    monkeypatch.setattr(avisos, "encolar", encolar)
    yield {"botones": botones, "encolar": encolar}


def _pedir(cliente: str = "San José", mensaje: str = TEXTO) -> str:
    return avisar_al_cliente.invoke(
        {"cliente": cliente, "mensaje": mensaje}, config=_config()
    )


# --------------------------------------------------------------- se prepara


def test_le_pide_el_visto_bueno_al_que_lo_pidio_con_el_texto_exacto(mundo) -> None:
    respuesta = _pedir()

    telefono, id_salida, nombre, tel_cliente, texto = mundo["botones"].call_args.args
    assert telefono == GERENTE          # a quien preguntó, no a un número fijo
    assert tel_cliente == CLIENTE_TEL
    assert nombre == "Panadería San José"
    # EL TEXTO EXACTO. Lo que aprueba tiene que ser lo que sale.
    assert texto == TEXTO
    # Y lo guardado es lo mismo que lo mostrado: si se guardara otra cosa, el
    # dueño aprobaría una pantalla y el cliente leería otra.
    assert salidas.leer(id_salida).texto == TEXTO
    # Al modelo se le dice explícitamente que TODAVÍA no salió.
    assert "TODAVÍA NO SALIÓ" in respuesta


def test_no_sale_nada_en_el_momento_de_pedirlo(mundo) -> None:
    _pedir()

    mundo["encolar"].assert_not_called()


def test_un_numero_que_no_es_del_equipo_no_puede_ni_preparar(mundo) -> None:
    respuesta = avisar_al_cliente.invoke(
        {"cliente": "San José", "mensaje": TEXTO},
        config={"configurable": {"thread_id": "c", "actor_scope": "customer",
                                 "customer_code": "CUST-0009",
                                 "actor_phone": "5493510000000",
                                 "inbound_message_id": "w"}},
    )

    assert SIN_PERMISO in respuesta
    mundo["botones"].assert_not_called()


def test_la_negativa_traducida_es_la_misma_que_la_constante() -> None:
    """Son dos literales en dos archivos distintos (app/idioma.py y
    app/runtime_context.py), así que cambiar uno no mueve al otro y el assert
    sirve. La versión en inglés existe: hasta este cambio, el dueño que habla
    inglés recibía su única negativa en castellano."""
    assert idioma.t("permiso.sin_autorizacion", "es") == SIN_PERMISO
    en = idioma.t("permiso.sin_autorizacion", "en")
    assert en != SIN_PERMISO
    assert "not authorized" in en


def test_un_cliente_que_no_existe_no_deja_nada_guardado(mundo) -> None:
    respuesta = _pedir(cliente="Ferretería Pérez")

    assert "No encontré" in respuesta
    mundo["botones"].assert_not_called()


def test_un_cliente_sin_telefono_se_dice_con_todas_las_letras(
    mundo, monkeypatch: pytest.MonkeyPatch
) -> None:
    sin_tel = dict(FICHA, mobile_no="")
    monkeypatch.setattr(erpnext, "get_list", Mock(return_value=[sin_tel]))

    respuesta = _pedir()

    assert "no tiene teléfono cargado" in respuesta
    mundo["botones"].assert_not_called()


def test_fuera_de_la_ventana_de_24h_se_frena_ANTES_de_molestar_al_dueno(
    mundo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Meta no deja escribirle primero a quien hace más de un día que no
    escribe, salvo con una plantilla aprobada, y para esto no hay ninguna.

    Sin este chequeo el dueño tocaba el botón, el mensaje se encolaba, se
    gastaba los ocho reintentos y moría en la cola de descarte — y él se
    quedaba creyendo que el cliente fue avisado.
    """
    monkeypatch.setattr(outbound_status, "window_open", Mock(return_value=False))

    respuesta = _pedir()

    assert "no te escribe" in respuesta
    mundo["botones"].assert_not_called()
    mundo["encolar"].assert_not_called()


def test_si_el_boton_no_sale_no_queda_una_salida_huerfana(
    mundo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un mensaje guardado que nadie puede aprobar es el teléfono de un cliente
    esperando una hora en Redis para vencer sin que nadie lo use."""
    guardadas: list[str] = []
    real = salidas.proponer

    def espiar(**kw):
        salida = real(**kw)
        guardadas.append(salida.id)
        return salida

    monkeypatch.setattr(salidas, "proponer", espiar)
    monkeypatch.setattr(notificar, "pedir_visto_bueno_de_envio", Mock(return_value=False))

    respuesta = _pedir()

    assert "No pude mandarle el mensaje al dueño" in respuesta
    assert guardadas, "no llegó ni a guardar: el test no probó lo que dice"
    assert salidas.leer(guardadas[0]) is None


# ------------------------------------------------------------ y recién sale


def _una_salida() -> str:
    return salidas.proponer(
        cliente="Panadería San José", telefono=CLIENTE_TEL,
        texto=TEXTO, pedida_por=GERENTE,
    ).id


def test_el_boton_manda_el_texto_aprobado_al_telefono_del_cliente(mundo) -> None:
    id_salida = _una_salida()

    respuesta = aprobacion.manejar_boton(f"mandar:{id_salida}", GERENTE)

    _evento, clave, telefono, texto = mundo["encolar"].call_args.args
    assert telefono == CLIENTE_TEL
    assert texto == TEXTO
    # La clave de idempotencia de la cola es el id de la salida: no hay pedido
    # acá, y ese id es lo único irrepetible que tenemos.
    assert clave == id_salida
    assert "Panadería San José" in respuesta


def test_el_segundo_toque_no_le_manda_lo_mismo_dos_veces(mundo) -> None:
    id_salida = _una_salida()

    aprobacion.manejar_boton(f"mandar:{id_salida}", GERENTE)
    segunda = aprobacion.manejar_boton(f"mandar:{id_salida}", GERENTE)

    assert mundo["encolar"].call_count == 1
    assert "ya no está" in segunda


def test_decir_que_no_no_manda_nada(mundo) -> None:
    id_salida = _una_salida()

    respuesta = aprobacion.manejar_boton(f"nomandar:{id_salida}", GERENTE)

    mundo["encolar"].assert_not_called()
    assert "no se lo mando" in respuesta
    # Y lo descartado no se puede mandar después.
    aprobacion.manejar_boton(f"mandar:{id_salida}", GERENTE)
    mundo["encolar"].assert_not_called()


def test_un_numero_de_afuera_no_puede_apretar_el_boton(mundo) -> None:
    id_salida = _una_salida()

    respuesta = aprobacion.manejar_boton(f"mandar:{id_salida}", "5493519999999")

    mundo["encolar"].assert_not_called()
    assert "permiso" in respuesta.casefold()
    # Y el mensaje sigue ahí para que lo apruebe quien corresponde: un extraño
    # que toca el botón no puede DESCARTARLE el mensaje al dueño.
    assert salidas.leer(id_salida) is not None


def test_si_la_cola_lo_rechaza_se_dice_que_NO_salio(mundo, monkeypatch) -> None:
    """«Lo aprobaste pero no salió» es una frase incómoda y es la verdadera. La
    cómoda —«listo, se lo mandé»— deja al dueño creyendo que avisó."""
    from app import avisos

    monkeypatch.setattr(avisos, "encolar", Mock(side_effect=ConnectionError("redis")))
    id_salida = _una_salida()

    respuesta = aprobacion.manejar_boton(f"mandar:{id_salida}", GERENTE)

    assert "NO salió" in respuesta


def test_un_mensaje_largo_se_MUESTRA_igual_que_como_se_guarda(mundo) -> None:
    """Lo que el dueño aprueba tiene que ser, carácter por carácter, lo que sale.

    `salidas.proponer` recorta a `LARGO_MAXIMO` porque Meta corta el cuerpo de
    un interactivo. Si el botón mostrara el texto ORIGINAL y se guardara el
    recortado, el dueño aprobaría un mensaje y el cliente recibiría otro más
    corto —cortado a la mitad de una frase, que es peor que no mandarlo—. Los
    tests de arriba usan un mensaje corto, donde los dos son iguales sin que
    nadie haga nada: no podían ver esta diferencia.
    """
    largo = "Llegó el queso. " * 200  # muy por encima de LARGO_MAXIMO

    avisar_al_cliente.invoke({"cliente": "San José", "mensaje": largo}, config=_config())

    _tel, id_salida, _nombre, _tel_c, mostrado = mundo["botones"].call_args.args
    guardado = salidas.leer(id_salida).texto
    assert len(guardado) == salidas.LARGO_MAXIMO, "el recorte no llegó a pasar"
    assert mostrado == guardado


def test_una_cola_que_falla_deja_reintentar_el_MISMO_boton(mundo, monkeypatch) -> None:
    """Lo que el dueño tiene en la mano es UN botón, y tiene que servir dos veces.

    `consumir` es un GETDEL: sin devolver la salida, el fallo de la cola la
    borra, el dueño lee «NO salió», toca otra vez y recibe «ya no está». Puede
    pedir el mensaje de nuevo —y volver a leerlo, y volver a aprobarlo— pero no
    puede reintentar el que ya miró, que es lo único que quería.

    Se reintenta con el MISMO id porque ése es la clave de idempotencia de la
    cola: un encolado que falló DESPUÉS de haber encolado no manda dos veces.
    """
    from app import avisos

    id_salida = _una_salida()
    monkeypatch.setattr(avisos, "encolar", Mock(side_effect=ConnectionError("redis")))
    primera = aprobacion.manejar_boton(f"mandar:{id_salida}", GERENTE)
    assert "NO salió" in primera

    recuperada = Mock(return_value=True)
    monkeypatch.setattr(avisos, "encolar", recuperada)
    segunda = aprobacion.manejar_boton(f"mandar:{id_salida}", GERENTE)

    assert "NO salió" not in segunda
    recuperada.assert_called_once()
    assert recuperada.call_args.args[1] == id_salida


def test_devolverla_no_la_convierte_en_un_boton_reusable(mundo, monkeypatch) -> None:
    """Se devuelve para REINTENTAR, no para poder mandar dos veces.

    Es la mitad que el arreglo podía romper: si devolver la salida dejara el
    botón vivo después de un envío exitoso, un dedo impaciente le manda al
    cliente la misma promesa dos veces — que es justo lo que `consumir` existía
    para impedir.
    """
    from app import avisos

    id_salida = _una_salida()
    monkeypatch.setattr(avisos, "encolar", Mock(side_effect=ConnectionError("redis")))
    aprobacion.manejar_boton(f"mandar:{id_salida}", GERENTE)

    encolar = Mock(return_value=True)
    monkeypatch.setattr(avisos, "encolar", encolar)
    aprobacion.manejar_boton(f"mandar:{id_salida}", GERENTE)
    tercera = aprobacion.manejar_boton(f"mandar:{id_salida}", GERENTE)

    encolar.assert_called_once()
    assert "ya no" in tercera.lower() or "no está" in tercera.lower()
