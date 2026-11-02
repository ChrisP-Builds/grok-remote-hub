"""Approvals broker, hook endpoint and gate plugin (ADR-027)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hub.approvals import (
    ApprovalBroker,
    is_loopback_peer,
    make_pre_tool_use_handler,
    write_gate_plugin,
)

ROOT = r"D:\Projects\Alpha"
SID = "sess-1"


def _event(command: str = "git push origin main", tool: str = "run_terminal_command", **extra) -> dict:
    tool_input = {"command": command} if tool == "run_terminal_command" else extra.pop("tool_input")
    return {"sessionId": SID, "cwd": ROOT, "toolName": tool, "toolInput": tool_input, **extra}


def _broker(tmp_path: Path, published: list, **kw) -> ApprovalBroker:
    async def publish(payload: dict) -> None:
        published.append(payload)

    return ApprovalBroker(tmp_path / "rules.json", publish, session_root_for=lambda sid: ROOT, **kw)


async def _answer_when_requested(broker: ApprovalBroker, published: list, decision: str, scope: str = "once") -> None:
    for _ in range(200):
        req = next((p for p in published if p["type"] == "approval_request"), None)
        if req:
            assert broker.answer(req["id"], decision, scope)
            return
        await asyncio.sleep(0.005)
    raise AssertionError("no approval_request published")


def test_safe_call_defers_without_asking(tmp_path: Path) -> None:
    published: list = []
    broker = _broker(tmp_path, published)
    reply = asyncio.run(broker.decide(_event("git status")))
    assert reply == {"decision": "defer"}
    assert published == []


def test_risky_call_asks_and_allow_once_permits_execution(tmp_path: Path) -> None:
    published: list = []
    broker = _broker(tmp_path, published)

    async def run() -> dict:
        task = asyncio.create_task(broker.decide(_event()))
        await _answer_when_requested(broker, published, "allow")
        reply = await task
        await broker.flush_emits()
        return reply

    assert asyncio.run(run()) == {"decision": "allow"}
    req, resolved = published
    assert req["type"] == "approval_request"
    assert (req["sessionId"], req["kind"], req["summary"], req["cwd"]) == (SID, "shell", "git push origin main", ROOT)
    assert req["similarRule"] == "git push *" and req["expiresAt"] > req["createdAt"]
    assert resolved == {"type": "approval_resolved", "id": req["id"], "sessionId": SID,
                        "decision": "allow", "by": "user", "scope": "once"}
    assert broker.pending_payload() == []
    assert broker.exec_refusal("shell", SID, "git  push origin main") is None
    assert broker.exec_refusal("shell", SID, "git push --force") is not None


def test_allow_similar_saves_editable_project_rule(tmp_path: Path) -> None:
    published: list = []
    broker = _broker(tmp_path, published)

    async def run() -> None:
        task = asyncio.create_task(broker.decide(_event()))
        await _answer_when_requested(broker, published, "allow", "project")
        await task
        await broker.flush_emits()

    asyncio.run(run())
    saved = json.loads((tmp_path / "rules.json").read_text(encoding="utf-8"))
    assert saved == {"projects": {ROOT: ["git push *"]}}
    published.clear()
    assert asyncio.run(broker.decide(_event("git push origin other"))) == {"decision": "defer"}
    assert published == []


def test_allow_similar_for_a_flag_led_command_saves_the_exact_command(tmp_path: Path) -> None:
    published: list = []
    broker = _broker(tmp_path, published)

    async def run() -> None:
        task = asyncio.create_task(broker.decide(_event('powershell -Command "git push"')))
        await _answer_when_requested(broker, published, "allow", "project")
        await task
        await broker.flush_emits()

    asyncio.run(run())
    assert published[0]["similarRule"] == 'powershell -Command "git push"'
    saved = json.loads((tmp_path / "rules.json").read_text(encoding="utf-8"))
    assert saved == {"projects": {ROOT: ['powershell -Command "git push"']}}
    assert broker.exec_refusal("shell", SID, 'powershell -Command "git push"') is None
    assert broker.exec_refusal("shell", SID, 'powershell -Command "git reset --hard"') is not None


def test_all_matching_rules_are_neither_saved_nor_honoured(tmp_path: Path) -> None:
    broker = _broker(tmp_path, [])
    (tmp_path / "rules.json").write_text(
        json.dumps({"projects": {ROOT: ["*", "file *", "git push *"]}}), encoding="utf-8"
    )
    assert broker.grants_for(ROOT) == ["git push *"]
    assert broker.exec_refusal("shell", SID, "rm -rf x") is not None
    assert broker.exec_refusal("file", SID, r"C:\Windows\x.ini") is not None
    broker._add_grant(ROOT, "*")
    broker._add_grant(ROOT, "file *")
    saved = json.loads((tmp_path / "rules.json").read_text(encoding="utf-8"))
    assert saved == {"projects": {ROOT: ["*", "file *", "git push *"]}}, "hand-written junk left as is, nothing added"


def test_hub_state_is_protected_whatever_the_session_root(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    rules = hub / "data" / "approval-rules.json"

    async def publish(payload: dict) -> None:
        pass

    broker = ApprovalBroker(rules, publish, session_root_for=lambda sid: str(hub))
    for path in (rules, hub / "data" / "hook.secret", hub / "data" / "grok-approval-gate" / "hooks" / "hooks.json",
                 hub / "config.toml"):
        assert broker.exec_refusal("file", SID, str(path)) is not None, path
    assert broker.exec_refusal("file", SID, str(hub / "hub" / "x.py")) is None
    assert broker.exec_refusal("shell", SID, "Set-Content config.toml x") is not None
    assert broker.exec_refusal("shell", SID, r"Copy-Item x data\approval-rules.json") is not None
    assert broker.exec_refusal("shell", SID, "git status") is None
    quick = ApprovalBroker(rules, publish, session_root_for=lambda sid: str(hub), timeout=0.01)
    event = {"sessionId": SID, "cwd": str(hub), "toolName": "run_terminal_command",
             "toolInput": {"command": "Set-Content config.toml x"}}
    assert asyncio.run(quick.decide(event))["decision"] == "deny"


def test_protected_paths_can_be_given_explicitly(tmp_path: Path) -> None:
    async def publish(payload: dict) -> None:
        pass

    broker = ApprovalBroker(
        tmp_path / "rules.json", publish, session_root_for=lambda sid: ROOT,
        protected_paths=[Path(ROOT) / "secrets"],
    )
    assert broker.exec_refusal("file", SID, ROOT + r"\secrets\k.pem") is not None
    assert broker.exec_refusal("file", SID, ROOT + r"\src\a.py") is None


def test_hand_edited_rules_file_is_picked_up(tmp_path: Path) -> None:
    broker = _broker(tmp_path, [])
    assert broker.exec_refusal("shell", SID, "npm install x") is not None
    (tmp_path / "rules.json").write_text(json.dumps({"projects": {ROOT.lower(): ["npm install *"]}}), encoding="utf-8")
    assert broker.exec_refusal("shell", SID, "npm install x") is None


def test_no_answer_denies_before_hook_timeout(tmp_path: Path) -> None:
    published: list = []
    broker = _broker(tmp_path, published, timeout=0.05)

    async def run() -> dict:
        reply = await broker.decide(_event())
        await broker.flush_emits()
        return reply

    reply = asyncio.run(run())
    assert reply["decision"] == "deny"
    assert "no answer" in reply["reason"].lower()
    assert published[-1]["by"] == "timeout"


def test_stalled_broadcast_cannot_delay_the_decision(tmp_path: Path) -> None:
    """One stuck socket must not push the hub past Grok's 330 s hook timeout (which fails open)."""
    never = asyncio.Event()

    async def stalled_publish(payload: dict) -> None:
        await never.wait()

    broker = ApprovalBroker(tmp_path / "rules.json", stalled_publish, session_root_for=lambda sid: ROOT, timeout=0.05)

    async def run() -> dict:
        return await asyncio.wait_for(broker.decide(_event()), 2.0)

    reply = asyncio.run(run())
    assert reply["decision"] == "deny" and "no answer" in reply["reason"].lower()


def test_slow_broadcast_still_delivers_request_before_resolved(tmp_path: Path) -> None:
    published: list = []

    async def slow_publish(payload: dict) -> None:
        await asyncio.sleep(0.02)
        published.append(payload)

    broker = ApprovalBroker(tmp_path / "rules.json", slow_publish, session_root_for=lambda sid: ROOT, timeout=0.01)

    async def run() -> dict:
        reply = await broker.decide(_event())
        assert published == [], "the decision did not wait for the broadcast"
        await broker.flush_emits()
        return reply

    assert asyncio.run(run())["decision"] == "deny"
    assert [p["type"] for p in published] == ["approval_request", "approval_resolved"]
    assert published[1]["id"] == published[0]["id"]


def test_deny_and_cancel_tell_the_model_not_to_retry(tmp_path: Path) -> None:
    published: list = []
    broker = _broker(tmp_path, published)

    async def run() -> tuple[dict, dict]:
        t1 = asyncio.create_task(broker.decide(_event()))
        await _answer_when_requested(broker, published, "deny")
        first = await t1
        await broker.flush_emits()
        published.clear()
        t2 = asyncio.create_task(broker.decide(_event("rm -rf build")))
        for _ in range(200):
            if broker.pending_payload():
                break
            await asyncio.sleep(0.005)
        assert broker.cancel_session(SID) == 1
        second = await t2
        await broker.flush_emits()
        return first, second

    denied, cancelled = asyncio.run(run())
    assert denied["decision"] == cancelled["decision"] == "deny"
    assert "do not retry" in denied["reason"].lower()
    assert published[-1]["by"] == "cancelled"


def test_modes(tmp_path: Path) -> None:
    broker = _broker(tmp_path, [])
    assert broker.mode_for(SID) == "balanced"
    assert broker.set_mode(SID, "TRUSTED") == "trusted"
    assert asyncio.run(broker.decide(_event("rm -rf /"))) == {"decision": "defer"}
    assert broker.set_mode(SID, "nonsense") == "balanced"
    assert broker.modes_payload() == {SID: "balanced"}


def test_exec_refusal_for_files(tmp_path: Path) -> None:
    broker = _broker(tmp_path, [])
    assert broker.exec_refusal("file", SID, ROOT + r"\src\a.py") is None
    assert "approval" in broker.exec_refusal("file", SID, r"C:\Windows\x.ini").lower()


def test_unknown_answer_id_is_rejected(tmp_path: Path) -> None:
    assert _broker(tmp_path, []).answer("nope", "allow", "once") is False


def test_is_loopback_peer() -> None:
    assert is_loopback_peer("127.0.0.1") and is_loopback_peer("::1")
    assert not is_loopback_peer("100.64.1.2") and not is_loopback_peer(None)


def test_hook_endpoint_requires_key_and_returns_decision(tmp_path: Path) -> None:
    broker = _broker(tmp_path, [])

    async def run() -> None:
        app = web.Application()
        app.router.add_post("/internal/pre-tool-use", make_pre_tool_use_handler(broker, "k3y"))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            assert (await client.post("/internal/pre-tool-use?key=bad", json=_event("git status"))).status == 403
            ok = await client.post("/internal/pre-tool-use?key=k3y", json=_event("git status"))
            assert ok.status == 200 and await ok.json() == {"decision": "defer"}
        finally:
            await client.close()

    asyncio.run(run())


def test_hook_endpoint_rejects_requests_proxied_by_tailscale_serve(tmp_path: Path) -> None:
    """Serve forwards tailnet requests from 127.0.0.1, so the peer check alone proves nothing."""
    broker = _broker(tmp_path, [])
    url = "/internal/pre-tool-use?key=k3y"

    async def run() -> None:
        app = web.Application()
        app.router.add_post("/internal/pre-tool-use", make_pre_tool_use_handler(broker, "k3y"))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            for headers in (
                {"Host": "hub.tail1234.ts.net"},
                {"Host": "hub.tail1234.ts.net:443"},
                {"Host": "192.168.1.5:8787"},
                {"Tailscale-User-Login": "me@example.com"},
                {"tailscale-user-name": "Me"},
                {"X-Forwarded-For": "100.64.1.2"},
            ):
                r = await client.post(url, json=_event("git status"), headers=headers)
                assert r.status == 403, headers
            for host in ("127.0.0.1:8787", "localhost:8787", "[::1]:8787", "127.5.5.5"):
                r = await client.post(url, json=_event("git status"), headers={"Host": host})
                assert r.status == 200, host
        finally:
            await client.close()

    asyncio.run(run())


def test_event_only_root_applies_saved_rules_but_never_offers_a_project_grant(tmp_path: Path) -> None:
    published: list = []

    async def publish(payload: dict) -> None:
        published.append(payload)

    broker = ApprovalBroker(tmp_path / "rules.json", publish, session_root_for=lambda sid: None)

    async def run() -> dict:
        task = asyncio.create_task(broker.decide(_event()))
        await _answer_when_requested(broker, published, "allow", "project")
        reply = await task
        await broker.flush_emits()
        return reply

    assert asyncio.run(run()) == {"decision": "allow"}
    assert published[0]["cwd"] == ROOT and published[0]["similarRule"] is None
    assert not (tmp_path / "rules.json").exists()
    (tmp_path / "rules.json").write_text(json.dumps({"projects": {ROOT: ["git push *"]}}), encoding="utf-8")
    assert asyncio.run(broker.decide(_event("git push origin other"))) == {"decision": "defer"}


def test_write_gate_plugin(tmp_path: Path) -> None:
    write_gate_plugin(tmp_path / "plugin", "http://127.0.0.1:8787/internal/pre-tool-use?key=k")
    hooks = json.loads((tmp_path / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    handler = hooks["hooks"]["PreToolUse"][0]["hooks"][0]
    assert handler == {"type": "http", "url": "http://127.0.0.1:8787/internal/pre-tool-use?key=k", "timeout": 330}


def test_acp_exec_gate_blocks_shell_and_write_before_running() -> None:
    from hub.acp_client import AcpClient
    from hub.config import Config

    client = AcpClient(Config(), secret="t-exec-gate")
    calls, errors, results = [], [], []
    client.exec_gate = lambda kind, sid, value: calls.append((kind, sid, value)) or "Blocked: nope"

    async def must_not_run(_params):
        raise AssertionError("terminal must not start")

    async def reply_error(msg_id, _code, message):
        errors.append((msg_id, message))

    async def reply_result(msg_id, _result):
        results.append(msg_id)

    client._terminals.create = must_not_run
    client._reply_error, client._reply_result = reply_error, reply_result

    async def run() -> None:
        await client._handle_client_request(
            {"id": 1, "method": "terminal/create", "params": {"sessionId": "s", "command": "git", "args": ["push"]}})
        await client._handle_client_request(
            {"id": 2, "method": "fs/write_text_file", "params": {"sessionId": "s", "path": r"C:\x.ini", "content": "x"}})

    asyncio.run(run())
    assert calls == [("shell", "s", "git push"), ("file", "s", r"C:\x.ini")]
    assert errors == [(1, "Blocked: nope"), (2, "Blocked: nope")]
    assert results == []


def test_acp_exec_gate_allows_when_gate_returns_none() -> None:
    from hub.acp_client import AcpClient
    from hub.config import Config

    client = AcpClient(Config(), secret="t-exec-gate-ok")
    client.exec_gate = lambda kind, sid, value: None
    results = []

    async def create(_params):
        return {"terminalId": "t1"}

    async def reply_result(msg_id, result):
        results.append((msg_id, result))

    client._terminals.create = create
    client._reply_result = reply_result
    asyncio.run(client._handle_client_request(
        {"id": 7, "method": "terminal/create", "params": {"sessionId": "s", "command": "git status"}}))
    assert results == [(7, {"terminalId": "t1"})]


def test_supervisor_passes_gate_plugin_dir_to_serve_and_leader(tmp_path: Path) -> None:
    from hub.agent_supervisor import AgentSupervisor
    from hub.config import Config

    plugin = tmp_path / "gate"
    seen: list[list[str]] = []

    async def capture(cmd, _label):
        seen.append(cmd)

    sup = AgentSupervisor(Config(gate_plugin_dir=plugin, log_dir=tmp_path), "secret")
    sup._spawn_detached = capture
    asyncio.run(sup._spawn())
    assert "--plugin-dir" not in seen[-1], "no plugin written yet"
    write_gate_plugin(plugin, "http://127.0.0.1:1/x")
    asyncio.run(sup._spawn())
    asyncio.run(sup._spawn_leader())
    for cmd, mode in ((seen[1], "serve"), (seen[2], "leader")):
        i = cmd.index("--plugin-dir")
        assert cmd[i + 1] == str(plugin) and i < cmd.index(mode)


def test_server_wires_approvals() -> None:
    src = (Path(__file__).resolve().parents[1] / "hub" / "server.py").read_text(encoding="utf-8")
    for needle in (
        'add_post("/internal/pre-tool-use", make_pre_tool_use_handler(self.approvals, self.hook_key))',
        "self.acp.exec_gate = self.approvals.exec_refusal",
        '"approval_request",',
        '"approval_resolved",',
        '"pendingApprovals": self.approvals.pending_payload()',
        '"approvalModes": self.approvals.modes_payload()',
        'if typ == "approval_answer":',
        'if typ == "set_approval_mode":',
        "self.approvals.cancel_session(session_id)",
        "write_gate_plugin(",
    ):
        assert needle in src, needle

def test_payload_marks_invisible_unicode_and_carries_server_clock(tmp_path: Path) -> None:
    """A prompt-injected agent must not disguise a command with bidi or zero-width characters."""
    published: list = []
    broker = _broker(tmp_path, published)
    snapshot: list = []

    async def run() -> None:
        task = asyncio.create_task(broker.decide(_event("rm -rf \u202e/dliub\u202c\u200b")))
        for _ in range(200):
            if broker.pending_payload():
                snapshot.extend(broker.pending_payload())
                break
            await asyncio.sleep(0.005)
        await _answer_when_requested(broker, published, "deny")
        await task
        await broker.flush_emits()

    asyncio.run(run())
    req = published[0]
    assert "\u202e" not in req["summary"] and "\u200b" not in req["summary"]
    assert "<U+202E>" in req["summary"] and "<U+200B>" in req["summary"]
    assert req["createdAt"] <= req["serverNow"] <= req["expiresAt"]
    assert snapshot and snapshot[0]["serverNow"] >= req["createdAt"]