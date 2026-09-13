"""El guard que faltaba: un test que se muere si un marcador cambia de texto.

QUÉ MEDÍA LA AUDITORÍA
`docs/AUDITORIA.md`, hallazgo 4: «le cambié el texto a `pendientes.MARCA_AVISO`
y no se rompió un solo test». Está reproducido: con el marcador mutado la suite
daba 2453 passed, 2 xfailed — exactamente lo mismo que sin mutar.

POR QUÉ DABA CERO, QUE ES LO QUE ESTE ARCHIVO ARREGLA
No es que nadie mire los marcadores: los miran catorce asserts. Es QUE LOS
MIRAN A TRAVÉS DE LA CONSTANTE, en los dos lados:

    assert any(t.startswith(pendientes.MARCA_AVISO) for _, _, t in escritos)

El test escribe la marca con la constante y después la afirma con la misma
constante, así que renombrarla mueve las dos mitades juntas y no queda nadie
para notar la diferencia. Es literalmente el defecto que la auditoría le
encontró al reloj del lado de los tests (hallazgo 2): seis archivos que
re-derivaron la suposición del código y por eso no podían discrepar con ella.

Un test así prueba que el código es consistente consigo mismo. Lo que no puede
probar es lo único que importa acá: que el texto es EL MISMO DE AYER. Eso sólo
lo prueba un literal escrito a mano, y por eso la tabla de abajo está escrita
a mano y tiene que seguir estándolo.

NO LA GENERES DESDE `marcas.MARCAS`.
Sería la misma tautología con más pasos, y se moriría exactamente cuando este
archivo deja de servir para algo.

LA TABLA ES UN CONTRATO CON ERPNEXT, NO CON EL CÓDIGO
Cada uno de estos textos está escrito en comentarios de pedidos reales. Si una
línea de acá cambia, no alcanza con cambiar el código: hay que leer los dos
textos (`Marca.heredados`) o el sistema pierde su propia historia — el barrido
de mañana no ve lo que escribió el de hoy, y le vuelve a avisar al cliente o no
cierra lo que había que cerrar.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app import (
    acciones,
    agenda,
    autonomia,
    confirmacion,
    decisiones,
    limites,
    marcas,
    pendientes,
    solicitudes,
    sombra,
)

# Afirma literales en español: los dos marcadores en prosa lo son.
pytestmark = pytest.mark.idioma("es")

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


# ---------------------------------------------------------------------------
# 1. LOS TEXTOS. Escritos a mano, uno por uno.
# ---------------------------------------------------------------------------

# nombre en el registro -> el texto EXACTO que está escrito en ERPNext.
TEXTOS_DURABLES = {
    "limite": "[limite]",
    "entrega": "[entrega]",
    "idioma": "[idioma]",
    "accion": "[accion]",
    "solicitud": "[solicitud]",
    "sombra": "[sombra]",
    "confirmacion": "[confirmado-por-agente]",
    "remito_agente": "[remito-preparado-por-agente]",
    "pendiente_aviso": "[pendiente-aviso]",
    "pendiente_cierre": "[pendiente-cerrado]",
    "agenda": "[agenda]",
    "revision_humana": "Requiere revisión humana:",
    "rechazo_manual": "Rechazado manualmente por",
}


def test_ningun_marcador_durable_cambio_de_texto():
    """EL test. Si éste se pone rojo, alguien renombró un formato durable.

    Arreglarlo cambiando la tabla es exactamente lo que NO hay que hacer sin
    decidir antes qué pasa con lo que ya está escrito en ERPNext.
    """
    assert {n: m.texto for n, m in marcas.MARCAS.items()} == TEXTOS_DURABLES


def test_el_registro_no_tiene_filas_de_mas_ni_de_menos():
    """El censo dejó de ser una afirmación y pasó a ser algo que se cuenta.

    La auditoría y el issue #24 dicen «once marcadores ... más dos en prosa»,
    pero enumeran DIEZ entre corchetes: el censo estaba corrido en uno. Diez
    más dos son doce.
    """
    entre_corchetes = [m for m in marcas.MARCAS.values() if not m.prosa]
    en_prosa = [m for m in marcas.MARCAS.values() if m.prosa]
    assert len(entre_corchetes) == 11
    assert len(en_prosa) == 2
    assert all(m.texto.startswith("[") and "]" in m.texto for m in entre_corchetes)


# ---------------------------------------------------------------------------
# 2. LAS CONSTANTES DE CADA MÓDULO SIGUEN SIENDO LAS DEL REGISTRO.
#
# El registro sólo sirve si es el mismo texto que usa el módulo. Sin esto, un
# `pendientes.MARCA_AVISO = "otra cosa"` dejaría el registro intacto y el guard
# de arriba verde, que es el agujero entero de nuevo.
# ---------------------------------------------------------------------------

def test_cada_modulo_usa_el_texto_del_registro():
    assert marcas.texto("limite") == limites.MARCA_DURABLE
    assert marcas.texto("entrega") == limites.MARCA_DURABLE_ENTREGA
    assert marcas.texto("idioma") == limites.MARCA_DURABLE_IDIOMA
    assert marcas.texto("accion") == acciones.MARCA_DURABLE
    assert marcas.texto("solicitud") == solicitudes.MARCA
    assert marcas.texto("sombra") == sombra.MARCA
    assert marcas.texto("confirmacion") == confirmacion.MARCA
    assert marcas.texto("remito_agente") == decisiones.MARCA_REMITO_AGENTE
    assert marcas.texto("pendiente_aviso") == pendientes.MARCA_AVISO
    assert marcas.texto("pendiente_cierre") == pendientes.MARCA_CIERRE
    assert marcas.texto("agenda") == agenda.MARCA
    assert marcas.texto("revision_humana") == autonomia.MARCA_REVISION
    assert marcas.texto("rechazo_manual") == autonomia.MARCA_RECHAZO


# ---------------------------------------------------------------------------
# 3. LOS QUE ESCRIBEN EL LITERAL A MANO.
#
# Los dos marcadores en prosa tienen la constante del lado del LECTOR
# (`autonomia`) y un literal interpolado del lado del ESCRITOR
# (`tools/pedidos.py`, `decisiones.py`). Nada los ataba: es la forma más pura
# del bug de este issue, porque las dos mitades pueden separarse sin que se
# caiga nada. Este test las ata leyendo el fuente.
# ---------------------------------------------------------------------------

def _literales(archivo: str) -> list[str]:
    """Todas las cadenas del módulo, incluidas las partes fijas de las f-string."""
    arbol = ast.parse((APP / archivo).read_text(encoding="utf-8"))
    encontrados = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
            encontrados.append(nodo.value)
    return encontrados


@pytest.mark.parametrize(
    ("archivo", "nombre"),
    [
        ("tools/pedidos.py", "revision_humana"),
        ("decisiones.py", "rechazo_manual"),
    ],
)
def test_el_que_escribe_la_prosa_escribe_el_texto_del_registro(archivo, nombre):
    """El escritor y el lector dicen lo mismo, y ahora algo se muere si no.

    No exige que el escritor IMPORTE la constante: exige que el texto que
    escribe empiece con el del registro. Un `Requiere revision humana:` sin
    tilde deja de matchear en `autonomia` y el número se va a cero sin avisar,
    y eso es lo que este assert hace imposible.
    """
    esperado = marcas.texto(nombre)
    assert any(t.startswith(esperado) for t in _literales(archivo)), (
        f"{archivo} ya no escribe un texto que empiece con {esperado!r}: "
        f"el contador de app/autonomia.py deja de verlo y baja a cero en silencio"
    )


# ---------------------------------------------------------------------------
# 4. EL TECHO SIN MOTIVO ESCRITO ES EL DEFECTO, NO EL NÚMERO.
#
# El issue #24: «Si es real, escribila — hoy no está en ninguna parte y eso es
# el problema, no el número». Acá eso deja de ser una convención y pasa a ser
# algo que falla.
# ---------------------------------------------------------------------------

def test_todo_techo_viene_con_su_motivo_escrito():
    for m in marcas.MARCAS.values():
        assert m.porque_el_techo.strip(), (
            f"la marca {m.nombre!r} declara techo {m.techo} sin decir por qué; "
            "un techo sin motivo escrito es el hallazgo 4 de la auditoría"
        )


def test_los_dos_techos_que_parecian_el_mismo_numero():
    """5 y 20 no son el mismo número escrito dos veces: buscan extremos opuestos.

    Cada uno pide la página en la DIRECCIÓN del extremo que quiere, y por eso
    truncar no puede dar una respuesta equivocada, sólo ninguna. Este test fija
    esa relación —no los números sueltos— porque es la que hace que 5 alcance
    para uno y 20 para el otro.
    """
    sombra_ = marcas.marca("sombra")
    confirmacion_ = marcas.marca("confirmacion")

    assert sombra_.lectura == marcas.MAS_NUEVA
    assert sombra_.orden == "creation desc"
    assert sombra_.techo == 5

    assert confirmacion_.lectura == marcas.MAS_VIEJA
    assert confirmacion_.orden == "creation asc"
    assert confirmacion_.techo == 20


def test_el_orden_sale_de_la_regla_de_seleccion_y_no_se_declara_aparte():
    """Pedir `desc` y quedarse con la más vieja es el bug silencioso que el
    registro hace imposible de escribir."""
    for m in marcas.MARCAS.values():
        if m.lectura == marcas.MAS_NUEVA:
            assert m.orden == "creation desc"
        elif m.lectura == marcas.MAS_VIEJA:
            assert m.orden == "creation asc"
        elif m.lectura == marcas.EXISTENCIA:
            assert m.orden is None


# ---------------------------------------------------------------------------
# 5. IDA Y VUELTA CONTRA EL DOBLE DE ERPNEXT.
#
# El texto solo no es todo el contrato: que `escribir` produzca algo que `leer`
# reconozca es la otra mitad, y es la que se rompe si el escritor y el lector se
# separan. El doble de tests/fakes.py respeta filtros, order_by y limit, así que
# acá se puede falsificar de verdad.
# ---------------------------------------------------------------------------

@pytest.fixture
def erp(monkeypatch):
    """Un ERPNext de mentira: guarda los comentarios y los contesta como Frappe."""
    from tests import fakes

    escritos: list[dict] = []

    def add_comment(doctype, name, text):
        escritos.append(
            {
                "content": text,
                "reference_doctype": doctype,
                "reference_name": name,
                # Empatan y `listar` desempata por orden de inserción, así que
                # el último escrito es el más nuevo en las dos direcciones.
                "creation": "2026-09-09 10:00:00",
            }
        )

    def policy_get_list(doctype, filters=None, fields=None, limit=20,
                        parent=None, order_by=None, start=0, timeout=None):
        assert doctype == "Comment"
        return fakes.listar(
            escritos, filters, limit=limit, order_by=order_by, start=start
        )

    monkeypatch.setattr(marcas.erpnext, "add_comment", add_comment)
    monkeypatch.setattr(marcas.erpnext, "registrar_comentario", add_comment)
    monkeypatch.setattr(marcas.erpnext, "policy_get_list", policy_get_list)
    return escritos


PEDIDO = "SAL-ORD-2026-00042"


@pytest.mark.parametrize(
    "nombre", ["pendiente_aviso", "pendiente_cierre", "limite", "entrega", "idioma"]
)
def test_lo_que_se_escribe_se_encuentra(erp, nombre):
    """Ida y vuelta de los marcadores de existencia."""
    m = marcas.marca(nombre)
    doc = PEDIDO if m.doctype == "Sales Order" else "Lacteos Test SA"
    assert marcas.existe(nombre, doc if m.doctype == "Sales Order" else None) is False
    marcas.escribir(nombre, doc, "2026-09-09T10:00:00-03:00")
    assert marcas.existe(nombre, doc if m.doctype == "Sales Order" else None) is True


def test_una_marca_no_encuentra_la_de_otro(erp):
    """El aviso y el cierre son dos hechos distintos y se anotan distinto.

    Si mañana alguien unificara los textos en una familia `[pendiente-...]`, el
    `like` de uno matchearía al otro y el barrido daría por avisado un pedido
    que sólo estaba cerrado. Acá se muere.
    """
    marcas.escribir("pendiente_aviso", PEDIDO, "sello")
    assert marcas.existe("pendiente_aviso", PEDIDO) is True
    assert marcas.existe("pendiente_cierre", PEDIDO) is False


def test_gana_la_mas_nueva_cuando_hay_dos(erp):
    """`[sombra]` se queda con la más nueva: dos barridos a la vez dejan dos."""
    marcas.escribir("sombra", PEDIDO, '{"pasa_reglas": false}', exigir=True)
    marcas.escribir("sombra", PEDIDO, '{"pasa_reglas": true}', exigir=True)
    assert marcas.leer("sombra", PEDIDO) == {"pasa_reglas": True}


def test_un_comentario_ilegible_no_es_un_registro(erp):
    """Y no se lleva puesto al que sí lo es: se saltea y sigue buscando."""
    marcas.escribir("sombra", PEDIDO, '{"pasa_reglas": true}', exigir=True)
    marcas.escribir("sombra", PEDIDO, "esto no es json", exigir=True)
    assert marcas.leer("sombra", PEDIDO) == {"pasa_reglas": True}


def test_el_techo_recorta_y_por_eso_el_orden_importa(erp):
    """Con más registros que el techo, el que gana tiene que seguir ganando.

    Es el test que un doble que ignorara `limit` u `order_by` no podría
    sostener, y por eso `tests/fakes.listar` los respeta.
    """
    techo = marcas.marca("sombra").techo
    for i in range(techo + 3):
        marcas.escribir("sombra", PEDIDO, f'{{"n": {i}}}', exigir=True)
    assert marcas.leer("sombra", PEDIDO) == {"n": techo + 2}


def test_el_json_sobrevive_al_html_que_mete_erpnext():
    """ERPNext envuelve el contenido en HTML y escapa las comillas."""
    parsear = marcas.json_tras(marcas.texto("sombra"))
    mangled = (
        '<div class="ql-editor read-mode"><p>'
        f"{marcas.texto('sombra')} {{&quot;pasa_reglas&quot;: true}}"
        "</p></div>"
    )
    assert parsear(mangled) == {"pasa_reglas": True}
    assert parsear("") is None
    assert parsear(f"{marcas.texto('sombra')} no es json") is None


def test_el_marcador_que_no_vive_en_un_comentario():
    """`[remito-preparado-por-agente]` va en un CAMPO del Delivery Note.

    Ninguna consulta de Comment lo encuentra jamás, y el registro lo dice en
    vez de que haya que descubrirlo. Un borrador sin la marca no se toca nunca:
    es como se distingue el que preparó el agente del que cargó una persona.
    """
    m = marcas.marca("remito_agente")
    assert m.portador == marcas.CAMPO
    assert m.campo == "remarks"
    assert marcas.en_campo("remito_agente", {"remarks": f"{m.texto} Preparado..."})
    assert not marcas.en_campo("remito_agente", {"remarks": "lo cargué a mano"})
    assert not marcas.en_campo("remito_agente", {})
    with pytest.raises(ValueError):
        marcas.filas("remito_agente", "DN-1")


def test_el_que_no_se_lee_lo_dice(erp):
    """`[accion]` se escribe y nadie lo consulta. El registro no lo esconde."""
    assert marcas.marca("accion").lectura == marcas.NO_SE_LEE
    assert marcas.marca("accion").parser is None
    with pytest.raises(ValueError):
        marcas.leer("accion", PEDIDO)


def test_un_nombre_que_no_existe_se_muere_con_la_lista():
    with pytest.raises(KeyError, match="pendiente_avisos"):
        marcas.texto("pendiente_avisos")


# ---------------------------------------------------------------------------
# 6. LOS TEXTOS HEREDADOS, QUE HOY NO USA NADIE.
#
# Ninguna fila cambia de texto en este PR, así que `heredados` está vacío en
# las doce. Se prueba igual: el día que una cambie, lo que hace falta es que
# leer los DOS textos ya funcione, no que haya que inventarlo con comentarios
# de clientes vivos del otro lado.
# ---------------------------------------------------------------------------

def test_hoy_ninguna_marca_cambio_de_texto():
    assert all(m.heredados == () for m in marcas.MARCAS.values())


def test_si_una_marca_cambiara_de_texto_se_leerian_los_dos(erp, monkeypatch):
    import dataclasses

    viejo = "[pendiente-aviso]"
    nueva = dataclasses.replace(
        marcas.marca("pendiente_aviso"),
        texto="[pendiente-recordado]",
        heredados=(viejo,),
    )
    monkeypatch.setitem(marcas.MARCAS, "pendiente_aviso", nueva)

    erp.append(
        {
            "content": f"{viejo} escrito el mes pasado",
            "reference_doctype": "Sales Order",
            "reference_name": PEDIDO,
            "creation": "2026-08-01 10:00:00",
        }
    )
    assert marcas.existe("pendiente_aviso", PEDIDO) is True, (
        "un pedido marcado con el texto viejo tiene que seguir contando como "
        "marcado, o el barrido le vuelve a escribir al cliente"
    )


# ---------------------------------------------------------------------------
# 7. EL ORDEN DE UN BARRIDO NO ES EL DE UN DOCUMENTO.
#
# Encontrado por Qodo en la review de este PR, y era una regresión de este PR.
# `confirmacion` declara MAS_VIEJA porque su lectura POR PEDIDO quiere la
# primera confirmación (el plazo de cancelación corre desde ahí). Pero
# `autonomia` no lee un pedido: barre una VENTANA sobre todos, y ahí el techo
# recorta — así que pedir `asc` arma el resumen con las 500 confirmaciones MÁS
# VIEJAS y deja afuera la actividad reciente. Antes de este PR el barrido pedía
# `desc` siempre.
#
# La lección de diseño: el orden lo decide LA LECTURA, no sólo el marcador. Un
# barrido siempre quiere la punta nueva, porque truncar una ventana tiene que
# tirar lo viejo y no lo de recién.
# ---------------------------------------------------------------------------

def test_un_barrido_pide_la_punta_nueva_aunque_el_marcador_lea_al_reves(erp):
    """El caso que lo rompía: `confirmacion` lee la más vieja POR PEDIDO."""
    from datetime import UTC, datetime

    for i in range(6):
        erp.append(
            {
                "content": f"{marcas.texto('confirmacion')} 2026-09-0{i + 1}T10:00:00+00:00",
                "reference_doctype": "Sales Order",
                "reference_name": f"SO-{i}",
                "creation": f"2026-09-0{i + 1} 10:00:00",
            }
        )
    filas, truncado = marcas.barrer(
        "confirmacion", datetime(2026, 1, 1, tzinfo=UTC), techo=3
    )
    assert truncado is True
    vistos = [f["reference_name"] for f in filas]
    assert vistos == ["SO-5", "SO-4", "SO-3"], (
        "un barrido truncado tiene que quedarse con lo MÁS NUEVO; con el orden "
        "del marcador (asc) el resumen se arma con lo más viejo y esconde la "
        "actividad reciente"
    )


# ---------------------------------------------------------------------------
# 8. UN TEXTO HEREDADO TIENE QUE PODER PARSEARSE, NO SÓLO ENCONTRARSE.
#
# También de la review de Qodo, y es un agujero en el test de heredados de más
# arriba: afirmaba `existe` y nada más. La consulta buscaba los dos textos pero
# el parser se compilaba contra UNO, así que un `[solicitud]` viejo se traía de
# ERPNext y después se descartaba por ilegible — que es justo la pérdida de
# historia que `heredados` existe para evitar.
# ---------------------------------------------------------------------------

def test_una_carga_escrita_con_el_texto_viejo_se_sigue_leyendo(erp, monkeypatch):
    import dataclasses

    viejo = "[solicitud]"
    # Un renombre de verdad: la fila declara el texto NUEVO y el viejo como
    # heredado. Ojo con el atajo de `dataclasses.replace` sin más: se lleva el
    # parser de la fila vieja, que matchea el literal viejo por casualidad y
    # deja pasar el bug.
    nueva = dataclasses.replace(
        marcas.marca("solicitud"),
        texto="[pedido-evento]",
        heredados=(viejo,),
    )
    assert nueva.parser is not None
    monkeypatch.setitem(marcas.MARCAS, "solicitud", nueva)

    erp.append(
        {
            "content": f'{viejo} {{"estado": "pendiente"}}',
            "reference_doctype": "Sales Order",
            "reference_name": PEDIDO,
            "creation": "2026-08-01 10:00:00",
        }
    )
    assert marcas.leer("solicitud", PEDIDO) == {"estado": "pendiente"}
    assert marcas.leer_lote("solicitud", [PEDIDO]) == {PEDIDO: {"estado": "pendiente"}}


def test_el_parser_no_puede_separarse_del_texto_de_su_fila():
    """El parser se DERIVA de los textos de la fila, así que no puede quedar
    apuntando a otro literal.

    Antes se le pasaba el texto a mano (`parser=json_tras("[solicitud]")`), que
    es el mismo literal escrito dos veces en la misma fila: una fila con
    `texto="[a]"` y el parser armado sobre `"[b]"` no se la agarraba nadie.
    """
    for m in marcas.MARCAS.values():
        if m.parser is None:
            continue
        for t in m.textos:
            assert m.parser(f'{t} {{"x": 1}}') == {"x": 1}, (
                f"el parser de {m.nombre!r} no reconoce su propio texto {t!r}"
            )
