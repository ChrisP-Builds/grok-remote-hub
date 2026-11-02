"""Hub side of ADR-029: broadcast stamps and logs scoped events; subscribe replays or falls back."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

import hub.server as server_mod
from hub.config import Config
from hub.ops_metrics import DEFAULT_COUNTERS
from hub.push import PushNotifier, SubscriptionStore

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")


def _method(name: str) -> str:
    """Method source without comment lines, so checks see code only."""
    match = re.search(rf"^    (async )?def {name}\(", SERVER, re.M)
    assert match, f"missing method {name}"
    nxt = re.search(r"^    (async )?def ", SERVER[match.end() :], re.M)
    body = SERVER[match.start() : match.end() + (nxt.start() if nxt else len(SERVER))]
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))


def _acp(session_id: str, text: str) -> dict:
    return {
        "type": "acp",
        "sessionId": session_id,
        "message": {"method": "session/update", "params": {"update": {"text": text}}},
    }


def test_ops_counters_declared() -> None:
    for name in ("ws_resume_replay", "ws_resume_fallback", "ws_resume_fallback_boot", "ws_resume_fallback_gap", "ws_resume_fallback_none"):
        assert name in DEFAULT_COUNTERS


def test_broadcast_logs_scoped_payloads_before_sending_the_same_text() -> None:
    body = _method("broadcast")
    append = body.find("self.event_log.append(session_id, payload)")
    loop = body.find("for ws in list(self.clients)")
    assert 0 < append < loop
    assert "SEQUENCED_TYPES" in body
    assert body.count("json.dumps(payload") == 1


def test_subscribe_decides_and_subscribes_without_an_await_between() -> None:
    assert re.search(r'if typ == "subscribe":\s+await self\._ws_subscribe\(ws, payload\)', SERVER)
    body = _method("_ws_subscribe")
    decide = body.find("decide_resume(")
    add = body.find(".add(sid)")
    assert 0 < decide < add
    assert "await" not in body[decide:add]
    assert body.count("replay_frame(") == 1
    first_await = body.find("await ")
    assert body.find("replay_frame(") < body.find("await ", body.find("replay_frame("))
    assert first_await > add
    assert body.find("ensure_watching(sid, path)") > body.find("replay_frame(")
    assert 'self.ops.inc("ws_resume_replay")' in body
    assert 'self.ops.inc("ws_resume_fallback")' in body
    assert 'self.ops.inc("ws_resume_fallback_" + decision.reason)' in body


def test_fallback_history_carries_the_head_captured_before_the_disk_read() -> None:
    body = _method("_history_snapshot_text")
    head = body.find("self.event_log.head(sid)")
    disk = body.find("load_session_history(")
    assert 0 < head < disk
    assert '"seq": head' in body
    assert "await" not in body


def test_queued_prompt_echo_is_scoped() -> None:
    queued = SERVER.find('"type": "queued"')
    echo = SERVER.find('"sessionUpdate": "user_message_chunk"', queued)
    close = SERVER.find("await self.broadcast(self.status_payload())", echo)
    assert 0 < queued < echo < close
    assert "session_id=view_session_id" in SERVER[echo:close]
    assert "Unscoped echo" not in SERVER


def _hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    hub = server_mod.Hub(cfg)
    hub.push_store = SubscriptionStore(tmp_path / "push.json")
    hub.notifier = PushNotifier(hub.push_store, tmp_path / "vapid.json", sender=None)
    app = hub.build_app()
    app.on_startup.clear()
    app.on_cleanup.clear()
    return hub, app


async def _recv(ws) -> dict:
    msg = await asyncio.wait_for(ws.receive(), 5)
    assert msg.type == WSMsgType.TEXT, msg
    return json.loads(msg.data)


async def _until(ws, typ: str) -> tuple[dict, list[dict]]:
    """Read until a message of this type; return it with everything read before it."""
    before: list[dict] = []
    while True:
        msg = await _recv(ws)
        if msg["type"] == typ:
            return msg, before
        before.append(msg)


def test_reconnect_with_since_seq_gets_exactly_the_missed_events_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hub, app = _hub(tmp_path, monkeypatch)

    async def run() -> None:
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            ws_a = await client.ws_connect("/ws")
            await ws_a.send_json({"type": "subscribe", "sessionId": "s1"})
            hist, before = await _until(ws_a, "history")
            assert "replay" not in [m["type"] for m in before]
            assert hist["sessionId"] == "s1" and hist["seq"] == 0 and hist["messages"] == []

            await hub.broadcast(_acp("s1", "e1"), session_id="s1")
            await hub.broadcast(_acp("s1", "e2"), session_id="s1")
            live = [(await _until(ws_a, "acp"))[0] for _ in range(2)]
            assert [m["seq"] for m in live] == [1, 2]
            assert [m["message"]["params"]["update"]["text"] for m in live] == ["e1", "e2"]
            await ws_a.close()
            await asyncio.sleep(0.05)

            e3 = _acp("s1", "e3")
            e4 = _acp("s1", "e4")
            await hub.broadcast(e3, session_id="s1")
            await hub.broadcast(_acp("other", "x"), session_id="other")
            await hub.broadcast(e4, session_id="s1")
            assert len(hub.clients) == 0

            ws_b = await client.ws_connect("/ws")
            await ws_b.send_json({"type": "subscribe", "sessionId": "s1", "sinceSeq": 2, "bootId": hub.boot_id})
            replay, before = await _until(ws_b, "replay")
            assert "history" not in [m["type"] for m in before]
            assert replay["sessionId"] == "s1"
            assert (replay["fromSeq"], replay["toSeq"], replay["count"]) == (2, 5, 2)
            assert replay["events"] == [e3, e4]
            assert [e["seq"] for e in replay["events"]] == [3, 5]

            await hub.broadcast(_acp("s1", "e5"), session_id="s1")
            nxt, before = await _until(ws_b, "acp")
            assert before == []
            assert nxt["seq"] == 6 and nxt["message"]["params"]["update"]["text"] == "e5"

            counters = hub.ops.snapshot_counters()
            assert counters["ws_resume_replay"] == 1
            assert counters["ws_resume_fallback"] == 1
            assert counters["ws_resume_fallback_none"] == 1
            await ws_b.close()
        finally:
            await client.close()

    asyncio.run(run())


def test_boot_mismatch_and_gap_fall_back_to_history_with_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hub, app = _hub(tmp_path, monkeypatch)

    async def run() -> None:
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await hub.broadcast(_acp("s1", "e1"), session_id="s1")
            await hub.broadcast(_acp("s1", "e2"), session_id="s1")
            await hub.broadcast(_acp("other", "x"), session_id="other")

            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "subscribe", "sessionId": "s1", "sinceSeq": 1, "bootId": "stale-boot"})
            hist, before = await _until(ws, "history")
            assert "replay" not in [m["type"] for m in before]
            assert hist["seq"] == 2 and hist["messages"] == []

            await ws.send_json({"type": "subscribe", "sessionId": "s1", "sinceSeq": 999, "bootId": hub.boot_id})
            hist, before = await _until(ws, "history")
            assert "replay" not in [m["type"] for m in before]
            assert hist["seq"] == 2

            await hub.broadcast(_acp("s1", "e3"), session_id="s1")
            nxt, _ = await _until(ws, "acp")
            assert nxt["seq"] == 4

            counters = hub.ops.snapshot_counters()
            assert counters["ws_resume_replay"] == 0
            assert counters["ws_resume_fallback"] == 2
            assert counters["ws_resume_fallback_boot"] == 1
            assert counters["ws_resume_fallback_gap"] == 1
            await ws.close()
        finally:
            await client.close()

    asyncio.run(run())


def test_unsubscribed_sessions_are_still_logged_for_later_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hub, app = _hub(tmp_path, monkeypatch)

    async def run() -> None:
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "subscribe", "sessionId": "s1"})
            hist, _ = await _until(ws, "history")
            assert hist["seq"] == 0
            await hub.broadcast(_acp("s2", "hidden"), session_id="s2")
            await hub.broadcast(_acp("s1", "seen"), session_id="s1")
            seen, before = await _until(ws, "acp")
            assert before == [] and seen["seq"] == 2
            await ws.send_json({"type": "subscribe", "sessionId": "s2", "sinceSeq": 0, "bootId": hub.boot_id})
            replay, before = await _until(ws, "replay")
            assert before == []
            assert replay["count"] == 1 and replay["events"][0]["seq"] == 1
            assert replay["events"][0]["message"]["params"]["update"]["text"] == "hidden"
            await ws.close()
        finally:
            await client.close()

    asyncio.run(run())
