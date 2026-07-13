"""Pure tests for honest agent vs ACP status mapping and heal gating."""

from __future__ import annotations

from pathlib import Path

from hub.status_view import (
    ACP_HEAL_MAX_ATTEMPTS,
    map_agent_status,
    should_attempt_acp_heal,
)

ROOT = Path(__file__).resolve().parents[1]


def test_map_agent_status_acp_disconnected() -> None:
    m = map_agent_status(True, False)
    assert m["agentProcess"] == "up"
    assert m["acpConnected"] is False
    assert m["agent"] == "down"  # not chat-ready
    assert m["agentDetail"] == "acp-disconnected"


def test_map_agent_status_ok() -> None:
    m = map_agent_status(True, True)
    assert m["agent"] == "up"
    assert m["agentProcess"] == "up"
    assert m["acpConnected"] is True
    assert m["agentDetail"] == "ok"


def test_map_agent_status_process_down() -> None:
    m = map_agent_status(False, False)
    assert m["agent"] == "down"
    assert m["agentProcess"] == "down"
    assert m["agentDetail"] == "process-down"


def _heal_kwargs(**overrides: object) -> dict:
    base: dict = {
        "agent_process_up": True,
        "acp_connected": False,
        "heal_in_progress": False,
        "attempts": 0,
        "disconnected_for_s": 12.0,
    }
    base.update(overrides)
    return base


def test_should_attempt_acp_heal_after_min_down() -> None:
    assert should_attempt_acp_heal(**_heal_kwargs(disconnected_for_s=12.0)) is True
    assert should_attempt_acp_heal(**_heal_kwargs(disconnected_for_s=9.0)) is False


def test_should_attempt_acp_heal_caps_attempts() -> None:
    assert (
        should_attempt_acp_heal(
            **_heal_kwargs(attempts=ACP_HEAL_MAX_ATTEMPTS, disconnected_for_s=60.0)
        )
        is False
    )


def test_should_attempt_acp_heal_skips_when_connected_or_process_down() -> None:
    assert should_attempt_acp_heal(**_heal_kwargs(acp_connected=True)) is False
    assert should_attempt_acp_heal(**_heal_kwargs(agent_process_up=False)) is False
    assert should_attempt_acp_heal(**_heal_kwargs(heal_in_progress=True)) is False
    assert should_attempt_acp_heal(**_heal_kwargs(disconnected_for_s=None)) is False


def test_should_attempt_acp_heal_backoff() -> None:
    # attempts=1 needs min_down + 1*backoff (default 10+5=15)
    assert should_attempt_acp_heal(**_heal_kwargs(attempts=1, disconnected_for_s=14.0)) is False
    assert should_attempt_acp_heal(**_heal_kwargs(attempts=1, disconnected_for_s=15.0)) is True


def test_server_has_acp_heal_hook() -> None:
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    assert "reconnect" in src
    assert "acp heal" in src.lower() or "heal_acp" in src or "acp_heal" in src
    assert "_maybe_heal_acp" in src
    assert "should_attempt_acp_heal" in src
    assert 'POST /api/admin/reconnect-acp' in src or "reconnect-acp" in src
    assert "_on_acp_connection" in src
    # Successful connect resets heal attempts
    on_conn = src[src.find("async def _on_acp_connection") :]
    on_conn = on_conn[:1200]
    assert "_acp_heal_attempts = 0" in on_conn
    assert "ACP heal" in on_conn or "heal" in on_conn.lower()
