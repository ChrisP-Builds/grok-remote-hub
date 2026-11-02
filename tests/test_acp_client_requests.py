"""Agent-to-hub requests: blocking ones leave the receive loop free; leader stderr is drained."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import hub.acp_client as acp_mod
from hub.acp_client import AcpClient
from hub.config import Config


def _client(**cfg: Any) -> AcpClient:
    return AcpClient(Config(**cfg), secret="test-secret-client-requests")


def _wait_for_exit_request(req_id: int, terminal_id: str) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "terminal/wait_for_exit",
            "params": {"sessionId": "s-blocked", "terminalId": terminal_id},
        }
    )


def _update_notification(session_id: str) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "hi"},
                },
            },
        }
    )


def test_wait_for_exit_request_does_not_block_the_receive_loop() -> None:
    """While one session's command runs, other sessions' frames must keep flowing."""

    async def run() -> None:
        client = _client()
        sent: list[dict] = []
        seen: list[str | None] = []

        async def send(payload: dict) -> None:
            sent.append(payload)

        client._send = send  # type: ignore[method-assign]
        client.on_message = lambda msg: seen.append(msg.get("method"))
        command_done = asyncio.Event()

        async def wait_for_exit(terminal_id: str) -> dict:
            assert terminal_id == "term_blocked"
            await command_done.wait()
            return {"exitCode": 0, "signal": None}

        client._terminals.wait_for_exit = wait_for_exit  # type: ignore[method-assign]

        await asyncio.wait_for(client._handle_raw(_wait_for_exit_request(7, "term_blocked")), timeout=0.5)
        await asyncio.wait_for(client._handle_raw(_update_notification("s-other")), timeout=0.5)
        assert "session/update" in seen
        assert not [p for p in sent if p.get("id") == 7], "replied before the command ended"

        command_done.set()
        for _ in range(50):
            if any(p.get("id") == 7 for p in sent):
                break
            await asyncio.sleep(0.01)
        assert [p for p in sent if p.get("id") == 7] == [
            {"jsonrpc": "2.0", "id": 7, "result": {"exitCode": 0, "signal": None}}
        ]

    asyncio.run(run())


def test_wait_for_exit_task_still_answers_errors_with_the_request_id() -> None:
    async def run() -> None:
        client = _client()
        sent: list[dict] = []

        async def send(payload: dict) -> None:
            sent.append(payload)

        client._send = send  # type: ignore[method-assign]

        async def wait_for_exit(terminal_id: str) -> dict:
            raise KeyError(f"unknown terminalId: {terminal_id}")

        client._terminals.wait_for_exit = wait_for_exit  # type: ignore[method-assign]
        await client._handle_raw(_wait_for_exit_request(9, "term_missing"))
        for _ in range(50):
            if sent:
                break
            await asyncio.sleep(0.01)
        assert len(sent) == 1
        assert sent[0]["id"] == 9
        assert sent[0]["error"]["code"] == -32000
        assert "term_missing" in sent[0]["error"]["message"]

    asyncio.run(run())


def test_leader_backend_does_not_leave_stderr_as_an_unread_pipe(monkeypatch) -> None:
    """A PIPE nobody reads fills up and then blocks the agent; serve already uses DEVNULL."""
    spawns: list[dict[str, Any]] = []

    async def run() -> None:
        client = _client(agent_backend="leader")
        assert client._is_leader_backend()

        async def fake_exec(*args: Any, **kwargs: Any) -> Any:
            spawns.append(kwargs)
            client._stop.set()
            raise OSError("spawn refused by test")

        monkeypatch.setattr(acp_mod.asyncio, "create_subprocess_exec", fake_exec)
        await client._maintain_stdio_leader()

    asyncio.run(run())
    assert len(spawns) == 1
    assert spawns[0]["stdin"] == asyncio.subprocess.PIPE
    assert spawns[0]["stdout"] == asyncio.subprocess.PIPE
    assert spawns[0]["stderr"] != asyncio.subprocess.PIPE
    if sys.platform == "win32":
        assert spawns[0]["creationflags"] == 0x08000000
