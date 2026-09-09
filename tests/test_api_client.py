"""Tests for APIClient against a real local aiohttp server."""

from __future__ import annotations


import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sui_bot.bot import APIClient

MAX_API_RESPONSE_BYTES = 8 * 1024 * 1024


@pytest.fixture()
def panel_app():
    async def ok_handler(request):
        return web.json_response({"success": True, "obj": {"clients": [{"id": 1}]}})

    async def fail_handler(request):
        return web.json_response({"success": False, "msg": "invalid token"})

    async def boom_handler(request):
        return web.json_response({}, status=502)

    async def huge_handler(request):
        return web.Response(body=b"x" * (MAX_API_RESPONSE_BYTES + 1))

    app = web.Application()
    app.router.add_get("/panel/apiv2/clients", ok_handler)
    app.router.add_get("/panel/apiv2/settings", fail_handler)
    app.router.add_get("/panel/apiv2/status", boom_handler)
    app.router.add_get("/panel/apiv2/load", huge_handler)
    return app


@pytest.fixture()
async def server(panel_app):
    test_server = TestServer(panel_app)
    await test_server.start_server()
    yield test_server
    await test_server.close()


class TestAPIClientGet:
    async def test_success_returns_envelope_and_clears_error(self, server):
        client = APIClient(str(server.make_url("/panel")), "tok")
        client.record_error("apiv2/clients", "stale")
        decoded = await client.get("apiv2/clients", attempts=1)
        assert decoded["success"] is True
        assert client.error_reason("apiv2/clients") == ""
        await client.close()

    async def test_panel_failure_is_recorded(self, server):
        client = APIClient(str(server.make_url("/panel")), "tok")
        decoded = await client.get("apiv2/settings", attempts=1)
        assert decoded is None
        assert client.error_reason("apiv2/settings") == " (Reason: invalid token)"
        await client.close()

    async def test_http_error_is_recorded(self, server):
        client = APIClient(str(server.make_url("/panel")), "tok")
        decoded = await client.get("apiv2/status", attempts=1)
        assert decoded is None
        assert "HTTP 502" in client.error_reason("apiv2/status")
        await client.close()

    async def test_oversized_response_is_rejected(self, server):
        client = APIClient(str(server.make_url("/panel")), "tok")
        decoded = await client.get("apiv2/load", attempts=1)
        assert decoded is None
        assert "safety limit" in client.error_reason("apiv2/load")
        await client.close()

    async def test_unreachable_host_returns_none_with_reason(self, unused_tcp_port):
        client = APIClient(f"http://127.0.0.1:{unused_tcp_port}/panel", "tok")
        decoded = await client.get("apiv2/clients", attempts=1)
        assert decoded is None
        assert client.error_reason("apiv2/clients") != ""
        await client.close()
