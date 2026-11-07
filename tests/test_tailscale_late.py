"""A hub that starts before Tailscale connects must adopt Tailscale once it is up (no restart)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

import hub.__main__ as main_mod
import hub.server as server_mod
from hub.config import Config


def _hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str = "s3cret") -> server_mod.Hub:
    monkeypatch.setattr(server_mod, "REMOTE_SESSIONS_FILE", tmp_path / "remote-sessions.json")
    monkeypatch.setattr(server_mod, "APPROVAL_RULES_FILE", tmp_path / "approval-rules.json")
    monkeypatch.setattr(server_mod, "LAST_REMOTE_SESSION_FILE", tmp_path / "last-remote-session.txt")
    cfg = Config(
        sessions_root=tmp_path / "sessions",
        agent_secret_path=tmp_path / "agent.secret",
        hook_secret_path=tmp_path / "hook.secret",
        log_dir=tmp_path / "logs",
        static_dir=tmp_path / "static",
        gate_plugin_dir=tmp_path / "gate",
        grok_bin="grok-is-not-started-by-this-test",
        hub_token=token,
    )
    hub = server_mod.Hub(cfg)
    main_mod.configure_tailscale(hub, cfg, ["127.0.0.1"], "local", None, [], None)
    return hub


def _adopt(hub, ips, *, add_site=None, owner="owner@example.com"):
    seen = iter(ips)
    sites: list[str] = []

    async def record_site(host: str) -> None:
        sites.append(host)

    result = asyncio.run(main_mod.adopt_tailscale_when_up(
        hub,
        add_site or record_site,
        find_ip=lambda: next(seen),
        find_dns_names=lambda: ["desk.tailnet.ts.net."],
        find_owner=lambda: owner,
        interval=0,
    ))
    return result, sites


def test_hub_started_before_tailscale_trusts_serve_once_tailscale_is_up(tmp_path, monkeypatch) -> None:
    hub = _hub(tmp_path, monkeypatch)
    assert "desk.tailnet.ts.net" not in hub.guard.allowed_hosts
    ip, sites = _adopt(hub, [None, None, "100.64.0.10"])
    assert ip == "100.64.0.10"
    assert sites == ["100.64.0.10"]
    assert "desk.tailnet.ts.net" in hub.guard.allowed_hosts
    assert hub.guard.serve_hosts == frozenset({"desk.tailnet.ts.net"})
    assert "owner@example.com" in hub.guard.tailscale_logins
    assert hub.serve_url == "https://desk.tailnet.ts.net/"
    assert (hub.bind_mode, hub.tailscale_ip) == ("tailscale", "100.64.0.10")
    assert hub.bind_hosts == ["127.0.0.1", "100.64.0.10"]


def test_late_tailscale_without_a_token_stays_local(tmp_path, monkeypatch) -> None:
    hub = _hub(tmp_path, monkeypatch, token="")
    ip, sites = _adopt(hub, ["100.64.0.10"])
    assert ip is None and sites == []
    assert hub.guard.serve_hosts == frozenset()
    assert hub.bind_mode == "local"


def test_serve_trust_still_applies_when_the_tailscale_ip_cannot_be_bound(tmp_path, monkeypatch) -> None:
    hub = _hub(tmp_path, monkeypatch)

    async def busy(_host: str) -> None:
        raise OSError("address in use")

    ip, _ = _adopt(hub, ["100.64.0.10"], add_site=busy)
    assert ip == "100.64.0.10"
    assert hub.guard.serve_hosts == frozenset({"desk.tailnet.ts.net"})
    assert hub.bind_hosts == ["127.0.0.1"]


def test_tailscale_cli_runs_without_a_console_window(monkeypatch) -> None:
    """The hub runs under pythonw; a console child without this flag flashes a window."""
    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="100.64.0.10\n", stderr="")

    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)
    assert server_mod._tailscale_ip() == "100.64.0.10"
    server_mod.tailscale_dns_names()
    main_mod._tailscale_owner_login()
    expected = 0x08000000 if sys.platform == "win32" else 0
    assert calls and all(c.get("creationflags", 0) == expected for c in calls)
