"""La auditoría final: con el idioma en inglés, ¿queda algo en español?

Este archivo es el guard. Los otros tests prueban una categoría cada uno; éste
recorre TODOS los constructores de mensajes migrados, los ejecuta en inglés, y
exige que lo que sale no tenga una palabra en español que no esté justificada
en tests/idioma_allowlist.py.

Dos auditorías, y hacen falta las dos:

  * la de EJECUCIÓN corre los constructores de verdad y mira el texto. Es la
    que encuentra el call site que nadie cableó — un test de catálogo pasa
    igual aunque el call site siga escribiendo el literal a mano.

  * la ESTÁTICA mira los puntos de salida (enviar_mensaje / enviar_botones /
    enviar_plantilla) y exige que ninguno reciba un literal en español escrito
    ahí mismo. Cubre los caminos que la de ejecución no alcanza.
"""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from idioma_allowlist import FILTRACIONES_EN_HANDOFF, PERMITIDO_EN_SALIDA_INGLESA
from idioma_captura import restos_en_espanol

from app import idioma

EN = idioma.EN
ES = idioma.ES

PEDIDO = "SAL-ORD-2026-00042"
MOTIVO = "no stock"
DATOS = ("Demo Bakery", "Whole Milk 1 L", "ARS", "manual", MOTIVO, "over the limit")

# Un resumen fijo para la auditoría de idiomas: sin reloj y sin red, así que
# las dos llamadas (una por idioma) dan exactamente el mismo texto.
_AUTONOMIA = {
    "dias": 7,
    "confirmaciones": {"total": 61, "solos": 0, "por_vos": 58, "acepto_el_cliente": 3,
                       "sin_fuente": 0, "truncado": False},
    "rechazos": {"total": 3, "truncado": False},
    "sombras": {"con_registro": 58, "pasan": 44, "frenados": 14,
                "postura": {"tope": 44}, "reglas": {"sin stock": 3},
                "truncado": False},
    "revisiones": None,
    "borradores": {"vivos": 12, "del_bot": 11, "a_mano": 1, "tope": 500,
                   "pasado": False, "pct": 2.4},
    "conteos": {"mirados": 6, "frescos": 5, "faltan": ["QUE-MUZ"]},
}

_SO = {
    "name": PEDIDO,
    "customer_name": "Demo Bakery",
    "grand_total": 6000,
    "currency": "ARS",
    "delivery_date": "2026-09-06",
    "items": [{"item_code": "MILK-1L", "item_name": "Whole Milk 1 L", "qty": 5,
               "uom": "Unit"}],
}


def _solicitud(**extra):
    from app import solicitudes

    campos = {
        "id": "SOL-1", "pedido": PEDIDO, "tipo": solicitudes.TIPO_ENTREGA,
        "estado": solicitudes.PENDIENTE, "cliente": "CUST-1",
        "cliente_nombre": "Demo Bakery", "resumen_items": "5 x Whole Milk 1 L",
        "total": 6000.0, "moneda": "ARS", "creada_en": 0.0,
        "vence_en": 6 * 3600.0, "sello": 0.0, "ofrecido": {"metodo": "entrega"},
    }
    campos.update(extra)
    return solicitudes.Solicitud(**campos)


def _todos_los_constructores(lengua):
    """(nombre, texto) de CADA mensaje determinista migrado, en `lengua`."""
    from app import autonomia, avisos, decisiones, main, notificar, pendientes, solicitudes

    sol = _solicitud(motivo=MOTIVO)
    salida = [
        # 1. aviso de avance, fallbacks y errores
        ("main.texto_progreso", main.texto_progreso(lengua)),
        ("main.texto_solo_texto", main.texto_solo_texto(lengua)),
        # Una por tipo: son las respuestas que más lee un cliente que manda
        # un audio, y cada una tiene su propio texto.
        *[
            (f"main.texto_solo_texto:{tipo}", main.texto_solo_texto(lengua, tipo))
            for tipo in ("audio", "image", "sticker", "video", "document",
                         "location", "contacts", "unknown")
        ],
        ("main.texto_error_tecnico", main.texto_error_tecnico(lengua)),
        ("main.texto_error_tecnico_avisado", main.texto_error_tecnico_avisado(lengua)),
        ("main.texto_respuesta_vacia", main.texto_respuesta_vacia(lengua)),
        # 2. estado del pedido
        ("avisos.texto_confirmacion_cliente",
         avisos.texto_confirmacion_cliente(_SO, lengua)),
        ("decisiones._texto_rechazo", decisiones._texto_rechazo(PEDIDO, MOTIVO, lengua)),
        ("decisiones._texto_cancelacion",
         decisiones._texto_cancelacion(PEDIDO, MOTIVO, lengua)),
        # La oferta de entrega del lado del cliente: `_sin_oferta` es el
        # constructor puro de ese camino (los otros escriben en ERPNext).
        ("solicitudes._sin_oferta", solicitudes._sin_oferta(PEDIDO, None, lengua)),
        ("solicitudes.terminos_texto",
         solicitudes.terminos_texto(
             {"metodo": "retiro", "fecha": "2026-09-08", "hora": "10:00",
              "cargo": 1500.0, "descuento_pct": 5}, "ARS", lengua)),
        ("solicitudes.texto_pendiente_cliente",
         solicitudes.texto_pendiente_cliente(sol, lengua)),
        # El borrador que espera a una persona (app/pendientes.py). El texto al
        # cliente NO lleva la edad del pedido a propósito: este audit llama cada
        # constructor una vez por idioma y compara, así que un dato que cambia
        # con el reloj lo rompería — y de paso el cliente no la necesita.
        ("pendientes.recordatorio_pendiente",
         pendientes.recordatorio_pendiente(PEDIDO, lengua)),
        ("pendientes.pendiente_cerrado",
         pendientes.pendiente_cerrado(PEDIDO, lengua)),
        ("pendientes.recordatorio_dueno",
         "\n".join(pendientes.recordatorio_dueno(3, f"· {PEDIDO} — 5 h", lengua))),
        ("pendientes.pendiente_cerrado_equipo",
         pendientes.pendiente_cerrado_equipo(PEDIDO, 48.0, lengua)),
        # El resumen de autonomía. Determinista: `resumen` es la parte que
        # lee ERPNext, `texto` es el constructor puro sobre sus datos.
        ("autonomia.texto", autonomia.texto(_AUTONOMIA, lengua)),
        # 3. avisos a la gerencia
        ("notificar._texto_libre(pendiente)",
         notificar._texto_libre(PEDIDO, _SO, False, "over the limit",
                                "5 x Whole Milk 1 L", lengua)),
        ("notificar._texto_libre(auto)",
         notificar._texto_libre(PEDIDO, _SO, True, "", "5 x Whole Milk 1 L", lengua)),
        ("notificar.texto_confirmacion",
         notificar.texto_confirmacion(_SO, "manual", "2026-09-05 16:14", lengua)),
        ("notificar.texto_falla_tecnica",
         "\n".join(notificar.texto_falla_tecnica(
             "5491100000000", "hi, do you have whole milk?", "OpenAIRateLimitError",
             lengua))),
        # 4. excepciones de entrega y vencimientos
        ("solicitudes.texto_oferta_cliente",
         solicitudes.texto_oferta_cliente(sol, lengua)),
        ("solicitudes.texto_rechazo_cliente",
         solicitudes.texto_rechazo_cliente(sol, lengua)),
        ("solicitudes.texto_vencida_cliente",
         solicitudes.texto_vencida_cliente(sol, lengua)),
        ("solicitudes.texto_respaldo_cliente",
         solicitudes.texto_respaldo_cliente(sol, lengua)),
        ("solicitudes.texto_revision_vencida_cliente",
         solicitudes.texto_revision_vencida_cliente(sol, lengua)),
        ("solicitudes.texto_respaldo_vencido_cliente",
         solicitudes.texto_respaldo_vencido_cliente(sol, lengua)),
    ]
    return salida


# ---------------------------------------------------- auditoría de ejecución


def _restos(nombre: str, texto: str) -> list[str]:
    """Los restos de ese constructor, menos los que están en handoff.

    El descuento es por CONSTRUCTOR y por PALABRA: una filtración anotada en
    `autonomia.texto` no permite nada en ningún otro constructor, y no permite
    ninguna otra palabra en ése. Ver la sección 10 de idioma_allowlist.py.
    """
    en_handoff = FILTRACIONES_EN_HANDOFF.get(nombre, {}).get("palabras", ())
    return [
        resto
        for resto in restos_en_espanol(texto, DATOS + PERMITIDO_EN_SALIDA_INGLESA)
        if resto not in en_handoff
    ]


def test_la_auditoria_final_no_encuentra_espanol_con_el_idioma_en_ingles():
    """LA prueba. Si esto falla, quedó un mensaje sin migrar."""
    sucios = {}
    for nombre, texto in _todos_los_constructores(EN):
        restos = _restos(nombre, texto)
        if restos:
            sucios[nombre] = restos
    assert sucios == {}, f"mensajes con español sin justificar: {sucios}"


def test_las_filtraciones_en_handoff_siguen_filtrando():
    """Cada entrada del handoff tiene que seguir HACIENDO FALTA.

    Es la fecha de vencimiento de la sección 10 del allowlist. Cuando la rama
    que tiene el archivo arregle la filtración, este test se cae y obliga a
    borrar la entrada. Sin esto, un permiso escrito «hasta que otro lo
    arregle» se queda para siempre y el audit vuelve a estar ciego en ese
    constructor, esta vez con la bendición de un comentario.

    La comparación es por igualdad y no por contención a propósito: si en ese
    constructor aparece una palabra NUEVA, el handoff no la cubre y esto
    también se cae.
    """
    textos = dict(_todos_los_constructores(EN))
    for nombre, entrada in FILTRACIONES_EN_HANDOFF.items():
        assert nombre in textos, (
            f"{nombre} está en el handoff pero ya no está en el registro de "
            "constructores: sacar el constructor del audit no es arreglar la "
            "filtración"
        )
        restos = set(restos_en_espanol(textos[nombre], DATOS + PERMITIDO_EN_SALIDA_INGLESA))
        esperadas = set(entrada["palabras"])
        assert restos == esperadas, (
            f"el handoff de {nombre} dice {sorted(esperadas)} y el detector "
            f"encuentra {sorted(restos)}. Si ya no filtra, borrar la entrada de "
            f"idioma_allowlist.py::FILTRACIONES_EN_HANDOFF ({entrada['issue']})"
        )


def test_cada_filtracion_en_handoff_dice_quien_la_arregla():
    """Un handoff sin motivo es el mismo bug que un detector con hueco.

    En los dos casos la próxima persona no puede distinguir una razón de una
    pereza. Así que el motivo, el archivo y el issue son obligatorios, y el
    motivo tiene que ser una explicación y no una etiqueta.
    """
    assert FILTRACIONES_EN_HANDOFF, (
        "si no queda ninguna filtración en handoff, borrar la sección 10 del "
        "allowlist y este test con ella"
    )
    for nombre, entrada in FILTRACIONES_EN_HANDOFF.items():
        assert entrada.get("palabras"), f"{nombre}: sin palabras, no permite nada"
        assert entrada.get("arregla", "").endswith(".py"), (
            f"{nombre}: falta el archivo que hay que tocar"
        )
        assert entrada.get("issue", "").startswith("https://"), (
            f"{nombre}: falta el issue donde se sigue"
        )
        motivo = entrada.get("motivo", "")
        assert len(motivo) > 120, f"{nombre}: el motivo tiene que explicar, no etiquetar"
        # El motivo tiene que decir DÓNDE, no sólo que existe.
        assert entrada["arregla"] in motivo, (
            f"{nombre}: el motivo no nombra el archivo que hay que tocar"
        )


def test_cada_constructor_dice_algo_distinto_en_cada_idioma():
    """Un texto idéntico en los dos idiomas es un texto sin traducir."""
    iguales = [
        nombre
        for (nombre, es), (_, en) in zip(
            _todos_los_constructores(ES), _todos_los_constructores(EN), strict=True
        )
        if es == en
    ]
    assert iguales == [], f"sin traducir: {iguales}"


def test_ningun_constructor_manda_los_dos_idiomas_a_la_vez():
    """La concatenación bilingüe era el parche viejo; no puede quedar ninguna."""
    for nombre, texto in _todos_los_constructores(EN):
        assert "Sobre tu pedido" not in texto, nombre
        assert "Hola!" not in texto, nombre
    for nombre, texto in _todos_los_constructores(ES):
        assert "About your order" not in texto, nombre
        assert "Hi!" not in texto, nombre


@pytest.mark.parametrize("dato", [PEDIDO, "2026-09-06", "Demo Bakery"])
def test_los_datos_sobreviven_identicos_a_los_dos_idiomas(dato):
    """Un número de pedido, una fecha y un nombre valen lo mismo en los dos."""
    for (nombre, es), (_, en) in zip(
        _todos_los_constructores(ES), _todos_los_constructores(EN), strict=True
    ):
        if dato in es:
            assert dato in en, f"{nombre} perdió {dato!r} al traducir"


# ------------------------------------------------------- auditoría estática

_SINKS = {"enviar_mensaje", "enviar_botones", "enviar_plantilla"}
_APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def _literales_en_sinks() -> list[tuple[str, int, str]]:
    """Literales en español pasados DIRECTO a un punto de salida."""
    hallados = []
    for archivo in sorted(_APP.rglob("*.py")):
        if "__pycache__" in str(archivo):
            continue
        arbol = ast.parse(archivo.read_text())
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            nombre = getattr(nodo.func, "attr", None) or getattr(nodo.func, "id", None)
            if nombre not in _SINKS:
                continue
            for arg in nodo.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    restos = restos_en_espanol(
                        arg.value, PERMITIDO_EN_SALIDA_INGLESA
                    )
                    if restos:
                        hallados.append(
                            (str(archivo.relative_to(_APP.parent)), nodo.lineno,
                             arg.value[:70])
                        )
    return hallados


def test_ningun_punto_de_salida_recibe_un_literal_en_espanol():
    """Nadie le pasa prosa escrita a mano a enviar_mensaje y compañía."""
    assert _literales_en_sinks() == []


def test_el_catalogo_esta_completo_en_los_dos_idiomas():
    assert idioma.claves_incompletas() == []


def test_el_catalogo_cubre_las_categorias_de_la_migracion():
    categorias = {clave.split(".")[0] for clave in idioma.CATALOGO}
    for esperada in ("ack", "fallback", "pedido", "gerencia", "entrega",
                     "codigo", "accion", "sistema", "stock", "precio", "idioma"):
        assert esperada in categorias, f"falta {esperada}"


def test_la_lista_de_intencionalmente_sin_traducir_esta_documentada():
    """El allowlist no puede ser una lista muda: cada grupo se explica."""
    import idioma_allowlist as permitido

    assert permitido.__doc__ and "no tiene idioma" in permitido.__doc__
    assert permitido.MARCAS_DURABLES
    assert permitido.ERPNEXT_CANONICO
    assert permitido.COMANDOS_ES
    # Los comandos en inglés se AGREGARON; los de siempre siguen.
    assert "acepto" in permitido.COMANDOS_ES
    assert "accept" in permitido.COMANDOS_EN_QUE_TAMBIEN_PARSEAN


# ----------------------------------------------- el detector, probado a mano
#
# Los tests de arriba usan el detector; éstos lo PRUEBAN. Hacen falta porque un
# detector que no se puede romper no está detectando: si mañana alguien saca
# `de` del conjunto o vuelve la comparación a substrings, algo tiene que
# morirse acá. Y tienen que ser tests de COMPORTAMIENTO — `assert "de" in
# _PALABRAS_ES` se muere con la mutación pero no prueba que el detector
# detecte nada.


# Texto que un cliente en inglés recibió de verdad, o casi. Cada uno murió sin
# `de` o sin `sin` en el conjunto, que es el bug que arregla este PR.
_FILTRA = [
    ("fresh counts: 5 de 6 · missing: QUE-MUZ", "de"),
    ("drafts competing for stock: 12 de 500", "de"),
    ("5 de 6", "de"),
    ("held back by rules: sin stock 3", "sin"),
    ("held back by rules: sin conteo de stock 2", "sin"),
]


@pytest.mark.parametrize("texto,palabra", _FILTRA)
def test_el_detector_agarra_las_dos_palabras_mas_comunes_del_idioma(texto, palabra):
    """`de` y `sin` son las que más se filtran, así que son las que más importan."""
    assert palabra in restos_en_espanol(texto, DATOS + PERMITIDO_EN_SALIDA_INGLESA)


# Lo contrario, y es la mitad que sostiene a la otra: con `de` y `sin` en el
# conjunto, un detector que compare substrings marca medio diccionario inglés y
# se vuelve inservible por ruido. Estos textos son el piso de esa propiedad.
_INGLES_LIMPIO = [
    "business as usual",
    "using a single order",
    "we decided to consider the wide side of the model",
    "order 5 of 6 · missing: QUE-MUZ",
    "insinuate, sincere, sine, index, inside, resin, basin",
    "Delivered to the server, see the overview",
    "Panaderia Lopez · SAL-ORD-2026-00042 · QUE-MUZ",
    "drafts competing for stock: 12 of 500",
]


@pytest.mark.parametrize("texto", _INGLES_LIMPIO)
def test_el_detector_no_marca_ingles_que_contiene_las_letras(texto):
    """Comparación por ficha entera: `business` no es `sin`, `order` no es `de`."""
    assert restos_en_espanol(texto, DATOS + PERMITIDO_EN_SALIDA_INGLESA) == []


# El recorte de `permitido` es la otra mitad de la tokenización, y era la que
# de verdad comparaba substrings. Con `ver` permitido como comando, recortarlo
# en cualquier parte de una palabra fabricaba fichas que nadie escribió:
# `versin` quedaba en `sin`, `paloverde` y `verde` en `de`, `delver` en `del`.
# Sobre un diccionario de 370.105 palabras inglesas eso son 17 falsos positivos
# que no existen recortando por ficha.
_PARTIDAS_POR_EL_RECORTE_VIEJO = ["versin", "paloverde", "verde", "delver", "estovers"]


@pytest.mark.parametrize("palabra", _PARTIDAS_POR_EL_RECORTE_VIEJO)
def test_el_recorte_de_lo_permitido_no_fabrica_fichas(palabra):
    """Recortar un permitido no puede partir en dos la palabra que lo contiene."""
    assert restos_en_espanol(palabra, DATOS + PERMITIDO_EN_SALIDA_INGLESA) == []


def test_lo_permitido_se_sigue_recortando_donde_tiene_que_recortarse():
    """El arreglo del recorte no puede haber apagado el recorte."""
    # Un comando en español dentro de un mensaje en inglés: permitido.
    assert restos_en_espanol("Reply confirmar to accept, rechazar to decline") == []
    # Una marca de auditoría: empieza con `[`, así que el ancla no se aplica.
    assert restos_en_espanol("[limite] · [confirmado-por-agente]") == []
    # Un estado canónico de ERPNext.
    assert restos_en_espanol("status: To Deliver and Bill") == []
    # Y un dato del test que viene en español de verdad.
    assert restos_en_espanol("Order for Panadería López", ("Panadería López",)) == []


def test_el_detector_sigue_agarrando_acentos_y_signos_de_apertura():
    """La otra pata del detector, que este PR no toca: que siga viva."""
    assert restos_en_espanol("¿Confirmás el pedido?") != []
    assert restos_en_espanol("Órdenes") != []
