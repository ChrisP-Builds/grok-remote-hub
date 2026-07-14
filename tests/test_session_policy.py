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
    entry_requires_resume_choice,
    is_hub_resume_candidate,
    is_no_output_error_message,
    is_turn_stuck_for_new_prompt,
    load_hub_session_ids,
    load_remote_sessions,
    needs_fresh_agent_session,
    recovery_keeps_session_id,
    resolve_ensure_action,
    resolve_live_session_id,
    save_remote_sessions,
    sessions_matching_cwd,
    should_auto_retry_no_output,
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


def test_is_hub_resume_candidate_true_paths() -> None:
    created = {"live-1"}
    remote_ids = {"map-1"}
    assert is_hub_resume_candidate(
        "live-1", created_set=created, remote_map_ids=remote_ids
    )
    assert is_hub_resume_candidate(
        "map-1", created_set=created, remote_map_ids=remote_ids
    )
    assert is_hub_resume_candidate(
        "stamped",
        created_set=set(),
        remote_map_ids=set(),
        hub_origin="user",
    )
    assert is_hub_resume_candidate(
        "stamped",
        created_set=set(),
        remote_map_ids=set(),
        hub_origin="attach",
    )


def test_is_hub_resume_candidate_false_foreign() -> None:
    assert (
        is_hub_resume_candidate(
            "cli-foreign",
            created_set=set(),
            remote_map_ids=set(),
            hub_origin=None,
        )
        is False
    )
    assert (
        is_hub_resume_candidate(
            "cli-foreign",
            created_set={"other"},
            remote_map_ids={"other-map"},
            hub_origin="",
        )
        is False
    )
    assert (
        is_hub_resume_candidate(
            None, created_set=set(), remote_map_ids=set(), hub_origin="user"
        )
        is False
    )
    assert (
        is_hub_resume_candidate(
            "x", created_set=set(), remote_map_ids=set(), hub_origin="other"
        )
        is False
    )


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


def test_disk_remote_map_does_not_skip_session_new() -> None:
    """After restart: remote map alone must not silent-reuse; may select load."""
    dead_id = "019f53c6-6107-76d2-99e9-ec7a09970671"
    remote = {cwd_key(r"D:\Projects\equine website"): dead_id}
    created: set[str] = set()  # process-local; disk must not seed this

    # Process-live-only resolver still requires session/new (no silent reuse).
    live, needs_new, reason = resolve_live_session_id(
        dead_id,
        r"D:\Projects\equine website",
        created,
        remote,
    )
    assert needs_new is True
    assert live is None
    assert reason == "need_session_new"

    # Ensure action: view equals map id → view resume candidate (resume_view).
    target, action, ensure_reason = resolve_ensure_action(
        dead_id,
        r"D:\Projects\equine website",
        created,
        remote,
    )
    assert action == "load"
    assert target == dead_id
    assert ensure_reason == "resume_view"
    assert action != "reuse"


def test_process_local_created_reuses_without_session_new() -> None:
    """Same hub process: view process-live wins → hub_session (even if byCwd same)."""
    hub_id = "019f53c6-6107-76d2-99e9-ec7a09970671"
    remote = {cwd_key(r"D:\Projects\equine website"): hub_id}
    created = {hub_id}
    live, needs_new, reason = resolve_live_session_id(
        hub_id,
        r"D:\Projects\equine website",
        created,
        remote,
    )
    assert needs_new is False
    assert live == hub_id
    assert reason == "hub_session"

    target, action, ensure_reason = resolve_ensure_action(
        hub_id,
        r"D:\Projects\equine website",
        created,
        remote,
    )
    assert action == "reuse"
    assert target == hub_id
    assert ensure_reason == "hub_session"


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


def test_resolve_ensure_process_live_reuse() -> None:
    created = {"hub-aaa"}
    target, action, reason = resolve_ensure_action(
        "hub-aaa",
        r"D:\Projects\Demo",
        created,
        {},
    )
    assert target == "hub-aaa"
    assert action == "reuse"
    assert reason == "hub_session"


def test_resolve_ensure_view_preferred_over_bycwd() -> None:
    """View hub resume candidate wins over a different byCwd map id."""
    viewed = "viewed-hub-id"
    mapped = "mapped-other-id"
    remote = {cwd_key(r"D:\Projects\Demo"): mapped}
    created: set[str] = set()
    target, action, reason = resolve_ensure_action(
        viewed,
        r"D:\Projects\Demo",
        created,
        remote,
        view_hub_origin="user",
        remote_hub_origin="attach",
    )
    assert target == viewed
    assert action == "load"
    assert reason == "resume_view"
    assert target != mapped


def test_resolve_ensure_view_process_live_over_bycwd_live() -> None:
    """View process-live wins over a different byCwd process-live id."""
    viewed = "viewed-live"
    mapped = "mapped-live"
    remote = {cwd_key(r"D:\Projects\Demo"): mapped}
    created = {viewed, mapped}
    target, action, reason = resolve_ensure_action(
        viewed,
        r"D:\Projects\Demo",
        created,
        remote,
    )
    assert target == viewed
    assert action == "reuse"
    assert reason == "hub_session"
    assert target != mapped


def test_resolve_ensure_empty_created_remote_map_loads() -> None:
    """Empty created + view equals remote map → resume_view (view wins)."""
    sid = "prior-process-hub"
    remote = {cwd_key(r"D:\Projects\X"): sid}
    target, action, reason = resolve_ensure_action(
        sid,
        r"D:\Projects\X",
        set(),
        remote,
    )
    assert target == sid
    assert action == "load"
    assert reason == "resume_view"


def test_resolve_ensure_foreign_new() -> None:
    target, action, reason = resolve_ensure_action(
        "cli-foreign-id",
        r"D:\Projects\Demo",
        set(),
        {},
        view_hub_origin=None,
    )
    assert target is None
    assert action == "new"
    assert reason == "need_session_new"


def test_resolve_ensure_resume_cwd_when_view_foreign() -> None:
    mapped = "hub-from-map"
    remote = {cwd_key(r"D:\Projects\Demo"): mapped}
    target, action, reason = resolve_ensure_action(
        "cli-foreign",
        r"D:\Projects\Demo",
        set(),
        remote,
        view_hub_origin=None,
        remote_hub_origin="attach",
    )
    assert target == mapped
    assert action == "load"
    assert reason == "resume_cwd"


def test_resolve_ensure_reuse_cwd_process_live() -> None:
    live = "hub-live-cwd"
    remote = {cwd_key(r"D:\Projects\Demo"): live}
    target, action, reason = resolve_ensure_action(
        "cli-foreign",
        r"D:\Projects\Demo",
        {live},
        remote,
    )
    assert target == live
    assert action == "reuse"
    assert reason == "reuse_cwd"


def test_multi_turn_same_hub_session_no_fresh() -> None:
    """Two prompts on same hub session: needs_fresh stays false; view live → hub_session."""
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
    # byCwd values land in hubIds
    hub_ids = load_hub_session_ids(path)
    assert "sess-a" in hub_ids
    assert "sess-b" in hub_ids


def test_remote_sessions_hub_ids_roundtrip(tmp_path: Path) -> None:
    """hubIds persist across saves; load_remote_sessions stays byCwd-only."""
    path = tmp_path / "remote-sessions.json"
    mapping = {cwd_key(r"D:\Projects\A"): "current-map-id"}
    save_remote_sessions(
        path,
        mapping,
        hub_ids={"current-map-id", "older-hub-id-not-in-bycwd"},
    )
    loaded = load_remote_sessions(path)
    assert loaded == {cwd_key(r"D:\Projects\A"): "current-map-id"}
    assert "hubIds" not in loaded  # byCwd dict only

    hub_ids = load_hub_session_ids(path)
    assert "current-map-id" in hub_ids
    assert "older-hub-id-not-in-bycwd" in hub_ids

    # Save without hub_ids arg preserves prior hubIds
    save_remote_sessions(path, {cwd_key(r"D:\Projects\A"): "newer-id"})
    hub_ids2 = load_hub_session_ids(path)
    assert "older-hub-id-not-in-bycwd" in hub_ids2
    assert "newer-id" in hub_ids2
    assert "current-map-id" in hub_ids2  # preserved even after map overwrite


def test_resolve_ensure_hub_ids_view_wins_over_bycwd() -> None:
    """Older viewed in hubIds wins over newer byCwd (view is continuity)."""
    viewed = "019f4d9f-older-hub-owned"
    mapped = "019f578a-current-bycwd"
    remote = {cwd_key(r"D:\Projects\Grok Remote Hub"): mapped}
    hub_owned = {viewed, mapped}
    target, action, reason = resolve_ensure_action(
        viewed,
        r"D:\Projects\Grok Remote Hub",
        set(),  # post-restart empty process-live
        remote,
        view_hub_origin=None,  # stamp missing
        remote_hub_origin=None,
        hub_owned_ids=hub_owned,
    )
    assert target == viewed
    assert action == "load"
    assert reason == "resume_view"
    assert target != mapped


def test_resolve_ensure_view_in_hub_ids_when_bycwd_empty() -> None:
    """View in hubIds with empty byCwd → resume_view."""
    viewed = "019f4d9f-older-hub-owned"
    hub_owned = {viewed}
    target, action, reason = resolve_ensure_action(
        viewed,
        r"D:\Projects\Grok Remote Hub",
        set(),
        {},  # no byCwd entry for this cwd
        view_hub_origin=None,
        remote_hub_origin=None,
        hub_owned_ids=hub_owned,
    )
    assert target == viewed
    assert action == "load"
    assert reason == "resume_view"


def test_resolve_ensure_foreign_view_falls_to_bycwd() -> None:
    """Foreign/not-resumeable view falls through to byCwd resume."""
    viewed = "019f4d9f-older-hub-owned"
    mapped = "019f578a-current-bycwd"
    remote = {cwd_key(r"D:\Projects\Grok Remote Hub"): mapped}
    target, action, reason = resolve_ensure_action(
        viewed,
        r"D:\Projects\Grok Remote Hub",
        set(),
        remote,
        view_hub_origin=None,
        remote_hub_origin=None,
        hub_owned_ids=None,
    )
    assert target == mapped
    assert action == "load"
    assert reason == "resume_cwd"


def test_resolve_ensure_empty_view_uses_bycwd() -> None:
    """Empty view → byCwd path (process-live or resume)."""
    mapped = "hub-from-map"
    remote = {cwd_key(r"D:\Projects\Demo"): mapped}
    # Process-live byCwd
    target, action, reason = resolve_ensure_action(
        None,
        r"D:\Projects\Demo",
        {mapped},
        remote,
    )
    assert target == mapped
    assert action == "reuse"
    assert reason == "reuse_cwd"
    # Resume candidate byCwd
    target2, action2, reason2 = resolve_ensure_action(
        "",
        r"D:\Projects\Demo",
        set(),
        remote,
        remote_hub_origin="attach",
    )
    assert target2 == mapped
    assert action2 == "load"
    assert reason2 == "resume_cwd"


def test_remote_sessions_load_missing_empty(tmp_path: Path) -> None:
    assert load_remote_sessions(tmp_path / "nope.json") == {}
    assert load_hub_session_ids(tmp_path / "nope.json") == set()


def test_remote_sessions_load_invalid_empty(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("not-json{{{", encoding="utf-8")
    assert load_remote_sessions(path) == {}
    assert load_hub_session_ids(path) == set()


def test_cwd_key_normalizes() -> None:
    assert cwd_key(r"D:\Projects\Foo") == cwd_key(r"d:/Projects/Foo/")
    assert cwd_key("") == ""


def test_sessions_matching_cwd_filters_sorts_and_excludes_subagents() -> None:
    items = [
        {
            "sessionId": "old",
            "cwd": r"D:\Projects\Demo",
            "updatedAt": "2026-01-01T00:00:00Z",
            "isSubagent": False,
        },
        {
            "sessionId": "new",
            "cwd": r"d:/Projects/Demo/",
            "updatedAt": "2026-07-01T12:00:00Z",
            "isSubagent": False,
        },
        {
            "sessionId": "sub",
            "cwd": r"D:\Projects\Demo",
            "updatedAt": "2026-07-02T00:00:00Z",
            "isSubagent": True,
        },
        {
            "sessionId": "other",
            "cwd": r"D:\Projects\Other",
            "updatedAt": "2026-07-03T00:00:00Z",
            "isSubagent": False,
        },
    ]
    matched = sessions_matching_cwd(items, r"D:\Projects\Demo")
    assert [m["sessionId"] for m in matched] == ["new", "old"]

    with_sub = sessions_matching_cwd(
        items, r"D:\Projects\Demo", exclude_subagents=False
    )
    assert [m["sessionId"] for m in with_sub] == ["sub", "new", "old"]

    assert sessions_matching_cwd(None, r"D:\Projects\Demo") == []
    assert sessions_matching_cwd(items, None) == []
    assert sessions_matching_cwd(items, "") == []


def test_entry_requires_resume_choice() -> None:
    assert entry_requires_resume_choice(0) is False
    assert entry_requires_resume_choice(1) is True
    assert entry_requires_resume_choice(3) is True


def test_recovery_keeps_session_id() -> None:
    assert recovery_keeps_session_id("a", "a", False) is True
    assert recovery_keeps_session_id("a", "b", False) is False
    assert recovery_keeps_session_id("a", "a", True) is False
    assert recovery_keeps_session_id("", "a", False) is False
    assert recovery_keeps_session_id("a", "", False) is False


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


def test_is_no_output_error_message() -> None:
    assert is_no_output_error_message(
        "no ACP session/update for 60.0s after prompt (threshold=60.0s)"
    )
    assert is_no_output_error_message(
        RuntimeError("force-cleared: no ACP session/update for 61.2s")
    )
    assert not is_no_output_error_message("mid-turn stall (120s)")
    assert not is_no_output_error_message("force-cleared: user cancel")
    assert not is_no_output_error_message("")
    assert not is_no_output_error_message(None)


def test_should_auto_retry_no_output() -> None:
    exc = RuntimeError(
        "no ACP session/update for 60.1s after prompt (threshold=60.0s)"
    )
    assert should_auto_retry_no_output(exc, already_retried=False) is True
    assert should_auto_retry_no_output(exc, already_retried=True) is False
    assert (
        should_auto_retry_no_output(
            "force-cleared turn: no ACP session/update for 90s",
            already_retried=False,
        )
        is True
    )
    assert (
        should_auto_retry_no_output(
            RuntimeError("mid-turn stall"), already_retried=False
        )
        is False
    )


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


def test_zero_updates_past_no_output_threshold_clears() -> None:
    """Product: live turn with zero stream past NO_OUTPUT_SECONDS must force-clear.

    123s empty working is past bound (regression for multi-minute silent wait).
    """
    for age in (NO_OUTPUT_SECONDS, NO_OUTPUT_SECONDS + 1, 123.0, 300.0):
        reason = should_force_clear_turn(False, age, age)
        assert reason is not None
        assert "no ACP session/update" in reason


def test_under_no_output_threshold_keeps_running_without_updates() -> None:
    reason = should_force_clear_turn(
        False,
        NO_OUTPUT_SECONDS - 1,
        NO_OUTPUT_SECONDS - 1,
    )
    assert reason is None


def test_auto_retry_only_once_same_session_no_fork_signal() -> None:
    """One same-session auto-retry for force-clear no-output; never a second."""
    reason = should_force_clear_turn(
        False,
        NO_OUTPUT_SECONDS + 0.1,
        NO_OUTPUT_SECONDS + 0.1,
    )
    assert reason is not None
    msg = f"Turn force-cleared: {reason}"
    assert should_auto_retry_no_output(msg, already_retried=False) is True
    assert should_auto_retry_no_output(msg, already_retried=True) is False


def test_client_stall_never_auto_unlocks() -> None:
    """Client soft-warn only; server owns unlock (CLIENT_STALL_UNLOCK_SECONDS=0)."""
    assert CLIENT_STALL_WARN_SECONDS == 120
    assert CLIENT_STALL_UNLOCK_SECONDS == 0
