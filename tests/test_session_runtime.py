"""Per-session runtime registry (ADR-030): semantics, prune rules, drop on delete, moved-field invariants."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import hub.server as server_mod
from hub.acp_client import AcpClient
from hub.compact import (
    hub_compact_gate_set_inflight,
    hub_compact_gate_suppresses_notification,
)
from hub.config import Config
from hub.session_policy import BG_ACTIVITY_TTL_S
from hub.session_runtime import (
    BackgroundActivity,
    SessionRuntimes,
    compact_gate_map,
    turn_map,
)

NOW = 1000.0


# --- registry -----------------------------------------------------------------


def test_get_or_create_is_idempotent_and_get_is_read_only() -> None:
    reg = SessionRuntimes()
    assert reg.get("a") is None
    assert reg.get(None) is None
    assert reg.get("") is None
    assert len(reg) == 0
    rt = reg.get_or_create("a")
    assert rt.session_id == "a"
    assert reg.get_or_create("a") is rt
    assert reg.get("a") is rt
    assert "a" in reg
    assert reg.items() == [("a", rt)]


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_get_or_create_refuses_empty_ids(bad: str | None) -> None:
    with pytest.raises(ValueError):
        SessionRuntimes().get_or_create(bad)  # type: ignore[arg-type]


def test_ids_are_stored_exactly_as_given() -> None:
    reg = SessionRuntimes()
    reg.get_or_create(" a ")
    assert reg.get(" a ") is not None
    assert reg.get("a") is None


def test_drop_removes_regardless_of_liveness() -> None:
    reg = SessionRuntimes()
    reg.get_or_create("a").turn = {"started_at": NOW}
    assert reg.drop("a") is not None
    assert reg.get("a") is None
    assert reg.drop("a") is None
    assert reg.drop("") is None


# --- prune rules --------------------------------------------------------------


def test_prune_drops_an_empty_entry_and_reports_it() -> None:
    reg = SessionRuntimes()
    reg.get_or_create("a")
    assert reg.prune("a") == ["a"]
    assert reg.get("a") is None
    assert reg.prune("a") == []
    assert reg.prune("missing") == []


LIVE_FIELDS = {
    "turn": lambda rt: setattr(rt, "turn", {"started_at": NOW}),
    "background": lambda rt: setattr(
        rt, "background", BackgroundActivity(last_at=NOW, started_at=NOW)
    ),
    "pending_question": lambda rt: rt.pending_question_ids.add("q1"),
    "load.loading": lambda rt: setattr(rt.load, "loading", True),
    "load.suppress_count": lambda rt: setattr(rt.load, "suppress_count", 1),
    "load.suppress_deadline": lambda rt: setattr(rt.load, "suppress_deadline", NOW + 1),
    "warm": lambda rt: setattr(rt, "warm", True),
    "ops_bg_phase": lambda rt: setattr(rt, "ops_bg_phase", "working"),
    "compact.agent_tokens": lambda rt: setattr(
        rt.compact, "agent_tokens", {"tokensBefore": 1, "tokensAfter": 1}
    ),
    "compact.gate_until": lambda rt: setattr(rt.compact, "gate_until", NOW + 0.001),
    "bg_status_debounce_until": lambda rt: setattr(
        rt, "bg_status_debounce_until", NOW + 0.001
    ),
}


@pytest.mark.parametrize("mutate", list(LIVE_FIELDS.values()), ids=list(LIVE_FIELDS))
def test_each_live_field_keeps_the_entry(mutate) -> None:
    reg = SessionRuntimes()
    mutate(reg.get_or_create("a"))
    assert reg.prune("a", now=NOW) == []
    assert reg.get("a") is not None


@pytest.mark.parametrize("deadline", [NOW, NOW - 1.0])
def test_expired_deadlines_do_not_keep_the_entry(deadline: float) -> None:
    reg = SessionRuntimes()
    rt = reg.get_or_create("a")
    rt.compact.gate_until = deadline
    rt.bg_status_debounce_until = deadline
    assert reg.prune("a", now=NOW) == ["a"]


def test_prune_all_sweeps_only_idle_entries() -> None:
    reg = SessionRuntimes()
    reg.get_or_create("idle")
    reg.get_or_create("busy").turn = {"started_at": NOW}
    reg.get_or_create("expired").compact.gate_until = NOW - 1.0
    assert sorted(reg.prune(now=NOW)) == ["expired", "idle"]
    assert [sid for sid, _ in reg.items()] == ["busy"]


def test_running_watchdog_and_held_lock_count_as_busy() -> None:
    async def run() -> None:
        reg = SessionRuntimes()
        rt = reg.get_or_create("a")
        rt.stall_watchdog = asyncio.create_task(asyncio.sleep(60))
        assert rt.is_busy()
        rt.stall_watchdog.cancel()
        with pytest.raises(asyncio.CancelledError):
            await rt.stall_watchdog
        assert not rt.is_busy()
        rt.end_turn_lock = asyncio.Lock()
        assert not rt.is_busy()
        async with rt.end_turn_lock:
            assert rt.is_busy()
            assert reg.prune("a") == []
        assert reg.prune("a") == ["a"]

    asyncio.run(run())


def test_inflight_load_and_release_timer_count_as_busy() -> None:
    async def run() -> None:
        reg = SessionRuntimes()
        rt = reg.get_or_create("a")
        loop = asyncio.get_running_loop()
        rt.load.inflight = loop.create_future()
        assert rt.is_busy()
        rt.load.inflight = None
        assert not rt.is_busy()
        rt.load.release_handle = loop.call_later(60, lambda: None)
        assert rt.is_busy()
        rt.load.release_handle.cancel()
        rt.load.release_handle = None
        assert not rt.is_busy()

    asyncio.run(run())


# --- field views --------------------------------------------------------------


def test_turn_map_reads_writes_and_prunes_through_the_registry() -> None:
    reg = SessionRuntimes()
    turns = turn_map(reg)
    assert turns == {}
    assert len(turns) == 0
    assert "a" not in turns
    turns["a"] = {"started_at": 1.0}
    assert reg.get("a").turn == {"started_at": 1.0}
    assert turns["a"] is reg.get("a").turn
    assert list(turns) == ["a"]
    assert turns.get("b") is None
    with pytest.raises(KeyError):
        turns["b"]
    assert turns.pop("a") == {"started_at": 1.0}
    assert reg.get("a") is None
    assert turns.pop("a", None) is None


def test_turn_map_clear_touches_only_the_turn_field() -> None:
    reg = SessionRuntimes()
    reg.get_or_create("warm-only").warm = True
    turns = turn_map(reg)
    assert "warm-only" not in turns
    turns["warm-only"] = {"started_at": 2.0}
    turns["other"] = {"started_at": 3.0}
    turns.clear()
    assert turns == {}
    assert reg.get("warm-only").warm is True
    assert reg.get("other") is None


def test_turn_map_setting_none_removes_the_turn() -> None:
    reg = SessionRuntimes()
    turns = turn_map(reg)
    turns["a"] = {"started_at": 1.0}
    turns["a"] = None  # type: ignore[assignment]
    assert "a" not in turns
    assert reg.get("a") is None


def test_compact_gate_view_drives_the_pure_gate_helpers() -> None:
    reg = SessionRuntimes()
    gate = compact_gate_map(reg)
    hub_compact_gate_set_inflight(gate, "a", now=NOW)
    deadline = reg.get("a").compact.gate_until
    assert deadline is not None and deadline > NOW
    assert hub_compact_gate_suppresses_notification(gate, "a", "completed", now=NOW + 1.0)
    assert not hub_compact_gate_suppresses_notification(gate, "a", "completed", now=deadline)
    assert reg.get("a") is None


# --- AcpClient invariants -----------------------------------------------------


def _client() -> AcpClient:
    return AcpClient(Config(), secret="test-secret-runtime")


def test_force_clear_leaves_no_turn_state_and_cancels_the_watchdog() -> None:
    async def run() -> None:
        client = _client()
        sid = "turn-sid"
        client._register_active_turn(sid, cwd_key="k")
        watchdog = asyncio.create_task(client._stall_watchdog_loop(sid, 3600.0))
        client.runtimes.get(sid).stall_watchdog = watchdog
        await asyncio.sleep(0)
        assert client.force_clear_turn("test", session_id=sid) is True
        assert sid not in client.active_turns
        assert client.runtimes.get(sid) is None
        await asyncio.sleep(0.05)
        # The loop sleeps 1s between checks and swallows CancelledError, so
        # wait_for cannot observe the cancel; only a real cancel ends it this fast.
        assert watchdog.done()
        assert client.force_clear_turn("again", session_id=sid) is False

    asyncio.run(run())


def test_turn_registered_without_watchdog_is_gone_after_clear_all() -> None:
    client = _client()
    client.active_turns["a"] = {"started_at": 1.0, "last_activity": 1.0, "saw_update": False}
    client.active_turns["b"] = {"started_at": 2.0, "last_activity": 2.0, "saw_update": False}
    assert client.turn_session_id == "b"
    assert client.force_clear_turn("all") is True
    assert client.active_turns == {}
    assert len(client.runtimes) == 0


def test_end_turn_leaves_no_runtime_for_the_session() -> None:
    async def run() -> None:
        client = _client()
        sid = "end-turn-sid"
        client._register_active_turn(sid)
        client.note_bg_activity(sid, kind="subagent_progress", now=100.0)
        client.notify_agent_cancel = AsyncMock(return_value=True)  # type: ignore[method-assign]
        result = await client.end_turn(sid, "user cancel", cancel_timeout=0.05)
        assert result["cleared"] is True
        assert client.runtimes.get(sid) is None
        again = await client.end_turn(sid, "again")
        assert again["cleared"] is False
        assert client.runtimes.get(sid) is None

    asyncio.run(run())


def test_bg_activity_expiry_and_clear_leave_no_entry() -> None:
    client = _client()
    sid = "bg-sid"
    client.note_bg_activity(sid, kind="subagent_progress", now=100.0)
    assert client.runtimes.get(sid).background is not None
    client.live_bg_activity_sessions(now=100.0 + float(BG_ACTIVITY_TTL_S) + 1.0)
    assert client.runtimes.get(sid) is None
    client.note_bg_activity(sid, kind="tool_call", now=200.0)
    client.clear_bg_activity()
    assert client.runtimes.get(sid) is None
    client.note_bg_activity(sid, kind="tool_call", now=300.0)
    client._pop_bg_activity(sid)
    assert client.runtimes.get(sid) is None


def test_load_release_clears_load_state_and_warm_survives_until_disconnect() -> None:
    async def run() -> None:
        client = _client()
        client.request = AsyncMock(return_value={"ok": True})  # type: ignore[method-assign]
        sid = "load-sid"
        await client.session_load(sid, r"D:\proj")
        rt = client.runtimes.get(sid)
        assert rt.warm is True
        assert rt.load.loading is True
        assert rt.load.release_handle is not None
        assert rt.load.suppress_deadline is not None
        client.release_load_suppress(sid)
        rt = client.runtimes.get(sid)
        assert rt is not None
        assert rt.warm is True
        assert not rt.load.is_active()
        assert not client.is_load_suppressing(sid)
        await client._maintain_session_cleanup(disconnect_reason="test")
        assert client.runtimes.get(sid) is None

    asyncio.run(run())


def test_pending_question_is_tracked_per_session_and_cleared_on_cancel() -> None:
    async def run() -> None:
        client = _client()
        client._reply_result = AsyncMock()  # type: ignore[method-assign]
        sid = "q-sid"
        params = {
            "sessionId": sid,
            "questions": [{"text": "Pick one", "options": [{"label": "a"}]}],
        }
        task = asyncio.create_task(client._handle_ask_user_question(7, params))
        await asyncio.sleep(0.01)
        assert client.sessions_with_pending_questions() == {sid}
        assert client.runtimes.get(sid).pending_question_ids == {"7"}
        client._cancel_pending_user_questions_for_session(sid)
        await asyncio.wait_for(task, 1.0)
        assert client.sessions_with_pending_questions() == set()
        assert client.runtimes.get(sid) is None
        client._reply_result.assert_awaited_once()

    asyncio.run(run())


# --- Hub lifecycle ------------------------------------------------------------


def _hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> server_mod.Hub:
    monkeypatch.setattr(server_mod, "REMOTE_SESSIONS_FILE", tmp_path / "remote-sessions.json")
    monkeypatch.setattr(server_mod, "APPROVAL_RULES_FILE", tmp_path / "approval-rules.json")
    monkeypatch.setattr(server_mod, "LAST_REMOTE_SESSION_FILE", tmp_path / "last-remote-session.txt")
    cfg = Config(
        sessions_root=tmp_path / "sessions",
        agent_secret_path=tmp_path / "agent.secret",
        hook_secret_path=tmp_path / "hook.secret",
        log_dir=tmp_path / "logs",
        static_dir=tmp_path / "static",
        gate_plugin_dir=tmp_path / "gate",
        grok_bin="grok-is-not-started-by-this-test",
    )
    return server_mod.Hub(cfg)


def _write_session(root: Path, sid: str) -> None:
    d = root / "D%3A%5CProjects%5CAlpha" / sid
    d.mkdir(parents=True)
    summary = {
        "info": {"id": sid, "cwd": r"D:\Projects\Alpha"},
        "generated_title": "Alpha work",
        "updated_at": "2026-07-09T12:00:00Z",
        "num_chat_messages": 3,
    }
    (d / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (d / "updates.jsonl").write_text("{}\n", encoding="utf-8")


def test_hub_and_client_share_one_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hub = _hub(tmp_path, monkeypatch)
    assert hub.runtimes is hub.acp.runtimes


def test_delete_drops_an_idle_runtime_and_keeps_a_busy_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = _hub(tmp_path, monkeypatch)
    idle = "019f493c-af12-7652-a6d8-bf645c10921c"
    busy = "019f06ee-305f-7903-b295-d706c08f0b4f"
    for sid in (idle, busy):
        _write_session(tmp_path / "sessions", sid)
    hub.runtimes.get_or_create(idle).warm = True
    hub.acp.active_turns[busy] = {"started_at": 1.0, "last_activity": 1.0, "saw_update": False}

    async def run() -> None:
        resp = await hub.handle_delete_session(SimpleNamespace(match_info={"id": idle}))
        assert resp.status == 200
        assert hub.runtimes.get(idle) is None
        resp = await hub.handle_delete_session(SimpleNamespace(match_info={"id": busy}))
        assert resp.status == 200
        assert hub.acp.is_session_active(busy)
        assert hub.runtimes.get(busy) is not None

    asyncio.run(run())


def test_bg_activity_debounce_and_ops_phase_live_on_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = _hub(tmp_path, monkeypatch)
    sid = "bg-hub-sid"

    async def run() -> None:
        hub.acp.note_bg_activity(sid, kind="subagent_progress")
        await asyncio.sleep(0.05)
        rt = hub.runtimes.get(sid)
        assert rt.ops_bg_phase == "working"
        assert rt.bg_status_debounce_until is not None
        hub.acp._pop_bg_activity(sid)
        assert hub.runtimes.get(sid) is rt
        hub._clear_ops_bg_phase(sid)
        assert hub.runtimes.get(sid) is rt
        assert hub.runtimes.prune(sid, now=time.monotonic() + 2.0) == [sid]

    asyncio.run(run())
