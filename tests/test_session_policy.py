"""Unit tests for hub remote-session policy (pure)."""

from __future__ import annotations

from pathlib import Path

from hub.session_policy import (
    CLIENT_STALL_UNLOCK_SECONDS,
    CLIENT_STALL_WARN_SECONDS,
    CONTEXT_SOFT_UPDATES_BYTES,
    HOT_PATH_MAX_HUB_PRE_PROMPT_S,
    MAX_TURN_SECONDS,
    MID_TURN_STALL_SECONDS,
    NO_OUTPUT_HEAVY_SECONDS,
    NO_OUTPUT_HEAVY_UPDATES_BYTES,
    NO_OUTPUT_RETRY_SECONDS,
    NO_OUTPUT_SECONDS,
    NO_OUTPUT_SOFT_SECONDS,
    STUCK_TURN_SECONDS,
    apply_turn_activity,
    counts_toward_agent_ttfb,
    cwd_key,
    entry_requires_resume_choice,
    is_hub_resume_candidate,
    is_live_hot_path,
    is_no_output_error_message,
    is_turn_stuck_for_new_prompt,
    load_hub_session_ids,
    load_remote_sessions,
    no_output_seconds_for_session,
    recovery_keeps_session_id,
    resolve_ensure_action,
    resolve_live_session_id,
    save_remote_sessions,
    sessions_matching_cwd,
    should_auto_retry_no_output,
    should_clear_turn_on_wake,
    should_force_clear_turn,
    should_skip_session_load,
    turn_telemetry,
)

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def test_tui_aligned_timeout_constants() -> None:
    assert NO_OUTPUT_SECONDS == 60.0
    assert NO_OUTPUT_SOFT_SECONDS == 180.0
    assert NO_OUTPUT_HEAVY_SECONDS == 300.0
    assert NO_OUTPUT_HEAVY_UPDATES_BYTES == 12_000_000
    assert NO_OUTPUT_RETRY_SECONDS == 90.0
    assert MID_TURN_STALL_SECONDS == 600.0
    assert MAX_TURN_SECONDS == 1800.0
    assert STUCK_TURN_SECONDS == 1800.0
    assert CLIENT_STALL_WARN_SECONDS == 120.0
    assert CLIENT_STALL_UNLOCK_SECONDS == 0


def test_no_output_seconds_for_session_none_is_base() -> None:
    assert no_output_seconds_for_session() == 60.0
    assert no_output_seconds_for_session(updates_bytes=None) == 60.0


def test_no_output_seconds_for_session_soft() -> None:
    # First-byte never scales by history size (CLI-like 60s).
    assert (
        no_output_seconds_for_session(
            updates_bytes=CONTEXT_SOFT_UPDATES_BYTES + 1
        )
        == 60.0
    )
    assert (
        no_output_seconds_for_session(updates_bytes=CONTEXT_SOFT_UPDATES_BYTES)
        == 60.0
    )


def test_no_output_seconds_for_session_heavy() -> None:
    assert (
        no_output_seconds_for_session(
            updates_bytes=NO_OUTPUT_HEAVY_UPDATES_BYTES + 1
        )
        == 60.0
    )
    assert no_output_seconds_for_session(updates_bytes=7_000_000) == 60.0
    assert (
        no_output_seconds_for_session(
            updates_bytes=NO_OUTPUT_HEAVY_UPDATES_BYTES
        )
        == 60.0
    )


def test_turn_telemetry_before_first_update() -> None:
    tel = turn_telemetry(
        started_at=100.0,
        last_activity=100.0,
        saw_update=False,
        now=108.0,
        first_update_at=None,
    )
    assert tel["ageSeconds"] == 8.0
    assert tel["silenceSeconds"] == 8.0
    assert tel["sawUpdate"] is False
    assert tel["ttfbSeconds"] is None


def test_turn_telemetry_silence_after_activity() -> None:
    tel = turn_telemetry(
        started_at=100.0,
        last_activity=120.0,
        saw_update=True,
        now=135.0,
        first_update_at=105.0,
    )
    assert tel["ageSeconds"] == 35.0
    assert tel["silenceSeconds"] == 15.0
    assert tel["sawUpdate"] is True
    # TTFB frozen at first_update_at, not last_activity
    assert tel["ttfbSeconds"] == 5.0


def test_turn_telemetry_ttfb_fallback_without_first_update_at() -> None:
    tel = turn_telemetry(
        started_at=50.0,
        last_activity=53.5,
        saw_update=True,
        now=60.0,
        first_update_at=None,
    )
    assert tel["ttfbSeconds"] == 3.5
    assert tel["ageSeconds"] == 10.0
    assert tel["silenceSeconds"] == 6.5


def test_turn_telemetry_missing_start() -> None:
    tel = turn_telemetry(
        started_at=None,
        last_activity=None,
        saw_update=False,
        now=10.0,
    )
    assert tel["ageSeconds"] is None
    assert tel["silenceSeconds"] is None
    assert tel["ttfbSeconds"] is None
    assert tel["sawUpdate"] is False


def test_turn_telemetry_no_activity_stamp_uses_age_for_silence() -> None:
    tel = turn_telemetry(
        started_at=10.0,
        last_activity=None,
        saw_update=False,
        now=22.0,
    )
    assert tel["ageSeconds"] == 12.0
    assert tel["silenceSeconds"] == 12.0
    assert tel["ttfbSeconds"] is None


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
    """Two prompts on same hub session: view live → hub_session both turns."""
    hub_id = "hub-multi-turn"
    created = {hub_id}
    remote = {cwd_key(r"D:\Projects\X"): hub_id}

    live1, n1, r1 = resolve_live_session_id(hub_id, r"D:\Projects\X", created, remote)
    assert live1 == hub_id and n1 is False and r1 == "hub_session"

    # Second turn: same state
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


def test_context_soft_updates_bytes_not_used_for_first_byte() -> None:
    """First-byte silence always base; soft/heavy history size ignored."""
    assert CONTEXT_SOFT_UPDATES_BYTES == 6_000_000
    assert no_output_seconds_for_session(updates_bytes=0) == NO_OUTPUT_SECONDS
    assert (
        no_output_seconds_for_session(updates_bytes=CONTEXT_SOFT_UPDATES_BYTES)
        == NO_OUTPUT_SECONDS
    )
    assert (
        no_output_seconds_for_session(updates_bytes=CONTEXT_SOFT_UPDATES_BYTES + 1)
        == NO_OUTPUT_SECONDS
    )
    assert (
        no_output_seconds_for_session(updates_bytes=NO_OUTPUT_HEAVY_UPDATES_BYTES + 1)
        == NO_OUTPUT_SECONDS
    )


def test_should_clear_turn_on_wake_not_running() -> None:
    assert (
        should_clear_turn_on_wake(
            turn_running=False,
            silence_seconds=999.0,
            acp_quality="zombie",
        )
        is False
    )


def test_should_clear_turn_on_wake_bad_quality() -> None:
    for q in ("stale", "zombie", "down", "STALE", "Zombie"):
        assert (
            should_clear_turn_on_wake(
                turn_running=True,
                silence_seconds=None,
                acp_quality=q,
            )
            is True
        ), q


def test_should_clear_turn_on_wake_silence_threshold() -> None:
    assert (
        should_clear_turn_on_wake(
            turn_running=True,
            silence_seconds=120.0,
            acp_quality="ok",
        )
        is True
    )
    assert (
        should_clear_turn_on_wake(
            turn_running=True,
            silence_seconds=119.9,
            acp_quality="ok",
        )
        is False
    )
    assert (
        should_clear_turn_on_wake(
            turn_running=True,
            silence_seconds=60.0,
            acp_quality="ok",
            silence_threshold_s=60.0,
        )
        is True
    )


def test_should_clear_turn_on_wake_open_tools_protects() -> None:
    """Healthy quality + open tools: silence alone must not clear mid-tool work."""
    assert (
        should_clear_turn_on_wake(
            turn_running=True,
            silence_seconds=300.0,
            acp_quality="ok",
            has_open_tools=True,
        )
        is False
    )
    assert (
        should_clear_turn_on_wake(
            turn_running=True,
            silence_seconds=300.0,
            acp_quality=None,
            has_open_tools=True,
        )
        is False
    )
    # Bad quality still clears even with open tools
    assert (
        should_clear_turn_on_wake(
            turn_running=True,
            silence_seconds=300.0,
            acp_quality="zombie",
            has_open_tools=True,
        )
        is True
    )
    # No open tools: silence still clears
    assert (
        should_clear_turn_on_wake(
            turn_running=True,
            silence_seconds=120.0,
            acp_quality="ok",
            has_open_tools=False,
        )
        is True
    )


def test_should_clear_turn_on_wake_healthy_keep() -> None:
    assert (
        should_clear_turn_on_wake(
            turn_running=True,
            silence_seconds=None,
            acp_quality="ok",
        )
        is False
    )
    assert (
        should_clear_turn_on_wake(
            turn_running=True,
            silence_seconds=10.0,
            acp_quality=None,
        )
        is False
    )
    assert (
        should_clear_turn_on_wake(
            turn_running=True,
            silence_seconds=None,
            acp_quality=None,
        )
        is False
    )


def test_app_js_reconcile_turn_after_wake_wired() -> None:
    """Structural: wake reconcile exists and visibility/pageshow/online wire it."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function reconcileTurnAfterWake" in js
    assert "function shouldClearTurnOnWake" in js
    assert "hasOpenTools" in js
    assert "visibilitychange" in js
    assert "pageshow" in js
    assert "scheduleWakeReconcile" in js
    assert "handleWakeReconcile" in js
    assert "reconcileTurnAfterWake" in js
    # visibility handler debounces into wake reconcile (not only connectWs)
    vis_idx = js.find('document.addEventListener("visibilitychange"')
    assert vis_idx >= 0
    vis_chunk = js[vis_idx : vis_idx + 900]
    assert "scheduleWakeReconcile" in vis_chunk
    assert 'addEventListener("online"' in js or "addEventListener('online'" in js
    assert 'addEventListener("pageshow"' in js or "addEventListener('pageshow'" in js


def test_is_live_hot_path_reuse_only() -> None:
    assert is_live_hot_path(ensure_action="reuse", acp_connected=True) is True
    assert is_live_hot_path(ensure_action="REUSE", acp_connected=True) is True
    assert is_live_hot_path(ensure_action="reuse", acp_connected=False) is False
    assert is_live_hot_path(ensure_action="load", acp_connected=True) is False
    assert is_live_hot_path(ensure_action="new", acp_connected=True) is False
    assert is_live_hot_path(ensure_action="", acp_connected=True) is False
    assert HOT_PATH_MAX_HUB_PRE_PROMPT_S == 5.0
    assert HOT_PATH_MAX_HUB_PRE_PROMPT_S < NO_OUTPUT_SECONDS


def test_counts_toward_agent_ttfb() -> None:
    assert counts_toward_agent_ttfb(None) is True
    assert counts_toward_agent_ttfb("") is True
    assert counts_toward_agent_ttfb("   ") is True
    assert counts_toward_agent_ttfb("user_message_chunk") is False
    assert counts_toward_agent_ttfb("USER_MESSAGE_CHUNK") is False
    assert counts_toward_agent_ttfb("available_commands_update") is False
    assert counts_toward_agent_ttfb("user_other") is False
    assert counts_toward_agent_ttfb("agent_thought_chunk") is True
    assert counts_toward_agent_ttfb("agent_message_chunk") is True
    assert counts_toward_agent_ttfb("tool_call") is True
    assert counts_toward_agent_ttfb("tool_call_update") is True


def test_apply_turn_activity_user_echo_not_ttfb() -> None:
    meta: dict = {
        "started_at": 100.0,
        "last_activity": 100.0,
        "saw_update": False,
        "first_update_at": None,
    }
    assert apply_turn_activity(meta, now=101.0, update_kind="user_message_chunk") is False
    assert meta["saw_update"] is True
    assert meta["last_activity"] == 101.0
    assert meta["first_update_at"] is None

    assert apply_turn_activity(meta, now=102.5, update_kind="agent_thought_chunk") is True
    assert meta["first_update_at"] == 102.5
    assert apply_turn_activity(meta, now=110.0, update_kind="tool_call") is False
    assert meta["first_update_at"] == 102.5
    assert meta["last_activity"] == 110.0


def test_apply_turn_activity_none_kind_is_agent() -> None:
    meta: dict = {
        "started_at": 1.0,
        "last_activity": 1.0,
        "saw_update": False,
        "first_update_at": None,
    }
    assert apply_turn_activity(meta, now=3.0, update_kind=None) is True
    assert meta["first_update_at"] == 3.0


def test_resolve_ensure_process_live_is_reuse() -> None:
    """Alias clarity: view in created_set => action reuse (hot path)."""
    created = {"hub-live-1"}
    target, action, reason = resolve_ensure_action(
        "hub-live-1",
        r"D:\Projects\Demo",
        created,
        {},
    )
    assert target == "hub-live-1"
    assert action == "reuse"
    assert reason == "hub_session"
    assert is_live_hot_path(ensure_action=action, acp_connected=True) is True


def _slice_method(src: str, name: str) -> str:
    for prefix in (f"async def {name}(", f"def {name}("):
        start = src.find(prefix)
        if start >= 0:
            break
    else:
        raise AssertionError(f"method {name} not found")
    line_start = src.rfind("\n", 0, start) + 1
    indent = 0
    while line_start + indent < len(src) and src[line_start + indent] in " \t":
        indent += 1
    pos = start + len(prefix)
    while pos < len(src):
        nl = src.find("\n", pos)
        if nl < 0:
            return src[start:]
        next_line = nl + 1
        if next_line >= len(src):
            return src[start:]
        if src[next_line] == "\n":
            pos = next_line
            continue
        j = next_line
        while j < len(src) and src[j] in " \t":
            j += 1
        if j >= len(src):
            return src[start:]
        line_indent = j - next_line
        if line_indent <= indent and (
            src.startswith("def ", j)
            or src.startswith("async def ", j)
            or src.startswith("class ", j)
        ):
            return src[start:next_line]
        pos = next_line
    return src[start:]


def test_should_skip_session_load_warm() -> None:
    """Already-warm session ids skip agent session/load in this process."""
    warm: set[str] = set()
    assert should_skip_session_load(warm, "abc") is False
    assert should_skip_session_load(warm, "") is False
    warm.add("sid-1")
    assert should_skip_session_load(warm, "sid-1") is True
    assert should_skip_session_load(warm, "sid-2") is False
    assert should_skip_session_load(frozenset({"x"}), "x") is True


def test_session_load_second_call_skips_request() -> None:
    """Mock: first session_load hits request; second warm skip does not."""
    import asyncio
    from unittest.mock import AsyncMock

    from hub.acp_client import AcpClient
    from hub.config import Config

    client = AcpClient(Config(), secret="test-secret-warm-load")
    client.request = AsyncMock(return_value={"ok": True})  # type: ignore[method-assign]

    async def _run() -> None:
        r1 = await client.session_load("sess-warm-1", r"D:\proj")
        assert r1 == {"ok": True}
        assert client.request.await_count == 1
        assert "sess-warm-1" in client._warm_sessions
        r2 = await client.session_load("sess-warm-1", r"D:\proj")
        assert r2 == {"skipped": True, "sessionId": "sess-warm-1"}
        assert client.request.await_count == 1
        assert client.loaded_session_id == "sess-warm-1"

    asyncio.run(_run())


def test_session_load_concurrent_single_flight() -> None:
    """Concurrent session_load same sid: only one agent request (single-flight)."""
    import asyncio
    from unittest.mock import AsyncMock

    from hub.acp_client import AcpClient
    from hub.config import Config

    client = AcpClient(Config(), secret="test-secret-inflight-load")
    call_count = 0

    async def _slow_request(*_a, **_k):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return {"ok": True, "sessionId": "sess-concurrent-1"}

    client.request = AsyncMock(side_effect=_slow_request)  # type: ignore[method-assign]

    async def _run() -> None:
        r1, r2 = await asyncio.gather(
            client.session_load("sess-concurrent-1", r"D:\proj"),
            client.session_load("sess-concurrent-1", r"D:\proj"),
        )
        assert r1 == {"ok": True, "sessionId": "sess-concurrent-1"}
        assert r2 == {"ok": True, "sessionId": "sess-concurrent-1"}
        assert call_count == 1
        assert client.request.await_count == 1
        assert "sess-concurrent-1" in client._warm_sessions
        assert "sess-concurrent-1" not in client._load_inflight

    asyncio.run(_run())


def test_session_load_warm_skip_structural() -> None:
    """session_load skips request when warm; warm set cleared on ACP disconnect."""
    src = (ROOT / "hub" / "acp_client.py").read_text(encoding="utf-8")
    body = _slice_method(src, "session_load")
    assert "should_skip_session_load" in body
    assert "session/load skip already warm" in body
    assert '"skipped": True' in body or "'skipped': True" in body
    assert "_warm_sessions" in body
    # session_new marks warm
    new_body = _slice_method(src, "session_new")
    assert "_warm_sessions.add" in new_body
    # single-flight concurrent join
    assert "_load_inflight" in body
    # disconnect path clears warm with loaded_session_id
    assert "_warm_sessions.clear()" in src
    assert "self.loaded_session_id = None" in src
    assert "_load_inflight.clear()" in src


def test_session_prompt_hot_path_no_warmup() -> None:
    """session_prompt waits load suppress settle then release before active turn."""
    src = (ROOT / "hub" / "acp_client.py").read_text(encoding="utf-8")
    body = _slice_method(src, "session_prompt")
    assert "asyncio.sleep(" not in body
    assert "wait_until_up(" not in body
    assert "wait_load_suppress_settled" in body
    assert "release_load_suppress" in body
    settle = body.find("wait_load_suppress_settled")
    rel = body.find("release_load_suppress")
    reg = body.find("_register_active_turn")
    req = body.find('"session/prompt"')
    assert settle >= 0 and rel >= 0 and reg >= 0 and req >= 0
    # settle before release before register before prompt send
    assert settle < rel < reg < req
    assert "prompt_send" in body
    assert "pre_send_ms" in body or "preSendMs" in body



def test_execute_prompt_hot_path_structural() -> None:
    """_execute_prompt: timing log, no wait_until_up, allow_load=False."""
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    body = _slice_method(src, "_execute_prompt")
    assert "wait_until_up(" not in body
    assert "prompt path timing" in body
    assert "ensure_ms" in body
    assert "allow_load=False" in body


def test_ensure_reuse_skips_load_new_wait() -> None:
    """_ensure_hub_agent_session: logs action; no wait_until_up on ensure path."""
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    body = _slice_method(src, "_ensure_hub_agent_session")
    assert "ensure action=%s" in body
    assert "wait_until_up(" not in body
    assert "resolve_ensure_action" in body
    assert 'action == "reuse"' in body



def test_forget_warm_session() -> None:
    """forget_warm_session drops warm set and clears loaded_session_id for sid."""
    from hub.acp_client import AcpClient
    from hub.config import Config

    client = AcpClient(Config(), secret="test-secret-forget-warm")
    client._warm_sessions.add("sess-a")
    client._warm_sessions.add("sess-b")
    client.loaded_session_id = "sess-a"
    client.forget_warm_session("sess-a")
    assert "sess-a" not in client._warm_sessions
    assert "sess-b" in client._warm_sessions
    assert client.loaded_session_id is None
    client.loaded_session_id = "sess-b"
    client.forget_warm_session("sess-a")  # other sid: loaded unchanged
    assert client.loaded_session_id == "sess-b"
    client.forget_warm_session("")
    assert "sess-b" in client._warm_sessions
