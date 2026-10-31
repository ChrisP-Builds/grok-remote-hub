"""Auth middleware policy: SPA static public; API gated when hub_token set."""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hub.harden_policy import is_public_unauthenticated_path
from hub.http_guard import GuardPolicy, install_guard

ROOT = Path(__file__).resolve().parents[1]


def test_server_installs_guard_which_uses_public_path_helper() -> None:
    server = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    assert "install_guard(app, self.guard)" in server
    guard = (ROOT / "hub" / "http_guard.py").read_text(encoding="utf-8")
    assert "is_public_unauthenticated_path(request.method, request.path)" in guard


def test_unauth_api_sessions_requires_auth_policy() -> None:
    """Middleware policy: unauthenticated GET /api/sessions is not public."""
    assert is_public_unauthenticated_path("GET", "/api/sessions") is False


def test_middleware_static_public_api_gated() -> None:
    """Real hub guard: static shell public, API gated by token."""

    async def ok(_request: web.Request) -> web.Response:
        return web.Response(text="ok", content_type="text/plain")

    async def json_ok(_request: web.Request) -> web.Response:
        return web.json_response({"sessions": []})

    async def run() -> None:
        app = web.Application()
        install_guard(app, GuardPolicy(token="secret"))
        app.router.add_get("/app.js", ok)
        app.router.add_get("/app.css", ok)
        app.router.add_get("/favicon.svg", ok)
        app.router.add_get("/vendor/x.js", ok)
        app.router.add_get("/", ok)
        app.router.add_get("/health", ok)
        app.router.add_get("/api/sessions", json_ok)

        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            for path in (
                "/app.js",
                "/app.css",
                "/favicon.svg",
                "/vendor/x.js",
                "/",
                "/health",
            ):
                resp = await client.get(path)
                assert resp.status == 200, path

            resp = await client.get("/api/sessions")
            assert resp.status == 401
            body = await resp.json()
            assert body.get("error") == "unauthorized"

            resp = await client.get(
                "/api/sessions",
                headers={"Authorization": "Bearer secret"},
            )
            assert resp.status == 200
        finally:
            await client.close()

    asyncio.run(run())
