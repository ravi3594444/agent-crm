"""Dashboard access boundary and safe ERP state mapping; no services required."""
import asyncio
import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "dashboard_under_test", Path(__file__).parents[1] / "app" / "dashboard.py"
)
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)

TOKEN = "dashboard-test-token-with-32-or-more-characters"


def request(method="GET", token=None, origin=None, path="/snapshot", body=None,
            trozos=None, leidos=None):
    """Un pedido al panel.

    `body` viaja entero; `trozos` lo manda en pedazos con `more_body`, que es
    como llega un `Transfer-Encoding: chunked` de verdad — y es la única forma
    de probar que el techo se mide mientras se junta y no después. `leidos`
    cuenta cuántas veces se pidió un trozo, que es lo ÚNICO que distingue las
    dos cosas: las dos terminan en 400, y sólo una deja de leer.
    """
    async def run():
        headers = [(b"host", b"agent.example")]
        if token is not None:
            headers.append((b"authorization", ("Bearer " + token).encode()))
        if origin:
            headers.append((b"origin", origin.encode()))
        events = []

        async def send(event):
            events.append(event)

        if trozos is not None:
            pendientes = list(trozos)

            async def receive():
                if leidos is not None:
                    leidos.append(1)
                if not pendientes:
                    return {"type": "http.disconnect"}
                trozo = pendientes.pop(0)
                return {"type": "http.request", "body": trozo,
                        "more_body": bool(pendientes)}
        else:
            crudo = body if isinstance(body, bytes) else (
                json.dumps(body).encode() if body is not None else b"")

            async def receive():
                return {"type": "http.request", "body": crudo}

        await dashboard.DashboardAPI()(
            {"type": "http", "method": method, "scheme": "https",
             "path": path, "root_path": "", "headers": headers}, receive, send
        )
        return events[0]["status"], dict(events[0]["headers"]), json.loads(events[1]["body"])
    return asyncio.run(run())


class DashboardBoundaryTest(unittest.TestCase):
    def test_disabled_and_short_tokens_never_read_data(self):
        for token in ("", "short"):
            with patch.dict(os.environ, {"DASHBOARD_API_TOKEN": token}), patch.object(dashboard, "snapshot") as snapshot:
                self.assertEqual(request(token=token)[0], 503)
                snapshot.assert_not_called()

    def test_absent_wrong_and_non_ascii_tokens_fail_before_erp(self):
        with patch.dict(os.environ, {"DASHBOARD_API_TOKEN": TOKEN}), patch.object(dashboard, "snapshot") as snapshot:
            for token in (None, "wrong", "é" * 40):
                self.assertEqual(request(token=token)[0], 401)
            snapshot.assert_not_called()

    def test_valid_access_is_no_store_and_read_only(self):
        with patch.dict(os.environ, {"DASHBOARD_API_TOKEN": TOKEN}), patch.object(dashboard, "snapshot", return_value={"mode": "live"}) as snapshot:
            code, headers, body = request(token=TOKEN)
            self.assertEqual(code, 200)
            self.assertEqual(headers[b"cache-control"], b"no-store")
            self.assertEqual(body, {"mode": "live"})
            snapshot.assert_called_once()
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                self.assertEqual(request(method, token=TOKEN)[0], 405)
            self.assertEqual(snapshot.call_count, 1)

    def test_cors_requires_exact_origin_not_wildcard_or_suffix(self):
        with patch.dict(os.environ, {"DASHBOARD_API_TOKEN": TOKEN, "DASHBOARD_ALLOWED_ORIGINS": "https://dashboard.example,*"}), patch.object(dashboard, "snapshot", return_value={}):
            code, headers, _ = request("OPTIONS", origin="https://dashboard.example")
            self.assertEqual(code, 200)
            self.assertEqual(headers[b"access-control-allow-origin"], b"https://dashboard.example")
            for origin in ("https://evil.example", "https://dashboard.example.evil.example", "null"):
                self.assertEqual(request("OPTIONS", origin=origin)[0], 403)
                self.assertEqual(request(token=TOKEN, origin=origin)[0], 403)
            self.assertEqual(request(token=TOKEN, origin="https://agent.example")[0], 200)

    def test_upstream_errors_are_redacted(self):
        with patch.dict(os.environ, {"DASHBOARD_API_TOKEN": TOKEN}), patch.object(dashboard, "snapshot", side_effect=RuntimeError("upstream secret")):
            code, _, body = request(token=TOKEN)
            self.assertEqual(code, 502)
            self.assertNotIn("secret", json.dumps(body))

    def test_unknown_routes_cannot_call_erp(self):
        with patch.object(dashboard, "snapshot") as snapshot:
            self.assertEqual(request(path="/orders/SO-1/submit")[0], 404)
            snapshot.assert_not_called()

    def test_non_finite_and_missing_numbers_stay_unknown(self):
        for value in (None, "", "bad", "nan", "inf", float("-inf")):
            self.assertIsNone(dashboard.number(value))
        self.assertEqual(dashboard.number("0"), 0)
        self.assertEqual(dashboard.number("-3.5"), -3.5)

    def test_order_state_preserves_closed_and_cancelled_drafts(self):
        for docstatus, erpstatus, expected in (
            (0, "Draft", "pending"), (0, "Closed", "closed"),
            (0, "On Hold", "on-hold"), (1, "To Deliver and Bill", "confirmed"),
            (1, "Completed", "completed"), (2, "Cancelled", "cancelled"),
            (None, "", "unknown"),
        ):
            row = dashboard.order_row({"docstatus": docstatus, "status": erpstatus})
            self.assertEqual(row["status"], expected)
            self.assertIsNone(row["total"])
            self.assertEqual(row["channel"], "ERPNext")


if __name__ == "__main__":
    unittest.main()


PERSONAL = "per-person-dashboard-token-with-32-or-more-chars"
STAFF_PHONE = "+54 9 351 000 0001"
STAFF_CANONICAL = "5493510000001"


class DashboardIdentityTest(unittest.TestCase):
    """Un token del dashboard prueba «soy este teléfono» sin WhatsApp.

    Las tres respuestas de `quien` son tres cosas distintas y se afirman por
    separado: `None` no pasa, `ANONIMO` pasa pero no es nadie, y un teléfono
    dice de quién es. `ANONIMO` es `""`, que es falsy y es VÁLIDO — un test
    que comparara por verdad no podría distinguirlo del rechazo.
    """

    def test_the_three_answers_are_three_different_things(self):
        # El teléfono va YA canónico: este test es sobre las tres respuestas y
        # nada más. Escrito raro, también fallaba cuando se rompía la
        # normalización, y entonces esa mutación mataba dos tests en vez del
        # suyo — que es medir acoplamiento, no protección.
        env = {"DASHBOARD_API_TOKEN": TOKEN,
               "DASHBOARD_TOKENS": f"{PERSONAL}:{STAFF_CANONICAL}"}
        with patch.dict(os.environ, env):
            self.assertIsNone(dashboard.quien("Bearer wrong"))
            self.assertIsNone(dashboard.quien(TOKEN))  # sin el esquema Bearer
            self.assertEqual(dashboard.quien("Bearer " + TOKEN), dashboard.ANONIMO)
            self.assertEqual(dashboard.quien("Bearer " + PERSONAL), STAFF_CANONICAL)

    def test_a_personal_token_alone_is_a_configured_install(self):
        """La regresión concreta: mirar sólo el token compartido daba 503 a
        una instalación configurada entera con tokens por persona."""
        with patch.dict(os.environ, {"DASHBOARD_TOKENS": f"{PERSONAL}:{STAFF_PHONE}"}, clear=False):
            os.environ.pop("DASHBOARD_API_TOKEN", None)
            self.assertTrue(dashboard.hay_acceso_configurado())
            with patch.object(dashboard, "snapshot", return_value={"mode": "live"}):
                self.assertEqual(request(token=PERSONAL)[0], 200)
            self.assertEqual(request(token="wrong")[0], 401)

    def test_a_guessable_personal_token_is_not_authentication(self):
        with patch.dict(os.environ, {"DASHBOARD_TOKENS": f"short:{STAFF_PHONE}"}, clear=False):
            os.environ.pop("DASHBOARD_API_TOKEN", None)
            self.assertEqual(dashboard._tokens_por_persona(), {})
            self.assertFalse(dashboard.hay_acceso_configurado())
            self.assertIsNone(dashboard.quien("Bearer short"))

    def test_only_a_named_staff_phone_may_decide_anything(self):
        """`decisiones.*` no verifica quién llama: confía en el llamador.

        Por eso las tres respuestas importan acá y no sólo «entró o no
        entró». El token compartido nunca alcanza: una decisión sin nombre
        no se puede auditar.
        """
        from app import router

        with patch.object(router, "STAFF", [STAFF_CANONICAL]):
            self.assertTrue(dashboard.puede_decidir(STAFF_CANONICAL))
            self.assertFalse(dashboard.puede_decidir(dashboard.ANONIMO))
            self.assertFalse(dashboard.puede_decidir(None))
            self.assertFalse(dashboard.puede_decidir("5493510009999"))

    def test_two_tokens_for_one_person_leave_that_person_with_none(self):
        """Un teléfono repetido tumba TODAS sus entradas, no sólo la segunda.

        ESTO INVIERTE LO QUE ESTE TEST AFIRMABA ANTES, que era que el primero
        siguiera entrando para no romperle el acceso a quien ya lo tenía. Era una
        decisión deliberada y estaba equivocada, porque lo único que un token por
        persona compra es poder revocarlo, y revocar es sacar una línea del
        `.env`. Con dos líneas y el primero válido, las dos son indistinguibles
        en el archivo: el dueño saca una, cree que cortó el acceso, y la otra
        sigue confirmando pedidos; saca la otra y ASCIENDE un token que hasta ese
        momento no hacía nada. Aceptar la primera y aceptar las dos le dan al
        dueño EL MISMO resultado para cualquier línea que borre, así que la
        versión vieja no protegía nada — sólo bajaba el número de credenciales
        vivas, que es otro objetivo y menor.

        Fallar cerrado cuesta poco acá y por razones que hay que nombrar, porque
        en otro sistema no valdrían: el panel no es la única puerta para
        confirmar (el botón de WhatsApp no mira `DASHBOARD_TOKENS`), y
        `configurar_dashboard.py` se niega a emitir el segundo, así que para
        llegar a este estado hay que haber editado el `.env` a mano.

        La mitad que NO cambia y sigue afirmada acá: el teléfono se normaliza,
        así que `+54 9 351 000 0001` y `005493510000001` son una persona y no
        dos — sin eso no habría duplicado que detectar.
        """
        otro = "second-token-for-the-same-person-32-plus"
        env = {"DASHBOARD_TOKENS": f"{PERSONAL}:+54 9 351 000 0001,{otro}:005493510000001"}
        with patch.dict(os.environ, env):
            self.assertIsNone(dashboard.quien("Bearer " + PERSONAL))
            self.assertIsNone(dashboard.quien("Bearer " + otro))

    def test_one_token_written_twice_never_resolves_to_the_first_phone(self):
        """El MISMO token en dos entradas con teléfonos distintos no es nadie.

        Es el duplicado que menos se veía y el que más caro sale. El token
        repetido se comprobaba ANTES que el teléfono, así que las dos entradas
        caían en esa rama y el token se quedaba resolviendo al PRIMER teléfono.
        O sea: a alguien se le da ese token para que mire, y entra con la
        identidad de la otra persona — y si esa otra está en `TELEFONOS_EQUIPO`,
        con el derecho a confirmar pedidos, que es lo único que el panel puede
        hacer que mueva plata.

        No alcanza con comprobar que el token no entra: hay que comprobar que no
        entra COMO EL PRIMERO, que es la escalada.
        """
        env = {"DASHBOARD_TOKENS": f"{PERSONAL}:+54 9 351 000 0001,{PERSONAL}:5493519998888"}
        with patch.dict(os.environ, env):
            self.assertIsNone(dashboard.quien("Bearer " + PERSONAL))
            self.assertNotEqual(dashboard.quien("Bearer " + PERSONAL), STAFF_CANONICAL)

    def test_the_refusal_of_every_duplicate_says_why_without_printing_it(self):
        """Un motivo POR ENTRADA caída, y ninguno lleva el token adentro.

        Dos motivos, no uno: las dos entradas se rechazan, así que las dos
        tienen que poder nombrarse. Con un solo motivo el dueño borra la que el
        mensaje nombra y sigue sin panel, sin saber por qué.

        Y el motivo se lee en la salida del preflight, que es exactamente lo que
        una persona pega en un chat cuando algo no anda: por eso nombra la
        ENTRADA y nunca el token.
        """
        otro = "second-token-for-the-same-person-32-plus"
        _, problemas = dashboard.entradas_de_tokens(
            f"{PERSONAL}:+54 9 351 000 0001,{otro}:005493510000001"
        )

        self.assertEqual(len(problemas), 2)
        self.assertIn("entrada 1", problemas[0])
        self.assertIn("entrada 2", problemas[1])
        for motivo in problemas:
            self.assertNotIn(otro, motivo)
            self.assertNotIn(PERSONAL, motivo)

    def test_a_good_entry_still_enters_when_another_one_is_a_duplicate(self):
        """Fallar cerrado es por TELÉFONO, no por archivo.

        La otra mitad de la de arriba, y la que impide que el arreglo se pase de
        largo: una entrada repetida tumba a sus gemelas y a nadie más. Sin esto,
        «rechazá el duplicado» podía escribirse como «rechazá el archivo entero»
        y dejar sin panel a todo el equipo por una línea mal pegada de otro.
        """
        otro = "second-token-for-the-same-person-32-plus"
        tercero = "a-token-for-somebody-else-entirely-32-plus"
        env = {"DASHBOARD_TOKENS": (
            f"{PERSONAL}:+54 9 351 000 0001,{otro}:005493510000001,"
            f"{tercero}:5493519998888"
        )}
        with patch.dict(os.environ, env):
            self.assertEqual(dashboard.quien("Bearer " + tercero), "5493519998888")
            self.assertIsNone(dashboard.quien("Bearer " + PERSONAL))

    def test_a_shared_token_with_spaces_around_it_still_authenticates(self):
        """El token compartido se compara con la MISMA regla en los dos lados.

        `readiness` lo recortaba antes de validarlo y `quien()` lo comparaba
        crudo, así que un `DASHBOARD_API_TOKEN=" … "` entrecomillado —que es como
        se pega un secreto— daba preflight en VERDE y 401 en todas las
        peticiones: el panel recorta lo que la persona tipea
        (`dashboard_ui/app.js`), y eso no coincidía nunca con el valor del
        entorno. Hallazgo 4 de la review de #45.

        La mutación que mata a este test: que `token_compartido()` vuelva a
        `os.getenv(...)` sin `.strip()`.
        """
        with patch.dict(os.environ, {"DASHBOARD_API_TOKEN": f"  {TOKEN}  "}):
            self.assertEqual(dashboard.quien("Bearer " + TOKEN), dashboard.ANONIMO)
            self.assertTrue(dashboard.hay_acceso_configurado())

    def test_a_per_person_token_with_spaces_around_it_also_authenticates(self):
        """El TERCER consumidor de la misma regla, y no tenía test.

        `normalizar_token` la usan tres: el token por persona, el compartido y
        `readiness`. Los otros dos los cuida un test cada uno; éste no lo cuidaba
        ninguno —sacarle el recorte a esta línea dejaba las 2897 en verde—, y es
        el mismo síntoma del hallazgo 4 en el otro lado del `.env`: una entrada
        `DASHBOARD_TOKENS=" tok ":<tel>` que el preflight cuenta como válida y
        que después no autentica a nadie.
        """
        with patch.dict(
            os.environ, {"DASHBOARD_TOKENS": f"  {PERSONAL}  :{STAFF_CANONICAL}"}
        ):
            self.assertEqual(dashboard.quien("Bearer " + PERSONAL), STAFF_CANONICAL)


# ------------------------------------------------- ventas y consejos del panel
ERP_ERROR = type("ERPNextError", (RuntimeError,), {})


def _erp_falso(pedidos, renglones=(), llamadas=None):
    from contextlib import nullcontext

    registro = llamadas if llamadas is not None else []

    class Falso:
        ERPNextError = ERP_ERROR
        default_company = staticmethod(lambda: "Lacteos Test SA")
        manager_scope = staticmethod(nullcontext)
        get_doc = staticmethod(lambda *a, **k: {"default_currency": "ARS"})

        @staticmethod
        def get_list(doctype, **kwargs):
            registro.append((doctype, kwargs))
            return list(renglones) if doctype == "Sales Order Item" else list(pedidos)

    return Falso


_AUSENTE = object()


def _con_modulos(modulos, fn):
    """Sustituye módulos de `app` y los DEVUELVE como estaban, incluso si no
    estaban.

    La primera versión de esto restauraba sólo cuando el valor viejo no era
    None, así que la primera vez —cuando `app.erpnext` todavía no era un
    atributo del paquete— dejaba el DOBLE puesto para siempre. Los tests de
    este archivo seguían pasando y caían nueve de `test_readiness.py`, que
    importa `app.dashboard` y se comía el ERPNext falso. Un centinela distingue
    «estaba en None» de «no estaba», que es justo la diferencia que se perdía.
    """
    import sys

    real = {k: sys.modules.get(k, _AUSENTE) for k in modulos}
    paquete = sys.modules.get("app")
    previos = {k.split(".")[1]: getattr(paquete, k.split(".")[1], _AUSENTE) for k in modulos}
    try:
        for k, v in modulos.items():
            sys.modules[k] = v
            if paquete is not None:
                setattr(paquete, k.split(".")[1], v)
        return fn()
    finally:
        for k, v in real.items():
            if v is _AUSENTE:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        if paquete is not None:
            for nombre, v in previos.items():
                if v is _AUSENTE:
                    if hasattr(paquete, nombre):
                        delattr(paquete, nombre)
                else:
                    setattr(paquete, nombre, v)


class PolicyFalso:
    import datetime as _dt
    _hoy_del_negocio = staticmethod(lambda: __import__("datetime").date(2026, 9, 15))


class VentasDelPanelTest(unittest.TestCase):
    def _correr(self, pedidos, renglones=(), dias=7, llamadas=None):
        return _con_modulos(
            {"app.erpnext": _erp_falso(pedidos, renglones, llamadas), "app.policy": PolicyFalso},
            lambda: dashboard.sales(dias),
        )

    def test_el_periodo_pedido_es_el_que_se_mide(self):
        """`?days=` no puede ser decorativo: si el backend lo ignorara, la
        pantalla diría «últimos 7 días» sobre números de 30, sin que nada falle.
        Ése es el peor modo de falla posible en una pantalla de ventas."""
        pedidos = [
            {"name": "SO-1", "customer": "C1", "customer_name": "Uno",
             "grand_total": 100, "transaction_date": "2026-09-14"},
            {"name": "SO-2", "customer": "C2", "customer_name": "Dos",
             "grand_total": 400, "transaction_date": "2026-09-01"},
        ]
        siete = self._correr(pedidos, dias=7)
        treinta = self._correr(pedidos, dias=30)
        self.assertEqual(siete["orders"], 1, siete)
        self.assertEqual(siete["total"], 100, siete)
        self.assertEqual(siete["since"], "2026-09-09", siete)
        self.assertEqual(treinta["orders"], 2, treinta)
        self.assertEqual(treinta["since"], "2026-08-17", treinta)

    def test_el_promedio_sin_pedidos_es_None_y_no_cero(self):
        salida = self._correr([])
        self.assertIsNone(salida["averageOrder"], salida)
        self.assertEqual(salida["orders"], 0)

    def test_la_tabla_hija_se_pide_con_su_doctype_padre(self):
        """Frappe rechaza una tabla hija sin `parent`: es el bug por el que
        `informe(que="stock_bajo")` nunca contestó contra un ERPNext real."""
        llamadas = []
        self._correr([{"name": "SO-1", "customer": "C1", "customer_name": "Uno",
                       "grand_total": 100, "transaction_date": "2026-09-14"}],
                     [], llamadas=llamadas)
        hija = [k for d, k in llamadas if d == "Sales Order Item"]
        self.assertEqual(len(hija), 1, llamadas)
        self.assertEqual(hija[0].get("parent"), "Sales Order", hija)


class DiasPedidosTest(unittest.TestCase):
    """Un entero sin techo del lado del cliente es pedirle a ERPNext que recorra
    todo. Se acota acá, donde el cliente no llega."""

    def test_se_acota_arriba_y_abajo_y_lo_ilegible_cae_en_el_default(self):
        self.assertEqual(dashboard.dias_pedidos(b"days=7"), 7)
        self.assertEqual(dashboard.dias_pedidos(b"days=30"), 30)
        self.assertEqual(dashboard.dias_pedidos(b"days=99999"), dashboard.DIAS_VENTAS_MAX)
        self.assertEqual(dashboard.dias_pedidos(b"days=0"), 1)
        self.assertEqual(dashboard.dias_pedidos(b"days=-5"), 1)
        self.assertEqual(dashboard.dias_pedidos(b"days=hola"), dashboard.DIAS_VENTAS_DEFECTO)
        self.assertEqual(dashboard.dias_pedidos(b""), dashboard.DIAS_VENTAS_DEFECTO)


class ConsejosDelPanelTest(unittest.TestCase):
    def _consejo(self, clase, clave, sobre, peso=0.0):
        c = type("C", (), {})()
        c.clase, c.clave, c.sobre = clase, clave, sobre
        c.titulo, c.cuerpo, c.supuesto, c.peso = "T", "B", "S", peso
        return c

    def _correr(self, *, activo=True, por_clase=None, rompe=()):
        import datetime as _dt
        hechos, prueba = por_clase or {}, self

        class ConsejosFalso:
            PERDIDA, DORMIDO, DEUDA, QUIEBRE = "perdida", "dormido", "deuda", "quiebre"
            activo = staticmethod(lambda: activo)

            @staticmethod
            def _d(clase, dia):
                if clase in rompe:
                    raise RuntimeError("lectura caída")
                prueba.assertIsInstance(dia, _dt.date)
                return list(hechos.get(clase, ()))

            perdidas = staticmethod(lambda dia: ConsejosFalso._d("perdida", dia))
            dormidos = staticmethod(lambda dia: ConsejosFalso._d("dormido", dia))
            deudas = staticmethod(lambda dia: ConsejosFalso._d("deuda", dia))
            quiebres = staticmethod(lambda dia: ConsejosFalso._d("quiebre", dia))

        return _con_modulos(
            {"app.consejos": ConsejosFalso, "app.erpnext": _erp_falso([]), "app.policy": PolicyFalso},
            dashboard.advice,
        )

    def test_apagado_informa_el_interruptor_y_muestra_igual(self):
        """`activo()` apaga el ENVÍO —Meta cobra por mensaje—, no la lectura."""
        salida = self._correr(activo=False, por_clase={
            "perdida": [self._consejo("perdida", "p:1", "SO-1")]})
        self.assertFalse(salida["enabled"], salida)
        self.assertEqual(len(salida["items"]), 1, salida)

    def test_el_enlace_es_el_del_tipo_y_los_otros_dos_van_en_null(self):
        salida = self._correr(por_clase={
            "perdida": [self._consejo("perdida", "p:1", "SO-1")],
            "deuda": [self._consejo("deuda", "d:1", "CUST-1")],
            "quiebre": [self._consejo("quiebre", "q:1", "LECHE")],
        })
        por_clase = {i["kind"]: i for i in salida["items"]}
        self.assertEqual(por_clase["perdida"]["orderId"], "SO-1")
        self.assertIsNone(por_clase["perdida"]["customerId"])
        self.assertEqual(por_clase["deuda"]["customerId"], "CUST-1")
        self.assertIsNone(por_clase["deuda"]["orderId"])
        self.assertEqual(por_clase["quiebre"]["productId"], "LECHE")
        self.assertIsNone(por_clase["quiebre"]["orderId"])

    def test_una_clase_caida_no_se_lleva_las_otras_tres(self):
        salida = self._correr(rompe=("deuda",), por_clase={
            "perdida": [self._consejo("perdida", "p:1", "SO-1")]})
        self.assertEqual(salida["errors"], ["deuda"], salida)
        self.assertEqual([i["id"] for i in salida["items"]], ["p:1"], salida)

    def test_el_peso_ordena_y_no_viaja(self):
        salida = self._correr(por_clase={"perdida": [
            self._consejo("perdida", "p:chico", "SO-1", peso=10.0),
            self._consejo("perdida", "p:grande", "SO-2", peso=900.0),
        ]})
        self.assertEqual([i["id"] for i in salida["items"]], ["p:grande", "p:chico"], salida)
        self.assertNotIn("weight", salida["items"][0])


class VentasHallazgosTest(unittest.TestCase):
    """Lo que la revisión encontró y este archivo no probaba."""

    def _correr(self, pedidos, renglones=(), dias=7, llamadas=None, romper=()):
        from contextlib import nullcontext

        registro = llamadas if llamadas is not None else []

        class Falso:
            ERPNextError = ERP_ERROR
            default_company = staticmethod(lambda: "Lacteos Test SA")
            manager_scope = staticmethod(nullcontext)

            @staticmethod
            def get_doc(doctype, name, timeout=None):
                if "moneda" in romper:
                    raise ERP_ERROR("sin moneda")
                return {"default_currency": "ARS"}

            @staticmethod
            def get_list(doctype, **kwargs):
                registro.append((doctype, kwargs))
                if doctype == "Sales Order Item":
                    if "renglones" in romper:
                        raise ERP_ERROR("sin renglones")
                    return list(renglones)
                return list(pedidos)

        return _con_modulos({"app.erpnext": Falso, "app.policy": PolicyFalso},
                            lambda: dashboard.sales(dias))

    def test_un_pedido_con_fecha_futura_no_entra(self):
        """ERPNext deja fechar un Sales Order adelante. Sin tope superior entra
        en los totales y arma una fila posterior a `until`, y el validador del
        panel rechaza el informe ENTERO por incoherente."""
        llamadas = []
        self._correr([], llamadas=llamadas)
        filtros = next(k["filters"] for d, k in llamadas if d == "Sales Order")
        operadores = {(f[0], f[1]) for f in filtros}
        self.assertIn(("transaction_date", "<="), operadores, filtros)
        self.assertIn(("transaction_date", ">="), operadores, filtros)

    def test_la_moneda_que_no_se_pudo_leer_es_None_y_no_cadena_vacia(self):
        """El validador acepta una moneda ISO o `null`. Con `""` tiraba la
        respuesta entera, incluido el `errors` que explicaba la falla."""
        salida = self._correr([], romper=("moneda",))
        self.assertIsNone(salida["currency"], salida)
        self.assertIn("currency", salida["errors"], salida)

    def test_un_ranking_que_no_se_pudo_leer_es_None_y_no_una_lista_vacia(self):
        """`[]` dice «no se vendió nada»; `None` dice «no pude mirar». Es la
        misma distinción que `averageOrder`, un campo más allá."""
        pedidos = [{"name": "SO-1", "customer": "C1", "customer_name": "Uno",
                    "grand_total": 100, "transaction_date": "2026-09-14"}]
        salida = self._correr(pedidos, romper=("renglones",))
        self.assertIsNone(salida["topProducts"], salida)
        self.assertIn("products", salida["errors"], salida)
        # El ranking de clientes sale de los pedidos, que SÍ se leyeron.
        self.assertEqual(len(salida["topCustomers"]), 1, salida)
        # Y no se anuncia un recorte que nadie midió.
        self.assertNotIn("topProducts", salida["truncated"], salida)

    def test_el_recorte_se_anuncia_cuando_el_tope_se_alcanza_de_verdad(self):
        """El centinela `+1` distingue «llegué al tope» de «esto es todo»."""
        pedidos = [{"name": "SO-1", "customer": "C1", "customer_name": "Uno",
                    "grand_total": 100, "transaction_date": "2026-09-14"}]
        tope = dashboard.LIMIT * 8
        muchos = [{"parent": "SO-1", "item_code": f"I{i}", "item_name": "x",
                   "qty": 1, "amount": 1} for i in range(tope + 1)]
        salida = self._correr(pedidos, muchos)
        self.assertIn("topProducts", salida["truncated"], salida)
        justo = muchos[:tope]
        salida = self._correr(pedidos, justo)
        self.assertNotIn("topProducts", salida["truncated"], salida)


# ---------------------------------------------------------------------------
# Los ajustes del negocio. El panel PROPONE; el código lo confirma WhatsApp.
# ---------------------------------------------------------------------------
PERSONA = "dashboard-named-token-with-32-or-more-characters"
TELEFONO = "5493511111111"


def _con_persona():
    """Un token CON NOMBRE y ese número en el equipo. Los dos hacen falta:
    `puede_decidir` exige las dos mitades, y con una sola esto mediría la otra."""
    from app import router

    return (
        patch.dict(os.environ, {"DASHBOARD_TOKENS": f"{PERSONA}:{TELEFONO}"}),
        patch.object(router, "es_equipo", lambda t: t == TELEFONO),
    )


class AjustesDelPanelTest(unittest.TestCase):
    def test_a_read_only_token_can_look_at_settings_and_cannot_propose(self):
        """LA GUARDA QUE MÁS CARO SALE SI FALTA. El token COMPARTIDO está
        autenticado y no es nadie en particular, así que puede mirar y nunca
        decidir — y una ruta de escritura nueva que no se nombre en la guarda
        del 403 queda alcanzable por él sin que nada se ponga rojo.

        MUTACIÓN: cambiar la guarda a `if confirm_match and not puede_decidir`,
        o sea volver a la condición de antes de esta ruta. Cae ésta y sólo ésta.
        """
        with patch.dict(os.environ, {"DASHBOARD_API_TOKEN": TOKEN}), \
                patch.object(dashboard, "settings", return_value={"groups": []}), \
                patch.object(dashboard, "proponer_ajuste") as proponer:
            self.assertEqual(request(token=TOKEN, path="/settings")[0], 200)
            code, _, body = request(
                "POST", token=TOKEN, path="/settings/propose",
                body={"setting": "tope", "value": "999999"},
            )
            self.assertEqual(code, 403)
            self.assertIn("cannot change settings", json.dumps(body))
            proponer.assert_not_called()

    def test_proposing_needs_a_post_and_a_setting(self):
        entorno, equipo = _con_persona()
        with entorno, equipo, patch.object(dashboard, "proponer_ajuste") as proponer:
            self.assertEqual(
                request(token=PERSONA, path="/settings/propose")[0], 405)
            code, _, body = request(
                "POST", token=PERSONA, path="/settings/propose", body={"value": "x"})
            self.assertEqual(code, 400)
            self.assertIn("which setting", json.dumps(body))
            proponer.assert_not_called()

    def test_a_body_that_cannot_be_used_never_reaches_the_write_pool(self):
        entorno, equipo = _con_persona()
        with entorno, equipo, patch.object(dashboard, "proponer_ajuste") as proponer:
            for cuerpo in (b"", b"not json", b'"just a string"', b"[1,2]"):
                code, _, _ = request(
                    "POST", token=PERSONA, path="/settings/propose", body=cuerpo)
                self.assertEqual(code, 400, cuerpo)
            proponer.assert_not_called()

    def test_an_oversized_body_is_cut_while_it_arrives_not_after(self):
        """El techo se mide ADENTRO del bucle, y el 400 NO lo demuestra.

        Ésa es la parte que casi se me pasa: comprobando el largo después del
        `while`, un cuerpo de 160 KiB también termina en 400 — sólo que después
        de que el proceso lo juntó entero, que es exactamente lo que el techo
        existe para no hacer. Las dos versiones dan el mismo status, así que un
        test que afirme el status no puede distinguirlas y no está midiendo el
        techo: está midiendo que hay un techo en alguna parte.

        Lo que las separa es cuántos trozos se llegaron a pedir. Con el corte
        adentro, se deja de leer apenas se pasa; sin él, se drena hasta el
        final.

        MUTACIÓN: mover el `if len(crudo) > CUERPO_MAXIMO` afuera del `while`.
        Cae ésta y sólo ésta.
        """
        entorno, equipo = _con_persona()
        trozo = b"x" * 4096
        leidos: list[int] = []
        with entorno, equipo, patch.object(dashboard, "proponer_ajuste") as proponer:
            code, _, _ = request(
                "POST", token=PERSONA, path="/settings/propose",
                trozos=[b'{"setting":"tope","value":"'] + [trozo] * 40,
                leidos=leidos,
            )
            self.assertEqual(code, 400)
            proponer.assert_not_called()
        # 8 KiB de techo contra trozos de 4 KiB: se corta en el tercero. El 41
        # es lo que se leería drenando todo.
        self.assertLess(len(leidos), 6)

    def test_the_settings_reader_is_told_who_is_looking(self):
        """`settings` necesita el teléfono para UNA cosa: decir si esa persona
        tiene un cambio esperando su código. La propuesta es por teléfono, así
        que pasarle otra cosa muestra la de otro o ninguna."""
        entorno, equipo = _con_persona()
        with entorno, equipo, patch.object(
                dashboard, "settings", return_value={"groups": []}) as leer:
            self.assertEqual(request(token=PERSONA, path="/settings")[0], 200)
            leer.assert_called_once_with(TELEFONO)

    def test_there_is_no_route_that_applies_the_code(self):
        """El segundo paso vive en WhatsApp, y eso es el diseño y no un hueco.

        `limites.aplicar` no tiene contador de intentos: no lo necesita mientras
        su única puerta sea un webhook firmado por Meta. Publicarla acá dejaría
        9000 valores con diez minutos de vida contra un endpoint sin límite.
        """
        entorno, equipo = _con_persona()
        with entorno, equipo:
            for ruta in ("/settings/confirm", "/settings/apply", "/settings/4242"):
                self.assertEqual(
                    request("POST", token=PERSONA, path=ruta)[0], 404, ruta)
