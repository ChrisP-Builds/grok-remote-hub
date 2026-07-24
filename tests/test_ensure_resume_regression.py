"""Regression: ensure resume policy (unresumable + byCwd reuse, recovery keep).

Pure policy tests — no ACP / hub process required.
"""

from __future__ import annotations

from hub.session_policy import (
    cwd_key,
    next_ensure_after_load_fail,
    recovery_keeps_session_id,
    resolve_ensure_action,
)


def test_unresumable_view_plus_bycwd_process_live_reuses_bycwd() -> None:
    """Unresumable view must fall through to process-live byCwd (reuse_cwd)."""
    dead_view = "019f73a3-dead-on-disk"
    live_map = "019f9210-process-live"
    path = r"D:\Projects\Grok Remote Hub"
    remote = {cwd_key(path): live_map}
    hub_owned = {dead_view, live_map}

    target, action, reason = resolve_ensure_action(
        dead_view,
        path,
        {live_map},
        remote,
        hub_owned_ids=hub_owned,
        unresumable_ids={dead_view},
    )
    assert target == live_map
    assert action == "reuse"
    assert reason == "reuse_cwd"


def test_next_ensure_after_load_fail_reuses_process_live_bycwd() -> None:
    """After permanent load fail on target, prefer process-live byCwd over new."""
    failed = "019f73a3-failed-load"
    live = "019f9210-already-minted"
    path = r"D:\Projects\Demo"
    target, action, reason = next_ensure_after_load_fail(
        failed,
        path,
        {live},
        {cwd_key(path): live},
        unresumable_ids={failed},
        hub_owned_ids={failed, live},
    )
    assert target == live
    assert action == "reuse"
    assert reason == "reuse_cwd"


def test_recovery_keeps_session_id_false_when_switched_to_new() -> None:
    """switched_to_new=True is never a keep, even if ids match."""
    assert recovery_keeps_session_id("same-id", "same-id", True) is False
    assert recovery_keeps_session_id("a", "b", True) is False
    assert recovery_keeps_session_id("same-id", "same-id", False) is True
