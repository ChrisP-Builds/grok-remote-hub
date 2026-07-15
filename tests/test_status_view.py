"""Pure tests for honest agent vs ACP status mapping and heal gating."""

from __future__ import annotations

from pathlib import Path

from hub.status_view import (
    ACP_HEAL_MAX_ATTEMPTS,
    ACP_STALE_SECONDS,
    ACP_ZOMBIE_SEND_FAILURES,
    map_acp_quality,
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
    assert m["acpQuality"] == "down"


def test_map_agent_status_ok() -> None:
    m = map_agent_status(True, True)
    assert m["agent"] == "up"
    assert m["agentProcess"] == "up"
    assert m["acpConnected"] is True
    assert m["agentDetail"] == "ok"
    assert m["acpQuality"] == "ok"


def test_map_agent_status_process_down() -> None:
    m = map_agent_status(False, False)
    assert m["agent"] == "down"
    assert m["agentProcess"] == "down"
    assert m["agentDetail"] == "process-down"
    assert m["acpQuality"] == "down"
    # Process down stays process-down even if raw acp flag is true
    m2 = map_agent_status(False, True)
    assert m2["agentDetail"] == "process-down"
    assert m2["acpConnected"] is False


def test_map_acp_quality_zombie_send_failures() -> None:
    q = map_acp_quality(
        agent_process_up=True,
        acp_connected=True,
        consecutive_send_failures=ACP_ZOMBIE_SEND_FAILURES,
    )
    assert q["acpQuality"] == "zombie"
    assert q["acpConnected"] is False
    assert q["agentDetail"] == "acp-zombie"
    m = map_agent_status(
        True,
        True,
        consecutive_send_failures=ACP_ZOMBIE_SEND_FAILURES,
    )
    assert m["acpQuality"] == "zombie"
    assert m["acpConnected"] is False
    assert m["agent"] == "down"
    assert m["agentDetail"] == "acp-zombie"


def test_map_acp_quality_stale_with_pending() -> None:
    q = map_acp_quality(
        agent_process_up=True,
        acp_connected=True,
        has_pending=True,
        seconds_since_recv=ACP_STALE_SECONDS,
    )
    assert q["acpQuality"] == "stale"
    assert q["acpConnected"] is False
    assert q["agentDetail"] == "acp-stale"
    m = map_agent_status(
        True,
        True,
        has_pending=True,
        seconds_since_recv=ACP_STALE_SECONDS + 1.0,
    )
    assert m["acpQuality"] == "stale"
    assert m["agent"] == "down"
    assert m["acpConnected"] is False


def test_map_acp_quality_idle_old_recv_still_ok() -> None:
    """Idle connected (no pending RPCs) with old recv stays chat-ready ok."""
    q = map_acp_quality(
        agent_process_up=True,
        acp_connected=True,
        has_pending=False,
        seconds_since_recv=ACP_STALE_SECONDS * 10,
    )
    assert q["acpQuality"] == "ok"
    assert q["acpConnected"] is True
    assert q["agentDetail"] == "ok"
    m = map_agent_status(
        True,
        True,
        has_pending=False,
        seconds_since_recv=999.0,
    )
    assert m["agent"] == "up"
    assert m["acpQuality"] == "ok"


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
    assert 'POST /api/admin/restart-agent' in src or "restart-agent" in src
    assert "handle_restart_agent" in src
    assert "_on_acp_connection" in src
    # Successful connect resets heal attempts
    on_conn = src[src.find("async def _on_acp_connection") :]
    on_conn = on_conn[:1200]
    assert "_acp_heal_attempts = 0" in on_conn
    assert "ACP heal" in on_conn or "heal" in on_conn.lower()


def test_server_status_exposes_turn_telemetry_and_capacity() -> None:
    """status_payload / health carry liveTurns telemetry + capacity banner fields."""
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    assert "turn_telemetry" in src
    assert "def _live_turns_payload" in src
    assert "def _capacity_payload" in src
    assert "turnSilenceSeconds" in src
    assert "turnAgeSeconds" in src
    assert '"capacity"' in src or "'capacity'" in src
    assert "busySessionIds" in src
    assert "ageSeconds" in src
    assert "silenceSeconds" in src
    assert "ttfbSeconds" in src
    assert "sawUpdate" in src
    # Must not drop acp quality fields from Item 1
    assert "acpQuality" in src
    acp = (ROOT / "hub" / "acp_client.py").read_text(encoding="utf-8")
    assert "first_update_at" in acp


def test_server_status_exposes_context_budget() -> None:
    """status/health expose soft contextBudget for primary loadedSessionId."""
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    policy = (ROOT / "hub" / "session_policy.py").read_text(encoding="utf-8")
    assert "def context_budget_level" in policy
    assert "CONTEXT_SOFT_UPDATES_BYTES" in policy
    assert "CONTEXT_SOFT_TOKENS" in policy
    assert "CONTEXT_SOFT_MESSAGE" in policy
    assert "def _context_budget_payload" in src
    assert "context_budget_level" in src
    assert "contextBudget" in src
    assert "updatesBytes" in src
    assert "updates.jsonl" in src
    # Soft only: no hard gate / session-new auto / KillAgent from this path
    budget_fn = src[src.find("def _context_budget_payload") :]
    budget_fn = budget_fn[:1800]
    assert "session/new" not in budget_fn
    assert "KillAgent" not in budget_fn
    assert "hard" not in budget_fn.lower() or "skip if hard" in budget_fn.lower()
