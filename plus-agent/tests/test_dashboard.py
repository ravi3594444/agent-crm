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
dashboard_erp_error = type("ERPNextError", (RuntimeError,), {})


def request(method="GET", token=None, origin=None, path="/snapshot"):
    async def run():
        headers = [(b"host", b"agent.example")]
        if token is not None:
            headers.append((b"authorization", ("Bearer " + token).encode()))
        if origin:
            headers.append((b"origin", origin.encode()))
        events = []

        async def send(event):
            events.append(event)

        async def receive():
            return {"type": "http.request", "body": b""}

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


# ---------------------------------------------------------------- ventas
#
# LO QUE ESTOS TESTS FIJAN
# ------------------------
# `sales()` hace UNA lectura de 30 días y compone DOS ventanas con ella, porque
# `readers[path]` se llama sin argumentos —esta API no tiene query string— y un
# top de productos no se puede recortar en el navegador. Ese "una lectura, dos
# respuestas" es justo la forma en la que un valor derivado alimenta a más de un
# consumidor, que es donde CLAUDE.md dice que hay que mutar cada consumidor por
# separado: si un test sólo mira `last30`, el filtro por ventana es un no-op ahí
# y la mutación sobrevive entera.
class VentasTest(unittest.TestCase):
    def _erp(self, pedidos, renglones=()):
        """Un ERPNext falso que contesta lo que se le PASA, no lo que este
        archivo supone: si `sales()` cambiara los filtros, este doble seguiría
        devolviendo lo mismo y no probaría nada, así que guarda las llamadas."""
        llamadas = []

        class Falso:
            ERPNextError = dashboard_erp_error

            @staticmethod
            def default_company():
                return "Lacteos Test SA"

            @staticmethod
            def manager_scope():
                from contextlib import nullcontext
                return nullcontext()

            @staticmethod
            def get_doc(doctype, name, timeout=None):
                return {"default_currency": "ARS"}

            @staticmethod
            def get_list(doctype, **kwargs):
                llamadas.append((doctype, kwargs))
                return list(renglones) if doctype == "Sales Order Item" else list(pedidos)

        return Falso, llamadas

    def _correr(self, pedidos, renglones=()):
        import datetime as _dt

        falso, llamadas = self._erp(pedidos, renglones)
        hoy = _dt.date(2026, 9, 15)

        class PolicyFalso:
            @staticmethod
            def _hoy_del_negocio():
                return hoy

        import sys
        modulos = {"app.erpnext": falso, "app.policy": PolicyFalso}
        real = {k: sys.modules.get(k) for k in modulos}
        paquete = sys.modules.get("app")
        try:
            for k, v in modulos.items():
                sys.modules[k] = v
            if paquete is not None:
                paquete.erpnext, paquete.policy = falso, PolicyFalso
            return dashboard.sales(), llamadas
        finally:
            for k, v in real.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_las_dos_ventanas_salen_de_una_lectura_y_no_son_la_misma(self):
        """Un pedido de hace 10 días entra en 30 y NO en 7.

        Las DOS mitades se afirman a propósito: con sólo `last30` la mutación
        que le pasa la fecha de 7 días a la ventana de 30 pasa desapercibida.
        """
        pedidos = [
            {"name": "SO-1", "customer": "C1", "customer_name": "Uno",
             "grand_total": 100, "transaction_date": "2026-09-14"},
            {"name": "SO-2", "customer": "C2", "customer_name": "Dos",
             "grand_total": 400, "transaction_date": "2026-09-05"},
        ]
        salida, _ = self._correr(pedidos)
        self.assertEqual(salida["last7"]["orders"], 1, salida)
        self.assertEqual(salida["last7"]["total"], 100, salida)
        self.assertEqual(salida["last30"]["orders"], 2, salida)
        self.assertEqual(salida["last30"]["total"], 500, salida)

    def test_el_promedio_sin_pedidos_es_None_y_no_cero(self):
        """Un 0 en pantalla se lee «vendí y el ticket fue cero»."""
        salida, _ = self._correr([])
        self.assertIsNone(salida["last7"]["averageOrder"], salida)
        self.assertEqual(salida["last7"]["orders"], 0)

    def test_el_top_de_productos_de_7_dias_no_cuenta_renglones_de_30(self):
        """La lectura de renglones es de 30 días para las DOS ventanas.

        Es el caso que un test sobre `last30` no puede ver: ahí el filtro por
        pedido-dentro-de-la-ventana no descarta nada y es indistinguible de no
        estar.
        """
        pedidos = [
            {"name": "SO-1", "customer": "C1", "customer_name": "Uno",
             "grand_total": 100, "transaction_date": "2026-09-14"},
            {"name": "SO-2", "customer": "C2", "customer_name": "Dos",
             "grand_total": 400, "transaction_date": "2026-09-05"},
        ]
        renglones = [
            {"parent": "SO-1", "item_code": "LECHE", "item_name": "Leche", "qty": 2, "amount": 100},
            {"parent": "SO-2", "item_code": "QUESO", "item_name": "Queso", "qty": 8, "amount": 400},
        ]
        salida, _ = self._correr(pedidos, renglones)
        self.assertEqual([f["id"] for f in salida["last7"]["topProducts"]], ["LECHE"], salida)
        self.assertEqual(
            sorted(f["id"] for f in salida["last30"]["topProducts"]), ["LECHE", "QUESO"], salida
        )

    def test_la_tabla_hija_se_pide_con_su_doctype_padre(self):
        """Frappe rechaza una tabla hija sin `parent`, y ese fue el bug por el
        que `informe(que="stock_bajo")` nunca contestó contra un ERPNext real."""
        pedidos = [{"name": "SO-1", "customer": "C1", "customer_name": "Uno",
                    "grand_total": 100, "transaction_date": "2026-09-14"}]
        _, llamadas = self._correr(pedidos, [])
        hija = [k for d, k in llamadas if d == "Sales Order Item"]
        self.assertEqual(len(hija), 1, llamadas)
        self.assertEqual(hija[0].get("parent"), "Sales Order", hija)
