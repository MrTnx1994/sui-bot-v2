"""Tests for the read-only /diag probe suite."""

from __future__ import annotations

from sui_bot.diagnostics import describe_probe, format_diag_report, probe_endpoints


class TestDescribeProbe:
    def test_none_response_uses_recorded_error(self):
        row = describe_probe("clients", None, 12, last_error="HTTP 502")
        assert row["ok"] is False
        assert "HTTP 502" in row["detail"]

    def test_unsuccessful_envelope_shows_panel_message(self):
        row = describe_probe("clients", {"success": False, "msg": "invalid token"}, 5)
        assert row["ok"] is False
        assert "invalid token" in row["detail"]

    def test_successful_clients_response(self):
        response = {"success": True, "obj": {"clients": [{"id": 1}, {"id": 2}]}}
        row = describe_probe("clients", response, 8)
        assert row["ok"] is True
        assert "clients=2" in row["detail"]

    def test_load_missing_suburi_is_failure(self):
        response = {"success": True, "obj": {"inbounds": [{"id": 1}]}}
        row = describe_probe("load (subURI+inbounds)", response, 8)
        assert row["ok"] is False
        assert "subURI=MISSING" in row["detail"]

    def test_load_with_suburi_is_ok(self):
        response = {"success": True, "obj": {"subURI": "https://h/sub", "inbounds": [{"id": 1}]}}
        row = describe_probe("load (subURI+inbounds)", response, 8)
        assert row["ok"] is True

    def test_list_obj_counts_items(self):
        row = describe_probe("changes", {"success": True, "obj": [1, 2, 3]}, 3)
        assert row["ok"] is True
        assert "items=3" in row["detail"]

    def test_empty_obj_is_failure(self):
        row = describe_probe("settings", {"success": True, "obj": {}}, 3)
        assert row["ok"] is False
        assert row["detail"] == "empty obj"

    def test_long_detail_is_shortened(self):
        row = describe_probe("clients", None, 1, last_error="x" * 500)
        assert len(row["detail"]) <= 160


class TestProbeEndpoints:
    async def test_probe_endpoints_runs_every_probe(self):
        calls = []

        async def fake_get(endpoint, params=None, attempts=None, log_failure=True):
            calls.append(endpoint)
            return {"success": True, "obj": {"clients": [], "inbounds": [], "user": [], "sys": {}, "subURI": "s"}}

        rows = await probe_endpoints(fake_get)
        assert len(rows) == 6
        assert len(calls) == 6
        assert all(row["ok"] for row in rows)

    async def test_probe_endpoints_swallows_exceptions(self):
        async def failing_get(endpoint, params=None, attempts=None, log_failure=True):
            raise RuntimeError("boom")

        rows = await probe_endpoints(failing_get)
        assert all(row["ok"] is False for row in rows)
        assert all("RuntimeError" in row["detail"] for row in rows)


class TestFormatDiagReport:
    def test_report_lists_counts(self):
        rows = [
            {"name": "clients", "ok": True, "detail": "clients=0", "elapsed_ms": 5},
            {"name": "onlines", "ok": False, "detail": "HTTP 500", "elapsed_ms": 7},
        ]
        report = format_diag_report(rows)
        assert "[1/2 OK]" in report
        assert "✅ clients: 5ms" in report
        assert "❌ onlines: 7ms — HTTP 500" in report
