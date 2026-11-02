"""Stop honesty: end_turn clears bg activity stamps (Working/Stuck lies)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from hub.acp_client import AcpClient
from hub.config import Config


def _client() -> AcpClient:
    return AcpClient(Config(), secret="test-secret-stop-bg")


def _bg(client: AcpClient, sid: str):
    rt = client.runtimes.get(sid)
    return rt.background if rt is not None else None


def test_end_turn_pops_bg_activity_for_session() -> None:
    async def run() -> None:
        client = _client()
        sid = "stop-bg-sid-1"
        client._register_active_turn(sid)
        client.note_bg_activity(sid, kind="subagent_progress", now=100.0)
        assert _bg(client, sid) is not None
        assert client.bg_activity_phase_for(sid, now=100.0) in ("working", "stuck")

        async def agent_ends_turn(session_id: str) -> bool:
            client._clear_active_turn(session_id)
            return True

        client.notify_agent_cancel = agent_ends_turn  # type: ignore[method-assign]
        result = await client.end_turn(sid, "user cancel")
        assert result["cancelled"] is True
        assert result["cleared"] is False
        assert _bg(client, sid) is None
        assert client.bg_activity_phase_for(sid, now=101.0) == "idle"
        assert sid not in client.active_turns

    asyncio.run(run())


def test_end_turn_clears_bg_even_when_cancel_fails() -> None:
    async def run() -> None:
        client = _client()
        sid = "stop-bg-sid-fail"
        client._register_active_turn(sid)
        client.note_bg_activity(sid, kind="tool_call", now=50.0)
        client.notify_agent_cancel = AsyncMock(return_value=False)  # type: ignore[method-assign]
        result = await client.end_turn(sid, "user cancel")
        assert result["cancelled"] is False
        assert result["cleared"] is True
        assert _bg(client, sid) is None

    asyncio.run(run())


def test_end_turn_all_sessions_clear_bg_activity() -> None:
    async def run() -> None:
        client = _client()
        a, b = "bg-all-a", "bg-all-b"
        client._register_active_turn(a)
        client._register_active_turn(b)
        client.note_bg_activity(a, kind="subagent_spawned", now=10.0)
        client.note_bg_activity(b, kind="subagent_progress", now=11.0)
        client.notify_agent_cancel = AsyncMock(return_value=True)  # type: ignore[method-assign]
        result = await client.end_turn(None, "admin clear all")
        assert result["cleared"] is True
        assert _bg(client, a) is None
        assert _bg(client, b) is None
        assert all(rt.background is None for _, rt in client.runtimes.items())

    asyncio.run(run())


def test_end_turn_docstring_mentions_nested_subagent_residual() -> None:
    doc = AcpClient.end_turn.__doc__ or ""
    assert "subagent" in doc.lower()
    assert "parent" in doc.lower() or "sessionId" in doc or "session id" in doc.lower()


def test_ws_cancel_broadcasts_status_after_end_turn() -> None:
    """Structural: _ws_cancel uses session_cancel/end_turn and rebroadcasts status."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "hub" / "server.py").read_text(
        encoding="utf-8"
    )
    # Prefer exact def, not _ws_cancel_safe wrapper.
    marker = "async def _ws_cancel(self, ws"
    idx = src.find(marker)
    assert idx >= 0, "_ws_cancel handler missing"
    block = src[idx : idx + 4500]
    assert "session_cancel" in block or "end_turn" in block
    assert "status_payload" in block
    assert "cancelled" in block
    assert "broadcast" in block
