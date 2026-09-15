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
        yield fakes.LeaseDoble()

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


def test_si_quedaron_dos_seguimientos_vivos_sale_UN_solo_mensaje(
    mundo, marcas_sin_redis, monkeypatch
) -> None:
    """El residuo del orden «crear antes de cancelar», pagado donde no se nota.

    `recordar` crea el nuevo antes de cancelar el viejo para que un fallo de
    escritura no deje CERO recordatorios. El precio es que una cancelación
    fallida deje dos vivos. Dos FILAS pueden convivir un rato; dos MENSAJES al
    equipo por el mismo pedido no, y el barrido es el que lo hace valer.
    """
    # conftest fija TELEFONOS_EQUIPO="" a propósito y `router.STAFF` se arma al
    # importar, así que sin esto `encolar_equipo` no le manda a nadie y el test
    # contaría cero mensajes por el motivo equivocado.
    from app import router

    monkeypatch.setattr(router, "STAFF", ["5493510000001"])
    viejo = agenda.crear(
        PEDIDO, agenda.SEGUIMIENTO, epoch(15), params={"por_que": "el viejo"},
        ahora=epoch(9),
    )
    nuevo = agenda.crear(
        PEDIDO, agenda.SEGUIMIENTO, epoch(16), params={"por_que": "el nuevo"},
        ahora=epoch(10),
    )
    assert viejo is not None and nuevo is not None
    # Los dos vivos a la vez: la cancelación del viejo no entró.
    assert len(agenda.vivas(PEDIDO, agenda.SEGUIMIENTO)) == 2

    agenda.tick(ahora=epoch(17))

    salientes = [
        e for e in _en_cola(marcas_sin_redis)
        if e["evento"].startswith("agenda_seguimiento")
    ]
    assert len(salientes) == 1
    assert "el nuevo" in salientes[0]["texto"]


# --------------------------------------- un borrador CERRADO ya no está vivo
#
# Un pedido deja de estar vivo de dos maneras y tres handlers miraban una sola.
# `docstatus` 1/2 lo cubre el test de arriba; ésta es la otra: `docstatus=0` con
# `status="Closed"`, que es como terminan un rechazo del equipo, un vencimiento
# y la baja que pide un cliente.
#
# Los tres tests afirman LAS DOS MITADES. «No se mandó nada» solo lo cumple
# también una fila que quedó trabada reintentando para siempre contra un
# documento que no va a volver a cambiar, que es el otro final malo. Que la fila
# TERMINE es lo que distingue «se cerró» de «se colgó».


def _cerrado(mundo) -> None:
    """El borrador que el equipo rechazó, o que venció, o que el cliente bajó."""
    mundo["docs"][PEDIDO] = {**mundo["docs"][PEDIDO], "status": "Closed"}


def test_un_borrador_cerrado_no_recibe_el_aviso_de_entrega_y_la_fila_se_termina(
    mundo, marcas_sin_redis, entrega_a_las_17, monkeypatch
) -> None:
    """El peor de los tres: el mensaje va al CLIENTE.

    Sin esto el cliente lee «tu pedido llega a las 17» sobre un pedido que ya
    nadie va a preparar. La fila se creó cuando el borrador estaba vivo, que es
    justamente cuando `crear_pedido` la programa.
    """
    monkeypatch.setattr(agenda, "horas_de_aviso", lambda: 3.0)
    mundo["docs"][PEDIDO] = {**mundo["docs"][PEDIDO], "delivery_date": "2026-09-08"}
    agenda.crear(
        PEDIDO,
        agenda.AVISO_ANTES_DE_ENTREGA,
        epoch(14),
        params={"horas": 3.0, "hora": "17"},
        ahora=epoch(9),
    )
    _cerrado(mundo)

    agenda.tick(ahora=epoch(15))

    assert _en_cola(marcas_sin_redis) == []
    assert agenda.vivas(PEDIDO, agenda.AVISO_ANTES_DE_ENTREGA) == []


def test_un_borrador_cerrado_no_despierta_al_dueno_y_la_fila_se_termina(
    mundo, marcas_sin_redis, entrega_a_las_17, monkeypatch
) -> None:
    """El re-ping existe para comprar un plazo que un pedido cerrado ya no tiene."""
    monkeypatch.setattr(agenda, "horas_de_aviso", lambda: 3.0)
    mundo["docs"][PEDIDO] = {**mundo["docs"][PEDIDO], "delivery_date": "2026-09-08"}
    agenda.crear(
        PEDIDO,
        agenda.RECORDATORIO_PLAZO_DUENO,
        epoch(13),
        params={"hora": "14:00"},
        ahora=epoch(9),
    )
    _cerrado(mundo)

    agenda.tick(ahora=epoch(14))

    assert mundo["al_dueno"] == []
    assert agenda.vivas(PEDIDO, agenda.RECORDATORIO_PLAZO_DUENO) == []


def test_un_borrador_cerrado_no_genera_seguimiento_al_equipo_y_la_fila_se_termina(
    mundo, marcas_sin_redis, monkeypatch
) -> None:
    """Este handler no tenía NINGUNA guarda de documento, ni siquiera `docstatus`.

    `router.STAFF` se arma al importar y conftest deja `TELEFONOS_EQUIPO` vacío,
    así que sin fijarlo el test contaría cero mensajes por el motivo equivocado.
    """
    from app import router

    monkeypatch.setattr(router, "STAFF", ["5493510000001"])
    agenda.crear(
        PEDIDO,
        agenda.SEGUIMIENTO,
        epoch(15),
        params={"por_que": "el cliente dijo que confirmaba hoy"},
        ahora=epoch(9),
    )
    _cerrado(mundo)

    agenda.tick(ahora=epoch(16))

    assert [
        e for e in _en_cola(marcas_sin_redis)
        if e["evento"].startswith("agenda_seguimiento")
    ] == []
    assert agenda.vivas(PEDIDO, agenda.SEGUIMIENTO) == []


def test_no_se_programan_filas_sobre_un_borrador_ya_cerrado(
    mundo, marcas_sin_redis, entrega_a_las_17, monkeypatch
) -> None:
    """Programarlas sería crearlas muertas.

    `programar_para_entrega` es idempotente y los caminos de recuperación de
    `crear_pedido` vuelven a pasar por ella con el pedido ya existente: si para
    entonces el borrador se cerró, no hay nada que programar.
    """
    monkeypatch.setattr(agenda, "horas_de_aviso", lambda: 3.0)
    doc = {
        **mundo["docs"][PEDIDO],
        "delivery_date": "2026-09-08",
        "status": "Closed",
    }

    assert agenda.programar_para_entrega(doc, ahora=epoch(9)) == []


# ===========================================================================
# 7. LA BAJA QUE PIDE EL CLIENTE (W4).
#
#    Dos filas y no una, porque el mismo hecho tiene dos consumidores que
#    quieren respuestas OPUESTAS de las horas de silencio. Los tests siguen esa
#    división: lo que decide la herramienta se prueba en la herramienta, lo que
#    decide el barrido se prueba en el barrido, y la membresía en
#    `_HABLAN_CON_ALGUIEN` se prueba UNA VEZ POR FILA — son dos consumidores de
#    la regla de silencio, así que una sola mutación no los cubre.
# ===========================================================================


def _borrador_de(cuenta: str = "CLI-001") -> dict:
    return {"name": PEDIDO, "docstatus": 0, "customer": cuenta, "customer_name": "Demo"}


def _dar_de_baja(cuenta: str = "CLI-001") -> str:
    return tools_pedidos.dar_de_baja_pedido.func(
        pedido=PEDIDO, config=_config_cliente(cuenta)
    )


@pytest.fixture
def herramienta_con_reloj(mundo, monkeypatch):
    """La herramienta tiene su propio reloj y tampoco lee el de verdad."""
    monkeypatch.setattr(tools_pedidos, "_ahora_del_negocio", lambda: momento(9))
    mundo["docs"][PEDIDO] = _borrador_de()
    return mundo


# ------------------------------------------------------ lo que decide la tool


def test_un_pedido_confirmado_no_se_da_de_baja_y_se_deriva_a_una_persona(
    herramienta_con_reloj,
) -> None:
    """Un pedido confirmado es un COMPROMISO: lo cancela una persona.

    Se afirma el token que vuelve, no sólo que el documento quedó intacto. Un
    test que sólo mirara «no se escribió nada» seguiría verde con la guarda
    borrada, porque `soltar_reserva` también refuta un no-borrador — una hora
    después, en el barrido y fuera de la vista del cliente. Lo que esta guarda
    compra es que el CLIENTE se entere ahora, y eso sólo se ve en la respuesta.
    """
    herramienta_con_reloj["docs"][PEDIDO] = {**_borrador_de(), "docstatus": 1}

    respuesta = _dar_de_baja()

    assert "confirmado" in respuesta.lower()
    assert "persona" in respuesta.lower()
    # Y nada quedó agendado: la negativa no es decorativa.
    assert agenda.vivas(PEDIDO, agenda.BAJA_DE_PEDIDO) == []


def test_un_docstatus_ilegible_no_cuenta_como_borrador_y_no_rompe_el_turno(
    herramienta_con_reloj,
) -> None:
    """Fallar cerrado, y sin excepción.

    `int("vaya")` levanta, y una excepción adentro de una herramienta rompe el
    hilo de conversación del cliente en vez de contestarle. Lo ilegible NO es
    un borrador: se niega, como hace `agenda.por_que_ya_no_vive` con el mismo
    campo.
    """
    herramienta_con_reloj["docs"][PEDIDO] = {**_borrador_de(), "docstatus": "vaya"}

    respuesta = _dar_de_baja()

    assert "confirmado" in respuesta.lower()
    assert agenda.vivas(PEDIDO, agenda.BAJA_DE_PEDIDO) == []


def test_la_baja_de_un_pedido_ajeno_se_niega_con_LA_MISMA_frase_que_uno_que_no_existe(
    herramienta_con_reloj,
) -> None:
    """UN token, UNA frase, las dos ramas — y se afirma comparándolas.

    Dos textos distintos le dicen al MODELO cuál de las dos cosas pasó, y el
    modelo se lo escribe al cliente: con eso, probar números ajenos y mirar la
    respuesta enumera los pedidos de otro. `assert a == b` es lo que impide que
    las dos ramas se separen sin que nadie lo note.
    """
    ajeno = _dar_de_baja("CLI-OTRO")

    del herramienta_con_reloj["docs"][PEDIDO]
    inexistente = _dar_de_baja("CLI-001")

    assert ajeno == inexistente
    assert PEDIDO in ajeno and "no encontré" in ajeno.lower()
    assert agenda.vivas(PEDIDO, agenda.BAJA_DE_PEDIDO) == []


@pytest.mark.parametrize(
    "estado",
    [
        # Uno de ABIERTOS y, aparte, REVISION_HUMANA: una condición escrita
        # sobre un subconjunto pasa uno y falla el otro, así que hace falta
        # nombrar los dos. `ABIERTOS` deja REVISION_HUMANA afuera A PROPÓSITO.
        "pendiente",
        "revision_humana",
    ],
)
def test_una_decision_en_curso_frena_la_baja_en_cualquier_estado_no_terminal(
    herramienta_con_reloj, monkeypatch, estado
) -> None:
    """Cualquier estado NO TERMINAL lleva un plazo vivo que el barrido honra.

    Dejar que una baja le corra la carrera es cómo un pedido dado de baja
    recibe una contraoferta, un «acepto» y un Submit.
    """
    from app import solicitudes

    assert estado not in solicitudes.TERMINALES
    monkeypatch.setattr(
        solicitudes, "leer", lambda pedido: type("S", (), {"estado": estado})()
    )

    respuesta = _dar_de_baja()

    assert "decisión en curso" in respuesta.lower()
    assert agenda.vivas(PEDIDO, agenda.BAJA_DE_PEDIDO) == []


def test_una_solicitud_terminal_no_frena_la_baja(
    herramienta_con_reloj, monkeypatch
) -> None:
    """La otra mitad: una guarda que prohíbe TODO también pasaría la de arriba."""
    from app import solicitudes

    monkeypatch.setattr(
        solicitudes, "leer", lambda pedido: type("S", (), {"estado": "cumplida"})()
    )

    respuesta = _dar_de_baja()

    assert "baja tomada" in respuesta.lower()
    # Y NO afirma que el borrador ya esté cerrado: eso lo hace el barrido
    # después y puede fallar. Lo probado acá es que la baja quedó tomada.
    assert "cerrado" not in respuesta.lower()
    assert len(agenda.vivas(PEDIDO, agenda.BAJA_DE_PEDIDO)) == 1


def test_la_herramienta_esta_registrada_para_clientes_y_no_para_gerencia() -> None:
    """Definir el `@tool` NO lo hace alcanzable.

    El agente se arma de una lista escrita a mano al importar, y una
    herramienta sin registrar se contesta con «esa herramienta no existe para
    esta conversación». Todos los tests de comportamiento de arriba pueden
    estar verdes con la función inalcanzable, así que se afirma el REGISTRO.
    """
    from app import graph

    nombres_cliente = [h.name for h in graph.TOOLS_CLIENTES]
    nombres_gerencia = [h.name for h in graph.TOOLS_GERENCIA]

    assert "dar_de_baja_pedido" in nombres_cliente
    assert "dar_de_baja_pedido" not in nombres_gerencia


# --------------------------------------------------- lo que decide el barrido


def test_la_baja_suelta_la_reserva_escribe_la_marca_y_encadena_el_aviso_al_dueno(
    herramienta_con_reloj, marcas_sin_redis, monkeypatch
) -> None:
    """El camino entero, y las TRES cosas que tienen que pasar juntas."""
    from app import solicitudes

    monkeypatch.setattr(
        solicitudes,
        "soltar_reserva",
        lambda p: (True, solicitudes.LO_CERRO_ESTA_LLAMADA),
    )
    _dar_de_baja()

    # Ronda 1: suelta la reserva y ANOTA la fase. Todavía no hay marca — y la
    # fila sigue viva, que es lo que impide que un fallo de acá en adelante se
    # lleve el rastro puesto.
    agenda.tick(ahora=epoch(9, 1))
    assert not marcas.existe("baja_cliente", PEDIDO)
    assert len(agenda.vivas(PEDIDO, agenda.BAJA_DE_PEDIDO)) == 1

    # Ronda 2: la marca y el aviso, y recién ahí la fila se cierra.
    agenda.tick(ahora=epoch(9, 2))
    assert marcas.existe("baja_cliente", PEDIDO)
    assert agenda.vivas(PEDIDO, agenda.BAJA_DE_PEDIDO) == []
    assert len(agenda.vivas(PEDIDO, agenda.AVISO_BAJA_AL_DUENO)) == 1


def test_sin_prueba_de_que_solto_el_stock_no_hay_marca_ni_aviso_y_la_fila_sigue_viva(
    herramienta_con_reloj, marcas_sin_redis, monkeypatch
) -> None:
    """Una liberación no probada NO es una baja.

    Escribir `[baja-por-cliente]` acá pondría «lo dio de baja su cliente» en el
    rastro de un pedido que SIGUE tomando stock — la clase de mentira que
    `app/confirmacion.py` existe para que el sistema no pueda contar.
    """
    from app import solicitudes

    monkeypatch.setattr(
        solicitudes, "soltar_reserva", lambda p: (False, "sigue comprometiendo stock")
    )
    _dar_de_baja()

    agenda.tick(ahora=epoch(9, 1))

    assert not marcas.existe("baja_cliente", PEDIDO)
    assert agenda.vivas(PEDIDO, agenda.AVISO_BAJA_AL_DUENO) == []
    # Y la fila NO se cerró: lo que no se pudo hacer se vuelve a intentar.
    assert len(agenda.vivas(PEDIDO, agenda.BAJA_DE_PEDIDO)) == 1


def test_una_segunda_baja_del_mismo_pedido_no_escribe_una_segunda_marca(
    herramienta_con_reloj, marcas_sin_redis, monkeypatch
) -> None:
    """`marcas.escribir` es un `add_comment` pelado, SIN dedup.

    Lo que la hace exactamente-una-vez es que sólo se llega a escribirla con el
    borrador todavía abierto. La segunda baja encuentra el borrador cerrado,
    termina su fila y no escribe nada — ni marca ni aviso.
    """
    from app import solicitudes

    cerrado = {"n": 0}

    def soltar(pedido):
        cerrado["n"] += 1
        herramienta_con_reloj["docs"][PEDIDO] = {
            **_borrador_de(), "status": "Closed"
        }
        return True, solicitudes.LO_CERRO_ESTA_LLAMADA

    monkeypatch.setattr(solicitudes, "soltar_reserva", soltar)

    _dar_de_baja()
    agenda.tick(ahora=epoch(9, 1))   # suelta
    agenda.tick(ahora=epoch(9, 2))   # marca y aviso
    _dar_de_baja()
    agenda.tick(ahora=epoch(9, 3))   # la segunda no encuentra nada que hacer
    agenda.tick(ahora=epoch(9, 4))   # y el aviso de la primera sale

    # Una sola liberación, UNA sola marca, y al dueño se le dijo UNA vez.
    # Las tres mitades: `soltar_reserva` es idempotente por su cuenta, pero
    # `marcas.escribir` no lo es y el aviso tampoco, así que las tres hay que
    # afirmarlas por separado.
    assert cerrado["n"] == 1
    marcadas = marcas.filas("baja_cliente", PEDIDO, campos=["name"], techo=10)
    assert len(marcadas) == 1
    assert len(herramienta_con_reloj["al_dueno"]) == 1


def test_soltar_la_reserva_NO_espera_a_la_manana_y_el_aviso_al_dueno_SI(
    herramienta_con_reloj, marcas_sin_redis, monkeypatch
) -> None:
    """Las dos filas, los dos consumidores de la regla de silencio, un test.

    Un cliente que se da de baja a las 23:00 no puede quedarse con el stock
    tomado hasta las 07:00 porque el aviso al dueño espera a la mañana. Y el
    aviso al dueño SÍ espera: a las 03:00 no se lee, se resiente.

    Las DOS mitades se afirman. Que la reserva se suelte de madrugada es la
    mitad que rompe si `baja_de_pedido` entra a `_HABLAN_CON_ALGUIEN`; que el
    aviso NO salga de madrugada es la que rompe si `aviso_baja_al_dueno` sale.
    """
    from app import solicitudes

    soltadas: list[float] = []

    def soltar(pedido):
        soltadas.append(1.0)
        herramienta_con_reloj["docs"][PEDIDO] = {**_borrador_de(), "status": "Closed"}
        return True, solicitudes.LO_CERRO_ESTA_LLAMADA

    monkeypatch.setattr(solicitudes, "soltar_reserva", soltar)
    monkeypatch.setattr(tools_pedidos, "_ahora_del_negocio", lambda: momento(23))

    _dar_de_baja()

    # 23:30, plena madrugada: la reserva se suelta IGUAL. Y la ronda siguiente
    # —también de madrugada— escribe la marca y agenda el aviso.
    agenda.tick(ahora=epoch(23, 30))
    assert soltadas == [1.0]
    agenda.tick(ahora=epoch(23, 31))
    assert marcas.existe("baja_cliente", PEDIDO)
    assert len(agenda.vivas(PEDIDO, agenda.AVISO_BAJA_AL_DUENO)) == 1

    # OTRA ronda, todavía de madrugada. Hace falta que sea otra: la fila del
    # aviso NACIÓ en la ronda de arriba, y `tick` arma su lote al empezar, así
    # que en esa ronda no se despacha por el lote y no por las horas de
    # silencio. Afirmar el silencio ahí lo afirmaba de mentira — sacar
    # `aviso_baja_al_dueno` de `_HABLAN_CON_ALGUIEN` no mataba este test. Acá
    # la fila ya está en el índice y vencida, así que lo ÚNICO que puede
    # callarla es la regla que este test dice probar.
    agenda.tick(ahora=epoch(3, dia=9))
    assert herramienta_con_reloj["al_dueno"] == []
    assert len(agenda.vivas(PEDIDO, agenda.AVISO_BAJA_AL_DUENO)) == 1

    # Y a la mañana sí.
    agenda.tick(ahora=epoch(7, 30, dia=9))
    assert len(herramienta_con_reloj["al_dueno"]) == 1
    assert PEDIDO in herramienta_con_reloj["al_dueno"][0][0]


# --------------------------------------------- lo que encontró la revisión de Qodo
#
# Cinco hallazgos High sobre esta función, y tres eran la MISMA raíz: soltar la
# reserva es irreversible, lo de después puede fallar solo, y la fila terminaba
# igual — así que un fallo transitorio se llevaba puesto el rastro o el aviso y
# el reintento ya no podía distinguirse de una baja nueva.


def test_un_cierre_del_equipo_en_el_medio_NO_se_le_atribuye_al_cliente(
    herramienta_con_reloj, marcas_sin_redis, monkeypatch
) -> None:
    """`soltar_reserva` contesta True para un borrador que cerró CUALQUIERA.

    Entre el `_documento` del handler y la lectura de `soltar_reserva` el equipo
    puede rechazar el pedido. Si esa rama se toma como propia, el rastro del
    pedido termina diciendo «lo dio de baja su cliente» sobre un cierre que hizo
    una persona — un dato falso en la auditoría, que es peor que no tener nada.
    """
    from app import solicitudes

    monkeypatch.setattr(
        solicitudes,
        "soltar_reserva",
        lambda p: (True, solicitudes.YA_ESTABA_CERRADO),
    )
    _dar_de_baja()

    agenda.tick(ahora=epoch(9, 1))
    agenda.tick(ahora=epoch(9, 2))

    # Ni marca, ni aviso al dueño: no fue una baja del cliente.
    assert not marcas.existe("baja_cliente", PEDIDO)
    assert agenda.vivas(PEDIDO, agenda.AVISO_BAJA_AL_DUENO) == []
    # Y la fila se cierra: es un final convergente, no una falla que reintentar.
    assert agenda.vivas(PEDIDO, agenda.BAJA_DE_PEDIDO) == []


def test_si_la_marca_no_queda_la_fila_sigue_viva_en_vez_de_perder_el_rastro(
    herramienta_con_reloj, marcas_sin_redis, monkeypatch
) -> None:
    """La reserva ya está suelta y es irreversible; la marca todavía no.

    Terminar la fila acá perdería para siempre el único rastro de que la baja la
    pidió el cliente, y ningún reintento podría reconstruirlo: para entonces el
    borrador está cerrado y esa rama —correctamente— no escribe marca.
    """
    from app import solicitudes

    monkeypatch.setattr(
        solicitudes,
        "soltar_reserva",
        lambda p: (True, solicitudes.LO_CERRO_ESTA_LLAMADA),
    )
    _dar_de_baja()
    agenda.tick(ahora=epoch(9, 1))          # suelta y anota la fase

    herramienta_con_reloj["caidas"].add("escribir:[baja-por-cliente]")
    agenda.tick(ahora=epoch(9, 2))          # la marca no entra

    assert not marcas.existe("baja_cliente", PEDIDO)
    assert len(agenda.vivas(PEDIDO, agenda.BAJA_DE_PEDIDO)) == 1

    # Y cuando ERPNext vuelve, la fila termina lo suyo SIN volver a soltar nada.
    herramienta_con_reloj["caidas"].clear()
    agenda.tick(ahora=epoch(9, 3))

    assert marcas.existe("baja_cliente", PEDIDO)
    assert agenda.vivas(PEDIDO, agenda.BAJA_DE_PEDIDO) == []
    assert len(agenda.vivas(PEDIDO, agenda.AVISO_BAJA_AL_DUENO)) == 1


def test_la_fase_soltada_no_vuelve_a_soltar_la_reserva(
    herramienta_con_reloj, marcas_sin_redis, monkeypatch
) -> None:
    """Un reintento de la segunda fase no puede re-ejecutar la primera.

    Es la otra mitad del test de arriba: que la fila siga viva sólo sirve si lo
    que reintenta es lo que faltaba. Volver a llamar `soltar_reserva` sobre un
    borrador ya cerrado devolvería «ya estaba cerrado» y la baja perdería su
    marca justo en el reintento que existía para escribirla.
    """
    from app import solicitudes

    llamadas = {"n": 0}

    def soltar(pedido):
        llamadas["n"] += 1
        return True, solicitudes.LO_CERRO_ESTA_LLAMADA

    monkeypatch.setattr(solicitudes, "soltar_reserva", soltar)
    _dar_de_baja()

    agenda.tick(ahora=epoch(9, 1))
    herramienta_con_reloj["caidas"].add("escribir:[baja-por-cliente]")
    agenda.tick(ahora=epoch(9, 2))
    herramienta_con_reloj["caidas"].clear()
    agenda.tick(ahora=epoch(9, 3))

    assert llamadas["n"] == 1
    assert marcas.existe("baja_cliente", PEDIDO)


def test_si_el_aviso_al_dueno_no_queda_agendado_la_fila_no_se_cierra(
    herramienta_con_reloj, marcas_sin_redis, monkeypatch
) -> None:
    """El dueño tiene que enterarse de que le sacaron un pedido de la cola.

    Si la fila terminara con el aviso sin agendar, no quedaría nada vivo que lo
    reintentara y el dueño no se enteraría nunca.
    """
    from app import solicitudes

    monkeypatch.setattr(
        solicitudes,
        "soltar_reserva",
        lambda p: (True, solicitudes.LO_CERRO_ESTA_LLAMADA),
    )
    _dar_de_baja()
    agenda.tick(ahora=epoch(9, 1))

    monkeypatch.setattr(agenda, "crear", lambda *a, **k: None)
    agenda.tick(ahora=epoch(9, 2))

    assert len(agenda.vivas(PEDIDO, agenda.BAJA_DE_PEDIDO)) == 1


def test_una_decision_que_aparece_despues_del_turno_frena_la_baja_en_el_barrido(
    herramienta_con_reloj, marcas_sin_redis, monkeypatch
) -> None:
    """La comprobación de la herramienta no alcanza: pasaron 60 s sin lock.

    La herramienta mira la solicitud en el turno del cliente y el barrido cierra
    el borrador al menos un minuto después, con otro lock (`pendiente:`) que el
    de la aceptación (`solicitud:`). En ese hueco se puede abrir una decisión, o
    el cliente puede aceptar una contraoferta — y cerrar el borrador ahí es cómo
    un pedido dado de baja termina con un «acepto» y un Submit encima.
    """
    from app import solicitudes

    solto = {"n": 0}

    def soltar(pedido):
        solto["n"] += 1
        return True, solicitudes.LO_CERRO_ESTA_LLAMADA

    monkeypatch.setattr(solicitudes, "soltar_reserva", soltar)
    _dar_de_baja()

    # La decisión aparece DESPUÉS de que la herramienta contestó.
    monkeypatch.setattr(
        solicitudes, "leer", lambda pedido: type("S", (), {"estado": "pendiente"})()
    )
    agenda.tick(ahora=epoch(9, 1))

    assert solto["n"] == 0
    assert not marcas.existe("baja_cliente", PEDIDO)
    # Y no termina en silencio: al cliente ya se le dijo que quedaba dado de
    # baja, así que esto lo tiene que ver una persona.
    assert len(herramienta_con_reloj["al_dueno"]) == 1


def test_dos_workers_sobre_el_mismo_pedido_no_escriben_dos_marcas(
    herramienta_con_reloj, marcas_sin_redis, monkeypatch
) -> None:
    """El lease del lock son 60 s y `soltar_reserva` puede gastar tres esperas
    HTTP, así que dos workers pueden llegar a la segunda fase del mismo pedido.
    Ni la marca ni la fila del aviso se protegen solas: `marcas.escribir` es un
    `add_comment` pelado y `crear` con otro `ahora` da otro id.
    """
    from app import solicitudes

    monkeypatch.setattr(
        solicitudes,
        "soltar_reserva",
        lambda p: (True, solicitudes.LO_CERRO_ESTA_LLAMADA),
    )
    _dar_de_baja()
    agenda.tick(ahora=epoch(9, 1))

    (fila,) = agenda.vivas(PEDIDO, agenda.BAJA_DE_PEDIDO)
    # Los dos workers corren el HANDLER, no `_despachar`: con el lease vencido
    # los dos re-leyeron la fila mientras seguía viva, así que los dos entran a
    # la segunda fase. Por `_despachar` esto no se puede escribir — el primero
    # termina la fila y el segundo se va sin llamar al handler, que es el motivo
    # por el que la primera versión de este test no mataba ninguna mutación:
    # lo que lo hacía pasar era el estado terminal de la fila, no la dedup.
    agenda._baja_de_pedido(fila, epoch(9, 2))
    agenda._baja_de_pedido(fila, epoch(9, 3))

    marcadas = marcas.filas("baja_cliente", PEDIDO, campos=["name"], techo=10)
    assert len(marcadas) == 1
    assert len(agenda.vivas(PEDIDO, agenda.AVISO_BAJA_AL_DUENO)) == 1


def test_la_baja_suelta_la_reserva_bajo_EL_MISMO_lock_que_usan_las_decisiones(
    herramienta_con_reloj, marcas_sin_redis, monkeypatch
) -> None:
    """Re-leer sin el lock sólo achica la ventana; no la cierra.

    La aceptación de una contraoferta corre bajo `solicitud:{pedido}` y puede
    hacer Submit entre la lectura y el cierre. Lo único que vuelve atómico «no
    hay decisión» + «cerrá el borrador» es tomar ESE lock, y por eso se afirma
    cuál se tomó y no sólo que el resultado haya salido bien.

    Y se afirma el ORDEN: `pendiente:` antes que `solicitud:`, una sola
    dirección. Al revés, esto se abraza con cualquier camino que tome primero
    la decisión y después despache una fila.
    """
    from app import solicitudes

    tomados_al_soltar: list[list[str]] = []

    def soltar(pedido):
        tomados_al_soltar.append(list(herramienta_con_reloj["locks"]))
        return True, solicitudes.LO_CERRO_ESTA_LLAMADA

    monkeypatch.setattr(solicitudes, "soltar_reserva", soltar)
    _dar_de_baja()

    agenda.tick(ahora=epoch(9, 1))

    # La reserva se soltó CON el lock de la decisión ya tomado.
    (tomados,) = tomados_al_soltar
    assert f"solicitud:{PEDIDO}" in tomados
    # Y en el orden que no se traba: primero el de la fila, después el de la
    # decisión.
    assert tomados.index(f"pendiente:{PEDIDO}") < tomados.index(f"solicitud:{PEDIDO}")


def test_el_aviso_al_dueno_sale_a_su_hora_y_no_cuando_el_plazo_ya_se_venció(
    mundo, marcas_sin_redis, entrega_a_las_17, monkeypatch
) -> None:
    """El caso NORMAL, que era el que no estaba probado.

    La fila se agenda a `plazo - RE_PING_DUENO_HORAS`, así que cuando suena, al
    plazo todavía le falta una hora entera. Comparar el PLAZO con `ahora` daba
    verdadero justo entonces y la fila se reprogramaba a la hora a la que YA
    estaba, una y otra vez: el dueño no se enteraba hasta que faltaba menos de
    un minuto, que es exactamente la hora que este aviso existe para darle.

    Los dos tests de al lado prueban el plazo MOVIDO y las horas de silencio.
    Ninguno pasa por acá: el plazo no se mueve y son las 13:00.
    """
    monkeypatch.setattr(agenda, "horas_de_aviso", lambda: 3.0)
    mundo["docs"][PEDIDO] = {**mundo["docs"][PEDIDO], "delivery_date": "2026-09-08"}
    # Entrega 17:00 menos 3 h de aviso = plazo 14:00; el re-ping, una hora
    # antes: 13:00. Y a las 13:00 es cuando tiene que sonar.
    agenda.crear(
        PEDIDO,
        agenda.RECORDATORIO_PLAZO_DUENO,
        epoch(13),
        params={"hora": "14:00"},
        ahora=epoch(9),
    )

    agenda.tick(ahora=epoch(13))

    assert len(mundo["al_dueno"]) == 1
    _, cuerpo = mundo["al_dueno"][0]
    assert "14:00" in cuerpo
    # Y la fila se terminó: no quedó viva reprogramándose contra sí misma.
    assert agenda.vivas(PEDIDO, agenda.RECORDATORIO_PLAZO_DUENO) == []


def test_un_seguimiento_sin_equipo_al_que_avisar_no_se_da_por_hecho(
    mundo, marcas_sin_redis, monkeypatch
) -> None:
    """`encolar_equipo` no LEVANTA cuando no hay a quién avisar: devuelve False.

    Sin mirar ese booleano, la fila se cerraba como hecha habiéndole hablado a
    nadie, y sin reintento. Las dos mitades: que no se haya mandado nada es la
    fácil —no había destinatario—; la que importa es que la fila SIGA VIVA,
    porque es la diferencia entre «se reintenta cuando haya equipo» y «se
    perdió».
    """
    from app import router

    # `TELEFONOS_EQUIPO` vacío es lo que hay en un checkout limpio, y es
    # exactamente el caso que `encolar_equipo` reporta con False.
    monkeypatch.setattr(router, "STAFF", [])
    agenda.crear(
        PEDIDO, agenda.SEGUIMIENTO, epoch(15), params={"por_que": "el motivo"},
        ahora=epoch(9),
    )

    agenda.tick(ahora=epoch(16))

    assert _en_cola(marcas_sin_redis) == []
    assert len(agenda.vivas(PEDIDO, agenda.SEGUIMIENTO)) == 1


def test_una_cancelacion_trabada_le_cuesta_el_reemplazo_a_SU_tipo_y_a_ningun_otro(
    mundo, marcas_sin_redis, entrega_a_las_17, monkeypatch
) -> None:
    """El castigo por una cancelación trabada es de ese tipo, no de la ronda.

    Dos filas vivas del MISMO tipo sobre el mismo pedido son DOS avisos al
    mismo cliente: `avisos.encolar` deduplica por `(evento, pedido)` y el id de
    la fila va adentro del evento —hace falta, para que dos filas legítimas no
    se tapen—, así que la vieja que no se pudo cancelar y su reemplazo hablan
    las dos. Por eso el tipo trabado no se repone.

    Pero cortar la reconciliación entera ahí le sacaba el reemplazo al OTRO
    tipo, que sí se había cancelado bien: eso no es un aviso de más, es un
    aviso de MENOS, y no lo repone ningún reintento mientras el trabado siga
    trabado. Las tres mitades que importan: la vieja sigue viva, su tipo no
    tiene gemela, y el otro tipo SÍ quedó repuesto.
    """
    monkeypatch.setattr(agenda, "horas_de_aviso", lambda: 3.0)
    mundo["docs"][PEDIDO] = {**mundo["docs"][PEDIDO], "delivery_date": "2026-09-08"}
    vieja = agenda.crear(
        PEDIDO,
        agenda.AVISO_ANTES_DE_ENTREGA,
        epoch(14),
        params={"horas": 3.0, "hora": "17"},
        ahora=epoch(9),
    )
    assert vieja is not None
    # Y la del dueño, que es la que SÍ se va a poder cancelar. Sin ella el test
    # no puede distinguir «repongo lo que corresponde» de «no repongo nada».
    del_dueno = agenda.crear(
        PEDIDO,
        agenda.RECORDATORIO_PLAZO_DUENO,
        epoch(13),
        params={"hora": "14:00"},
        ahora=epoch(9),
    )
    assert del_dueno is not None

    # La entrega se corre al día 9: la reconciliación querría cancelar las dos y
    # reprogramarlas. Y ERPNext rechaza la escritura de la cancelación — las dos
    # la escriben con el mismo motivo, así que la caída se afina por tipo abajo.
    mundo["docs"][PEDIDO] = {**mundo["docs"][PEDIDO], "delivery_date": "2026-09-09"}
    # La caída tiene que ser SELECTIVA por tipo: `mundo["caidas"]` afina por
    # texto, pero las dos cancelaciones traen el mismo motivo y el tipo está en
    # otra parte del blob. Este envoltorio mira las dos cosas —y las saca del
    # cuerpo que le PASAN, no de lo que el test supone que dice—, así que
    # cancelar el tipo equivocado tampoco pasaría por acá.
    escribir = erpnext.registrar_comentario

    def trabar_solo_la_del_aviso(doctype, name, texto):
        cuerpo = str(texto)
        if '"evento":"cancelada"' in cuerpo and agenda.AVISO_ANTES_DE_ENTREGA in cuerpo:
            raise erpnext.ERPNextError("ERPNext no la tomó")
        return escribir(doctype, name, cuerpo)

    monkeypatch.setattr(erpnext, "registrar_comentario", trabar_solo_la_del_aviso)

    repuestas = agenda.reconciliar_entrega(PEDIDO, ahora=epoch(10))

    # El tipo trabado: la vieja sigue viva y no tiene gemela.
    vivas = agenda.vivas(PEDIDO, agenda.AVISO_ANTES_DE_ENTREGA)
    assert [f.id for f in vivas] == [vieja.id]
    assert agenda.AVISO_ANTES_DE_ENTREGA not in {f.tipo for f in repuestas}
    # El otro: se canceló, y su reemplazo está — con el plazo NUEVO, que es lo
    # que hace que reponerlo sirva de algo.
    del_dueno_vivas = agenda.vivas(PEDIDO, agenda.RECORDATORIO_PLAZO_DUENO)
    assert [f.id for f in del_dueno_vivas] != [del_dueno.id]
    assert len(del_dueno_vivas) == 1
    assert agenda.RECORDATORIO_PLAZO_DUENO in {f.tipo for f in repuestas}


def test_una_escritura_de_cache_atrasada_no_revive_una_fila_terminada(
    mundo, marcas_sin_redis
) -> None:
    """Lo durable y el caché son DOS escrituras, y pueden llegar en desorden.

    Los ids son determinísticos a propósito, así que dos workers trabajan sobre
    la MISMA fila. El que escribió `pendiente` primero puede cachear último, y
    con eso `leer` servía la foto vieja, la fila volvía al índice y se
    despachaba algo que ya estaba hecho.

    Las dos mitades, porque son dos daños distintos: que `leer` siga diciendo
    HECHO, y que la fila NO vuelva al índice — una fila fuera del índice no se
    despacha aunque su blob mienta, y un blob correcto con la fila adentro del
    índice se despacharía igual.
    """
    fila = agenda.crear(
        PEDIDO, agenda.SEGUIMIENTO, epoch(15), params={"por_que": "x"},
        ahora=epoch(9),
    )
    assert fila is not None
    agenda.registrar(fila, "hecha", ahora=epoch(10), estado=agenda.HECHO)

    # El worker lento vuelve con la foto PENDIENTE de las 09:00 y la cachea
    # tarde. Es la misma fila: mismo `sobre`, mismo id.
    agenda._cachear(fila)

    assert agenda.leer(PEDIDO, fila.id).estado == agenda.HECHO
    assert agenda.vivas(PEDIDO, agenda.SEGUIMIENTO) == []


# ===========================================================================
# 6. Lo que encontró la SEGUNDA revisión, sobre los arreglos de la primera.
#    Un arreglo que no se prueba es el mismo defecto mudado de lugar, y esto
#    vale igual para el arreglo de un arreglo.
# ===========================================================================


def test_un_aviso_al_equipo_ya_encolado_deja_terminar_la_fila_en_el_reintento(
    mundo, marcas_sin_redis, monkeypatch
) -> None:
    """«Ya estaba encolado» es HECHO, no fracaso, y la diferencia es un bucle.

    `avisos.encolar` contesta False cuando la clave de idempotencia ya está —o
    sea, cuando el aviso salió en una ronda anterior—. Contando sólo las
    llamadas que ESCRIBIERON, `encolar_equipo` reportaba fracaso en todo
    reintento, y una fila que necesitara una segunda ronda por cualquier otro
    motivo no podía terminar nunca: el mensaje ya había salido y el barrido
    seguía tratándola como pendiente para siempre.

    Las dos mitades: que la fila TERMINE, y que el cliente no reciba el aviso
    dos veces —terminar reenviando sería el otro final malo—.
    """
    from app import router

    monkeypatch.setattr(router, "STAFF", ["5493510000001"])
    agenda.crear(
        PEDIDO, agenda.SEGUIMIENTO, epoch(15), params={"por_que": "el motivo"},
        ahora=epoch(9),
    )

    # Primera ronda: el aviso se encola bien, pero la marca de HECHA no entra.
    # La fila queda viva — que es justo el estado desde el que hay que poder
    # salir.
    mundo["caidas"].add("escribir:\"hecha\"")
    assert agenda.tick(ahora=epoch(16)) == 0
    assert len(agenda.vivas(PEDIDO, agenda.SEGUIMIENTO)) == 1
    encolados = len(_en_cola(marcas_sin_redis))
    assert encolados == 1

    # Segunda ronda, con ERPNext sano: `encolar_equipo` ve la clave puesta y no
    # vuelve a escribir. Eso NO puede leerse como «no avisé a nadie».
    mundo["caidas"].clear()
    assert agenda.tick(ahora=epoch(16, 1)) == 1
    assert agenda.vivas(PEDIDO, agenda.SEGUIMIENTO) == []
    assert len(_en_cola(marcas_sin_redis)) == encolados


def test_ejecutar_ahora_despacha_su_hija_y_no_el_resto_del_pedido(
    mundo, marcas_sin_redis, monkeypatch
) -> None:
    """`ejecutar_ahora` no es un barrido escondido adentro de una llamada.

    Despachar la hija en el acto es el arreglo que mantiene el cierre
    sincrónico. Buscarla con `vivas(sobre)` a secas arrastraba TODA fila
    vencida del pedido —de cualquier tipo, fuera de su turno y fuera del tope
    de `POR_RONDA`, que existe para que una ronda no se coma el proceso—.

    Las dos mitades, porque cada una sola se cumple trivialmente: la hija SÍ
    sale en el acto (si no, el cierre dejó de ser sincrónico) y el seguimiento
    vencido del mismo pedido NO — y sigue vivo, esperando su barrido.
    """
    from app import pendientes, router, solicitudes

    monkeypatch.setattr(router, "STAFF", ["5493510000001"])
    monkeypatch.setattr(pendientes, "_tiene_marca", lambda so, m: False)
    monkeypatch.setattr(pendientes, "_sigue_esperando", lambda so: {"name": so})
    monkeypatch.setattr(solicitudes, "soltar_reserva", lambda so: (True, "soltado"))

    # Un seguimiento del MISMO pedido, vencido hace rato. No tiene nada que ver
    # con el cierre y no es asunto de esta llamada.
    seguimiento = agenda.crear(
        PEDIDO, agenda.SEGUIMIENTO, epoch(12), params={"por_que": "otra cosa"},
        ahora=epoch(9),
    )
    assert seguimiento is not None

    assert agenda.ejecutar_ahora(
        agenda.CIERRE_BORRADOR, PEDIDO, epoch(15), params={"horas": 4.0}
    ) is True

    eventos = {e["evento"].split(":")[0] for e in _en_cola(marcas_sin_redis)}
    assert "pendiente_cerrado_equipo" in eventos, eventos
    assert "seguimiento_equipo" not in eventos, eventos
    assert [f.id for f in agenda.vivas(PEDIDO, agenda.SEGUIMIENTO)] == [seguimiento.id]


def test_el_aviso_de_cierre_no_se_da_por_hecho_si_la_cola_no_lo_tomo(
    mundo, marcas_sin_redis, monkeypatch
) -> None:
    """El handler del aviso no hace nada irreversible: no puede usar `ya_paso`.

    El cierre lo hizo la fila madre, y probado. Lo único que aporta éste es el
    mensaje, así que darlo por hecho con la cola caída pierde exactamente la
    única cosa que la fila existía para hacer.

    Las dos mitades: que la fila siga VIVA con la cola caída, y que con la cola
    sana el aviso salga —seguir viva y no salir nunca sería el otro final malo—.
    """
    from app import avisos as cola
    from app import pendientes, router, solicitudes

    monkeypatch.setattr(router, "STAFF", ["5493510000001"])
    monkeypatch.setattr(pendientes, "_tiene_marca", lambda so, m: False)
    monkeypatch.setattr(pendientes, "_sigue_esperando", lambda so: {"name": so})
    monkeypatch.setattr(solicitudes, "soltar_reserva", lambda so: (True, "soltado"))

    real = cola.encolar
    monkeypatch.setattr(
        cola, "encolar", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("redis"))
    )
    agenda.ejecutar_ahora(
        agenda.CIERRE_BORRADOR, PEDIDO, epoch(15), params={"horas": 4.0}
    )

    assert _en_cola(marcas_sin_redis) == []
    vivas = agenda.vivas(PEDIDO, agenda.AVISO_CIERRE)
    assert len(vivas) == 1

    monkeypatch.setattr(cola, "encolar", real)
    assert agenda.tick(ahora=epoch(15, 1)) == 1
    eventos = {e["evento"].split(":")[0] for e in _en_cola(marcas_sin_redis)}
    assert "pendiente_cerrado" in eventos, eventos
    assert agenda.vivas(PEDIDO, agenda.AVISO_CIERRE) == []


def test_un_aviso_de_cierre_que_no_quedo_agendado_se_reintenta_y_sale_una_vez(
    mundo, marcas_sin_redis, monkeypatch
) -> None:
    """El cierre YA pasó; el aviso que no quedó tiene que poder volver.

    La fila del cierre se terminaba igual, así que un `crear` que no entraba
    dejaba el borrador cerrado y al cliente sin enterarse — para siempre, porque
    nadie volvía a mirar. Ahora la fila del cierre NO se termina, y la ronda
    siguiente entra por la rama de la marca y reintenta el `crear`.

    Las dos mitades que no se cumplen solas: que el aviso salga en el reintento,
    y que salga UNA vez —el reintento pasa por `crear` otra vez, y un id sacado
    del reloj habría dejado dos filas, o sea dos mensajes al mismo cliente—.
    """
    from app import pendientes, router, solicitudes

    monkeypatch.setattr(router, "STAFF", ["5493510000001"])
    marca = {"puesta": False}
    monkeypatch.setattr(pendientes, "_tiene_marca", lambda so, m: marca["puesta"])
    monkeypatch.setattr(pendientes, "_sigue_esperando", lambda so: {"name": so})
    sueltas = []
    monkeypatch.setattr(
        solicitudes, "soltar_reserva",
        lambda so: (marca.__setitem__("puesta", True), sueltas.append(so),
                    (True, "soltado"))[2],
    )

    # La escritura de la fila del aviso es la que no entra.
    mundo["caidas"].add('escribir:"tipo":"aviso_cierre"')
    agenda.ejecutar_ahora(
        agenda.CIERRE_BORRADOR, PEDIDO, epoch(15), params={"horas": 4.0}
    )
    assert agenda.vivas(PEDIDO, agenda.AVISO_CIERRE) == []
    assert len(agenda.vivas(PEDIDO, agenda.CIERRE_BORRADOR)) == 1

    # Ronda siguiente, con ERPNext sano. La marca ya está, así que el stock no
    # se suelta dos veces — y el aviso sí se repone. Sale en la ronda de después
    # y no en ésta, porque `tick` trabaja sobre la foto del índice que tomó al
    # empezar: acá el cierre dejó de ser sincrónico, y es el precio correcto de
    # no perder el aviso.
    mundo["caidas"].clear()
    agenda.tick(ahora=epoch(15, 1))
    assert len(agenda.vivas(PEDIDO, agenda.AVISO_CIERRE)) == 1
    agenda.tick(ahora=epoch(15, 2))

    assert sueltas == [PEDIDO]
    assert agenda.vivas(PEDIDO, agenda.CIERRE_BORRADOR) == []
    eventos = [e["evento"].split(":")[0] for e in _en_cola(marcas_sin_redis)]
    assert eventos.count("pendiente_cerrado") == 1, eventos


def test_el_reintento_del_cierre_no_resucita_un_aviso_que_ya_salio(
    mundo, marcas_sin_redis, monkeypatch
) -> None:
    """La rama de la marca corre más de una vez, y el aviso sale UNA.

    Es el reintento visto desde el otro lado: el cierre sale bien y el aviso
    también, pero la marca de HECHA de la fila del cierre no entra, así que la
    ronda siguiente vuelve a entrar por la rama de la marca. Ahí
    `_asegurar_aviso_de_cierre` mira si la fila del aviso EXISTE antes de
    crearla, en el estado que sea: sin esa lectura, `crear` le anexa un evento
    `creada` a una fila ya despachada y la revive —`vivas` se queda con el
    evento más nuevo—, o con un id sacado del reloj le deja una gemela. Los dos
    caminos terminan igual: el cliente recibe el mismo aviso dos veces.
    """
    from app import pendientes, router, solicitudes

    monkeypatch.setattr(router, "STAFF", ["5493510000001"])
    marca = {"puesta": False}
    monkeypatch.setattr(pendientes, "_tiene_marca", lambda so, m: marca["puesta"])
    monkeypatch.setattr(pendientes, "_sigue_esperando", lambda so: {"name": so})
    monkeypatch.setattr(
        solicitudes, "soltar_reserva",
        lambda so: (marca.__setitem__("puesta", True), (True, "soltado"))[1],
    )

    # El cierre y su aviso salen bien; lo que NO entra es la marca de hecha de
    # la fila del cierre, así que esa fila queda viva y se vuelve a despachar.
    mundo["caidas"].add('escribir:"evento":"hecha"')
    agenda.ejecutar_ahora(
        agenda.CIERRE_BORRADOR, PEDIDO, epoch(15), params={"horas": 4.0}
    )
    salieron = [e["evento"].split(":")[0] for e in _en_cola(marcas_sin_redis)]
    assert salieron.count("pendiente_cerrado") == 1, salieron
    assert len(agenda.vivas(PEDIDO, agenda.CIERRE_BORRADOR)) == 1

    # Segunda ronda, con ERPNext sano. El índice va por vencimiento, así que la
    # fila del aviso —que vence en 0— sale primero y TERMINA; recién después
    # entra la del cierre, y su rama de la marca se encuentra con un aviso ya
    # despachado, que es el caso que importa. La tercera ronda es la que haría
    # hablar a una gemela o a una fila resucitada, si las hubiera.
    mundo["caidas"].clear()
    agenda.tick(ahora=epoch(15, 1))
    agenda.tick(ahora=epoch(15, 2))

    de_nuevo = [e["evento"].split(":")[0] for e in _en_cola(marcas_sin_redis)]
    assert de_nuevo.count("pendiente_cerrado") == 1, de_nuevo
    assert agenda.vivas(PEDIDO, agenda.AVISO_CIERRE) == []
