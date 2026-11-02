"""/health answers the full body to every caller the guard authenticated, not only token holders."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hub.http_guard import LOOPBACK_HOSTS
from tests.test_ws_resume import _hub

SERVE_HOST = "hub.tail1234.ts.net"
OWNER = "owner@example.com"
TOKEN = "t0ken-for-health-tests"


def _tailscale_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    hub, app = _hub(tmp_path, monkeypatch)
    hub.config.hub_token = TOKEN
    hub.guard.token = TOKEN
    hub.guard.allowed_hosts = frozenset(LOOPBACK_HOSTS | {SERVE_HOST})
    hub.guard.serve_hosts = frozenset({SERVE_HOST})
    hub.guard.tailscale_logins = frozenset({OWNER})
    return hub, app


async def _health(client: TestClient, **headers: str) -> dict:
    resp = await client.get("/health", headers=headers)
    assert resp.status == 200
    return await resp.json()


def test_health_is_full_for_a_tailscale_login_and_reduced_for_strangers(tmp_path: Path, monkeypatch) -> None:
    _, app = _tailscale_hub(tmp_path, monkeypatch)

    async def run() -> None:
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            via_login = await _health(client, Host=SERVE_HOST, **{"Tailscale-User-Login": OWNER})
            assert "turnRunning" in via_login, "wake reconcile reads turnRunning; missing means idle"
            assert via_login["turnRunning"] is False

            via_token = await _health(client, Authorization=f"Bearer {TOKEN}")
            assert "turnRunning" in via_token

            stranger = await _health(client)
            assert stranger["ok"] is True
            assert "turnRunning" not in stranger

            spoofed = await _health(client, **{"Tailscale-User-Login": OWNER})
            assert "turnRunning" not in spoofed, "login header without the Serve host must not count"

            wrong_user = await _health(client, Host=SERVE_HOST, **{"Tailscale-User-Login": "x@example.com"})
            assert "turnRunning" not in wrong_user
        finally:
            await client.close()

    asyncio.run(run())


def test_health_is_full_when_no_token_is_configured(tmp_path: Path, monkeypatch) -> None:
    _, app = _hub(tmp_path, monkeypatch)

    async def run() -> None:
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            assert "turnRunning" in await _health(client)
        finally:
            await client.close()

    asyncio.run(run())
