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

# Los puntos de salida, y los ENVOLTORIOS que llegan a ellos. Los tres
# primeros son las funciones de `app/whatsapp.py` que hablan con Meta; los
# otros cinco son las que el resto de `app/` llama de verdad, y son las que
# reciben la prosa.
#
# Mirar sólo los tres de abajo era la razón por la que esta auditoría no
# auditaba nada: hay 18 llamadas a esos tres en todo `app/`, y NINGUNA recibe
# un literal — la prosa entra por los envoltorios, o ya viene resuelta del
# catálogo. Con esos tres solos, `test_ningun_punto_de_salida_recibe_un_literal_en_espanol`
# no podía fallar nunca, y el registro escrito a mano de
# `_todos_los_constructores` quedaba como el único audit vivo.
_SINKS = {
    # Las tres puertas reales a Meta.
    "enviar_mensaje",
    "enviar_botones",
    "enviar_plantilla",
    # Los envoltorios por los que entra la prosa. `app/notificar.py` y
    # `app/avisos.py` los exponen, y el resto de la app llama a éstos.
    "pedir_confirmacion_conteo",
    "pedir_codigo_de_ajuste",
    "encolar_equipo",
    "alertar_excepcion",
    "avisar_escalamiento",
}
_APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def _texto_del_nodo(nodo: ast.AST) -> str | None:
    """El texto de un literal, incluidas las f-strings.

    Una f-string es un `ast.JoinedStr` y no un `ast.Constant`, así que mirar
    sólo constantes dejaba afuera justo la forma en que se escribe la prosa
    interpolada — que es casi toda. De la f-string se juntan sus partes
    constantes: los `{...}` son DATOS, y un dato no tiene idioma.
    """
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
        return nodo.value
    if isinstance(nodo, ast.JoinedStr):
        partes = [
            t.value
            for t in nodo.values
            if isinstance(t, ast.Constant) and isinstance(t.value, str)
        ]
        return " ".join(partes) if partes else None
    return None


def _literales_en_sinks() -> list[tuple[str, int, str]]:
    """Literales en español pasados DIRECTO a un punto de salida.

    Mira los argumentos posicionales Y los nombrados, y las f-strings además
    de las constantes. Las cuatro cosas hacen falta: `enviar_plantilla` recibe
    el cuerpo por `texto=`, y la prosa interpolada se escribe con f-strings.
    """
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
            candidatos = list(nodo.args) + [kw.value for kw in nodo.keywords]
            for arg in candidatos:
                texto = _texto_del_nodo(arg)
                if texto is None:
                    continue
                if restos_en_espanol(texto, PERMITIDO_EN_SALIDA_INGLESA):
                    hallados.append(
                        (str(archivo.relative_to(_APP.parent)), nodo.lineno, texto[:70])
                    )
    return hallados


def _alcance_de_la_auditoria_estatica() -> tuple[int, int]:
    """(llamadas a un sink, textos que se inspeccionaron de verdad)."""
    llamadas = textos = 0
    for archivo in sorted(_APP.rglob("*.py")):
        if "__pycache__" in str(archivo):
            continue
        for nodo in ast.walk(ast.parse(archivo.read_text())):
            if not isinstance(nodo, ast.Call):
                continue
            nombre = getattr(nodo.func, "attr", None) or getattr(nodo.func, "id", None)
            if nombre not in _SINKS:
                continue
            llamadas += 1
            for arg in list(nodo.args) + [kw.value for kw in nodo.keywords]:
                if _texto_del_nodo(arg) is not None:
                    textos += 1
    return llamadas, textos


def test_la_auditoria_estatica_inspecciona_algo():
    """El guard del guard: una auditoría vacía no es una auditoría limpia.

    Mirando sólo `enviar_mensaje`/`enviar_botones`/`enviar_plantilla` y sólo
    `ast.Constant`, esta auditoría encontraba 18 llamadas y **0** textos, así
    que `test_ningun_punto_de_salida_recibe_un_literal_en_espanol` no podía
    fallar nunca — pasaba por vacía, y su docstring prometía cubrir «los
    caminos que la de ejecución no alcanza». El registro escrito a mano de
    `_todos_los_constructores` quedaba como el único audit vivo, y por eso una
    filtración en `app/tools/captura.py` vivía en el árbol con la suite verde.

    Un cero acá no es una buena noticia: es la auditoría avisando que no está
    mirando. Por eso el número es una aserción y no un comentario.
    """
    llamadas, textos = _alcance_de_la_auditoria_estatica()
    assert llamadas >= 18, f"se perdieron puntos de salida: {llamadas}"
    assert textos > 0, (
        "la auditoría estática no inspeccionó NINGÚN texto: mira los "
        "envoltorios correctos y las f-strings, o no está auditando nada"
    )


def test_ningun_punto_de_salida_recibe_un_literal_en_espanol():
    """Nadie le pasa prosa escrita a mano a enviar_mensaje y compañía."""
    assert _literales_en_sinks() == []


# --------------------------------------- los títulos de los botones

# LA ASERCIÓN QUE FALTABA, y por qué la que ya estaba no alcanzaba.
#
# `_literales_en_sinks` mira los ARGUMENTOS de `enviar_botones`, y el título de
# un botón no es un argumento: viaja adentro de una lista de diccionarios. Así
# que `{"id": ..., "title": "Confirmar"}` pasaba por el punto de salida sin que
# ninguna de las dos auditorías lo tocara —la estática no lo veía y la de
# ejecución no corre `notificar_equipo`— y el dueño con IDIOMA_GERENCIA=en
# recibía un aviso en inglés con botones en español.
#
# Tampoco estaba en tests/idioma_allowlist.py, que es donde habría que haberlo
# defendido si fuera a propósito. No lo era: la regla del allowlist es «se
# traduce lo que LEE UNA PERSONA en WhatsApp», y un botón es literalmente eso.
#
# Se miran TODOS los diccionarios de `app/`, no sólo los que están escritos
# dentro de una llamada a `enviar_botones`: una lista de botones armada tres
# líneas antes y pasada por variable es la misma filtración, y es la forma en
# que se escribe apenas hay una condición de por medio.
def _titulos_literales() -> list[tuple[str, int, str]]:
    """Títulos de botón escritos como literal en `app/`. Deben ser cero."""
    hallados = []
    for archivo in sorted(_APP.rglob("*.py")):
        if "__pycache__" in str(archivo):
            continue
        for nodo in ast.walk(ast.parse(archivo.read_text())):
            if not isinstance(nodo, ast.Dict):
                continue
            for clave, valor in zip(nodo.keys, nodo.values, strict=True):
                if not (isinstance(clave, ast.Constant) and clave.value == "title"):
                    continue
                texto = _texto_del_nodo(valor)
                if texto is not None:
                    hallados.append(
                        (str(archivo.relative_to(_APP.parent)), nodo.lineno, texto)
                    )
    return hallados


def _titulos_inspeccionados() -> int:
    """Cuántos títulos de botón existen en `app/`, literales o no."""
    total = 0
    for archivo in sorted(_APP.rglob("*.py")):
        if "__pycache__" in str(archivo):
            continue
        for nodo in ast.walk(ast.parse(archivo.read_text())):
            if isinstance(nodo, ast.Dict):
                total += sum(
                    1
                    for clave in nodo.keys
                    if isinstance(clave, ast.Constant) and clave.value == "title"
                )
    return total


def test_ningun_titulo_de_boton_es_un_literal():
    """Un botón lo lee una persona, así que sale del catálogo y no del código."""
    assert _titulos_literales() == []


def test_la_auditoria_de_botones_encuentra_los_botones():
    """El guard del guard: si no ve ningún título, no está auditando nada.

    Son cuatro: los tres del producto (`ok:`, `ver:` y `conteo:`) y el que
    `app/whatsapp.py` arma para Meta con el valor que le pasaron.
    """
    assert _titulos_inspeccionados() >= 4


def test_las_etiquetas_de_boton_estan_en_el_catalogo_en_los_dos_idiomas():
    """Y son las que el producto manda: el test de arriba prueba que no hay
    literales, éste que existe algo que ponerles en su lugar."""
    for clave in ("boton.confirmar", "boton.ver_detalle", "boton.confirmar_conteo"):
        assert clave in idioma.CATALOGO, clave
        assert idioma.t(clave, ES) != idioma.t(clave, EN), clave


def test_el_catalogo_esta_completo_en_los_dos_idiomas():
    assert idioma.claves_incompletas() == []


def test_el_catalogo_cubre_las_categorias_de_la_migracion():
    categorias = {clave.split(".")[0] for clave in idioma.CATALOGO}
    for esperada in ("ack", "fallback", "pedido", "gerencia", "entrega",
                     "codigo", "accion", "sistema", "stock", "precio", "idioma",
                     "boton", "dia"):
        assert esperada in categorias, f"falta {esperada}"


def test_la_lista_de_intencionalmente_sin_traducir_esta_documentada():
    """El allowlist no puede ser una lista muda: cada grupo se explica."""
    import idioma_allowlist as permitido

    assert permitido.__doc__ and "no tiene idioma" in permitido.__doc__
    # NO `assert permitido.MARCAS_DURABLES`, que era lo que decía: eso sólo
    # pedía que la tupla no estuviera vacía, así que pasaba con las cuatro de
    # doce que listaba y habría pasado igual con una sola. Ahora sale del
    # registro y se afirma que están TODAS.
    from app import marcas

    assert set(permitido.MARCAS_DURABLES) == {m.texto for m in marcas.MARCAS.values()}
    assert len(permitido.MARCAS_DURABLES) == 12
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


# El catálogo inglés usa guillemets y `<>` de verdad: `gerencia.pendientes_cuerpo`
# dice `Reply «confirmar <order>», «rechazar <order>»`. Así que la falla más
# probable de una migración a medias —traducir la prosa y dejar el nombre del
# placeholder en español— produce una ficha `<pedido>`, y ésa no se reducía a
# `pedido`. Lo mismo tapaba justo lo que este PR vino a poder ver.
_ENVUELTO_EN_SIGNOS = [
    ("The rule «sin stock» blocked 3 orders.", "sin"),
    ("fresh counts: «5 de 6»", "de"),
    ("Reply «confirmar <pedido>», «rechazar <pedido>».", "pedido"),
    ("placeholder <motivo> never got migrated", "motivo"),
    ("Blocked by «tope del pedido».", "del"),
    # Los signos de apertura eran el peor caso: son los más españoles que hay
    # y aun así escondían la palabra que envolvían. El texto quedaba marcado
    # por el acento —`¿` está en `_ACENTOS`— así que no era ceguera, pero la
    # lista de palabras mentía: `¿sin conteo de stock?` reportaba el `de` del
    # medio y no el `sin` pegado al signo.
    ("¿sin stock?", "sin"),
    ("¡de!", "de"),
    ("¿sin conteo de stock?", "sin"),
]


@pytest.mark.parametrize("texto,palabra", _ENVUELTO_EN_SIGNOS)
def test_los_signos_de_los_bordes_no_esconden_la_palabra(texto, palabra):
    """Una palabra entre guillemets o entre `<>` sigue siendo esa palabra."""
    assert palabra in restos_en_espanol(texto, DATOS + PERMITIDO_EN_SALIDA_INGLESA)


def test_las_llaves_de_los_placeholders_no_se_recortan():
    """Un nombre de placeholder no es prosa, y no tiene idioma.

    `{}` es sintaxis de `str.format` y los nombres que van adentro son
    argumentos de Python, deliberadamente en español en las DOS versiones: la
    plantilla inglesa de `pedido.confirmado_cliente` dice
    `Order {pedido} confirmed`. Si `_BORDES` recortara las llaves, `{pedido}`
    daría la ficha `pedido` y el audit marcaría 41 de las 129 claves inglesas
    del catálogo — ruido puro, que es lo que vuelve inservible a un audit.

    Es la contracara del arreglo de `«»` y `<>`: ahí el signo escondía una
    palabra que SÍ era prosa. Acá el signo dice que lo de adentro no lo es.
    Este test es lo que impide que alguien «complete» el conjunto de signos.
    """
    assert restos_en_espanol("Order {pedido} confirmed") == []
    assert restos_en_espanol("Delivery: {entrega} · waiting {horas}") == []
    # Y el catálogo inglés entero, que es de donde salen esas plantillas.
    sucias = {
        clave
        for clave, valores in idioma.CATALOGO.items()
        for texto in (
            valores[EN] if isinstance(valores.get(EN), (list, tuple)) else [valores.get(EN)]
        )
        if texto and restos_en_espanol(texto, PERMITIDO_EN_SALIDA_INGLESA)
    }
    assert sucias == set(), f"el catálogo inglés no puede filtrar: {sorted(sucias)}"


def test_lo_permitido_no_se_recorta_en_el_medio_de_una_palabra():
    """Las anclas van SIEMPRE, incluso en lo que no empieza con letra.

    Sin ancla, las cuatro marcas de auditoría volvían a ser recortes por
    substring y fabricaban fichas: `nothing[limite]confirma` quedaba en
    `nothing confirma` y el detector marcaba `confirma`.
    """
    assert restos_en_espanol("nothing[limite]confirma") == []
    assert restos_en_espanol("audit[entrega]pedidos here") == []
    # Y donde la marca aparece de verdad, se recorta igual.
    assert restos_en_espanol("[limite] · [confirmado-por-agente]") == []
    assert restos_en_espanol("note [limite] here") == []


def test_un_permitido_vacio_no_deja_ciego_al_detector():
    """Recortar la cadena vacía partía el texto entre CADA letra.

    Y un texto en fichas de un caracter no tiene ninguna palabra, así que el
    detector devolvía «limpio» sobre cualquier cosa. Latente —hoy ningún test
    pasa un permitido vacío— pero es la forma de ceguera más fácil de
    introducir sin querer, porque basta con un dato que resultó ser "".
    """
    assert "de" in restos_en_espanol("5 de 6", ("",))
    assert "sin" in restos_en_espanol("sin stock 3", ("", "", ""))


def test_el_detector_sigue_agarrando_acentos_y_signos_de_apertura():
    """La otra pata del detector, que este PR no toca: que siga viva."""
    assert restos_en_espanol("¿Confirmás el pedido?") != []
    assert restos_en_espanol("Órdenes") != []


def test_el_orden_de_lo_permitido_no_cambia_el_resultado():
    """Cada recorte muta el texto, así que el orden podía destruir un match.

    `notificar.texto_falla_tecnica` está en el registro y cita el mensaje del
    cliente tal cual, así que la forma es real: un test que pase esa cita como
    permitido la pone DESPUÉS del allowlist, que ya trae `confirmar`. Con el
    recorte en orden de lista, `confirmar` se comía un pedazo de la cita, el
    resto quedaba suelto y el detector marcaba `['pedido', 'sin']` sobre un
    texto enteramente permitido.
    """
    texto = "Message:\n> tengo un pedido sin confirmar\nError: RateLimit"
    cita = "tengo un pedido sin confirmar"
    for orden in (("confirmar", cita), (cita, "confirmar")):
        assert restos_en_espanol(texto, orden) == [], orden
    # Lo mismo entre dos permitidos donde uno contiene al otro.
    for orden in (("To Deliver", "To Deliver and Bill"),
                  ("To Deliver and Bill", "To Deliver")):
        assert restos_en_espanol("status: To Deliver and Bill", orden) == [], orden


def test_lo_permitido_se_recorta_aunque_el_mensaje_lo_haya_re_capitalizado():
    """Los constructores hacen `detalle.capitalize()` sobre lo que interpolan.

    Así que un dato permitido en minúscula puede llegar al texto en mayúscula
    (app/decisiones.py y app/solicitudes.py lo hacen en seis lugares). Si el
    recorte fuera sensible a mayúsculas, ese dato quedaría sin recortar y un
    nombre o motivo legítimamente español daría un rojo falso.
    """
    assert restos_en_espanol("Held back: Sin stock 3", ("sin stock",)) == []
    assert restos_en_espanol("Reason: No hay stock.", ("no hay stock",)) == []
    # Y ampliar el recorte no puede tapar un resto: sin el permitido, se marca.
    assert "sin" in restos_en_espanol("Held back: Sin stock 3")
