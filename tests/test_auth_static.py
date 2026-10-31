"""Auth middleware policy: SPA static public; API gated when hub_token set."""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hub.harden_policy import is_public_unauthenticated_path

ROOT = Path(__file__).resolve().parents[1]


def test_server_imports_and_uses_public_path_helper() -> None:
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    assert "is_public_unauthenticated_path" in src
    assert "from hub.harden_policy import" in src
    mid = src.find("async def _auth_middleware")
    assert mid > 0
    chunk = src[mid : mid + 500]
    assert "is_public_unauthenticated_path(request.method, request.path)" in chunk


def test_unauth_api_sessions_requires_auth_policy() -> None:
    """Middleware policy: unauthenticated GET /api/sessions is not public."""
    assert is_public_unauthenticated_path("GET", "/api/sessions") is False


def test_middleware_static_public_api_gated() -> None:
    """aiohttp middleware mirrors Hub._auth_middleware (no full Hub start)."""

    @web.middleware
    async def token_gate(request: web.Request, handler):
        if is_public_unauthenticated_path(request.method, request.path):
            return await handler(request)
        auth = request.headers.get("Authorization") or ""
        token = "secret"
        if auth.lower().startswith("bearer ") and auth[7:].strip() == token:
            return await handler(request)
        if request.rel_url.query.get("token") == token:
            return await handler(request)
        return web.json_response({"error": "unauthorized"}, status=401)

    async def ok(_request: web.Request) -> web.Response:
        return web.Response(text="ok", content_type="text/plain")

    async def json_ok(_request: web.Request) -> web.Response:
        return web.json_response({"sessions": []})

    async def run() -> None:
        app = web.Application(middlewares=[token_gate])
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
