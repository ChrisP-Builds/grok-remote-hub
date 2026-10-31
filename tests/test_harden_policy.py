"""Unit tests for hub.harden_policy (token gate, public health, heal exhaust)."""

from __future__ import annotations

from pathlib import Path

from hub.harden_policy import (
    PUBLIC_LIVENESS_KEYS,
    RICH_HEALTH_KEYS,
    empty_hub_token_allowed,
    heal_exhaust_operator_fields,
    health_response_body,
    is_loopback_host,
    is_public_unauthenticated_path,
    public_liveness_body,
    require_hub_token,
    should_auto_restart_agent_on_heal_exhaust,
)

ROOT = Path(__file__).resolve().parents[1]


def test_is_loopback_host() -> None:
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("LOCALHOST") is True
    assert is_loopback_host("::1") is True
    assert is_loopback_host("[::1]") is True
    assert is_loopback_host("127.0.0.2") is True
    assert is_loopback_host("0.0.0.0") is False
    assert is_loopback_host("100.64.1.2") is False
    assert is_loopback_host("192.168.1.1") is False
    assert is_loopback_host("") is False


def test_empty_hub_token_allowed_only_all_loopback() -> None:
    assert empty_hub_token_allowed(["127.0.0.1"]) is True
    assert empty_hub_token_allowed(["127.0.0.1", "localhost"]) is True
    assert empty_hub_token_allowed(["127.0.0.1", "100.64.1.2"]) is False
    assert empty_hub_token_allowed(["100.64.1.2"]) is False
    assert empty_hub_token_allowed([]) is False


def test_require_hub_token_matrix() -> None:
    ok, err = require_hub_token(["127.0.0.1"], "")
    assert ok is True
    assert err is None

    ok, err = require_hub_token(["127.0.0.1", "100.1.2.3"], "")
    assert ok is False
    assert err is not None
    assert "hub_token" in err
    assert "config.toml" in err

    ok, err = require_hub_token(["127.0.0.1", "100.1.2.3"], "secret")
    assert ok is True
    assert err is None

    ok, err = require_hub_token(["100.1.2.3"], "  token  ")
    assert ok is True
    assert err is None

    # Whitespace-only token is empty
    ok, err = require_hub_token(["100.1.2.3"], "   ")
    assert ok is False


def test_public_liveness_body_strips_rich_keys() -> None:
    full = {
        "ok": True,
        "bootId": "abc",
        "uptimeSeconds": 12,
        "pid": 99,
        "port": 8787,
        "hubVersion": "0.4.0",
        "opsCounters": {"x": 1},
        "opsRecent": [{"e": 1}],
        "acpTraceRecent": [{"t": 1}],
        "liveTurns": [{"sessionId": "s"}],
        "pendingQuestionSessions": ["s"],
        "capacity": {"activeTurnCount": 1},
        "hosts": ["127.0.0.1", "100.1.2.3"],
        "tailscaleIp": "100.1.2.3",
        "acpHealAttempts": 3,
        "acpHealError": "boom",
        "acpHealExhausted": True,
        "agent": "up",
        "turnRunning": True,
    }
    pub = public_liveness_body(full)
    assert set(pub.keys()) <= PUBLIC_LIVENESS_KEYS
    assert pub["ok"] is True
    assert pub["bootId"] == "abc"
    assert pub["pid"] == 99
    for rich in (
        "opsCounters",
        "opsRecent",
        "acpTraceRecent",
        "liveTurns",
        "hosts",
        "tailscaleIp",
        "acpHealAttempts",
    ):
        assert rich not in pub
        assert rich in RICH_HEALTH_KEYS


def test_public_liveness_ok_defaults_true() -> None:
    assert public_liveness_body({})["ok"] is True
    assert public_liveness_body({"ok": False})["ok"] is False


def test_health_response_body_matrix() -> None:
    full = {
        "ok": True,
        "bootId": "b",
        "opsCounters": {"a": 1},
        "hosts": ["127.0.0.1"],
        "pid": 1,
        "port": 8787,
        "uptimeSeconds": 1,
        "hubVersion": "x",
    }
    # Empty token: always full (local loopback dev)
    out = health_response_body(full, authenticated=False, hub_token="")
    assert "opsCounters" in out
    assert out["hosts"] == ["127.0.0.1"]

    out = health_response_body(full, authenticated=True, hub_token="")
    assert "opsCounters" in out

    # Token set + authenticated: full
    out = health_response_body(full, authenticated=True, hub_token="secret")
    assert "opsCounters" in out

    # Token set + unauthenticated: public only
    out = health_response_body(full, authenticated=False, hub_token="secret")
    assert "opsCounters" not in out
    assert "hosts" not in out
    assert out["ok"] is True
    assert out["bootId"] == "b"


def test_should_auto_restart_agent_on_heal_exhaust() -> None:
    base = dict(
        enabled=True,
        agent_process_up=True,
        acp_connected=False,
        heal_attempts=3,
        max_attempts=3,
        turn_active=False,
        auto_restarts_used=0,
        max_auto_restarts=1,
    )
    assert should_auto_restart_agent_on_heal_exhaust(**base) is True

    # Disabled
    assert (
        should_auto_restart_agent_on_heal_exhaust(**{**base, "enabled": False})
        is False
    )
    # Mid-turn must block
    assert (
        should_auto_restart_agent_on_heal_exhaust(**{**base, "turn_active": True})
        is False
    )
    # Not exhausted
    assert (
        should_auto_restart_agent_on_heal_exhaust(**{**base, "heal_attempts": 2})
        is False
    )
    # Process down
    assert (
        should_auto_restart_agent_on_heal_exhaust(
            **{**base, "agent_process_up": False}
        )
        is False
    )
    # ACP already connected
    assert (
        should_auto_restart_agent_on_heal_exhaust(**{**base, "acp_connected": True})
        is False
    )
    # Budget used
    assert (
        should_auto_restart_agent_on_heal_exhaust(
            **{**base, "auto_restarts_used": 1}
        )
        is False
    )


def test_heal_exhaust_operator_fields() -> None:
    fields = heal_exhaust_operator_fields(
        heal_attempts=3,
        max_attempts=3,
        agent_process_up=True,
        acp_connected=False,
        last_error="boom",
    )
    assert fields["acpHealExhausted"] is True
    assert fields["acpHealAttempts"] == 3
    assert fields["acpHealError"] == "boom"

    fields = heal_exhaust_operator_fields(
        heal_attempts=1,
        max_attempts=3,
        agent_process_up=True,
        acp_connected=False,
        last_error="boom",
    )
    assert fields["acpHealExhausted"] is False
    assert fields["acpHealError"] is None

    fields = heal_exhaust_operator_fields(
        heal_attempts=3,
        max_attempts=3,
        agent_process_up=True,
        acp_connected=True,
        last_error="boom",
    )
    assert fields["acpHealExhausted"] is False
    assert fields["acpHealError"] is None

    fields = heal_exhaust_operator_fields(
        heal_attempts=3,
        max_attempts=3,
        agent_process_up=False,
        acp_connected=False,
        last_error="boom",
    )
    assert fields["acpHealExhausted"] is False


def test_is_public_unauthenticated_path_spa_shell() -> None:
    assert is_public_unauthenticated_path("GET", "/app.js") is True
    assert is_public_unauthenticated_path("GET", "/app.css") is True
    assert is_public_unauthenticated_path("GET", "/favicon.svg") is True
    assert is_public_unauthenticated_path("GET", "/vendor/foo.js") is True
    assert is_public_unauthenticated_path("GET", "/") is True
    assert is_public_unauthenticated_path("GET", "/health") is True
    assert is_public_unauthenticated_path("HEAD", "/app.js") is True
    assert is_public_unauthenticated_path("POST", "/app.js") is False
    assert is_public_unauthenticated_path("GET", "/index.html") is True
    assert is_public_unauthenticated_path("GET", "/favicon.ico") is True


def test_is_public_unauthenticated_path_protects_api_ws_preview() -> None:
    assert is_public_unauthenticated_path("GET", "/api/sessions") is False
    assert is_public_unauthenticated_path("GET", "/api/admin/diagnostics") is False
    assert is_public_unauthenticated_path("POST", "/api/sessions") is False
    assert is_public_unauthenticated_path("GET", "/ws") is False
    assert is_public_unauthenticated_path("GET", "/preview-site/x") is False
    assert is_public_unauthenticated_path("GET", "/api") is False
    assert is_public_unauthenticated_path("GET", "/preview-site") is False
    # Health always public even for non-GET
    assert is_public_unauthenticated_path("POST", "/health") is True
    # Static-looking under protected prefixes stays protected
    assert is_public_unauthenticated_path("GET", "/api/foo.js") is False
    assert is_public_unauthenticated_path("GET", "/preview-site/x.css") is False


def test_is_public_unauthenticated_path_static_extensions() -> None:
    assert is_public_unauthenticated_path("GET", "/chunk.map") is True
    assert is_public_unauthenticated_path("GET", "/font.woff2") is True
    assert is_public_unauthenticated_path("GET", "/logo.png") is True
    assert is_public_unauthenticated_path("PUT", "/logo.png") is False
    assert is_public_unauthenticated_path("GET", "/unknown-route") is False


def test_server_wires_harden_policy() -> None:
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    assert "heal_exhaust_operator_fields" in src
    assert "health_response_body" in src
    assert "should_auto_restart_agent_on_heal_exhaust" in src
    assert "acpHealExhausted" in src
    assert "_acp_heal_auto_restarts_used" in src
    assert "auto_restart_agent_on_heal_exhaust" in src
    assert "heal exhaust auto-restart" in src
    # handle_health uses auth-aware body
    idx = src.find("async def handle_health")
    assert idx > 0
    chunk = src[idx : idx + 500]
    assert "check_auth" in chunk
    assert "health_response_body" in chunk
    assert "install_guard(app, self.guard)" in src


def test_main_requires_hub_token_gate() -> None:
    src = (ROOT / "hub" / "__main__.py").read_text(encoding="utf-8")
    assert "require_hub_token" in src
    assert "hub_token configured" in src
    assert "loopback-only" in src or "loopback" in src


def test_config_has_auto_restart_flag() -> None:
    from hub.config import Config, load_config

    c = Config()
    assert c.auto_restart_agent_on_heal_exhaust is False
    # load_config still works
    loaded = load_config(ROOT / "config.example.toml")
    assert loaded.auto_restart_agent_on_heal_exhaust is False


def test_app_js_health_uses_api_url() -> None:
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert 'apiUrl("/health")' in js
    # No bare unauthenticated health fetch left for SPA paths
    assert 'fetch("/health"' not in js
    assert "acpHealExhausted" in js


def test_app_js_hub_token_persist_keys() -> None:
    """SPA must persist hub token and reuse it when the URL has no ?token=."""
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "grh.hubToken" in js
    assert "hubToken" in js
    assert "replaceState" in js
