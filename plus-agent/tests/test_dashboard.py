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

    def test_one_token_per_person_and_a_second_one_is_refused(self):
        """Un teléfono, un token. El segundo no entra.

        La versión anterior afirmaba que dos tokens de la misma persona daban la
        MISMA identidad, y esa mitad sigue valiendo y sigue afirmada: el teléfono
        se normaliza, así que `+54 9 351 000 0001` y `005493510000001` son la
        misma persona y no dos. Lo que estaba mal era la conclusión — que los DOS
        sirvieran.

        Revocar el acceso de alguien es sacar su línea del `.env`. Con dos
        tokens, el dueño saca una, cree que le cortó el acceso, y la otra sigue
        entrando y sigue pudiendo confirmar pedidos. `configurar_dashboard.py` ya
        rechazaba emitir el segundo; el panel lo aceptaba igual si la
        configuración venía editada a mano o de antes, que es el único caso en el
        que esto pasa de verdad. Hallazgo 3 de la review de #45.

        Las dos mitades: que el primero SIGA entrando (rechazar los dos sería
        romperle el acceso al que ya lo tenía) y que el segundo NO.
        """
        otro = "second-token-for-the-same-person-32-plus"
        env = {"DASHBOARD_TOKENS": f"{PERSONAL}:+54 9 351 000 0001,{otro}:005493510000001"}
        with patch.dict(os.environ, env):
            self.assertEqual(dashboard.quien("Bearer " + PERSONAL), STAFF_CANONICAL)
            self.assertIsNone(dashboard.quien("Bearer " + otro))

    def test_the_refusal_of_a_second_token_says_why_without_printing_it(self):
        """El motivo llega a `readiness`, y no lleva el token adentro.

        Un descarte mudo es la forma en que el dueño pega un token en el `.env`,
        no entra, y no tiene dónde enterarse. Y el motivo se lee en la salida del
        preflight, que es exactamente lo que una persona pega en un chat: por eso
        nombra la ENTRADA y no el token.
        """
        otro = "second-token-for-the-same-person-32-plus"
        _, problemas = dashboard.entradas_de_tokens(
            f"{PERSONAL}:+54 9 351 000 0001,{otro}:005493510000001"
        )

        self.assertEqual(len(problemas), 1)
        self.assertIn("entrada 2", problemas[0])
        self.assertNotIn(otro, problemas[0])
        self.assertNotIn(PERSONAL, problemas[0])

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
