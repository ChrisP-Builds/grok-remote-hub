"""Integration: real AcpClient stall watchdog force-clears zero-update turns.

Drives shipped `_stall_watchdog_loop` (not pure policy only). Short-bound
thresholds so tests finish in seconds, not multi-minute waits.
"""

from __future__ import annotations

import asyncio
import time

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

