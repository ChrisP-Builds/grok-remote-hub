"""Integration: real AcpClient stall watchdog force-clears zero-update turns.

Drives shipped `_stall_watchdog_loop` (not pure policy only). Short-bound
thresholds so tests finish in seconds, not multi-minute waits.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from hub.acp_client import AcpClient
from hub.config import Config
from hub.session_policy import is_no_output_error_message, should_auto_retry_no_output


def _client() -> AcpClient:
    return AcpClient(Config(), secret="test-secret-watchdog")


async def _wait_until_idle(client: AcpClient, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not client.active_turns:
            return True
        await asyncio.sleep(0.05)
    return not client.active_turns


def test_stall_watchdog_loop_force_clears_zero_update_turn() -> None:
    """Register active turn with no ACP updates; real watchdog clears it.

    Product gate: wiring regression (watchdog never started / never clears)
    would leave active_turns non-empty past no_output threshold.
    """

    async def run() -> None:
        client = _client()
        sid = "watchdog-zero-update-sid"
        client._register_active_turn(sid, cwd_key="test-cwd")
        assert client.turn_running is True
        assert sid in client.active_turns
        assert client.active_turns[sid].get("saw_update") is False

        # Tiny threshold; loop sleeps 1s then evaluates age.
        thr = 0.3
        task = asyncio.create_task(
            client._stall_watchdog_loop(sid, thr),
            name="test-stall-watchdog",
        )
        try:
            cleared = await _wait_until_idle(client, timeout=4.0)
            assert cleared is True, (
                f"active_turns still {list(client.active_turns)!r} "
                f"reason={client.last_force_clear_reason!r}"
            )
            assert client.turn_running is False
            assert client.turn_session_ids == []
            assert client.last_force_clear_session == sid
            assert client.last_force_clear_reason
            assert "no ACP session/update" in client.last_force_clear_reason
            # Same signal path as hub no-output recovery eligibility
            assert is_no_output_error_message(client.last_force_clear_reason)
            assert should_auto_retry_no_output(
                f"Turn force-cleared: {client.last_force_clear_reason}",
                already_retried=False,
            )
            assert not should_auto_retry_no_output(
                f"Turn force-cleared: {client.last_force_clear_reason}",
                already_retried=True,
            )
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(run())


def test_stall_watchdog_does_not_clear_under_no_output_threshold() -> None:
    """With age still under no_output_seconds, real loop keeps the turn."""

    async def run() -> None:
        client = _client()
        sid = "watchdog-under-threshold-sid"
        client._register_active_turn(sid)
        # High threshold so first 1s poll never clears
        thr = 30.0
        task = asyncio.create_task(
            client._stall_watchdog_loop(sid, thr),
            name="test-stall-under",
        )
        try:
            await asyncio.sleep(1.3)
            assert sid in client.active_turns
            assert client.turn_running is True
            assert client.last_force_clear_reason is None
        finally:
            client.force_clear_turn("test cleanup", session_id=sid)
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(run())


def test_stall_watchdog_status_shape_after_clear_matches_health_idle() -> None:
    """After watchdog clear, turn fields match idle health/liveTurns shape."""

    async def run() -> None:
        client = _client()
        sid = "watchdog-status-shape"
        client._register_active_turn(sid)
        task = asyncio.create_task(client._stall_watchdog_loop(sid, 0.3))
        try:
            assert await _wait_until_idle(client, timeout=4.0)
            # Mirrors hub status_payload liveTurns / turnRunning when idle
            live_turns = [
                {"sessionId": s, "state": "running"} for s in client.turn_session_ids
            ]
            assert client.turn_running is False
            assert live_turns == []
            assert client.turn_session_id is None
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(run())


def test_note_activity_agent_ttfb_excludes_user_chunk() -> None:
    """AcpClient.note_activity freezes first_update_at only for agent kinds."""
    client = _client()
    sid = "ttfb-user-echo"
    client._register_active_turn(sid)
    meta = client.active_turns[sid]
    assert meta["first_update_at"] is None
    assert meta["saw_update"] is False

    client.note_activity(sid, update_kind="user_message_chunk")
    assert meta["saw_update"] is True
    assert meta["first_update_at"] is None

    client.note_activity(sid, update_kind="available_commands_update")
    assert meta["first_update_at"] is None

    client.note_activity(sid, update_kind="agent_thought_chunk")
    first = meta["first_update_at"]
    assert first is not None

    time.sleep(0.02)
    client.note_activity(sid, update_kind="agent_message_chunk")
    assert meta["first_update_at"] == first


def test_note_activity_none_kind_counts_as_agent() -> None:
    """Tool RPC path (no kind) freezes agent TTFB."""
    client = _client()
    sid = "ttfb-tool-rpc"
    client._register_active_turn(sid)
    client.note_activity(sid)
    assert client.active_turns[sid]["first_update_at"] is not None
    assert client.active_turns[sid]["saw_update"] is True


def test_track_update_subagent_progress_notes_activity() -> None:
    """_x.ai/session_notification subagent_progress must count as turn activity.

    Incident: agent kept emitting subagent_progress while hub force-cleared
    no-output because only auto_compact_* was treated as activity.
    """

    async def run() -> None:
        from hub.session_policy import should_force_clear_turn

        client = _client()
        sid = "019f91eb-subagent-progress"
        client._register_active_turn(sid)
        meta = client.active_turns[sid]
        started = float(meta["started_at"])
        assert meta["saw_update"] is False

        msg = {
            "method": "_x.ai/session_notification",
            "params": {
                "sessionId": sid,
                "kind": "subagent_progress",
            },
        }
        await client._track_update(msg)

        assert meta["saw_update"] is True
        last = float(meta["last_activity"])
        assert last >= started
        # Bg stamp recorded even while still mid-turn
        assert sid in client.live_bg_activity_sessions()

        # After activity, no-output path must not fire even past threshold age
        age = 120.0
        silence = 1.0  # activity just noted
        reason = should_force_clear_turn(
            True,  # saw_update
            age,
            silence,
            no_output_seconds=90.0,
            mid_turn_stall_seconds=600.0,
            max_turn_seconds=1800.0,
        )
        assert reason is None

        # Without activity, same age would force-clear
        reason_dead = should_force_clear_turn(
            False,
            age,
            age,
            no_output_seconds=90.0,
            mid_turn_stall_seconds=600.0,
            max_turn_seconds=1800.0,
        )
        assert reason_dead is not None
        assert "no ACP session/update" in reason_dead

        client.force_clear_turn("test cleanup", session_id=sid)

    asyncio.run(run())


def test_track_update_subagent_progress_bg_after_force_clear() -> None:
    """After force-clear empties active_turns, subagent_progress still stamps bg."""

    async def run() -> None:
        from hub.multi_turn import STATUS_WORKING, merge_session_flags

        client = _client()
        sid = "orphan-subagent-sid"
        client._register_active_turn(sid)
        client.force_clear_turn("no ACP session/update", session_id=sid)
        assert sid not in client.active_turns

        await client._track_update(
            {
                "method": "_x.ai/session_notification",
                "params": {
                    "sessionId": sid,
                    "kind": "subagent_progress",
                },
            }
        )
        assert sid in client.live_bg_activity_sessions()
        flags = merge_session_flags(
            [sid],
            active_sessions=set(client.turn_session_ids),
            pending_question_sessions=set(),
            background_active=client.live_bg_activity_sessions(),
        )
        assert flags[sid] == STATUS_WORKING

    asyncio.run(run())


def test_bg_activity_ages_continuous_within_ttl() -> None:
    """Two pulses within TTL: age from first stamp; silence from last; started_at stable.

    Catches the bug where ageSeconds == silenceSeconds and each progress
    pulse made the UI age restart at 0 (subagent looked like 9s restarts).
    """
    client = _client()
    sid = "bg-age-continuous-sid"
    became = client.note_bg_activity(sid, kind="subagent_progress", now=100.0)
    assert became is True
    assert client._bg_activity_started_at[sid] == 100.0
    assert client.bg_activity_kind(sid) == "subagent_progress"

    became2 = client.note_bg_activity(sid, kind="subagent_progress", now=108.0)
    assert became2 is False  # still within TTL episode
    assert client._bg_activity_started_at[sid] == 100.0  # episode start unchanged

    age, silence = client.bg_activity_ages(sid, now=110.0)
    assert age == pytest.approx(10.0)  # from first stamp
    assert silence == pytest.approx(2.0)  # from last stamp
    assert age != silence


def test_bg_activity_ages_new_episode_after_ttl() -> None:
    """After TTL expiry, next note starts a new episode (started_at resets)."""
    from hub.session_policy import BG_ACTIVITY_TTL_S

    client = _client()
    sid = "bg-age-ttl-sid"
    client.note_bg_activity(sid, kind="subagent_progress", now=100.0)
    assert sid in client.live_bg_activity_sessions(now=100.0)

    # Expire past TTL — live set drops and episode stamps are cleared.
    expired_now = 100.0 + float(BG_ACTIVITY_TTL_S) + 1.0
    live = client.live_bg_activity_sessions(now=expired_now)
    assert sid not in live
    assert sid not in client._bg_activity_started_at
    assert sid not in client._bg_activity_at
    assert client.bg_activity_ages(sid, now=expired_now) == (None, None)

    # New episode
    client.note_bg_activity(sid, kind="subagent_progress", now=160.0)
    assert client._bg_activity_started_at[sid] == 160.0
    age, silence = client.bg_activity_ages(sid, now=165.0)
    assert age == pytest.approx(5.0)
    assert silence == pytest.approx(5.0)


def test_bg_activity_ages_none_when_not_live() -> None:
    """Unknown / never-stamped session returns (None, None)."""
    client = _client()
    assert client.bg_activity_ages(None) == (None, None)
    assert client.bg_activity_ages("") == (None, None)
    assert client.bg_activity_ages("never-stamped") == (None, None)
    assert client.bg_activity_kind("never-stamped") is None


def test_bg_activity_phase_stuck_and_auto_clear() -> None:
    """Heartbeat-only: working @30s, stuck @150s, auto-clear past max @700s.

    Pulses must stay within BG_ACTIVITY_TTL_S so the episode does not reset.
    """
    from hub.session_policy import BG_ACTIVITY_MAX_S

    client = _client()
    sid = "bg-stuck-sid"
    # Episode starts at t=0 with heartbeat only; keep last_at fresh every ~8s.
    t = 0.0
    client.note_bg_activity(sid, kind="subagent_progress", now=t)
    while t < 30.0:
        t += 8.0
        client.note_bg_activity(sid, kind="subagent_progress", now=t)
    assert client.bg_activity_phase_for(sid, now=t) == "working"
    assert sid in client.live_bg_working_sessions(now=t)
    assert sid not in client.live_bg_stuck_sessions(now=t)
    assert client._bg_activity_started_at[sid] == 0.0

    # Continue heartbeats past grace (90s) → stuck at ~150s
    while t < 150.0:
        t += 8.0
        client.note_bg_activity(sid, kind="subagent_progress", now=t)
    assert client.bg_activity_phase_for(sid, now=t) == "stuck"
    status = client.bg_activity_status_map(now=t)
    assert status.get(sid) == "stuck"
    assert sid in client.live_bg_stuck_sessions(now=t)
    assert sid not in client.live_bg_working_sessions(now=t)

    # Rich activity refreshes → working even at age ~200
    while t < 200.0:
        t += 8.0
        client.note_bg_activity(sid, kind="subagent_progress", now=t)
    client.note_bg_activity(sid, kind="goal_updated", now=t)
    assert client.bg_activity_phase_for(sid, now=t) == "working"
    assert client._bg_activity_rich_at[sid] == t
    rich_t = t

    # Heartbeats only again after rich ages out of stuck window (120s)
    while t < rich_t + 130.0:
        t += 8.0
        client.note_bg_activity(sid, kind="subagent_progress", now=t)
    assert client.bg_activity_phase_for(sid, now=t) == "stuck"

    # Past max: prune drops stamps → idle (keep pulses so TTL still live)
    while t < float(BG_ACTIVITY_MAX_S) + 10.0:
        t += 8.0
        client.note_bg_activity(sid, kind="subagent_progress", now=t)
    cleared = client.prune_expired_bg_activity(now=t)
    assert sid in cleared
    assert sid not in client.live_bg_activity_sessions(now=t)
    assert client.bg_activity_phase_for(sid, now=t) == "idle"


def test_clear_bg_activity_wipes_all_maps() -> None:
    client = _client()
    sid = "bg-clear-sid"
    client.note_bg_activity(sid, kind="subagent_spawned", now=10.0)
    assert sid in client.live_bg_activity_sessions(now=10.0)
    assert sid in client._bg_activity_rich_at
    client.clear_bg_activity()
    assert client._bg_activity_at == {}
    assert client._bg_activity_started_at == {}
    assert client._bg_activity_kind == {}
    assert client._bg_activity_rich_at == {}


def test_track_update_auto_compact_still_notes_activity() -> None:
    """Regression: auto_compact_* on session_notification still notes activity."""

    async def run() -> None:
        client = _client()
        sid = "compact-activity-sid"
        client._register_active_turn(sid)
        meta = client.active_turns[sid]
        await client._track_update(
            {
                "method": "x.ai/session_notification",
                "params": {
                    "sessionId": sid,
                    "update": {"sessionUpdate": "auto_compact_started"},
                },
            }
        )
        assert meta["saw_update"] is True
        client.force_clear_turn("test cleanup", session_id=sid)

    asyncio.run(run())


def test_track_update_unknown_notification_kind_does_not_note() -> None:
    """Empty/unknown notification kinds must not fake activity (allowlist)."""

    async def run() -> None:
        client = _client()
        sid = "noise-notification-sid"
        client._register_active_turn(sid)
        meta = client.active_turns[sid]
        await client._track_update(
            {
                "method": "_x.ai/session_notification",
                "params": {
                    "sessionId": sid,
                    "kind": "random_noise_event",
                },
            }
        )
        assert meta["saw_update"] is False
        client.force_clear_turn("test cleanup", session_id=sid)

    asyncio.run(run())

