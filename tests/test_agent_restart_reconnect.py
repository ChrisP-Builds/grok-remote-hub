"""Agent recycle → ACP reconnect reliability (port-up alone is not success)."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from hub.acp_client import AcpClient
from hub.config import Config
from hub.harden_policy import (
    agent_recycle_complete,
    agent_recycle_reconnect_plan,
    should_auto_restart_agent_on_heal_exhaust,
)

ROOT = Path(__file__).resolve().parents[1]


def test_agent_recycle_complete_requires_acp() -> None:
    assert agent_recycle_complete(process_up=False, acp_connected=False) is False
    assert agent_recycle_complete(process_up=True, acp_connected=False) is False
    assert agent_recycle_complete(process_up=False, acp_connected=True) is False
    assert agent_recycle_complete(process_up=True, acp_connected=True) is True
    # Truthiness only
    assert agent_recycle_complete(process_up=1, acp_connected="yes") is True  # type: ignore[arg-type]
    assert agent_recycle_complete(process_up=0, acp_connected=True) is False  # type: ignore[arg-type]


def test_agent_recycle_reconnect_plan_bounds() -> None:
    plan = agent_recycle_reconnect_plan()
    assert plan["reconnect_attempts"] == 3
    assert plan["reconnect_timeout_s"] == 15.0
    assert plan["settle_after_port_s"] == 0.75
    assert plan["reset_maintain_loop"] is True

    plan2 = agent_recycle_reconnect_plan(
        reconnect_attempts=0,
        reconnect_timeout_s=0.1,
        settle_after_port_s=-1.0,
    )
    assert plan2["reconnect_attempts"] == 1
    assert plan2["reconnect_timeout_s"] == 1.0
    assert plan2["settle_after_port_s"] == 0.0
    assert plan2["reset_maintain_loop"] is True


def test_reconnect_signature_accepts_reset_maintain() -> None:
    sig = inspect.signature(AcpClient.reconnect)
    assert "reset_maintain" in sig.parameters
    p = sig.parameters["reset_maintain"]
    assert p.default is False
    assert p.kind == inspect.Parameter.KEYWORD_ONLY


def test_reconnect_with_reset_maintain_restarts_task() -> None:
    """reset_maintain=True cancels a stalled maintain loop and starts a fresh one."""

    async def run() -> None:
        client = AcpClient(Config(), secret="t")
        client._stop.clear()
        started: list[int] = []
        gen = {"n": 0}

        async def fake_maintain() -> None:
            gen["n"] += 1
            my = gen["n"]
            started.append(my)
            try:
                if my == 1:
                    # Stalled backoff sleep (the bug: reconnect without reset waits forever).
                    await asyncio.sleep(100)
                else:
                    # Fresh loop after reset: mark chat-ready immediately.
                    client.connected = True
                    client._ws = object()
                    await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise

        client._maintain = fake_maintain  # type: ignore[method-assign]
        client._maintain_task = asyncio.create_task(fake_maintain(), name="acp-stall")
        await asyncio.sleep(0.05)
        assert started == [1]
        assert not client.connected

        await client.reconnect(timeout=2.0, reset_maintain=True)
        assert client.connected
        assert client._ws is not None
        assert 2 in started
        assert client._maintain_task is not None
        assert not client._maintain_task.done()

        # Cleanup
        client._stop.set()
        client._maintain_task.cancel()
        try:
            await client._maintain_task
        except asyncio.CancelledError:
            pass
        client.connected = False
        client._ws = None

    asyncio.run(run())


def test_reconnect_without_reset_does_not_cancel_running_maintain() -> None:
    """Default reconnect only revives maintain when done; does not cancel backoff."""

    async def run() -> None:
        client = AcpClient(Config(), secret="t")
        client._stop.clear()
        cancelled = {"hit": False}

        async def stalled() -> None:
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cancelled["hit"] = True
                raise

        client._maintain_task = asyncio.create_task(stalled(), name="acp-stall")
        await asyncio.sleep(0.05)
        task_before = client._maintain_task

        try:
            await client.reconnect(timeout=0.4, reset_maintain=False)
            raise AssertionError("expected TimeoutError")
        except TimeoutError:
            pass

        assert client._maintain_task is task_before
        assert not task_before.done()
        assert cancelled["hit"] is False

        task_before.cancel()
        try:
            await task_before
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


def test_restart_agent_process_source_uses_reset_and_recycle_complete() -> None:
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    assert "reset_maintain=True" in src
    assert "agent_recycle_complete" in src
    assert "agent_recycle_reconnect_plan" in src
    assert "force_clear_turn" in src
    # Multi-attempt reconnect loop
    assert "reconnect attempt" in src
    # Heal budget left usable after recycle failure
    helper = src[src.find("async def _restart_agent_process") :]
    helper = helper[: helper.find("async def handle_restart_agent")]
    assert "_acp_heal_attempts = 0" in helper
    assert "agent_restart_reconnect_ok" in helper or "agent_restart_reconnect_fail" in helper


def test_heal_budget_reset_after_restart_failure_path() -> None:
    """Failure path must leave heal attempts at 0 so auto-heal budget remains."""
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    start = src.find("async def _restart_agent_process")
    end = src.find("async def handle_restart_agent", start)
    block = src[start:end]
    # Partial failure sets error + disconnected_at and zeros attempts
    assert "partial failure" in block
    assert "self._acp_heal_attempts = 0" in block
    assert "self._acp_disconnected_at = time.monotonic()" in block or (
        "_acp_disconnected_at is None" in block
    )


def test_mid_turn_auto_skip_still_in_should_auto_restart() -> None:
    base = dict(
        enabled=True,
        agent_process_up=True,
        acp_connected=False,
        heal_attempts=3,
        max_attempts=3,
        turn_active=True,
        auto_restarts_used=0,
        max_auto_restarts=1,
    )
    assert should_auto_restart_agent_on_heal_exhaust(**base) is False
    base["turn_active"] = False
    assert should_auto_restart_agent_on_heal_exhaust(**base) is True


def test_force_restart_logs_acp_not_verified() -> None:
    src = (ROOT / "hub" / "agent_supervisor.py").read_text(encoding="utf-8")
    assert "ACP not yet verified" in src


def test_acp_client_reconnect_source_reset_branch() -> None:
    src = (ROOT / "hub" / "acp_client.py").read_text(encoding="utf-8")
    assert "reset_maintain" in src
    assert "old.cancel()" in src or ".cancel()" in src
    assert "starting fresh loop" in src or "fresh loop" in src
