"""Cancel agent turn when hub force-clears (no orphan session/prompt).

Regression: hub-side force_clear without session/cancel left the agent holding
the old prompt so the next user message hung forever.
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

    asyncio.run(run())


def test_session_cancel_uses_notify_agent_cancel() -> None:
    src = (ROOT / "hub" / "acp_client.py").read_text(encoding="utf-8")
    idx = src.find("async def session_cancel")
    assert idx >= 0
    block = src[idx : idx + 800]
    assert "notify_agent_cancel" in block
    assert "force_clear_turn" in block


def test_stall_watchdog_schedules_notify_agent_cancel_after_force_clear() -> None:
    """After force-clear for no-output, watchdog best-effort cancels agent."""

    async def run() -> None:
        client = _client()
        sid = "watchdog-cancel-sid"
        cancel_calls: list[str] = []

        async def track_cancel(session_id: str) -> bool:
            cancel_calls.append(session_id)
            return False

        client.notify_agent_cancel = track_cancel  # type: ignore[method-assign]
        client._register_active_turn(sid)
        thr = 0.3
        task = asyncio.create_task(
            client._stall_watchdog_loop(sid, thr),
            name="test-stall-cancel",
        )
        try:
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                if sid not in client.active_turns:
                    break
                await asyncio.sleep(0.05)
            assert sid not in client.active_turns
            # Give create_task a tick to run
            for _ in range(20):
                if cancel_calls:
                    break
                await asyncio.sleep(0.05)
            assert cancel_calls == [sid], f"expected cancel for {sid}, got {cancel_calls}"
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


def test_handle_reset_turn_calls_notify_agent_cancel() -> None:
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    idx = src.find("async def handle_reset_turn")
    assert idx >= 0
    end = src.find("\n    async def ", idx + 1)
    block = src[idx : end if end > idx else idx + 2500]
    assert "notify_agent_cancel" in block
    assert "force_clear_turn" in block
    # Cancel before local force_clear
    assert block.find("notify_agent_cancel") < block.find("force_clear_turn")


def test_no_output_auto_retry_calls_notify_agent_cancel() -> None:
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    idx = src.find("async def _no_output_auto_retry")
    assert idx >= 0
    block = src[idx : idx + 900]
    assert "notify_agent_cancel" in block
    assert "force_clear_turn" in block
