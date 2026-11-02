"""Hub prompt path: hub-ended turns are reported, only the turn that was meant is ended."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import hub.server as server_mod
from tests.test_session_runtime import _hub, _write_session

SID = "019f06ee-305f-7903-b295-d706c08f0b4f"
CWD = r"D:\Projects\Alpha"


def _prompt_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[server_mod.Hub, list[dict]]:
    hub = _hub(tmp_path, monkeypatch)
    _write_session(tmp_path / "sessions", SID)
    sent: list[dict] = []

    async def broadcast(payload: dict, session_id: str | None = None) -> None:
        sent.append(payload)

    async def ensure(view: str, cwd: str, ws=None, *, notify_switch: bool = True):
        return view, False, "hub_session", "reuse"

    hub.broadcast = broadcast  # type: ignore[method-assign]
    hub._ensure_hub_agent_session = ensure  # type: ignore[method-assign]
    return hub, sent


def _turns(sent: list[dict]) -> list[tuple[str, Any]]:
    return [(p["state"], p.get("error")) for p in sent if p.get("type") == "turn" and p.get("sessionId") == SID]


def _errors(sent: list[dict]) -> list[str]:
    return [str(p.get("message") or p.get("error") or "") for p in sent if p.get("type") == "error"]


def test_watchdog_ended_turn_is_retried_not_reported_as_success(tmp_path: Path, monkeypatch) -> None:
    from hub.acp_client import TurnEndedError

    hub, sent = _prompt_hub(tmp_path, monkeypatch)
    retries: list[dict] = []

    async def session_prompt(session_id: str, text: str, **kw: Any) -> Any:
        raise TurnEndedError("no ACP session/update for 60.0s after prompt (threshold=60.0s)")

    async def retry(**kw: Any) -> bool:
        retries.append(kw)
        return False

    hub.acp.session_prompt = session_prompt  # type: ignore[method-assign]
    hub._no_output_auto_retry = retry  # type: ignore[method-assign]
    asyncio.run(hub._execute_prompt(SID, "hello", CWD))
    assert retries and retries[0]["text"] == "hello" and retries[0]["session_id"] == SID
    assert ("idle", None) not in _turns(sent), "a turn that produced nothing was pushed as done"


def test_watchdog_stall_end_shows_the_stall_message(tmp_path: Path, monkeypatch) -> None:
    from hub.acp_client import TurnEndedError

    hub, sent = _prompt_hub(tmp_path, monkeypatch)

    async def session_prompt(session_id: str, text: str, **kw: Any) -> Any:
        raise TurnEndedError("mid-turn stall (600.0s since last ACP activity, threshold=600s)")

    hub.acp.session_prompt = session_prompt  # type: ignore[method-assign]
    asyncio.run(hub._execute_prompt(SID, "hello", CWD))
    assert _turns(sent)[-1] == ("idle", server_mod.MID_TURN_STALL_USER_MSG)
    assert any(server_mod.MID_TURN_STALL_USER_MSG in e for e in _errors(sent))


def test_stuck_turn_ended_for_a_new_prompt_shows_the_stall_message(tmp_path: Path, monkeypatch) -> None:
    from hub.acp_client import TurnEndedError

    hub, sent = _prompt_hub(tmp_path, monkeypatch)

    async def session_prompt(session_id: str, text: str, **kw: Any) -> Any:
        raise TurnEndedError("stuck turn before new prompt")

    hub.acp.session_prompt = session_prompt  # type: ignore[method-assign]
    asyncio.run(hub._execute_prompt(SID, "hello", CWD))
    assert _turns(sent)[-1] == ("idle", server_mod.MID_TURN_STALL_USER_MSG)


def test_user_stop_honoured_by_the_agent_ends_idle_without_an_error(tmp_path: Path, monkeypatch) -> None:
    from hub.acp_client import TurnEndedError

    hub, sent = _prompt_hub(tmp_path, monkeypatch)

    async def session_prompt(session_id: str, text: str, **kw: Any) -> Any:
        raise TurnEndedError("user cancel")

    hub.acp.session_prompt = session_prompt  # type: ignore[method-assign]
    asyncio.run(hub._execute_prompt(SID, "hello", CWD))
    assert _turns(sent)[-1] == ("idle", None)
    assert _errors(sent) == []


def test_admin_reset_honoured_by_the_agent_ends_idle_without_a_stall_message(tmp_path: Path, monkeypatch) -> None:
    """The reset handler owns its own notice; the prompt path must not add a stall error."""
    from hub.acp_client import TurnEndedError

    hub, sent = _prompt_hub(tmp_path, monkeypatch)

    async def session_prompt(session_id: str, text: str, **kw: Any) -> Any:
        raise TurnEndedError("admin POST /api/admin/reset-turn")

    hub.acp.session_prompt = session_prompt  # type: ignore[method-assign]
    asyncio.run(hub._execute_prompt(SID, "hello", CWD))
    assert _turns(sent)[-1] == ("idle", None)
    assert _errors(sent) == []


def test_completed_turn_still_ends_idle_with_no_error(tmp_path: Path, monkeypatch) -> None:
    hub, sent = _prompt_hub(tmp_path, monkeypatch)

    async def session_prompt(session_id: str, text: str, **kw: Any) -> Any:
        return {"stopReason": "end_turn"}

    hub.acp.session_prompt = session_prompt  # type: ignore[method-assign]
    asyncio.run(hub._execute_prompt(SID, "hello", CWD))
    assert _turns(sent) == [("running", None), ("idle", None)]


def test_prompt_error_path_leaves_a_newer_turn_for_the_session_alone(tmp_path: Path, monkeypatch) -> None:
    """After this prompt failed, a newer prompt for the same session is running; it must survive."""
    hub, sent = _prompt_hub(tmp_path, monkeypatch)

    async def session_prompt(session_id: str, text: str, **kw: Any) -> Any:
        hub.acp._register_active_turn(session_id)
        raise RuntimeError("boom")

    hub.acp.session_prompt = session_prompt  # type: ignore[method-assign]
    hub.acp.end_turn = AsyncMock(side_effect=AssertionError("ended a turn this prompt did not own"))  # type: ignore[method-assign]
    asyncio.run(hub._execute_prompt(SID, "hello", CWD))
    assert SID in hub.acp.active_turns
    assert _turns(sent)[-1] == ("idle", "boom")


def _reset_request(session_id: str) -> Any:
    async def json() -> dict:
        return {"sessionId": session_id}

    return SimpleNamespace(json=json)


def test_reset_turn_reports_an_error_only_when_it_cleared_something(tmp_path: Path, monkeypatch) -> None:
    """The client's Stop fallback can hit reset-turn just as the turn ends on its own."""
    hub, sent = _prompt_hub(tmp_path, monkeypatch)
    hub.acp.notify_agent_cancel = AsyncMock(return_value=False)  # type: ignore[method-assign]

    async def run() -> None:
        resp = await hub.handle_reset_turn(_reset_request(SID))
        body = server_mod.json.loads(resp.text)
        assert body["ok"] is True and body["cleared"] is False
        assert _turns(sent) == [], "false 'Turn reset by admin' error on an idle session"

        hub.acp._register_active_turn(SID)
        sent.clear()
        resp = await hub.handle_reset_turn(_reset_request(SID))
        body = server_mod.json.loads(resp.text)
        assert body["cleared"] is True
        assert _turns(sent) == [("idle", "Turn reset by admin")]
        assert SID not in hub.acp.active_turns

    asyncio.run(run())
