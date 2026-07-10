from __future__ import annotations

import json
from pathlib import Path

from hub.history import parse_updates_jsonl


def _line(session_update: str, **extra) -> str:
    update = {"sessionUpdate": session_update, **extra}
    return json.dumps(
        {
            "timestamp": 1,
            "method": "session/update",
            "params": {"sessionId": "s1", "update": update},
        }
    )


def test_parse_and_merge_chunks(tmp_path: Path) -> None:
    path = tmp_path / "updates.jsonl"
    lines = [
        _line("user_message_chunk", content={"type": "text", "text": "Hello "}),
        _line("user_message_chunk", content={"type": "text", "text": "world"}),
        _line("agent_thought_chunk", content={"type": "text", "text": "think "}),
        _line("agent_thought_chunk", content={"type": "text", "text": "more"}),
        _line("agent_message_chunk", content={"type": "text", "text": "Hi "}),
        _line("agent_message_chunk", content={"type": "text", "text": "there"}),
        _line(
            "tool_call",
            toolCallId="call-1",
            title="read_file",
            rawInput={"target_file": "a.py"},
        ),
        _line(
            "tool_call_update",
            toolCallId="call-1",
            title="Read a.py",
            status="completed",
        ),
        _line("turn_completed"),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    msgs = parse_updates_jsonl(path, max_messages=200)
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "thought", "assistant", "tool", "system"]
    assert msgs[0]["text"] == "Hello world"
    assert msgs[1]["text"] == "think more"
    assert msgs[2]["text"] == "Hi there"
    assert msgs[3]["text"] == "Read a.py"
    assert msgs[3]["meta"]["toolCallId"] == "call-1"
    assert msgs[4]["role"] == "system"


def test_cap_max_messages(tmp_path: Path) -> None:
    path = tmp_path / "updates.jsonl"
    lines = [
        _line("user_message_chunk", content={"type": "text", "text": f"msg{i}"})
        for i in range(10)
    ]
    # Separate with assistant so they do not all merge
    mixed = []
    for i, line in enumerate(lines):
        mixed.append(line)
        mixed.append(
            _line("agent_message_chunk", content={"type": "text", "text": f"a{i}"})
        )
    path.write_text("\n".join(mixed) + "\n", encoding="utf-8")
    msgs = parse_updates_jsonl(path, max_messages=5)
    assert len(msgs) == 5


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    assert parse_updates_jsonl(tmp_path / "none.jsonl") == []


def test_skips_available_commands(tmp_path: Path) -> None:
    path = tmp_path / "updates.jsonl"
    path.write_text(
        "\n".join(
            [
                _line("available_commands_update", availableCommands=[{"name": "help"}]),
                _line("user_message_chunk", content={"type": "text", "text": "hi"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    msgs = parse_updates_jsonl(path)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
