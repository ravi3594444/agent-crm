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
            self.assertEqual(request(path="/orders/submit")[0], 404)
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
