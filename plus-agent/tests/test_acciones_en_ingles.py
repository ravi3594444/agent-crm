"""El motivo de un `AccionError` sale en el idioma del que lo lee.

EL HUECO QUE ESTO CIERRA
`app/tools/gestion.py` armaba la frase de alrededor con `idioma.t(...)` y le
metía adentro la excepción tal cual — y `app/acciones.py` levanta 28 mensajes
escritos en castellano. Con `IDIOMA_GERENCIA=en` el dueño recibía la cáscara en
inglés y el motivo en castellano: media frase en cada idioma. Es el mismo
defecto que `LimiteError` ya había resuelto llevando `clave` y `datos`, y la
forma de arreglarlo estaba escrita en `limites.py` desde entonces.

POR QUÉ NO ALCANZA CON «SALE EN INGLÉS»
`idioma.t` devuelve la CLAVE cuando no existe, así que un texto sin traducir y
una clave sin cargar se ven distintos de un castellano pero iguales entre sí.
Cada prueba corre la misma rama en los DOS idiomas, exige que difieran, y exige
que ninguno de los dos sea la clave cruda.
"""
from __future__ import annotations

import pytest

from app import acciones, idioma

# Este archivo afirma texto en INGLÉS, así que lo declara.
pytestmark = pytest.mark.idioma("en")


def _motivos(hacer) -> tuple[str, str]:
    """La misma rama, leída en los dos idiomas. (castellano, inglés)."""
    try:
        hacer()
    except acciones.AccionError as exc:
        return idioma.motivo_de(exc, idioma.ES), idioma.motivo_de(exc, idioma.EN)
    raise AssertionError("no levantó AccionError")


def _sano(es: str, en: str, clave: str) -> None:
    assert es and en
    assert es != en, "el mismo texto en los dos idiomas es un texto sin traducir"
    assert clave not in es and clave not in en, (
        f"salió la clave cruda: {clave} no está en el catálogo"
    )


def test_un_verbo_que_no_existe_explica_en_el_idioma_del_dueno():
    """La rama más corriente, y la que tiene datos interpolados.

    MUTACIÓN: sacarle `clave=` a ese `raise`. Cae éste y sólo éste — el
    castellano no cambia, porque `str(exc)` sigue siendo el mismo.
    """
    es, en = _motivos(lambda: acciones.resolver("volar"))
    _sano(es, en, "accion.verbo_desconocido")
    # El dato se interpola igual en los dos: una palabra que tecleó el dueño no
    # se traduce.
    assert "volar" in es and "volar" in en


def test_un_dato_que_es_una_frase_tambien_se_traduce():
    """LA PARTE QUE `limites.motivo` NO TENÍA, y por la que existe el llamable.

    `accion.terminos_no_entendidos` interpola «qué día y a qué hora», que es
    prosa y no un dato. Quien levanta la excepción no sabe quién la va a leer,
    así que ese pedazo viaja como `lambda lengua:` y se arma acá.

    MUTACIÓN: en `idioma.motivo_de`, dejar de llamar al llamable
    (`{k: v for k, v in crudos.items()}`). Cae éste y sólo éste: el texto sale
    con un `<function ...>` adentro.
    """
    accion = acciones.TODAS["retiro"]
    es, en = _motivos(
        lambda: acciones._parametros(accion, "a la tarde", "SAL-ORD-2026-00008")
    )
    _sano(es, en, "accion.terminos_no_entendidos")
    assert "<function" not in en and "lambda" not in en
    # La frase que viaja como llamable, en cada idioma. No se leen del catálogo:
    # contra `idioma.t(...)` las dos mitades del assert se mueven juntas.
    assert "qué día y a qué hora" in es
    assert "what day and what time" in en


def test_el_llamable_se_arma_por_cada_lectura_y_no_una_sola_vez():
    """El segundo consumidor del mismo valor derivado, mutado por separado.

    `motivo_de` corre una vez por lector y el llamable tiene que armarse con LA
    LENGUA DE ESE LECTOR, cada vez. Cachear el resultado de la primera lectura
    —la optimización obvia— le daría al segundo lector el idioma del primero.

    Se lee TRES veces (en, es, en) y se exige el fragmento correcto en cada
    una. La primera versión de esta prueba sólo pedía que las tres difirieran
    entre sí, y con eso no distinguía «cada lectura en su idioma» de «plantilla
    traducida con el fragmento pegado del primer lector» — que es exactamente
    lo que dejó pasar cuando la mutación se corrió de verdad.

    MUTACIÓN: cachear en `motivo_de` (`exc.datos = datos` después de resolver).
    Cae ésta y sólo ésta.
    """
    accion = acciones.TODAS["retiro"]
    try:
        acciones._parametros(accion, "a la tarde", "SAL-ORD-2026-00008")
    except acciones.AccionError as exc:
        primero_en = idioma.motivo_de(exc, idioma.EN)
        en_medio_es = idioma.motivo_de(exc, idioma.ES)
        de_nuevo_en = idioma.motivo_de(exc, idioma.EN)
    else:
        raise AssertionError("no levantó AccionError")

    assert primero_en == de_nuevo_en, "la segunda lectura en inglés cambió"
    # Y el FRAGMENTO —lo que viaja como llamable— en el idioma de cada lectura.
    # Sin esto, una plantilla traducida con el fragmento del primer lector pasa.
    assert "what day and what time" in primero_en
    assert "what day and what time" in de_nuevo_en
    assert "qué día y a qué hora" in en_medio_es
    assert "what day and what time" not in en_medio_es


def test_sin_clave_el_motivo_sigue_siendo_el_castellano_de_siempre():
    """La compatibilidad hacia atrás, que es la mitad fácil de romper.

    Una excepción de otro módulo, o una `AccionError` vieja sin clave, tiene que
    seguir saliendo — y nunca vacía. Es lo que hacía `limites.motivo` antes de
    delegar, y lo que sigue haciendo el log.

    MUTACIÓN: en `motivo_de`, devolver "" cuando no hay clave. Cae éste y sólo
    éste.
    """
    suelta = acciones.AccionError("algo que nadie migró todavía")
    assert idioma.motivo_de(suelta, idioma.EN) == "algo que nadie migró todavía"
    assert idioma.motivo_de(ValueError("otra cosa"), idioma.EN) == "otra cosa"


def test_la_herramienta_entrega_la_frase_entera_en_un_solo_idioma(monkeypatch):
    """LO QUE LEE EL DUEÑO, que es lo que ninguna de las de arriba mira.

    Las otras prueban `idioma.motivo_de`. Ésta prueba el CALL SITE: que
    `proponer_accion` use el motivo traducido y no la excepción. Se escribió
    porque la mutación que devuelve `exc=exc` —el bug que reportó la revisión—
    sobrevivía a las cuatro anteriores con 3275 en verde.

    MUTACIÓN: en `app/tools/gestion.py`, volver a `exc=exc`. Cae ésta y sólo
    ésta.
    """
    from app import router
    from app.tools.gestion import proponer_accion

    GERENTE = "5493511234567"
    monkeypatch.setenv("TELEFONOS_EQUIPO", GERENTE)
    monkeypatch.setenv("IDIOMA_GERENCIA", "en")
    router.recargar()

    respuesta = str(
        proponer_accion.invoke(
            {"accion": "volar", "pedido": "SAL-ORD-2026-00008", "detalle": ""},
            config={"configurable": {"actor_scope": "management",
                                     "actor_phone": GERENTE,
                                     "thread_id": "t", "inbound_message_id": "m"}},
        )
    )

    # El motivo, en inglés y adentro de la frase de alrededor.
    assert "is not an action that exists" in respuesta
    # Y NADA del castellano que `str(exc)` sigue teniendo para el log.
    assert "no es una acción que exista" not in respuesta
    assert "que puedo preparar" not in respuesta


@pytest.mark.parametrize(
    "verbo, en_castellano, en_ingles",
    [("cancelar", "el motivo", "the reason"), ("rechazar", "por qué", "why")],
)
def test_el_hueco_que_falta_se_nombra_en_el_idioma_del_que_lo_lee(
    verbo: str, en_castellano: str, en_ingles: str
):
    """EL SEGUNDO llamable del archivo, mutado aparte del primero.

    `accion.falta_dato` interpola `{que}`, y `{que}` es prosa —«el motivo», «por
    qué»— que además cambia según el verbo. Son dos cosas distintas que se
    pueden romper por separado: que el fragmento no se traduzca, y que se
    traduzca el fragmento del OTRO verbo. Por eso las dos ramas corren, y cada
    una exige su propio fragmento y niega el del catálogo castellano.

    MUTACIÓN: en `acciones._parametros`, `"que": lambda lengua: idioma.t(
    clave_que, lengua)` -> `"que": que`. Caen estos dos y sólo estos: el
    castellano no se mueve —es el mismo texto— y el inglés queda con el hueco
    en castellano adentro de una frase inglesa.
    """
    accion = acciones.TODAS[verbo]
    es, en = _motivos(lambda: acciones._parametros(accion, "", "SAL-ORD-2026-00008"))
    _sano(es, en, "accion.falta_dato")

    assert en_castellano in es
    assert en_ingles in en
    # Y NADA del castellano adentro del inglés. Es lo único que la mutación
    # cambia: la cáscara ya salía traducida.
    assert en_castellano not in en
    # El verbo sí se interpola igual en los dos: es el comando que teclea el
    # dueño, no una palabra del idioma.
    assert verbo in es and verbo in en
