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


def test_notify_agent_cancel_sends_acp_notification() -> None:
    """ACP session/cancel is a notification: no id, and the agent never replies to it."""
    client = _client()
    assert inspect.iscoroutinefunction(client.notify_agent_cancel)

    async def run() -> None:
        sid = "cancel-notify-sid"
        client._register_active_turn(sid)
        sent: list[dict] = []

        async def send(payload: dict) -> None:
            sent.append(payload)

        client._send = send  # type: ignore[method-assign]
        client.request = AsyncMock(side_effect=AssertionError("cancel is not a request"))  # type: ignore[method-assign]
        assert await client.notify_agent_cancel(sid) is True
        assert sent == [
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}}
        ]
        assert sid in client.active_turns

    asyncio.run(run())


def test_notify_agent_cancel_false_when_send_fails() -> None:
    async def run() -> None:
        client = _client()
        sid = "cancel-send-fail-sid"
        client._register_active_turn(sid)
        client._send = AsyncMock(side_effect=ConnectionError("ACP not connected"))  # type: ignore[method-assign]
        assert await client.notify_agent_cancel(sid) is False
        assert sid in client.active_turns

    asyncio.run(run())


def test_end_turn_waits_for_the_agent_to_end_the_turn() -> None:
    """The agent answers the prompt with stopReason cancelled; the hub has nothing to force-clear."""

    async def run() -> None:
        client = _client()
        sid = "cancel-acked-sid"
        client._register_active_turn(sid)

        async def send(payload: dict) -> None:
            asyncio.get_running_loop().call_later(0.05, client._clear_active_turn, sid)

        client._send = send  # type: ignore[method-assign]
        result = await client.end_turn(sid, "user cancel", cancel_timeout=2.0)
        assert result["cancelled"] is True
        assert result["cleared"] is False
        assert client.last_force_clear_reason is None
        assert sid not in client.active_turns

    asyncio.run(run())


def test_end_turn_kills_the_command_the_agent_is_blocked_on() -> None:
    """Grok waits on terminal/wait_for_exit through a cancel; the hub ends that command."""

    async def run() -> None:
        client = _client()
        sid = "cancel-blocked-sid"
        client._register_active_turn(sid)
        client._send = AsyncMock(return_value=None)  # type: ignore[method-assign]
        killed: list[str] = []

        async def kill_awaited(session_id: str) -> int:
            killed.append(session_id)
            client._clear_active_turn(session_id)
            return 1

        client._terminals.kill_awaited = kill_awaited  # type: ignore[method-assign]
        result = await client.end_turn(sid, "user cancel", cancel_timeout=2.0)
        assert killed == [sid]
        assert result["cancelled"] is True
        assert result["cleared"] is False

    asyncio.run(run())


def test_end_turn_leaves_a_follow_up_turn_alone() -> None:
    """The agent ends turn A on cancel and a queued prompt starts B before end_turn resumes."""

    async def run() -> None:
        client = _client()
        sid = "replaced-turn-sid"
        client._register_active_turn(sid)
        first = client.active_turns[sid]

        def agent_ends_a_then_b_starts() -> None:
            client._clear_active_turn(sid)
            client._register_active_turn(sid)

        async def send(payload: dict) -> None:
            asyncio.get_running_loop().call_later(0.02, agent_ends_a_then_b_starts)

        client._send = send  # type: ignore[method-assign]
        result = await client.end_turn(sid, "user cancel", cancel_timeout=2.0)
        assert result["cancelled"] is True
        assert result["cleared"] is False
        second = client.active_turns.get(sid)
        assert second is not None and second is not first
        assert client.last_force_clear_reason is None

    asyncio.run(run())


def test_session_prompt_cleanup_leaves_a_replacement_turn_alone() -> None:
    """A force-clear ends this prompt's turn and a new prompt registers before the task resumes."""

    async def run() -> None:
        client = _client()
        sid = "prompt-replaced-sid"
        replacement_watchdog = asyncio.create_task(asyncio.sleep(30))

        async def request(method: str, params: dict | None = None, timeout: float = 120.0, *, session_id=None):
            client.force_clear_turn("no ACP session/update for 60.0s after prompt", session_id=sid)
            client._register_active_turn(sid)
            client.runtimes.get(sid).stall_watchdog = replacement_watchdog
            raise TimeoutError("Turn force-cleared: no ACP session/update for 60.0s after prompt")

        client.request = request  # type: ignore[method-assign]
        try:
            try:
                await client.session_prompt(sid, "hi", allow_load=False)
            except TimeoutError:
                pass
            else:
                raise AssertionError("force-cleared prompt must raise")
            rt = client.runtimes.get(sid)
            assert rt is not None and rt.turn is not None
            assert rt.stall_watchdog is replacement_watchdog
            assert not replacement_watchdog.cancelled()
        finally:
            replacement_watchdog.cancel()
            try:
                await replacement_watchdog
            except asyncio.CancelledError:
                pass

    asyncio.run(run())


def test_end_turn_force_clears_when_the_agent_ignores_cancel() -> None:
    async def run() -> None:
        client = _client()
        sid = "cancel-ignored-sid"
        client._register_active_turn(sid)
        client._send = AsyncMock(return_value=None)  # type: ignore[method-assign]
        result = await client.end_turn(sid, "user cancel", cancel_timeout=0.05)
        assert result["cancelled"] is False
        assert result["cleared"] is True
        assert sid not in client.active_turns

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

        result = await client.end_turn(sid, "test end_turn order", cancel_timeout=0.05)
        assert result["cancelled"] is False
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
    assert "rt.warm = False" in acp
    assert "async def wait_load_suppress_settled" in acp



def test_no_output_auto_retry_ends_no_turn_and_never_restarts() -> None:
    """The failed prompt ended its own turn; any turn on the session now is a newer one (ADR-031)."""
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    idx = src.find("async def _no_output_auto_retry")
    assert idx >= 0
    end = src.find("\n    async def ", idx + 1)
    block = src[idx : end if end > idx else idx + 5000]
    assert "end_turn(" not in block
    # First-byte + retry thresholds wired from session_policy
    assert "NO_OUTPUT_RETRY_SECONDS" in block
    assert "no_output_seconds_for_session" in block
    # P0.5: must NOT call restart-agent after no-output failure.
    assert "await self._restart_agent_process" not in block
    assert "_restart_agent_process(" not in block
    # Optional log that escalation is disabled is fine.
    assert "would escalate restart" in block or "disabled" in block.lower()


def test_no_output_heal_short_circuits_permanent_unresumable() -> None:
    """Permanent unresumable must not reconnect ACP or re-prompt (ADR-009)."""
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    h_idx = src.find("async def _heal_session_for_no_output_retry")
    assert h_idx >= 0
    h_end = src.find("\n    async def ", h_idx + 1)
    heal = src[h_idx : h_end if h_end > h_idx else h_idx + 5000]
    assert "_unresumable_session_ids" in heal
    assert "skip reconnect" in heal
    assert "_drop_unresumable_from_hub_ids" in heal
    # Already-unresumable check before reconnect path.
    already = heal.find("already unresumable")
    reconnect = heal.find("reconnecting ACP")
    assert already >= 0 and reconnect >= 0 and already < reconnect
    # Permanent after first load also skips reconnect.
    assert "permanent load failure" in heal
    # Never call session/new on no-output heal (ADR-009). Docstring may mention it.
    body_start = heal.find('"""', heal.find('"""') + 3)
    heal_body = heal[body_start + 3 :] if body_start >= 0 else heal
    assert "session_new" not in heal_body
    assert "session/new" not in heal_body

    r_idx = src.find("async def _no_output_auto_retry")
    assert r_idx >= 0
    r_end = src.find("\n    async def ", r_idx + 1)
    retry = src[r_idx : r_end if r_end > r_idx else r_idx + 6000]
    assert "skip re-prompt" in retry
    assert "_unresumable_session_ids" in retry
    assert "NO_OUTPUT_RETRY_FAILED_MSG" in retry
    # Permanent branch returns before session_prompt re-try.
    skip = retry.find("skip re-prompt")
    prompt = retry.find("session_prompt")
    assert skip >= 0 and prompt >= 0 and skip < prompt
