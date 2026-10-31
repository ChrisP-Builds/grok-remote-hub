"""Trust-boundary middlewares: Host allowlist, same-origin check, token gate."""

from __future__ import annotations

import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hub.http_guard import (
    GuardPolicy,
    build_allowed_hosts,
    install_guard,
    needs_origin_check,
    normalize_host,
    origin_allowed,
    host_allowed,
    token_matches,
)

LOCAL = "127.0.0.1:8787"


def test_normalize_host_strips_port_brackets_and_dot() -> None:
    assert normalize_host("127.0.0.1:8787") == "127.0.0.1"
    assert normalize_host("[::1]:8787") == "::1"
    assert normalize_host("Machine.Tailnet.ts.net.") == "machine.tailnet.ts.net"
    assert normalize_host("localhost") == "localhost"


def test_build_allowed_hosts_includes_loopback_binds_and_tailscale_names() -> None:
    allowed = build_allowed_hosts(
        bind_hosts=["127.0.0.1", "100.64.1.2"],
        tailscale_ip="100.64.1.2",
        tailscale_dns_names=["desk.tailnet.ts.net."],
        extra=["Hub.Example"],
    )
    for host in ("localhost", "127.0.0.1", "::1", "100.64.1.2",
                 "desk.tailnet.ts.net", "desk", "hub.example"):
        assert host in allowed, host


def test_host_allowed() -> None:
    allowed = build_allowed_hosts(["127.0.0.1"], None, [], [])
    assert host_allowed("127.0.0.1:8787", allowed)
    assert host_allowed("localhost:8787", allowed)
    assert not host_allowed("attacker.example:8787", allowed)
    # Non-browser clients may omit Host; DNS rebinding always sends one.
    assert host_allowed(None, allowed)


def test_origin_allowed_requires_same_origin() -> None:
    assert origin_allowed(None, LOCAL, frozenset())
    assert origin_allowed("http://127.0.0.1:8787", LOCAL, frozenset())
    assert not origin_allowed("https://attacker.example", LOCAL, frozenset())
    # Same host, different port (e.g. a local dev server) is a different origin.
    assert not origin_allowed("http://127.0.0.1:5173", LOCAL, frozenset())
    # Sandboxed documents (CSP sandbox previews) send Origin: null.
    assert not origin_allowed("null", LOCAL, frozenset())
    # HTTPS proxy (Tailscale Serve) on the default port.
    assert origin_allowed("https://desk.tailnet.ts.net", "desk.tailnet.ts.net", frozenset())
    assert origin_allowed(
        "https://desk.tailnet.ts.net", LOCAL, frozenset({"https://desk.tailnet.ts.net"})
    )


def test_needs_origin_check_for_ws_and_state_changing_methods() -> None:
    assert needs_origin_check("GET", "/ws")
    assert needs_origin_check("POST", "/api/sessions")
    assert needs_origin_check("PUT", "/api/fs/write")
    assert needs_origin_check("DELETE", "/api/sessions/x")
    assert not needs_origin_check("GET", "/api/sessions")
    assert not needs_origin_check("HEAD", "/")


def test_token_matches_is_exact() -> None:
    assert token_matches("secret", "secret")
    assert not token_matches("secre", "secret")
    assert not token_matches("", "secret")


def _app(policy: GuardPolicy) -> web.Application:
    app = web.Application()
    install_guard(app, policy)

    async def ok(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    app.router.add_get("/ws", ok)
    app.router.add_get("/api/sessions", ok)
    app.router.add_post("/api/sessions", ok)
    app.router.add_get("/preview-site/{path:.*}", ok)
    app.router.add_post("/internal/pre-tool-use", ok)
    return app


def _run(policy: GuardPolicy, scenario) -> None:
    async def run() -> None:
        client = TestClient(TestServer(_app(policy)))
        await client.start_server()
        try:
            await scenario(client)
        finally:
            await client.close()

    asyncio.run(run())


def _loopback_policy(token: str = "") -> GuardPolicy:
    return GuardPolicy(token=token, allowed_hosts=build_allowed_hosts(["127.0.0.1"], None, [], []))


def test_middleware_rejects_foreign_host_dns_rebinding() -> None:
    async def scenario(client: TestClient) -> None:
        resp = await client.get("/api/sessions", headers={"Host": "attacker.example:8787"})
        assert resp.status == 403
        resp = await client.get("/api/sessions", headers={"Host": "localhost:8787"})
        assert resp.status == 200

    _run(_loopback_policy(), scenario)


def test_middleware_blocks_cross_site_websocket_without_token() -> None:
    """Tokenless loopback: a foreign page must not open /ws (CSWSH)."""

    async def scenario(client: TestClient) -> None:
        host = f"127.0.0.1:{client.port}"
        evil = {"Host": host, "Origin": "https://attacker.example"}
        assert (await client.get("/ws", headers=evil)).status == 403
        assert (await client.post("/api/sessions", headers=evil)).status == 403
        sandboxed = {"Host": host, "Origin": "null"}
        assert (await client.get("/ws", headers=sandboxed)).status == 403
        same = {"Host": host, "Origin": f"http://{host}"}
        assert (await client.get("/ws", headers=same)).status == 200
        assert (await client.post("/api/sessions", headers=same)).status == 200

    _run(_loopback_policy(), scenario)


def test_middleware_token_gate() -> None:
    async def scenario(client: TestClient) -> None:
        assert (await client.get("/")).status == 200
        assert (await client.get("/health")).status == 200
        assert (await client.get("/api/sessions")).status == 401
        assert (await client.get("/api/sessions?token=wrong")).status == 401
        assert (await client.get("/api/sessions?token=s3cret")).status == 200
        bearer = {"Authorization": "Bearer s3cret"}
        assert (await client.get("/api/sessions", headers=bearer)).status == 200

    _run(_loopback_policy(token="s3cret"), scenario)


def test_preview_paths_skip_token_gate_for_capability_check() -> None:
    """Preview subresources carry no token; the handler checks the capability key."""

    async def scenario(client: TestClient) -> None:
        assert (await client.get("/preview-site/key/style.css")).status == 200
        assert (await client.post("/api/sessions")).status == 401

    _run(_loopback_policy(token="s3cret"), scenario)


def test_security_headers_on_responses() -> None:
    async def scenario(client: TestClient) -> None:
        resp = await client.get("/")
        assert resp.headers.get("Referrer-Policy") == "same-origin"
        assert "frame-ancestors 'self'" in resp.headers.get("Content-Security-Policy", "")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    _run(_loopback_policy(), scenario)


def test_tailscale_self_dns_names_parses_status_json() -> None:
    from hub.server import tailscale_self_dns_names

    status = {"Self": {"DNSName": "desk.tailnet.ts.net.", "HostName": "DESK"}}
    assert tailscale_self_dns_names(status) == ["desk.tailnet.ts.net."]
    assert tailscale_self_dns_names({}) == []
    assert tailscale_self_dns_names({"Self": None}) == []


def test_grok_hook_path_skips_token_but_not_host_check() -> None:
    """Grok's HTTP hook cannot send the browser token; the handler checks its own key."""

    async def scenario(client: TestClient) -> None:
        assert (await client.post("/internal/pre-tool-use?key=x")).status == 200
        foreign = {"Host": "attacker.example:8787"}
        assert (await client.post("/internal/pre-tool-use?key=x", headers=foreign)).status == 403
        assert (await client.get("/internal/pre-tool-use")).status in (401, 405)

    _run(_loopback_policy(token="s3cret"), scenario)