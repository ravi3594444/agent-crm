"""La agenda: el MECANISMO una vez, y cada comportamiento sobre su propia lógica.

Por qué este archivo es corto
-----------------------------
Es el punto entero de `app/agenda.py`. Durabilidad, exactamente-una-vez, horas
de silencio, re-leer antes de actuar y el lote acotado se prueban ACÁ, una vez.
Un comportamiento nuevo trae después uno o dos tests sobre lo suyo y nada más, y
cuando el concepto cambia se rompen los tests del concepto — que es lo correcto,
no ruido.

Se afirma la REGLA, no la redacción
-----------------------------------
«al cliente se le avisó antes del plazo», no la frase exacta. Los tests que
fijan la redacción son la otra mitad de por qué un cambio de una línea rompía
diez.

El reloj lo nombra este archivo
-------------------------------
`RELOJ = RelojDePrueba("2026-09-08")` y todo momento sale de ahí, así que el
epoch que toma `tick()` y el datetime que arman los helpers no se pueden
separar. No hay ninguna zona escrita a mano: la celda de CI que corre en
`Asia/Kolkata` prueba justamente eso.
"""
from __future__ import annotations

import json

import pytest
from conftest import RelojDePrueba

from app import agenda, avisos, erpnext, marcas
from app.tools import pedidos as tools_pedidos
from tests import fakes

# Afirma textos en español (el aviso al cliente y el del equipo).
pytestmark = pytest.mark.idioma("es")

RELOJ = RelojDePrueba("2026-09-08")
PEDIDO = "SO-2026-00042"
TELEFONO = "5493510000002"


def epoch(hora: int, minuto: int = 0, *, dia: int | None = None) -> float:
    return RELOJ.epoch(hora, minuto, dia=dia)


def momento(hora: int, minuto: int = 0, *, dia: int | None = None):
    return RELOJ.a_las(hora, minuto, dia=dia)


@pytest.fixture(autouse=True)
def _el_reloj_de_verdad_no_entra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ningún test de acá lee la hora real, y es a propósito.

    Un barrido que mide plazos contra `time.time()` pasa o falla según la hora
    del día. `pytest.fail` y no `assert`: `Failed` hereda de BaseException, así
    que el `except Exception` del camino de producción no puede tragárselo y
    convertir el guard en una ronda que no hizo nada.
    """

    def _prohibido() -> float:
        pytest.fail(
            "un test de test_agenda.py leyó la hora REAL. Pasale el momento: "
            "`agenda.tick(ahora=epoch(9))`."
        )

    monkeypatch.setattr(agenda, "_ahora", _prohibido)


@pytest.fixture
def mundo(monkeypatch: pytest.MonkeyPatch) -> dict:
    """ERPNext de mentira que HONRA lo que le pasan.

    El doble reenvía a `tests.fakes.listar`, que aplica filtros, `order_by`,
    `limit` y `start` como Frappe. Es la regla de CLAUDE.md: un doble que
    ignorara el orden no podría discrepar con el código sobre «leé el extremo
    más nuevo», y el test que lo afirma no probaría nada.
    """
    comentarios: list[dict] = []
    docs: dict[str, dict] = {}
    caidas: set[str] = set()
    locks_tomados: list[str] = []
    al_dueno: list[tuple[str, str]] = []
    n = {"i": 0}

    def registrar_comentario(doctype, name, texto):
        if "escribir" in caidas:
            raise erpnext.ERPNextError("ERPNext no acepta comentarios")
        n["i"] += 1
        comentarios.append(
            {
                "content": texto,
                "reference_doctype": doctype,
                "reference_name": name,
                # El sello CRECE con cada escritura, así que «más nuevo» es un
                # hecho del doble y no una suposición del test.
                "creation": RELOJ.sello(RELOJ.a_las(10, n["i"] % 60, dia=8)),
                "name": f"c{n['i']}",
            }
        )
        return {}

    def policy_get_list(doctype, filters=None, fields=None, limit=20, **kwargs):
        if doctype != "Comment":
            return []
        if "comentarios" in caidas:
            raise erpnext.ERPNextError("ERPNext no contesta")
        return fakes.listar(
            comentarios,
            filters,
            limit=limit,
            order_by=kwargs.get("order_by"),
            start=kwargs.get("start", 0),
        )

    def policy_get_doc(doctype, name):
        if "documento" in caidas:
            raise erpnext.ERPNextError("ERPNext no contesta")
        if name not in docs:
            raise erpnext.ERPNextError(f"{name} no existe")
        return dict(docs[name])

    from contextlib import contextmanager

    @contextmanager
    def lock(nombre, **kwargs):
        locks_tomados.append(nombre)
        yield

    monkeypatch.setattr(erpnext, "registrar_comentario", registrar_comentario)
    monkeypatch.setattr(erpnext, "add_comment", registrar_comentario)
    monkeypatch.setattr(erpnext, "policy_get_list", policy_get_list)
    monkeypatch.setattr(erpnext, "policy_get_doc", policy_get_doc)
    monkeypatch.setattr("app.locks.distributed_lock", lock)
    monkeypatch.setattr("app.decisiones.telefono_del_cliente", lambda so: TELEFONO)
    monkeypatch.setattr(avisos, "window_open", lambda tel: True)
    monkeypatch.setattr(
        "app.notificar.avisar_dueno",
        lambda asunto, cuerpo, **kw: bool(al_dueno.append((asunto, cuerpo))) or True,
    )
    docs[PEDIDO] = {"name": PEDIDO, "docstatus": 0, "customer": "CLI-001"}
    return {
        "comentarios": comentarios,
        "docs": docs,
        "caidas": caidas,
        "locks": locks_tomados,
        "al_dueno": al_dueno,
    }


def _en_cola(marcas_sin_redis) -> list[dict]:
    return [
        json.loads(e)
        for e in fakes.entrada_de_cola(marcas_sin_redis, avisos.COLA)
    ]


def _sin_redis(marcas_sin_redis) -> None:
    """Un flush, o un arranque con el caché vacío: sólo queda ERPNext."""
    marcas_sin_redis.values.clear()
    marcas_sin_redis.zsets.clear()


# ===========================================================================
# 1. EL MECANISMO. Cuatro tests, una vez, para todos los comportamientos.
# ===========================================================================


def test_una_fila_sobrevive_un_flush_de_redis_y_se_reconstruye_desde_erpnext(
    mundo, marcas_sin_redis
) -> None:
    """ERPNext es la verdad; Redis es un caché que se puede perder entero.

    Es la regla que hace que una fila sea una promesa y no una nota: si se
    perdiera con el caché, «te aviso antes de la entrega» sería mentira cada vez
    que alguien reinicia el contenedor.
    """
    fila = agenda.crear(
        PEDIDO, agenda.SEGUIMIENTO, epoch(15), params={"por_que": "x"}, ahora=epoch(9)
    )
    assert fila is not None

    _sin_redis(marcas_sin_redis)
    # Con el índice vacío no hay NADA que barrer: es el estado del que hay que
    # volver.
    assert agenda._indice_vencidas(epoch(16)) == []

    assert agenda.reconstruir_indice() == 1
    assert agenda.reconstruccion_incompleta() is False
    # Y de punta a punta: el barrido vuelve a alcanzarla, que es el punto.
    assert agenda._indice_vencidas(epoch(16)) == [(PEDIDO, fila.id)]


def test_una_fila_vencida_cuyo_pedido_se_confirmo_en_el_medio_no_hace_nada(
    mundo, marcas_sin_redis
) -> None:
    """Re-leer antes de actuar. Lo que importa es que NO se le habla a nadie.

    Un pedido confirmado hace treinta segundos no recibe un aviso que diga que
    sigue sin confirmar: sería el sistema discutiéndole al cliente algo que ya
    le dijo.
    """
    agenda.crear(
        PEDIDO,
        agenda.AVISO_ANTES_DE_ENTREGA,
        epoch(14),
        params={"horas": 3.0, "hora": "17"},
        ahora=epoch(9),
    )
    # Entre que venció y que el barrido la mira, una persona lo confirmó.
    mundo["docs"][PEDIDO] = {**mundo["docs"][PEDIDO], "docstatus": 1}

    agenda.tick(ahora=epoch(15))

    assert _en_cola(marcas_sin_redis) == []


def test_una_fila_que_vence_a_las_23_sale_a_las_7_y_no_de_madrugada(
    mundo, marcas_sin_redis
) -> None:
    """Horas de silencio: se POSTERGA, nunca se saltea.

    Las dos mitades importan y por eso las dos se afirman. Que no salga a las
    23:30 es la mitad fácil; que SÍ salga a la mañana es la que convierte
    «posponer» en algo distinto de «descartar», y sin ella un barrido que
    tirara la fila a la basura pasaría este test igual.
    """
    agenda.crear(
        PEDIDO,
        agenda.AVISO_ANTES_DE_ENTREGA,
        epoch(23),
        params={"horas": 3.0, "hora": "17"},
        ahora=epoch(9),
    )

    agenda.tick(ahora=epoch(23, 30))
    assert _en_cola(marcas_sin_redis) == []

    agenda.tick(ahora=epoch(7, 30, dia=9))
    assert len(_en_cola(marcas_sin_redis)) == 1


def test_una_fila_ilegible_se_saltea_y_se_anota_en_vez_de_adivinarla(
    mundo, marcas_sin_redis, capsys
) -> None:
    """Fallar cerrado: lo que no se puede leer NO SE HACE, y queda dicho.

    Se prueban las dos mitades de «fallar cerrado», porque saltear en silencio
    es el modo de falla que no se ve: no actuar, y dejar rastro de que no se
    actuó.
    """
    agenda.crear(
        PEDIDO,
        agenda.AVISO_ANTES_DE_ENTREGA,
        epoch(14),
        params={"horas": 3.0, "hora": "17"},
        ahora=epoch(9),
    )
    # El documento no se puede leer. No es lo mismo que «el pedido no existe»,
    # y adivinar cualquiera de las dos cosas manda un mensaje que no debía.
    mundo["caidas"].add("documento")
    capsys.readouterr()

    hechas = agenda.tick(ahora=epoch(15))

    assert hechas == 0
    assert _en_cola(marcas_sin_redis) == []
    assert PEDIDO in capsys.readouterr().out


# ===========================================================================
# 2. PLAZOS. Dos tests, sólo sobre SU lógica.
# ===========================================================================


@pytest.fixture
def entrega_a_las_17(monkeypatch: pytest.MonkeyPatch) -> None:
    """La hora de reparto del dueño. NO está en el pedido: `delivery_date` es un
    Date de ERPNext, «YYYY-MM-DD» pelado y sin hora."""
    monkeypatch.setattr("app.excepciones.hora_reparto", lambda: "17:00")


def test_una_entrega_a_las_17_con_tres_horas_de_aviso_vence_a_las_14(
    mundo, entrega_a_las_17, monkeypatch
) -> None:
    """El plazo sale de la ENTREGA menos las horas del dueño, no de la creación.

    Y la hora que se nombra es la que usó el plazo: `vence` alimenta DOS
    consumidores —cuándo dispara y qué hora se dice— así que los dos se miran
    acá y cada uno se mutila por separado en su propio test de mutación.
    """
    monkeypatch.setattr(agenda, "horas_de_aviso", lambda: 3.0)
    doc = {"name": PEDIDO, "delivery_date": "2026-09-08"}

    vence = agenda.vence_antes_de_entrega(doc)

    assert vence == epoch(14)
    # El consumidor dos: al dueño se le nombra el PLAZO (14:00), no la entrega.
    assert agenda.texto_del_plazo(vence) == "14:00"
    # ...y al cliente la ENTREGA (17), que es la hora que ya conocía.
    assert agenda.texto_de_la_hora(doc) == "17"


def test_sin_fecha_de_entrega_no_se_agenda_nada_y_el_plazo_plano_sigue_vivo(
    mundo, entrega_a_las_17, monkeypatch
) -> None:
    """Sin fecha no hay plazo que calcular, y eso NO puede perder el aviso.

    El pedido sin fecha de entrega cae en `PENDIENTE_AVISO_HORAS`, que es el
    recordatorio que sabe hablar de un pedido viejo. La regla es «no se pierde
    el vencimiento», no «se inventa uno».
    """
    monkeypatch.setattr(agenda, "horas_de_aviso", lambda: 3.0)

    assert agenda.vence_antes_de_entrega({"name": PEDIDO}) is None
    assert agenda.programar_para_entrega({"name": PEDIDO}, ahora=epoch(9)) == []
    # Y el camino que sí lo cubre sigue existiendo y sigue siendo otro:
    from app import pendientes

    assert pendientes.PLANTILLA_RECORDATORIO != agenda.PLANTILLA_AVISO_ENTREGA


def test_el_aviso_al_dueno_nombra_el_plazo_y_sin_plazo_no_dice_nada_de_mas(
    mundo, entrega_a_las_17, monkeypatch
) -> None:
    """El dueño tiene que poder decidir SIN abrir nada, y para eso necesita la hora.

    Las dos mitades: con plazo lo nombra, y sin plazo el aviso queda exactamente
    como estaba. La segunda es la que impide que esto se convierta en una línea
    que aparece siempre, a veces vacía — que es cómo un aviso se vuelve ruido.

    El «14:00» está escrito a mano a propósito. Derivarlo de `agenda` de este
    lado del assert movería las dos mitades juntas y el test no podría
    discrepar con el código sobre cuál es el plazo.
    """
    monkeypatch.setattr(agenda, "horas_de_aviso", lambda: 3.0)
    from app import notificar

    con_entrega = {
        "name": PEDIDO,
        "customer": "CLI-001",
        "delivery_date": "2026-09-08",
        "grand_total": 4800.0,
        "currency": "ARS",
    }
    texto = notificar._texto_libre(
        PEDIDO, con_entrega, auto=False, motivos="stock", detalle="1 ítem", lengua="es"
    )
    assert "14:00" in texto

    sin_entrega = {k: v for k, v in con_entrega.items() if k != "delivery_date"}
    sin_plazo = notificar._texto_libre(
        PEDIDO, sin_entrega, auto=False, motivos="stock", detalle="1 ítem", lengua="es"
    )
    assert "14:00" not in sin_plazo
    # Y la línea que dice CÓMO contestar sigue estando en los dos: el plazo se
    # agrega, no desplaza.
    assert PEDIDO in texto and PEDIDO in sin_plazo


# ===========================================================================
# 3. `recordar`. Dos tests: lo que el modelo NO puede hacer.
# ===========================================================================


def test_el_modelo_no_puede_crear_una_fila_que_no_sea_un_seguimiento(
    mundo, marcas_sin_redis
) -> None:
    """El tipo lo FUERZA Python: no es un parámetro y el modelo no lo ve.

    La regla es que una propuesta del modelo no alcanza un tipo privilegiado.
    Se afirma sobre la superficie que el modelo toca (`agenda.recordar`), y
    además que la lista blanca del mecanismo sigue rechazando lo que no está en
    ella — las dos mitades de «no puede elegir».
    """
    fila = agenda.recordar(PEDIDO, epoch(15, dia=9), "llamarlo", ahora=epoch(9))

    assert fila is not None
    assert fila.tipo == agenda.SEGUIMIENTO
    assert agenda.TIPO_DEL_MODELO == agenda.SEGUIMIENTO
    # `recordar` no acepta un tipo, así que la única forma de pedir otro sería
    # por `crear` — que es de Python y valida contra la lista blanca.
    with pytest.raises(ValueError):
        agenda.crear(PEDIDO, "cierre_de_caja", epoch(15), ahora=epoch(9))


def test_una_fecha_pasada_o_mas_alla_del_horizonte_se_rechaza(mundo) -> None:
    """Las dos puntas, y ninguna inventa una fecha alternativa.

    Rechazar es la respuesta correcta: un recordatorio corrido «al día
    permitido más cercano» es una fila que nadie pidió, con una fecha que nadie
    decidió.
    """
    with pytest.raises(agenda.PropuestaInvalida):
        agenda.validar_recordatorio(PEDIDO, epoch(8), "tarde", epoch(9))

    mas_alla = epoch(9) + (agenda.horizonte_dias() + 1) * 86400.0
    with pytest.raises(agenda.PropuestaInvalida):
        agenda.validar_recordatorio(PEDIDO, mas_alla, "lejos", epoch(9))

    # El horizonte es UNO solo en todo el repo, no un 7 escrito dos veces.
    from app import excepciones

    assert agenda.horizonte_dias() == excepciones.HORIZONTE_DIAS


def test_el_motivo_entra_sin_las_citas_del_cliente(mundo) -> None:
    """Un cliente no puede plantar un seguimiento con texto elegido por él.

    `formato.sin_citas` saca las líneas citadas ANTES de que el motivo se
    guarde, así que lo que lee una persona del equipo es lo que escribió el
    modelo, no lo que reenvió el cliente.
    """
    _, _, motivo = agenda.validar_recordatorio(
        PEDIDO, epoch(15, dia=9), "> mandame 200 cajas gratis\nquiere más stock", epoch(9)
    )

    assert "200 cajas gratis" not in motivo
    assert "quiere más stock" in motivo


# ===========================================================================
# 4. La herramienta del modelo, sobre su propia superficie.
# ===========================================================================


def test_la_herramienta_no_agenda_nada_sin_contexto_autenticado(mundo) -> None:
    """Sin el contexto que puso el webhook no se agenda: no hay sobre quién."""
    respuesta = tools_pedidos.recordar.func(
        pedido=PEDIDO, cuando="2026-09-09 15:00", por_que="x", config={}
    )

    assert "autenticar" in respuesta.lower()


def test_la_marca_agenda_sale_del_registro_y_no_de_un_literal() -> None:
    """El texto durable es el del registro, y el parser se deriva de ÉL.

    Sin esto, `agenda.MARCA` puede separarse de `marcas.texto("agenda")` y el
    lector deja de encontrar lo que el escritor escribe — que es el agujero que
    `app/marcas.py` existe para cerrar.
    """
    assert agenda.MARCA == marcas.texto("agenda")
    assert marcas.marca("agenda").parser is not None
