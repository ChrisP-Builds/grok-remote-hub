"""Pure tests for honest agent vs ACP status mapping and heal gating."""

from __future__ import annotations

from pathlib import Path

from hub.status_view import (
    ACP_HEAL_MAX_ATTEMPTS,
    ACP_PROBE_INTERVAL_S,
    ACP_PROBE_SILENCE_S,
    ACP_STALE_SECONDS,
    ACP_ZOMBIE_SEND_FAILURES,
    map_acp_quality,
    map_agent_status,
    should_attempt_acp_heal,
    should_probe_acp_liveness,
    should_suppress_session_load_fanout,
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


def test_should_attempt_acp_heal_skips_stale_when_turn_active() -> None:
    """Pending session/prompt silence is stall-watchdog territory, not reconnect."""
    assert (
        should_attempt_acp_heal(
            **_heal_kwargs(
                disconnected_for_s=12.0,
                acp_quality="stale",
                turn_active=True,
            )
        )
        is False
    )


def test_should_attempt_acp_heal_allows_stale_when_idle() -> None:
    """Idle stale (no live turn) may still heal after min_down."""
    assert (
        should_attempt_acp_heal(
            **_heal_kwargs(
                disconnected_for_s=12.0,
                acp_quality="stale",
                turn_active=False,
            )
        )
        is True
    )


def test_should_attempt_acp_heal_allows_zombie_when_turn_active() -> None:
    """Wire truly dead (zombie) still heals mid-turn after min_down."""
    assert (
        should_attempt_acp_heal(
            **_heal_kwargs(
                disconnected_for_s=12.0,
                acp_quality="zombie",
                turn_active=True,
            )
        )
        is True
    )


def test_acp_stale_seconds_above_no_output() -> None:
    """Stale quality must flip after no-output stall policy owns silent prompts."""
    from hub.session_policy import NO_OUTPUT_SECONDS

    assert ACP_STALE_SECONDS > NO_OUTPUT_SECONDS


def test_should_probe_acp_liveness_gates() -> None:
    assert (
        should_probe_acp_liveness(
            connected=True,
            has_pending=False,
            seconds_since_recv=ACP_PROBE_SILENCE_S,
            seconds_since_probe=None,
        )
        is True
    )
    # Not connected
    assert (
        should_probe_acp_liveness(
            connected=False,
            has_pending=False,
            seconds_since_recv=100.0,
            seconds_since_probe=None,
        )
        is False
    )
    # Pending RPC: stale path handles it
    assert (
        should_probe_acp_liveness(
            connected=True,
            has_pending=True,
            seconds_since_recv=100.0,
            seconds_since_probe=None,
        )
        is False
    )
    # Recent recv
    assert (
        should_probe_acp_liveness(
            connected=True,
            has_pending=False,
            seconds_since_recv=ACP_PROBE_SILENCE_S - 1.0,
            seconds_since_probe=None,
        )
        is False
    )
    # Probe too recent
    assert (
        should_probe_acp_liveness(
            connected=True,
            has_pending=False,
            seconds_since_recv=100.0,
            seconds_since_probe=ACP_PROBE_INTERVAL_S - 1.0,
        )
        is False
    )
    # Never received
    assert (
        should_probe_acp_liveness(
            connected=True,
            has_pending=False,
            seconds_since_recv=None,
            seconds_since_probe=None,
        )
        is False
    )


def test_server_has_acp_heal_hook() -> None:
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    assert "reconnect" in src
    assert "acp heal" in src.lower() or "heal_acp" in src or "acp_heal" in src
    assert "_maybe_heal_acp" in src
    assert "should_attempt_acp_heal" in src
    assert "maybe_probe_liveness" in src
    assert 'POST /api/admin/reconnect-acp' in src or "reconnect-acp" in src
    assert 'POST /api/admin/restart-agent' in src or "restart-agent" in src
    assert 'GET /api/admin/acp-trace' in src or "acp-trace" in src
    assert "handle_restart_agent" in src
    assert "handle_acp_trace" in src
    assert "_on_acp_connection" in src
    # Successful connect resets heal attempts
    on_conn = src[src.find("async def _on_acp_connection") :]
    on_conn = on_conn[:1200]
    assert "_acp_heal_attempts = 0" in on_conn
    assert "ACP heal" in on_conn or "heal" in on_conn.lower()
    acp = (ROOT / "hub" / "acp_client.py").read_text(encoding="utf-8")
    assert "async def maybe_probe_liveness" in acp
    assert "AcpTrace" in acp


def test_suppress_tool_call_during_load() -> None:
    loading = {"sess-a"}
    assert (
        should_suppress_session_load_fanout(
            loading_session_ids=loading,
            session_id="sess-a",
            method="session/update",
            update_kind="tool_call",
        )
        is True
    )
    assert (
        should_suppress_session_load_fanout(
            loading_session_ids=loading,
            session_id="sess-a",
            method="_x.ai/session/update",
            update_kind="agent_thought_chunk",
        )
        is True
    )
    assert (
        should_suppress_session_load_fanout(
            loading_session_ids=loading,
            session_id="sess-a",
            method="_x.ai/session_notification",
            update_kind="auto_compact_start",
        )
        is True
    )


def test_does_not_suppress_when_sid_has_active_turn() -> None:
    """Live re-prompt must receive note_activity even if load suppress still set."""
    loading = {"sess-a"}
    active = frozenset({"sess-a"})
    assert (
        should_suppress_session_load_fanout(
            loading_session_ids=loading,
            session_id="sess-a",
            method="session/update",
            update_kind="tool_call",
            active_turn_session_ids=active,
        )
        is False
    )
    assert (
        should_suppress_session_load_fanout(
            loading_session_ids=loading,
            session_id="sess-a",
            method="_x.ai/session/update",
            update_kind="agent_thought_chunk",
            active_turn_session_ids=active,
        )
        is False
    )


def test_missing_sid_not_suppressed_when_active_turn_exists() -> None:
    loading = {"sess-a"}
    active = frozenset({"sess-a"})
    assert (
        should_suppress_session_load_fanout(
            loading_session_ids=loading,
            session_id=None,
            method="session/update",
            update_kind="tool_call",
            active_turn_session_ids=active,
        )
        is False
    )


def test_allow_available_commands_during_load() -> None:
    loading = {"sess-a"}
    assert (
        should_suppress_session_load_fanout(
            loading_session_ids=loading,
            session_id="sess-a",
            method="session/update",
            update_kind="available_commands_update",
        )
        is False
    )
    # Still allowed when active turn is set
    assert (
        should_suppress_session_load_fanout(
            loading_session_ids=loading,
            session_id="sess-a",
            method="session/update",
            update_kind="available_commands_update",
            active_turn_session_ids=frozenset({"sess-a"}),
        )
        is False
    )


def test_no_suppress_when_loading_empty() -> None:
    assert (
        should_suppress_session_load_fanout(
            loading_session_ids=set(),
            session_id="sess-a",
            method="session/update",
            update_kind="tool_call",
        )
        is False
    )


def test_suppress_missing_sid_while_any_load_active() -> None:
    loading = {"sess-a"}
    assert (
        should_suppress_session_load_fanout(
            loading_session_ids=loading,
            session_id=None,
            method="session/update",
            update_kind="tool_call",
        )
        is True
    )
    assert (
        should_suppress_session_load_fanout(
            loading_session_ids=loading,
            session_id=None,
            method="x.ai/session_notification",
            update_kind=None,
        )
        is True
    )


def test_do_not_suppress_other_session_while_loading() -> None:
    loading = {"sess-a"}
    assert (
        should_suppress_session_load_fanout(
            loading_session_ids=loading,
            session_id="sess-b",
            method="session/update",
            update_kind="tool_call",
        )
        is False
    )


def test_suppress_ignores_non_update_methods() -> None:
    loading = {"sess-a"}
    assert (
        should_suppress_session_load_fanout(
            loading_session_ids=loading,
            session_id="sess-a",
            method="session/prompt",
            update_kind=None,
        )
        is False
    )


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


def test_server_status_no_context_budget_banner() -> None:
    """status/health no longer expose contextBudget; journal size still used for stalls."""
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    policy = (ROOT / "hub" / "session_policy.py").read_text(encoding="utf-8")
    assert "def context_budget_level" not in policy
    assert "CONTEXT_SOFT_MESSAGE" not in policy
    assert "CONTEXT_SOFT_TOKENS" not in policy
    assert "CONTEXT_SOFT_UPDATES_BYTES" in policy
    assert "no_output_seconds_for_session" in policy
    assert "def _context_budget_payload" not in src
    assert "contextBudget" not in src
    assert "context_budget_level" not in src
    # Internal stall scaling still measures updates.jsonl size
    assert "def _session_updates_bytes" in src
    assert "updates.jsonl" in src
    assert "no_output_seconds_for_session" in src
