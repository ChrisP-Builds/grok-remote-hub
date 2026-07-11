"""Unit tests for hub remote-session policy (pure)."""

from __future__ import annotations

from pathlib import Path

from hub.session_policy import (
    CLIENT_STALL_UNLOCK_SECONDS,
    CLIENT_STALL_WARN_SECONDS,
    MAX_TURN_SECONDS,
    MID_TURN_STALL_SECONDS,
    NO_OUTPUT_SECONDS,
    STUCK_TURN_SECONDS,
    cwd_key,
    is_turn_stuck_for_new_prompt,
    load_remote_sessions,
    needs_fresh_agent_session,
    resolve_live_session_id,
    save_remote_sessions,
    should_force_clear_turn,
)


def test_tui_aligned_timeout_constants() -> None:
    assert NO_OUTPUT_SECONDS == 60.0
    assert MID_TURN_STALL_SECONDS == 600.0
    assert MAX_TURN_SECONDS == 1800.0
    assert STUCK_TURN_SECONDS == 1800.0
    assert CLIENT_STALL_WARN_SECONDS == 120.0
    assert CLIENT_STALL_UNLOCK_SECONDS == 0


def test_needs_fresh_when_empty_id() -> None:
    assert needs_fresh_agent_session(None, set()) is True
    assert needs_fresh_agent_session("", set()) is True
    assert needs_fresh_agent_session("   ", set()) is True


def test_needs_fresh_when_not_in_created_set() -> None:
    created = {"hub-aaa", "hub-bbb"}
    assert needs_fresh_agent_session("cli-session-xyz", created) is True
    assert needs_fresh_agent_session("019f493c-af12-7652-a6d8-bf645c10921c", created) is True


def test_no_fresh_when_hub_created() -> None:
    created = {"hub-aaa", "hub-bbb"}
    assert needs_fresh_agent_session("hub-aaa", created) is False
    assert needs_fresh_agent_session("hub-bbb", created) is False


def test_created_set_is_process_local_without_restore() -> None:
    # Without loading remote-sessions.json, empty set means all need fresh.
    assert needs_fresh_agent_session("hub-from-prior-process", set()) is True


def test_resolve_live_foreign_needs_new() -> None:
    created: set[str] = set()
    remote: dict[str, str] = {}
    live, needs_new, reason = resolve_live_session_id(
        "cli-foreign-id",
        r"D:\Projects\Demo",
        created,
        remote,
    )
    assert live is None
    assert needs_new is True
    assert reason == "need_session_new"


def test_resolve_live_foreign_reuses_cwd_remote() -> None:
    created = {"hub-live-1"}
    remote = {cwd_key(r"D:\Projects\Demo"): "hub-live-1"}
    live, needs_new, reason = resolve_live_session_id(
        "cli-foreign-id",
        r"D:\Projects\Demo",
        created,
        remote,
    )
    assert live == "hub-live-1"
    assert needs_new is False
    assert reason == "reuse_cwd"
    assert live != "cli-foreign-id"


def test_resolve_live_hub_session_same() -> None:
    created = {"hub-aaa"}
    live, needs_new, reason = resolve_live_session_id(
        "hub-aaa",
        r"D:\Projects\Demo",
        created,
        {},
    )
    assert live == "hub-aaa"
    assert needs_new is False
    assert reason == "hub_session"


def test_multi_turn_same_hub_session_no_fresh() -> None:
    """Two prompts on same hub session: needs_fresh stays false."""
    hub_id = "hub-multi-turn"
    created = {hub_id}
    remote = {cwd_key(r"D:\Projects\X"): hub_id}

    assert needs_fresh_agent_session(hub_id, created) is False
    live1, n1, r1 = resolve_live_session_id(hub_id, r"D:\Projects\X", created, remote)
    assert live1 == hub_id and n1 is False and r1 == "hub_session"

    # Second turn: same state
    assert needs_fresh_agent_session(hub_id, created) is False
    live2, n2, r2 = resolve_live_session_id(hub_id, r"D:\Projects\X", created, remote)
    assert live2 == hub_id and n2 is False and r2 == "hub_session"
    assert live1 == live2


def test_remote_sessions_json_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "remote-sessions.json"
    mapping = {
        cwd_key(r"D:\Projects\A"): "sess-a",
        cwd_key(r"D:/Projects/B"): "sess-b",
    }
    save_remote_sessions(path, mapping)
    assert path.is_file()
    loaded = load_remote_sessions(path)
    assert loaded[cwd_key(r"D:\Projects\A")] == "sess-a"
    assert loaded[cwd_key(r"D:\Projects\B")] == "sess-b"


def test_remote_sessions_load_missing_empty(tmp_path: Path) -> None:
    assert load_remote_sessions(tmp_path / "nope.json") == {}


def test_remote_sessions_load_invalid_empty(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("not-json{{{", encoding="utf-8")
    assert load_remote_sessions(path) == {}


def test_cwd_key_normalizes() -> None:
    assert cwd_key(r"D:\Projects\Foo") == cwd_key(r"d:/Projects/Foo/")
    assert cwd_key("") == ""


def test_should_force_clear_none_while_active() -> None:
    # Recent activity, saw updates, under max duration
    assert (
        should_force_clear_turn(
            True,
            age_since_start=60.0,
            age_since_activity=5.0,
        )
        is None
    )
    # Never saw update but still under no-output threshold
    assert (
        should_force_clear_turn(
            False,
            age_since_start=10.0,
            age_since_activity=10.0,
        )
        is None
    )


def test_should_force_clear_healthy_long_agentic() -> None:
    """200s age with activity 10s ago must keep running (TUI-length tools)."""
    assert (
        should_force_clear_turn(
            True,
            age_since_start=200.0,
            age_since_activity=10.0,
        )
        is None
    )
    # Still healthy well past old 90s mid-stall and 120s stuck walls
    assert (
        should_force_clear_turn(
            True,
            age_since_start=500.0,
            age_since_activity=30.0,
        )
        is None
    )


def test_should_force_clear_no_output() -> None:
    reason = should_force_clear_turn(
        False,
        age_since_start=NO_OUTPUT_SECONDS,
        age_since_activity=NO_OUTPUT_SECONDS,
    )
    assert reason is not None
    assert "no ACP session/update" in reason


def test_should_force_clear_mid_turn_stall() -> None:
    """Regression: first update must not stop stall detection forever."""
    reason = should_force_clear_turn(
        True,
        age_since_start=700.0,
        age_since_activity=MID_TURN_STALL_SECONDS,
    )
    assert reason is not None
    assert "mid-turn stall" in reason
    # Under threshold: keep running
    assert (
        should_force_clear_turn(
            True,
            age_since_start=700.0,
            age_since_activity=MID_TURN_STALL_SECONDS - 1.0,
        )
        is None
    )


def test_should_force_clear_max_turn_even_with_activity() -> None:
    reason = should_force_clear_turn(
        True,
        age_since_start=MAX_TURN_SECONDS,
        age_since_activity=1.0,
    )
    assert reason is not None
    assert "max turn duration" in reason


def test_should_force_clear_max_turn_priority_over_mid_stall() -> None:
    reason = should_force_clear_turn(
        True,
        age_since_start=MAX_TURN_SECONDS + 10.0,
        age_since_activity=MID_TURN_STALL_SECONDS + 10.0,
    )
    assert reason is not None
    assert "max turn duration" in reason


def test_should_force_clear_age_1900_max_turn() -> None:
    reason = should_force_clear_turn(
        True,
        age_since_start=1900.0,
        age_since_activity=5.0,
    )
    assert reason is not None
    assert "max turn duration" in reason


def test_is_turn_stuck_for_new_prompt_matches_force_clear() -> None:
    # Healthy activity: not stuck for new prompt
    assert (
        is_turn_stuck_for_new_prompt(True, age_since_start=200.0, age_since_activity=10.0)
        is False
    )
    # Mid-turn stall
    assert (
        is_turn_stuck_for_new_prompt(
            True,
            age_since_start=700.0,
            age_since_activity=MID_TURN_STALL_SECONDS,
        )
        is True
    )
    # Max wall
    assert (
        is_turn_stuck_for_new_prompt(
            True,
            age_since_start=1900.0,
            age_since_activity=5.0,
        )
        is True
    )
    # No-output dead on arrival
    assert (
        is_turn_stuck_for_new_prompt(
            False,
            age_since_start=NO_OUTPUT_SECONDS,
            age_since_activity=NO_OUTPUT_SECONDS,
        )
        is True
    )
