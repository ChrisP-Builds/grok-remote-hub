"""Cancel agent turn when hub force-clears (no orphan session/prompt).

Regression: hub-side force_clear without session/cancel left the agent holding
the old prompt so the next user message hung forever.

P0: cancel must be awaited *before* local clear (end_turn protocol).
"""

from __future__ import annotations

import asyncio
import inspect
import time
from pathlib import Path
from unittest.mock import AsyncMock

from hub.acp_client import AcpClient
from hub.config import Config

ROOT = Path(__file__).resolve().parents[1]


def _client() -> AcpClient:
    return AcpClient(Config(), secret="test-secret-cancel-on-clear")


def test_notify_agent_cancel_exists_and_does_not_force_clear() -> None:
    """notify_agent_cancel is public async API and leaves local turn alone."""
    client = _client()
    assert hasattr(client, "notify_agent_cancel")
    assert inspect.iscoroutinefunction(client.notify_agent_cancel)

    async def run() -> None:
        sid = "cancel-only-sid"
        client._register_active_turn(sid)
        # request always fails → returns False, no force_clear
        client.request = AsyncMock(side_effect=RuntimeError("no agent"))  # type: ignore[method-assign]
        ok = await client.notify_agent_cancel(sid)
        assert ok is False
        assert sid in client.active_turns
        assert client.turn_running is True
        # Methods tried (same set as former session_cancel)
        methods = [c.args[0] for c in client.request.await_args_list]
        assert "session/cancel" in methods
        assert "session/prompt/cancel" in methods

    asyncio.run(run())


def test_notify_agent_cancel_returns_true_on_first_success() -> None:
    async def run() -> None:
        client = _client()
        sid = "cancel-ok-sid"
        client.request = AsyncMock(return_value={})  # type: ignore[method-assign]
        ok = await client.notify_agent_cancel(sid)
        assert ok is True
        assert client.request.await_count == 1
        assert client.request.await_args.args[0] == "session/cancel"
        assert client._cancel_method == "session/cancel"

    asyncio.run(run())


def test_notify_agent_cancel_caches_method() -> None:
    """Successful cancel method is preferred on the next call."""

    async def run() -> None:
        client = _client()
        sid = "cancel-cache-sid"
        # First success on second method in the default list.
        calls: list[str] = []

        async def request(method: str, *args: object, **kwargs: object) -> dict:
            calls.append(method)
            if method == "session/prompt/cancel":
                return {}
            raise RuntimeError(f"nope {method}")

        client.request = request  # type: ignore[method-assign]
        ok = await client.notify_agent_cancel(sid)
        assert ok is True
        assert client._cancel_method == "session/prompt/cancel"
        assert "session/cancel" in calls
        assert calls[-1] == "session/prompt/cancel"

        # Second call should try cached method first and succeed once.
        calls.clear()
        ok2 = await client.notify_agent_cancel(sid)
        assert ok2 is True
        assert calls[0] == "session/prompt/cancel"
        assert len(calls) == 1

    asyncio.run(run())


def test_end_turn_cancel_before_clear_order() -> None:
    """end_turn awaits notify while turn still active, then force_clears."""

    async def run() -> None:
        client = _client()
        sid = "end-turn-order-sid"
        order: list[str] = []
        client._register_active_turn(sid)

        async def track_cancel(session_id: str) -> bool:
            order.append(f"cancel:{session_id}")
            # Turn must still be active when cancel runs.
            assert session_id in client.active_turns
            return True

        def track_clear(reason: str, session_id: str | None = None) -> bool:
            order.append(f"clear:{session_id}:{reason}")
            return AcpClient.force_clear_turn(client, reason, session_id=session_id)

        client.notify_agent_cancel = track_cancel  # type: ignore[method-assign]
        client.force_clear_turn = track_clear  # type: ignore[method-assign]

        result = await client.end_turn(sid, "test end_turn order")
        assert result["cancelled"] is True
        assert result["cleared"] is True
        assert result["reason"] == "test end_turn order"
        assert order == [
            f"cancel:{sid}",
            f"clear:{sid}:test end_turn order",
        ]
        assert sid not in client.active_turns

    asyncio.run(run())


def test_session_cancel_uses_end_turn_or_notify_before_clear() -> None:
    src = (ROOT / "hub" / "acp_client.py").read_text(encoding="utf-8")
    idx = src.find("async def session_cancel")
    assert idx >= 0
    block = src[idx : idx + 800]
    # session_cancel goes through end_turn (cancel-then-clear).
    assert "end_turn" in block


def test_stall_watchdog_awaits_cancel_before_force_clear() -> None:
    """Watchdog ends turn with cancel while active, then clears (not after)."""

    async def run() -> None:
        client = _client()
        sid = "watchdog-cancel-sid"
        order: list[str] = []

        async def track_cancel(session_id: str) -> bool:
            still_active = session_id in client.active_turns
            order.append(f"cancel:active={still_active}")
            return False

        real_clear = client.force_clear_turn

        def track_clear(reason: str, session_id: str | None = None) -> bool:
            order.append(f"clear:{reason[:40]}")
            return real_clear(reason, session_id=session_id)

        client.notify_agent_cancel = track_cancel  # type: ignore[method-assign]
        client.force_clear_turn = track_clear  # type: ignore[method-assign]
        client._register_active_turn(sid)
        thr = 0.3
        task = asyncio.create_task(
            client._stall_watchdog_loop(sid, thr),
            name="test-stall-cancel",
        )
        try:
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                if sid not in client.active_turns and order:
                    break
                await asyncio.sleep(0.05)
            assert sid not in client.active_turns
            assert any(x.startswith("cancel:") for x in order), order
            assert any(x.startswith("clear:") for x in order), order
            # Cancel first, clear second.
            cancel_i = next(i for i, x in enumerate(order) if x.startswith("cancel:"))
            clear_i = next(i for i, x in enumerate(order) if x.startswith("clear:"))
            assert cancel_i < clear_i, order
            assert order[cancel_i] == "cancel:active=True"
            assert client.last_force_clear_reason
            assert "no ACP session/update" in client.last_force_clear_reason
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(run())


def test_handle_reset_turn_uses_end_turn() -> None:
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    idx = src.find("async def handle_reset_turn")
    assert idx >= 0
    end = src.find("\n    async def ", idx + 1)
    block = src[idx : end if end > idx else idx + 2500]
    assert "end_turn" in block
    # Must not fire-and-forget unlock-only without end_turn protocol.
    assert "end_turn" in block


def test_no_output_heal_forces_reload_no_warm_skip() -> None:
    """No-output heal must forget warm and session/load; never skip as success."""
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    idx = src.find("async def _heal_session_for_no_output_retry")
    assert idx >= 0
    end = src.find("\n    async def ", idx + 1)
    block = src[idx : end if end > idx else idx + 4000]
    assert "forget_warm_session" in block
    assert "_try_session_load" in block
    # Must not treat already-loaded as success without loading.
    assert "skip load (already loaded)" not in block
    # forget_warm before first try load
    fw = block.find("forget_warm_session")
    load = block.find("_try_session_load")
    assert fw >= 0 and load >= 0 and fw < load
    # Quiet suppress must settle after load; no finally force-release.
    assert "wait_load_suppress_settled" in block
    assert "release_load_suppress" not in block
    acp = (ROOT / "hub" / "acp_client.py").read_text(encoding="utf-8")
    assert "def forget_warm_session" in acp
    assert "_warm_sessions.discard" in acp
    assert "async def wait_load_suppress_settled" in acp



def test_no_output_auto_retry_uses_end_turn_no_auto_restart() -> None:

    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    idx = src.find("async def _no_output_auto_retry")
    assert idx >= 0
    end = src.find("\n    async def ", idx + 1)
    block = src[idx : end if end > idx else idx + 5000]
    assert "end_turn" in block
    # First-byte + retry thresholds wired from session_policy
    assert "NO_OUTPUT_RETRY_SECONDS" in block
    assert "no_output_seconds_for_session" in block
    # P0.5: must NOT call restart-agent after no-output failure.
    assert "await self._restart_agent_process" not in block
    assert "_restart_agent_process(" not in block
    # Optional log that escalation is disabled is fine.
    assert "would escalate restart" in block or "disabled" in block.lower()
