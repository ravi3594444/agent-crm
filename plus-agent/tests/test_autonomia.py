"""Los números de autonomía, contados sobre hechos durables.

EL DOBLE ES EL COMPARTIDO, A PROPÓSITO
Todo lo que hace este módulo es leer comentarios POR MARCADOR, así que un doble
que ignore el filtro `content like` devolvería todos los comentarios a todas
las consultas y CADA número pasaría por la razón equivocada — con el agravante
de que el fallo se vería como código correcto. `tests/fakes.py::listar` /
`_coincide` ya implementan `like` bien (`str(esperado).replace("%","") in
str(valor)`), así que se usa ése y no se escribe uno nuevo.

Una imprecisión conocida del doble compartido, que estos tests NO usan como
apoyo: `like` borra todos los `%`, así que no distingue un patrón anclado de
uno sin anclar. Ningún marcador de acá se separa de otro por prefijo.

`_coincide` tampoco implementa `>=`, así que el filtro de fecha de la consulta
se ignora y el doble devuelve la ventana entera. Eso es deliberado: el módulo
vuelve a filtrar por fecha en Python, y el test de la ventana prueba ESE
filtro, que es el que decide el número tanto acá como en producción.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import autonomia, confirmacion, erpnext, inventario, policy, sombra
from tests.fakes import listar

AHORA = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
PO_AGENTE = "WA-" + "0123456789abcdef" * 2 + "01234567"


def _sello(dias_atras: float = 0) -> str:
    return (AHORA - timedelta(days=dias_atras)).strftime("%Y-%m-%d %H:%M:%S")


def _comentario(pedido: str, contenido: str, dias_atras: float = 0) -> dict:
    """Un comentario como el que devuelve ERPNext.

    `reference_doctype` va SIEMPRE: el módulo filtra por él, y el doble
    compartido hace cumplir ese filtro. Sin el campo, `_coincide` compara
    None contra "Sales Order" y no devuelve nada — que es justamente el tipo
    de omisión que un doble escrito a mano dejaría pasar.
    """
    return {
        "content": contenido,
        "reference_doctype": "Sales Order",
        "reference_name": pedido,
        "creation": _sello(dias_atras),
        "name": f"c-{pedido}-{dias_atras}",
    }


@pytest.fixture
def mundo(monkeypatch: pytest.MonkeyPatch) -> dict:
    """ERPNext de mentira sobre el doble COMPARTIDO (fakes.listar)."""
    comentarios: list[dict] = []
    borradores: list[dict] = []
    renglones: list[dict] = []
    frescos: set[str] = set()
    caidas: set[str] = set()

    def policy_get_list(doctype, filters=None, fields=None, limit=20, **kwargs):
        if doctype in caidas:
            raise erpnext.ERPNextError(f"ERPNext no contesta para {doctype}")
        fuente = {
            "Comment": comentarios,
            "Sales Order": borradores,
            "Sales Order Item": renglones,
        }.get(doctype, [])
        return listar(
            fuente,
            filters,
            limit=limit,
            order_by=kwargs.get("order_by"),
            start=kwargs.get("start", 0),
        )

    monkeypatch.setattr(erpnext, "policy_get_list", policy_get_list)
    monkeypatch.setattr(
        inventario,
        "confiable",
        lambda code, dep: (code in frescos, "" if code in frescos else f"nadie contó {code}"),
    )
    monkeypatch.setattr(autonomia, "_desde", lambda dias: AHORA - timedelta(days=dias))
    return {
        "comentarios": comentarios,
        "borradores": borradores,
        "renglones": renglones,
        "frescos": frescos,
        "caidas": caidas,
    }


def _borrador(nombre: str, po_no: str = PO_AGENTE) -> dict:
    return {
        "name": nombre,
        "customer": "CUST-0007",
        "customer_name": "Panadería López",
        "grand_total": 8450.0,
        "creation": _sello(0.1),
        "docstatus": 0,
        "status": "Draft",
        "po_no": po_no,
    }


# ------------------------------------------------------ la tabla de cubetas


@pytest.mark.parametrize(
    "motivo,esperado",
    [
        # Los motivos REALES de app/policy.py, con su interpolación.
        ("auto-confirmación desactivada", "auto-confirmación apagada"),
        ("límites sin verificar: no pude leer los límites configurados", "límites ilegibles"),
        ("inventario no marcado como confiable", "inventario apagado"),
        ("monto $8.450 supera el tope de $0", "tope del pedido"),
        ("cliente con solo 1 pedidos confirmados", "cliente nuevo"),
        ("cliente nuevo: $9.000 supera su tope de $5.000", "cliente nuevo"),
        ("pedido $30.000 supera 2x su promedio", "muy por encima de su promedio"),
        ("no se pudo verificar el historial", "historial ilegible"),
        ("tiene $12.000 vencidos", "deuda vencida"),
        ("nadie confirmó un conteo de QUE-MUZ", "sin conteo de stock"),
        ("stock insuficiente de LEC-ENT-1L", "sin stock"),
        ("entrega a revisar: Av. Colón 1234 — fuera de zona", "zona de entrega"),
        ("fecha de entrega vencida", "fecha de entrega"),
        ("12 de QUE-MUZ supera el máximo de 10 por producto", "cantidad por producto"),
        ("descuento general no autorizado", "descuento"),
        ("precio fuera de lista en QUE-MUZ", "lista o moneda"),
        ("pedido sin productos", "pedido incompleto"),
        # Y lo que no está en la tabla tiene que VERSE, no desaparecer.
        ("se cayó un meteorito sobre el depósito", autonomia.OTROS),
        ("", autonomia.OTROS),
        (None, autonomia.OTROS),
    ],
)
def test_every_real_policy_reason_lands_in_a_named_bucket(motivo, esperado) -> None:
    assert autonomia.grupo(motivo) == esperado


def test_an_unbucketed_reason_is_counted_not_dropped(mundo) -> None:
    """«otros» con su cuenta es la señal de que falta una fila en la tabla."""
    mundo["comentarios"].append(
        _comentario("SO-1", f"{sombra.MARCA} " + _json_sombra(False, ["un motivo nuevo"]))
    )

    datos = autonomia.sombras()

    assert datos["reglas"] == {autonomia.OTROS: 1}


def _json_sombra(pasa: bool, reglas: list[str], postura: list[str] | None = None) -> str:
    import json

    return json.dumps(
        {
            "pasa_reglas": pasa,
            "motivos_reglas": reglas,
            "motivos_postura": postura if postura is not None else ["tope"],
            "total": 8450.0,
            "habitual": None,
            "tope_vigente": 0.0,
            "ts": _sello(0),
        },
        ensure_ascii=False,
    )


# ----------------------------------------------------- quién confirmó qué


def test_the_three_confirmation_sources_are_counted_apart(mundo) -> None:
    """`fuente=` se escribía desde el principio y nadie lo leía de vuelta."""
    mundo["comentarios"].extend(
        [
            _comentario("SO-1", f"{confirmacion.MARCA} {_sello(1)} fuente=automática (política)"),
            _comentario("SO-2", f"{confirmacion.MARCA} {_sello(1)} fuente=manual (confirmación humana, 549351)"),
            _comentario("SO-3", f"{confirmacion.MARCA} {_sello(1)} fuente=solicitud SOL-9 aceptada por el cliente"),
            _comentario("SO-4", f"{confirmacion.MARCA} {_sello(1)} fuente="),
        ]
    )

    cuenta = autonomia.confirmaciones()

    assert cuenta["solos"] == 1
    assert cuenta["por_vos"] == 1
    assert cuenta["acepto_el_cliente"] == 1
    assert cuenta["sin_fuente"] == 1
    assert cuenta["total"] == 4


def test_an_order_with_two_marks_counts_once_and_the_oldest_wins(mundo) -> None:
    """Mismo criterio que confirmacion._desde_erpnext: la primera manda."""
    mundo["comentarios"].extend(
        [
            _comentario("SO-1", f"{confirmacion.MARCA} x fuente=automática (política)", 3),
            _comentario("SO-1", f"{confirmacion.MARCA} x fuente=manual (humana)", 1),
        ]
    )

    cuenta = autonomia.confirmaciones()

    assert cuenta["total"] == 1
    assert (cuenta["solos"], cuenta["por_vos"]) == (1, 0)


def test_confirmations_outside_the_window_are_not_counted(mundo) -> None:
    """Prueba el filtro de fecha en Python: el doble compartido no hace `>=`."""
    mundo["comentarios"].extend(
        [
            _comentario("SO-DENTRO", f"{confirmacion.MARCA} x fuente=automática (política)", 2),
            _comentario("SO-FUERA", f"{confirmacion.MARCA} x fuente=automática (política)", 30),
        ]
    )

    assert autonomia.confirmaciones(dias=7)["total"] == 1
    assert autonomia.confirmaciones(dias=60)["total"] == 2


def test_a_marker_query_does_not_pick_up_another_marker(mundo) -> None:
    """La razón por la que este archivo usa el doble compartido."""
    mundo["comentarios"].extend(
        [
            _comentario("SO-1", f"{confirmacion.MARCA} x fuente=automática (política)"),
            _comentario("SO-1", f"{sombra.MARCA} " + _json_sombra(True, [])),
            _comentario("SO-1", f"{autonomia.MARCA_REVISION} monto supera el tope de $0"),
            _comentario("SO-1", f"{autonomia.MARCA_RECHAZO} un integrante autorizado"),
        ]
    )

    assert autonomia.confirmaciones()["total"] == 1
    assert autonomia.sombras()["con_registro"] == 1
    assert autonomia.revisiones()["pedidos"] == 1
    assert autonomia.rechazos()["total"] == 1


def test_unreadable_confirmations_are_none_not_zero(mundo) -> None:
    """Informar una autonomía de cero que nadie midió hace bajar un límite."""
    mundo["caidas"].add("Comment")

    assert autonomia.confirmaciones() is None
    assert autonomia.rechazos() is None
    assert autonomia.sombras() is None
    assert autonomia.revisiones() is None


# ------------------------------------------------------------- la sombra


def test_shadow_counts_passes_posture_and_rules(mundo) -> None:
    mundo["comentarios"].extend(
        [
            _comentario("SO-1", f"{sombra.MARCA} " + _json_sombra(True, [], ["tope", "stock apagado"])),
            _comentario("SO-2", f"{sombra.MARCA} " + _json_sombra(True, [], ["tope"])),
            _comentario("SO-3", f"{sombra.MARCA} " + _json_sombra(False, ["stock insuficiente de X"], ["tope"])),
        ]
    )

    datos = autonomia.sombras()

    assert datos["con_registro"] == 3
    assert datos["pasan"] == 2
    assert datos["frenados"] == 1
    assert datos["postura"] == {"tope": 3, "stock apagado": 1}
    assert datos["reglas"] == {"sin stock": 1}


def test_one_order_counts_once_per_bucket_however_many_lines_failed(mundo) -> None:
    """Tres renglones sin stock son UN pedido frenado, no tres."""
    mundo["comentarios"].append(
        _comentario(
            "SO-1",
            f"{sombra.MARCA} "
            + _json_sombra(
                False,
                [
                    "stock insuficiente de A",
                    "stock insuficiente de B",
                    "stock insuficiente de C",
                ],
            ),
        )
    )

    assert autonomia.sombras()["reglas"] == {"sin stock": 1}


def test_the_newest_shadow_record_wins(mundo) -> None:
    mundo["comentarios"].extend(
        [
            _comentario("SO-1", f"{sombra.MARCA} " + _json_sombra(False, ["stock insuficiente de X"]), 3),
            _comentario("SO-1", f"{sombra.MARCA} " + _json_sombra(True, []), 1),
        ]
    )

    datos = autonomia.sombras()

    assert datos["con_registro"] == 1
    assert datos["pasan"] == 1


# --------------------------------------------- el techo de borradores (W3)


def test_the_live_draft_count_is_counted_not_inferred(mundo) -> None:
    """Se cuenta contando.

    Los motivos de `evaluar` son deliberadamente uniformes —«no se pudo
    verificar stock de X» dice lo mismo para un techo lleno que para una caída
    de ERPNext— así que sacar este número de ahí sería adivinarlo.
    """
    mundo["borradores"].extend(
        [_borrador("SO-BOT-1"), _borrador("SO-BOT-2"), _borrador("SO-A-MANO", po_no="OC-4471")]
    )

    datos = autonomia.borradores_vivos()

    assert datos["vivos"] == 3
    assert datos["del_bot"] == 2
    assert datos["a_mano"] == 1
    assert datos["tope"] == policy.MAX_BORRADORES
    assert datos["pasado"] is False


def test_hand_entered_drafts_count_toward_the_same_cap(mundo) -> None:
    """`_borradores_que_reservan` filtra por docstatus y status, no por origen."""
    mundo["borradores"].extend(
        [_borrador(f"SO-A-MANO-{i}", po_no="OC-1") for i in range(5)]
    )

    assert autonomia.borradores_vivos()["vivos"] == 5


def test_past_the_cap_is_reported_as_past(mundo, monkeypatch) -> None:
    monkeypatch.setattr(policy, "MAX_BORRADORES", 3)
    mundo["borradores"].extend([_borrador(f"SO-{i}") for i in range(4)])

    datos = autonomia.borradores_vivos()

    assert datos["pasado"] is True
    assert datos["pct"] > 100


def test_exactly_at_the_cap_is_not_past_it(mundo, monkeypatch) -> None:
    """El límite es `>`, igual que en policy: 500 de 500 todavía funciona."""
    monkeypatch.setattr(policy, "MAX_BORRADORES", 3)
    mundo["borradores"].extend([_borrador(f"SO-{i}") for i in range(3)])

    datos = autonomia.borradores_vivos()

    assert datos["pasado"] is False
    assert datos["pct"] == 100.0


def test_an_unreadable_count_is_none(mundo) -> None:
    mundo["caidas"].add("Sales Order")

    assert autonomia.borradores_vivos() is None


# ------------------------------------------------------------- los conteos


def test_key_products_are_the_ones_actually_ordered(mundo) -> None:
    """No hay lista de «productos clave» configurada: la define lo que se vendió."""
    mundo["renglones"].extend(
        [
            {"item_code": "LEC-ENT-1L", "warehouse": "Dep", "creation": _sello(1)},
            {"item_code": "QUE-MUZ", "warehouse": "Dep", "creation": _sello(1)},
            {"item_code": "LEC-ENT-1L", "warehouse": "Dep", "creation": _sello(2)},
        ]
    )
    mundo["frescos"].add("LEC-ENT-1L")

    datos = autonomia.conteos()

    assert datos["mirados"] == 2  # sin duplicar
    assert datos["frescos"] == 1
    assert datos["faltan"] == ["QUE-MUZ"]


def test_no_orders_means_no_missing_counts(mundo) -> None:
    """Un producto que nadie pidió no es un conteo que falte."""
    assert autonomia.conteos() == {"mirados": 0, "frescos": 0, "faltan": []}


# --------------------------------------------------- el resumen y su texto


def test_each_section_of_the_summary_fails_alone(mundo) -> None:
    mundo["borradores"].append(_borrador("SO-1"))
    mundo["caidas"].add("Comment")

    datos = autonomia.resumen()

    assert datos["confirmaciones"] is None
    assert datos["sombras"] is None
    assert datos["borradores"] is not None  # esta sí se pudo leer


def test_the_text_says_it_could_not_read_instead_of_zero(mundo) -> None:
    mundo["caidas"].add("Comment")

    texto = autonomia.texto(autonomia.resumen(), "es")

    assert "no pude leer" in texto
    assert "confirmados: 0" not in texto


def test_the_text_carries_the_real_numbers(mundo) -> None:
    mundo["comentarios"].extend(
        [
            _comentario("SO-1", f"{confirmacion.MARCA} x fuente=automática (política)"),
            _comentario("SO-2", f"{sombra.MARCA} " + _json_sombra(True, [])),
        ]
    )
    mundo["borradores"].append(_borrador("SO-3"))

    texto = autonomia.texto(autonomia.resumen(), "es")

    assert "solos: 1" in texto
    assert f"de {policy.MAX_BORRADORES}" in texto


@pytest.mark.parametrize("lengua", ["es", "en"])
def test_the_text_is_built_in_both_languages(mundo, lengua) -> None:
    texto = autonomia.texto(autonomia.resumen(), lengua)

    assert texto.strip()
    assert "{" not in texto  # ningún parámetro sin interpolar


# ------------------------------------------------------------- la escalera


def test_the_level_is_launch_until_something_is_armed(mundo, monkeypatch) -> None:
    monkeypatch.setattr(inventario, "maestra_encendida", lambda: False)

    class Cfg:
        tope = 0.0

    assert autonomia.nivel(Cfg(), {"sombras": None})["nivel"] == autonomia.NIVELES[0]


def test_shadow_records_move_it_to_level_one(mundo, monkeypatch) -> None:
    monkeypatch.setattr(inventario, "maestra_encendida", lambda: False)

    class Cfg:
        tope = 0.0

    datos = {"sombras": {"con_registro": 12, "pasan": 9}}

    assert autonomia.nivel(Cfg(), datos)["nivel"] == autonomia.NIVELES[1]


def test_a_ceiling_without_trusted_stock_is_still_launch_posture(
    mundo, monkeypatch
) -> None:
    """Un tope puesto con el stock apagado no auto-confirma nada."""
    monkeypatch.setattr(inventario, "maestra_encendida", lambda: False)

    class Cfg:
        tope = 25_000.0

    assert autonomia.nivel(Cfg(), {"sombras": None})["nivel"] == autonomia.NIVELES[0]


def test_a_ceiling_with_trusted_stock_is_level_two(mundo, monkeypatch) -> None:
    monkeypatch.setattr(inventario, "maestra_encendida", lambda: True)

    class Cfg:
        tope = 25_000.0

    assert autonomia.nivel(Cfg(), {"sombras": None})["nivel"] == autonomia.NIVELES[2]


def test_the_level_never_recommends_a_change(mundo, monkeypatch) -> None:
    """Decir «subilo a 25 mil» es una frase del dueño, no del sistema."""
    monkeypatch.setattr(inventario, "maestra_encendida", lambda: True)

    class Cfg:
        tope = 25_000.0

    devuelto = autonomia.nivel(Cfg(), {"sombras": None})

    assert set(devuelto) == {
        "nivel",
        "tope",
        "stock_confiable",
        "sombra_encendida",
        "habrian_pasado",
        "con_registro",
    }


# ------------------------------- el techo, donde el dueño lo puede ver (W3)


def test_the_digest_section_warns_above_eighty_percent(mundo, monkeypatch) -> None:
    from app import digest

    monkeypatch.setattr(policy, "MAX_BORRADORES", 10)
    mundo["borradores"].extend([_borrador(f"SO-{i}") for i in range(9)])

    texto = digest.seccion_autonomia()

    assert "9 de 10" in texto
    assert "90 %" in texto
    assert "ningún producto" in texto


def test_the_digest_section_is_quiet_below_the_threshold(mundo, monkeypatch) -> None:
    from app import digest

    monkeypatch.setattr(policy, "MAX_BORRADORES", 10)
    mundo["borradores"].extend([_borrador(f"SO-{i}") for i in range(3)])

    texto = digest.seccion_autonomia()

    assert "borradores compitiendo por stock: 3 de 10" in texto
    assert "⚠️" not in texto and "🚨" not in texto


def test_the_digest_section_shouts_past_the_cap(mundo, monkeypatch) -> None:
    """Pasado el techo no se confirma solo NADA, y el motivo parece una caída."""
    from app import digest

    monkeypatch.setattr(policy, "MAX_BORRADORES", 3)
    mundo["borradores"].extend([_borrador(f"SO-{i}") for i in range(5)])

    texto = digest.seccion_autonomia()

    assert "PASASTE EL TECHO" in texto
    assert "caída de ERPNext" in texto


def test_the_digest_section_fails_alone(mundo) -> None:
    from app import digest

    assert "seccion_autonomia" in dict(digest._SECCIONES)
    mundo["caidas"].update({"Comment", "Sales Order", "Sales Order Item"})

    # Cada número dice «no pude leer»; la sección sale igual.
    assert "Autonomía" in digest.seccion_autonomia()


def test_readiness_warns_above_eighty_percent(mundo, monkeypatch) -> None:
    from app import readiness

    monkeypatch.setattr(policy, "MAX_BORRADORES", 10)
    mundo["borradores"].extend([_borrador(f"SO-{i}") for i in range(9)])
    reporte = readiness.Reporte()

    readiness.chequear_borradores(reporte)

    niveles = {clave: nivel for nivel, clave, _ in reporte.lineas}
    assert niveles["Borradores vivos"] == readiness.AVISO
    assert reporte.listo is True  # un aviso no bloquea


def test_past_the_cap_is_still_only_a_warning(mundo, monkeypatch) -> None:
    """Nunca bloquea, ni pasado el techo. Mismo criterio que chequear_solicitudes.

    En este archivo FALTA/ERROR significa que el sistema haría algo MAL.
    Pasado el techo deja de auto-confirmar y todo pedido espera a una persona:
    la postura de lanzamiento. Nada se sobrevende y a nadie se le promete de
    más — es menos útil, en la dirección segura, y eso no es lo que estos
    niveles miden.
    """
    from app import readiness

    monkeypatch.setattr(policy, "MAX_BORRADORES", 3)
    mundo["borradores"].extend([_borrador(f"SO-{i}") for i in range(4)])
    reporte = readiness.Reporte()

    readiness.chequear_borradores(reporte)

    niveles = {clave: nivel for nivel, clave, _ in reporte.lineas}
    assert niveles["Borradores vivos"] == readiness.AVISO
    assert reporte.listo is True  # no bloquea el despliegue de la limpieza
    # Pero es el aviso más fuerte del archivo, con la consecuencia en la línea.
    texto = reporte.texto()
    assert "PASASTE EL TECHO" in texto
    assert "auto-confirmación apagada para TODOS los productos" in texto
    assert "hasta bajar de 3" in texto


def test_without_network_the_cap_check_makes_no_erpnext_call(monkeypatch) -> None:
    """`deploy.yml` corre check-env-offline como puerta: no puede salir a la red.

    Y no se calla: dice que no lo verificó, que es la regla del módulo.
    """
    from app import erpnext, readiness

    def explota(*a, **k):
        raise AssertionError("chequear_borradores salió a ERPNext sin red")

    monkeypatch.setattr(erpnext, "policy_get_list", explota)
    monkeypatch.setattr(erpnext, "policy_get_doc", explota)
    reporte = readiness.Reporte()

    readiness.chequear_borradores(reporte, con_red=False)

    niveles = {clave: nivel for nivel, clave, _ in reporte.lineas}
    assert niveles["Borradores vivos"] == readiness.AVISO
    assert "sin red" in reporte.texto()
    assert reporte.listo is True


def test_the_offline_preflight_never_blocks_on_the_cap(monkeypatch) -> None:
    """De punta a punta: `--sin-red` no puede frenar el despliegue por el techo."""
    from app import erpnext, readiness

    monkeypatch.setattr(
        erpnext,
        "policy_get_list",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("salió a la red")),
    )

    reporte = readiness.ejecutar({"REDIS_URL": ""}, con_red=False)

    culpables = [c for n, c, _ in reporte.lineas if n in (readiness.FALTA, readiness.ERROR)]
    assert "Borradores vivos" not in culpables


def test_readiness_is_ok_when_there_is_room(mundo, monkeypatch) -> None:
    from app import readiness

    monkeypatch.setattr(policy, "MAX_BORRADORES", 100)
    mundo["borradores"].append(_borrador("SO-1"))
    reporte = readiness.Reporte()

    readiness.chequear_borradores(reporte)

    niveles = {clave: nivel for nivel, clave, _ in reporte.lineas}
    assert niveles["Borradores vivos"] == readiness.OK
    assert "1 del bot + 0 cargados a mano" in reporte.texto()


def test_readiness_says_it_could_not_count_rather_than_zero(mundo) -> None:
    from app import readiness

    mundo["caidas"].add("Sales Order")
    reporte = readiness.Reporte()

    readiness.chequear_borradores(reporte)

    assert "no pude contarlos" in reporte.texto()


def test_a_full_read_cap_is_reported_as_a_floor_not_a_total(mundo, monkeypatch) -> None:
    """Un techo de lectura lleno hace que el número sea un piso.

    Informarlo como el total sería un número engañoso, que es peor que un
    número faltante: el dueño decide con él.
    """
    monkeypatch.setattr(autonomia, "MAX_COMENTARIOS", 2)
    mundo["comentarios"].extend(
        [
            _comentario(f"SO-{i}", f"{confirmacion.MARCA} x fuente=automática (política)")
            for i in range(5)
        ]
    )

    cuenta = autonomia.confirmaciones()
    assert cuenta["truncado"] is True
    assert cuenta["total"] == 2  # el piso

    assert "son un piso" in autonomia.texto(autonomia.resumen(), "es")


def test_nothing_is_flagged_as_truncated_when_it_fits(mundo) -> None:
    mundo["comentarios"].append(
        _comentario("SO-1", f"{confirmacion.MARCA} x fuente=automática (política)")
    )

    assert autonomia.confirmaciones()["truncado"] is False
    assert "son un piso" not in autonomia.texto(autonomia.resumen(), "es")


def test_the_sweep_templates_do_not_get_the_misleading_optional_message() -> None:
    """El mensaje genérico diría lo contrario de la verdad para estas cinco.

    «sale como texto libre mientras el destinatario haya escrito en las últimas
    24 h» es cierto para las demás plantillas y FALSO para éstas: son las únicas
    que se disparan horas después del último mensaje del cliente, cuando la
    ventana ya está cerrada.

    El nivel de cada una lo decide `test_readiness.py` —tres bloquean siempre y
    dos dependen de su límite—, así que acá se afirma lo que vale para las cinco
    sin importar el nivel: que ninguna recibe el mensaje genérico, y que todas
    nombran la ventana.
    """
    from app import readiness

    reporte = readiness.Reporte()
    readiness.chequear_plantillas({}, reporte, None, "")

    por_clave = {clave: mensaje for _, clave, mensaje in reporte.lineas}
    for variable in readiness.PLANTILLAS_FUERA_DE_VENTANA:
        assert "ventana" in por_clave[variable], variable
        assert "opcional en el piloto" not in por_clave[variable], variable
    # Y las otras conservan su mensaje, que para ellas sí es cierto.
    assert "opcional en el piloto" in por_clave["WHATSAPP_STAFF_PENDING_TEMPLATE"]


# ------------------------------------- la zona con la que se lee ERPNext


def test_erpnext_timestamps_are_read_in_the_business_zone_not_utc(monkeypatch):
    """El MISMO campo no puede significar dos horas distintas.

    ERPNext guarda `creation` sin zona, en la hora de su propio sistema — la
    del negocio. `pendientes.edad_horas` ya lo lee así para decidir la edad de
    un borrador. Leerlo como UTC acá movía la ventana del informe el offset
    entero (tres horas en Buenos Aires), así que un comentario del borde
    entraba o salía por error.
    """
    from app import autonomia, pendientes

    monkeypatch.setenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires")

    momento = autonomia._creacion({"creation": "2026-09-08 09:00:00"})

    assert momento is not None
    assert momento.tzinfo is not None
    assert momento.utcoffset() == pendientes._zona().utcoffset(momento.replace(tzinfo=None))
    # Y el corte de la ventana se calcula en la misma zona, porque el string
    # que se le manda a ERPNext se compara contra esos mismos `creation`.
    assert autonomia._desde(7).utcoffset() == momento.utcoffset()


def test_a_timestamp_that_already_carries_a_zone_is_left_alone(monkeypatch):
    from app import autonomia

    monkeypatch.setenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires")

    momento = autonomia._creacion({"creation": "2026-09-08T09:00:00+00:00"})

    assert momento is not None and momento.utcoffset().total_seconds() == 0


# ------------------- una caída no es una postura que el dueño eligió


def test_an_unreadable_shadow_record_is_not_counted_as_a_posture_choice(mundo) -> None:
    """El informe rinde la postura como «frenados sólo por la postura que elegiste».

    Meter ahí un «límites sin verificar» le diría al dueño que decidió algo que
    no decidió, y encima lo contaría como un pedido frenado a propósito.
    """
    import json

    caido = json.dumps(
        {
            "pasa_reglas": False,
            "motivos_reglas": [],
            "motivos_postura": [],
            "ilegible": "límites sin verificar: Redis no contesta",
            "total": 8450.0,
            "habitual": None,
            "tope_vigente": 0.0,
            "ts": _sello(0),
        },
        ensure_ascii=False,
    )
    mundo["comentarios"].extend(
        [
            _comentario("SO-1", f"{sombra.MARCA} " + _json_sombra(True, [])),
            _comentario("SO-2", f"{sombra.MARCA} {caido}"),
        ]
    )

    datos = autonomia.sombras()

    assert datos["con_registro"] == 2  # los dos tienen registro durable
    assert datos["ilegibles"] == 1
    assert datos["pasan"] == 1
    # Y NO cuenta como frenado: de los DECIDIDOS, uno pasó y ninguno se frenó.
    assert datos["frenados"] == 0
    assert datos["postura"] == {"tope": 1}  # sólo el que sí decidió


# ------------------------- el techo de lectura de los rechazos también avisa


def test_a_capped_rejection_read_is_reported_as_a_floor(mundo, monkeypatch) -> None:
    """Era el único número del módulo que se informaba como exacto sin saberlo."""
    monkeypatch.setattr(autonomia, "MAX_COMENTARIOS", 2)
    for n in range(4):
        mundo["comentarios"].append(
            _comentario(f"SO-{n}", f"{autonomia.MARCA_RECHAZO} un integrante")
        )

    datos = autonomia.rechazos()

    assert datos["truncado"] is True
    # Y el aviso de la última línea lo dice, no sólo para conf/sombra/revisión.
    assert "son un piso" in autonomia.texto({"dias": 7, "rechazos": datos}, "es")


# ----------------------------- las dos fuentes de frenos, sumadas de verdad


def _json_ilegible() -> str:
    """Un registro de sombra que no pudo leer los límites: no decidió nada."""
    import json

    return json.dumps(
        {
            "pasa_reglas": False,
            "motivos_reglas": [],
            "motivos_postura": [],
            "ilegible": "límites sin verificar: Redis no contesta",
            "total": 8450.0,
            "habitual": None,
            "tope_vigente": 0.0,
            "ts": _sello(0),
        },
        ensure_ascii=False,
    )


def test_a_shadow_blocker_no_longer_hides_the_review_only_blockers(mundo) -> None:
    """El bug del issue #7, en su forma mínima.

    Un solo freno de sombra hacía que `som["reglas"]` no estuviera vacío, el
    `or` cortaba ahí, y TODOS los frenos que sólo figuraban en las revisiones
    desaparecían del informe. Con dos pedidos y dos fuentes alcanza para
    verlo: el que sólo está en la revisión tiene que seguir en el desglose.
    """
    mundo["comentarios"].extend(
        [
            _comentario("SO-1", f"{sombra.MARCA} " + _json_sombra(False, ["stock insuficiente de X"])),
            _comentario("SO-2", f"{autonomia.MARCA_REVISION} tiene $12.000 vencidos"),
        ]
    )

    texto = autonomia.texto(autonomia.resumen(), "es")

    assert "sin stock 1" in texto  # el de la sombra
    assert "deuda vencida 1" in texto  # el que sólo está en la revisión


def test_an_order_in_both_sources_is_counted_once(mundo) -> None:
    """La deduplicación es lo que hace que sumar sea correcto.

    Un pedido con registro de sombra deja TAMBIÉN su comentario de revisión
    humana — los dos los escribe el mismo paso. Si las revisiones no lo
    saltaran, su cubeta sumaría dos y el desglose diría más frenos que pedidos
    frenados.
    """
    mundo["comentarios"].extend(
        [
            _comentario("SO-1", f"{sombra.MARCA} " + _json_sombra(False, ["stock insuficiente de X"])),
            _comentario("SO-1", f"{autonomia.MARCA_REVISION} stock insuficiente de X"),
        ]
    )

    datos = autonomia.resumen()

    assert datos["sombras"]["reglas"] == {"sin stock": 1}
    assert datos["revisiones"]["grupos"] == {}  # saltado: ya lo contó la sombra
    assert datos["revisiones"]["excluidos"] == 1
    assert "sin stock 1" in autonomia.texto(datos, "es")


def test_the_counted_set_leaves_out_records_that_could_not_decide(mundo) -> None:
    """Un registro ilegible no aportó a ninguna cubeta, así que no se excluye.

    Si `contados` fuera «todos los que tienen registro», el pedido cuya sombra
    no pudo leer los límites quedaría fuera de las DOS fuentes y su freno
    desaparecería del informe — el mismo bug que este PR arregla, entrando por
    la otra puerta.
    """
    mundo["comentarios"].extend(
        [
            _comentario("SO-1", f"{sombra.MARCA} " + _json_ilegible()),
            _comentario("SO-1", f"{autonomia.MARCA_REVISION} tiene $12.000 vencidos"),
        ]
    )

    datos = autonomia.resumen()

    assert datos["sombras"]["con_registro"] == 1
    assert datos["sombras"]["contados"] == frozenset()
    # El freno se cuenta por la prosa, que es la única fuente que sí lo sabe.
    assert datos["revisiones"]["grupos"] == {"deuda vencida": 1}
    assert "deuda vencida 1" in autonomia.texto(datos, "es")


def test_the_buckets_of_both_sources_add_up(mundo) -> None:
    """Misma cubeta en las dos fuentes, pedidos distintos: tiene que sumar 2."""
    mundo["comentarios"].extend(
        [
            _comentario("SO-1", f"{sombra.MARCA} " + _json_sombra(False, ["stock insuficiente de X"])),
            _comentario("SO-2", f"{autonomia.MARCA_REVISION} no se pudo verificar stock de Y"),
        ]
    )

    assert "sin stock 2" in autonomia.texto(autonomia.resumen(), "es")


def test_reviews_count_everything_when_the_shadow_read_is_down(mundo) -> None:
    """Sin saber qué contó la otra fuente, contar de más es la única opción.

    Un desglose inflado hace mirar un freno que ya no está. Uno recortado hace
    subir un límite que no había que subir, que es la dirección peligrosa.
    """
    mundo["comentarios"].append(
        _comentario("SO-1", f"{autonomia.MARCA_REVISION} tiene $12.000 vencidos")
    )

    assert autonomia.revisiones(excluir=None)["grupos"] == {"deuda vencida": 1}
    assert autonomia.revisiones(excluir=frozenset())["grupos"] == {"deuda vencida": 1}
    assert autonomia.revisiones(excluir={"SO-1"})["grupos"] == {}


def test_an_unreadable_breakdown_says_so_instead_of_showing_a_dash(mundo) -> None:
    """Un «—» en el desglose se lee como «ningún freno».

    Que es exactamente la respuesta que hace subir un límite, así que cuando
    ninguna de las dos fuentes se pudo leer el desglose tiene que decir que no
    se pudo leer.
    """
    mundo["caidas"].add("Comment")

    texto = autonomia.texto(autonomia.resumen(), "es")

    assert "frenados por reglas: no pude leer" in texto
    assert "el desglose" in texto and "está incompleto" in texto


def test_no_blockers_at_all_still_shows_a_dash(mundo) -> None:
    """La contracara: leído y vacío no es lo mismo que no leído."""
    texto = autonomia.texto(autonomia.resumen(), "es")

    assert "frenados por reglas: —" in texto
    assert "está incompleto" not in texto  # las dos fuentes se leyeron bien


@pytest.mark.parametrize("caida", ["sombras", "revisiones"])
def test_half_a_breakdown_says_it_is_half(caida) -> None:
    """Una fuente caída deja un desglose REAL pero corto, y hay que decirlo.

    Se arma el dict a mano porque el doble hace caer las dos lecturas juntas
    (las dos son «Comment»), y lo que hay que probar es justo la mitad. `texto`
    es un constructor puro sobre estos datos, así que ése es el borde.
    """
    datos = {
        "dias": 7,
        "sombras": {"con_registro": 1, "pasan": 0, "frenados": 1, "ilegibles": 0,
                    "contados": frozenset({"SO-1"}), "postura": {},
                    "reglas": {"sin stock": 1}, "truncado": False},
        "revisiones": {"pedidos": 1, "grupos": {"deuda vencida": 1},
                       "excluidos": 1, "truncado": False},
    }
    datos[caida] = None

    texto = autonomia.texto(datos, "es")

    assert "está incompleto" in texto
    # Y lo que SÍ se pudo leer se sigue mostrando: media respuesta es mejor
    # que ninguna, siempre que diga que es media.
    esperado = "deuda vencida 1" if caida == "sombras" else "sin stock 1"
    assert esperado in texto


def test_the_skipped_count_is_what_this_window_skipped(mundo) -> None:
    """`excluidos` no es el tamaño del conjunto que entró.

    Lo que entra son TODOS los pedidos con registro de sombra, y la mayoría no
    dejó comentario de revisión en esta ventana. Informar `len(excluir)` sería
    informar un número que no es el que su nombre dice — el mismo error que
    este módulo ya arregló dos veces en otros contadores.
    """
    mundo["comentarios"].append(
        _comentario("SO-1", f"{autonomia.MARCA_REVISION} tiene $12.000 vencidos")
    )

    datos = autonomia.revisiones(excluir={"SO-1", "SO-2", "SO-3"})

    assert datos["excluidos"] == 1  # sólo SO-1 estaba acá para saltar
    assert datos["grupos"] == {}


# ----------------------------------------------- los techos que no avisaban


def test_a_live_draft_count_that_filled_the_read_says_it_is_a_floor(
    mundo, monkeypatch
) -> None:
    """`pasado` y `truncado` no dicen lo mismo, y hacían falta los dos.

    `pasado` dice que se pasó el techo del NEGOCIO. `truncado` dice que el
    número mismo no se pudo medir completo. Con una sola fila de más, `vivos`
    valía `MAX_BORRADORES + 1` tanto con 501 borradores como con 5000 y se
    informaba igual — un techo presentado como si fuera el total.
    """
    monkeypatch.setattr(policy, "MAX_BORRADORES", 3)
    mundo["borradores"].extend([_borrador(f"SO-{i}") for i in range(9)])

    datos = autonomia.borradores_vivos()

    assert datos["pasado"] is True
    assert datos["truncado"] is True
    assert "es un piso" in autonomia.texto({"dias": 7, "borradores": datos}, "es")


def test_a_live_draft_count_that_fits_is_still_reported_as_exact(
    mundo, monkeypatch
) -> None:
    """El caso que separa los dos: techo del negocio pasado, cuenta completa.

    Sin este test, `truncado = pasado` pasaría el de arriba sin decir nada
    nuevo, que es exactamente el estado del que se viene.
    """
    monkeypatch.setattr(policy, "MAX_BORRADORES", 3)
    mundo["borradores"].extend([_borrador(f"SO-{i}") for i in range(4)])

    datos = autonomia.borradores_vivos()

    assert datos["pasado"] is True
    assert datos["truncado"] is False
    assert "es un piso" not in autonomia.texto({"dias": 7, "borradores": datos}, "es")


def test_a_breakdown_cut_for_length_says_how_many_it_left_out() -> None:
    """El corte se lleva SIEMPRE las cubetas más chicas, así que el error está
    acotado — pero va en la dirección peligrosa: un desglose de frenos corto se
    lee como «hay menos frenos de los que hay», y eso es lo que hace subir un
    límite sobre evidencia que no está.
    """
    grupos = {f"freno {i}": 7 - i for i in range(7)}

    linea = autonomia._linea_grupos(grupos, "es")

    assert linea.startswith("freno 0 7, freno 1 6")
    assert "+1 sin mostrar" in linea
    assert "freno 6" not in linea  # la más chica es la que se cae


def test_a_breakdown_that_fits_adds_nothing(mundo) -> None:
    """Nada de «+0 sin mostrar»: el aviso sólo aparece cuando cortó."""
    assert autonomia._linea_grupos({"freno a": 2, "freno b": 1}, "es") == (
        "freno a 2, freno b 1"
    )


def test_a_cut_breakdown_says_it_in_the_language_it_was_asked_in() -> None:
    """El «+N sin mostrar» pasa por el catálogo, no es un literal pegado."""
    grupos = {f"freno {i}": 9 - i for i in range(8)}

    assert "+2 not shown" in autonomia._linea_grupos(grupos, "en")
