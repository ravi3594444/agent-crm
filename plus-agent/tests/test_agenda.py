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
        # Y una falla SELECTIVA: `escribir:<texto>` rompe sólo las escrituras
        # que lo contienen. Hace falta para escribir «esta escritura entró y
        # esta otra no», que es el caso que distingue un orden de operaciones
        # del otro; con un interruptor que rompe todo, los dos órdenes se ven
        # iguales y el test no puede discrepar con el código.
        for caida in caidas:
            if caida.startswith("escribir:") and caida[9:] in str(texto):
                raise erpnext.ERPNextError("ERPNext no acepta este comentario")
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
    mundo, marcas_sin_redis, entrega_a_las_17, monkeypatch
) -> None:
    """Horas de silencio: se POSTERGA, nunca se saltea.

    Las dos mitades importan y por eso las dos se afirman. Que no salga a las
    23:30 es la mitad fácil; que SÍ salga a la mañana es la que convierte
    «posponer» en algo distinto de «descartar», y sin ella un barrido que
    tirara la fila a la basura pasaría este test igual.

    El pedido lleva su fecha de entrega porque el handler RE-LEE el plazo antes
    de hablar: sin ella no habría plazo vigente y la fila se cerraría sin
    mandar nada, que es correcto pero es otra regla y la probamos aparte.
    """
    monkeypatch.setattr(agenda, "horas_de_aviso", lambda: 3.0)
    mundo["docs"][PEDIDO] = {**mundo["docs"][PEDIDO], "delivery_date": "2026-09-08"}
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
    assert marcas.texto("agenda") == agenda.MARCA
    assert marcas.marca("agenda").parser is not None


# ===========================================================================
# 5. Lo que encontró la revisión de Qodo. Un test por arreglo: un arreglo sin
#    test es el mismo defecto mudado de lugar.
# ===========================================================================


def _config_cliente(cuenta: str = "CLI-001") -> dict:
    return {
        "configurable": {
            "thread_id": "cli:thread",
            "actor_scope": "customer",
            "customer_code": cuenta,
            "actor_phone": "5493510000000",
            "inbound_message_id": "wamid.test-agenda",
        }
    }


def test_un_cliente_no_puede_agendar_sobre_el_pedido_de_otro(mundo, monkeypatch) -> None:
    """El número de pedido lo dice el MODELO, y el modelo lee lo que le escribió
    un cliente. Sin esta guarda, nombrar el pedido de otro le cuelga encima un
    recordatorio que el equipo lee como si fuera de esa cuenta.

    La negativa es la MISMA que la de un pedido inexistente, a propósito: dos
    respuestas distintas dejan enumerar pedidos ajenos probando números.
    """
    # La herramienta también tiene su reloj, y tampoco lee el de verdad: sin
    # esto el test caduca solo el día que «2026-09-09» pasa a ser pasado.
    monkeypatch.setattr(tools_pedidos, "_ahora_del_negocio", lambda: momento(9))
    mundo["docs"][PEDIDO] = {**mundo["docs"][PEDIDO], "customer": "CLI-001"}

    ajeno = tools_pedidos.recordar.func(
        pedido=PEDIDO,
        cuando="2026-09-09 15:00",
        por_que="x",
        config=_config_cliente("CLI-OTRO"),
    )

    assert PEDIDO in ajeno and "no encontré" in ajeno.lower()
    # Y lo que importa: NO quedó escrito nada. Una negativa que igual agenda es
    # una negativa decorativa.
    assert agenda.vivas(PEDIDO, agenda.SEGUIMIENTO) == []

    # El dueño de la cuenta SÍ puede: la guarda no rompe el caso normal, que es
    # la otra mitad — una guarda que prohíbe todo también pasaría la primera.
    propio = tools_pedidos.recordar.func(
        pedido=PEDIDO,
        cuando="2026-09-09 15:00",
        por_que="x",
        config=_config_cliente("CLI-001"),
    )

    assert "anotado" in propio.lower()
    assert len(agenda.vivas(PEDIDO, agenda.SEGUIMIENTO)) == 1


def test_sin_plazo_vigente_no_se_manda_el_aviso_con_la_hora_vieja(
    mundo, marcas_sin_redis, entrega_a_las_17, monkeypatch
) -> None:
    """La hora guardada al crear la fila es justo la que pudo dejar de ser cierta.

    Si al pedido le sacaron la fecha de entrega, el plazo no existe: mandarle al
    cliente «no te lo confirmé para las 17» nombra una hora que ya no está
    prometida. Fallar cerrado es no mandar nada.
    """
    monkeypatch.setattr(agenda, "horas_de_aviso", lambda: 3.0)
    agenda.crear(
        PEDIDO,
        agenda.AVISO_ANTES_DE_ENTREGA,
        epoch(14),
        params={"horas": 3.0, "hora": "17"},
        ahora=epoch(9),
    )
    # El pedido del `mundo` no tiene delivery_date: se la sacaron.
    agenda.tick(ahora=epoch(15))

    assert _en_cola(marcas_sin_redis) == []


def test_el_aviso_al_dueno_recalcula_el_plazo_en_vez_de_leer_el_guardado(
    mundo, marcas_sin_redis, entrega_a_las_17, monkeypatch
) -> None:
    """Una excepción de entrega aceptada reescribe la fecha DESPUÉS de la fila.

    Decirle al dueño «contestá antes de las 14» cuando el plazo pasó a ser otro
    día es peor que no decirle nada: contesta tarde creyendo que llegó.
    """
    monkeypatch.setattr(agenda, "horas_de_aviso", lambda: 3.0)
    # La fila se creó con la entrega del día 8 (plazo 14:00)...
    agenda.crear(
        PEDIDO,
        agenda.RECORDATORIO_PLAZO_DUENO,
        epoch(13),
        params={"hora": "14:00"},
        ahora=epoch(9),
    )
    # ...y después el cliente aceptó una contraoferta para el día 9.
    mundo["docs"][PEDIDO] = {**mundo["docs"][PEDIDO], "delivery_date": "2026-09-09"}

    agenda.tick(ahora=epoch(14))

    # No se le avisó con el plazo viejo: la fila se corrió al plazo nuevo.
    assert mundo["al_dueno"] == []
    viva = agenda.vivas(PEDIDO, agenda.RECORDATORIO_PLAZO_DUENO)
    assert len(viva) == 1
    assert viva[0].params["hora"] == "14:00"  # las 14 del día 9, no del 8
    assert viva[0].vence == epoch(13, dia=9)


def test_el_re_ping_al_dueno_tambien_espera_a_la_manana(
    mundo, marcas_sin_redis, entrega_a_las_17, monkeypatch
) -> None:
    """Una entrega temprana lo despertaría de madrugada, y eso no se contesta.

    El aviso al CLIENTE de ese mismo plazo ya está postergado hasta las 07:00,
    así que tocarle el hombro al dueño a las 3 no compra el plazo: sólo lo
    despierta.
    """
    # Entrega 17:00 menos catorce horas de aviso: el plazo cae a las 03:00, y
    # el re-ping una hora antes. Ése es el caso que las horas de silencio
    # existen para atrapar.
    monkeypatch.setattr(agenda, "horas_de_aviso", lambda: 14.0)
    mundo["docs"][PEDIDO] = {**mundo["docs"][PEDIDO], "delivery_date": "2026-09-08"}
    agenda.crear(
        PEDIDO,
        agenda.RECORDATORIO_PLAZO_DUENO,
        epoch(2),
        params={"hora": "03:00"},
        ahora=epoch(1),
    )

    agenda.tick(ahora=epoch(3, 30))
    assert mundo["al_dueno"] == []

    agenda.tick(ahora=epoch(7, 30))
    assert len(mundo["al_dueno"]) == 1


def test_un_seguimiento_nuevo_no_borra_el_viejo_si_no_se_pudo_escribir(
    mundo, marcas_sin_redis
) -> None:
    """Se crea PRIMERO y se cancela después, y por eso nunca quedan cero.

    Al revés, una escritura fallida después de una cancelación exitosa dejaba el
    pedido sin ningún recordatorio. Lo peor de este orden es que queden dos
    —ruido—; lo peor del otro era un olvido.
    """
    primero = agenda.recordar(PEDIDO, epoch(15, dia=9), "el primero", ahora=epoch(9))
    assert primero is not None

    # Falla SÓLO la creación del nuevo. La cancelación del viejo entraría sin
    # problema — y ésa es justamente la diferencia entre los dos órdenes: con
    # «cancelar primero» el viejo ya estaría muerto a esta altura.
    mundo["caidas"].add("escribir:el segundo")
    assert agenda.recordar(PEDIDO, epoch(16, dia=9), "el segundo", ahora=epoch(9)) is None

    mundo["caidas"].discard("escribir:el segundo")
    vivas = agenda.vivas(PEDIDO, agenda.SEGUIMIENTO)
    assert [f.id for f in vivas] == [primero.id]


def test_no_se_agenda_un_seguimiento_sin_poder_leer_los_que_ya_hay(mundo) -> None:
    """«Uno vivo por pedido» no puede depender de una lectura que falló.

    Crear igual sería crear un SEGUNDO seguimiento vivo sin saberlo, y justo
    cuando ERPNext no contesta — o sea, la regla se caía sola en el único
    momento en que hacía falta.
    """
    mundo["caidas"].add("comentarios")

    with pytest.raises(agenda.PropuestaInvalida):
        agenda.recordar(PEDIDO, epoch(15, dia=9), "x", ahora=epoch(9))


def test_un_borrador_ya_cerrado_termina_su_fila_en_vez_de_reintentar_para_siempre(
    mundo, marcas_sin_redis, monkeypatch
) -> None:
    """Las tres respuestas de la marca son tres cosas, y sólo una es «reintentá».

    Con True aplastado contra None, un borrador ya cerrado dejaba la fila viva
    para siempre: el handler contestaba «no pude» en cada barrido y la fila no
    llegaba nunca a un estado terminal.
    """
    from app import pendientes

    monkeypatch.setattr(pendientes, "_tiene_marca", lambda pedido, nombre: True)

    assert agenda.ejecutar_ahora(
        agenda.CIERRE_BORRADOR, PEDIDO, epoch(15), params={"horas": 4.0}
    ) is True
    assert agenda.vivas(PEDIDO, agenda.CIERRE_BORRADOR) == []


def test_una_cache_a_medias_obliga_a_reconstruir_aunque_el_indice_no_este_vacio(
    mundo, marcas_sin_redis, monkeypatch
) -> None:
    """El evento durable YA quedó, así que puede haber una fila fuera del índice.

    `_toca_reconstruir` sólo mira si el índice está VACÍO, y con otra fila
    adentro no lo está: sin la deuda anotada, esa fila no se despachaba nunca.
    """
    agenda.crear(PEDIDO, agenda.SEGUIMIENTO, epoch(15), ahora=epoch(9))
    assert agenda._toca_reconstruir() is False  # índice sano y no vacío

    def zadd_roto(*a, **k):
        raise RuntimeError("Redis no toma la escritura")

    monkeypatch.setattr(marcas_sin_redis, "zadd", zadd_roto)
    agenda.crear("SO-2026-00043", agenda.SEGUIMIENTO, epoch(16), ahora=epoch(9))

    assert agenda._toca_reconstruir() is True


def test_el_aviso_al_dueno_nombra_el_plazo_recalculado_y_no_el_que_traia_la_fila(
    mundo, marcas_sin_redis, entrega_a_las_17, monkeypatch
) -> None:
    """El otro consumidor del plazo recalculado: el TEXTO, no sólo el horario.

    El test de al lado prueba que la fila se corre cuando el plazo se mueve, y
    pasa igual aunque el mensaje siga armándose con la hora guardada — porque en
    ese caso no se manda ninguno. Éste es el caso en que SÍ se manda: el plazo
    ya venció, así que no hay reprogramación que tape el texto.
    """
    monkeypatch.setattr(agenda, "horas_de_aviso", lambda: 3.0)
    mundo["docs"][PEDIDO] = {**mundo["docs"][PEDIDO], "delivery_date": "2026-09-08"}
    # La fila trae una hora que quedó vieja (el pedido se movió y volvió, o la
    # hora de reparto cambió). El plazo de verdad, hoy, son las 14:00.
    agenda.crear(
        PEDIDO,
        agenda.RECORDATORIO_PLAZO_DUENO,
        epoch(13),
        params={"hora": "09:30"},
        ahora=epoch(9),
    )

    agenda.tick(ahora=epoch(15))

    assert len(mundo["al_dueno"]) == 1
    _, cuerpo = mundo["al_dueno"][0]
    assert "14:00" in cuerpo
    assert "09:30" not in cuerpo
