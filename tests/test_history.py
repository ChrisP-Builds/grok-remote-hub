from __future__ import annotations

import json
from pathlib import Path

from hub.history import (
    _extract_text,
    normalize_status,
    parse_updates_jsonl,
    tool_label,
    tool_summary,
)


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
            **{"_meta": {"x.ai/tool": {"label": "Read", "kind": "read", "name": "read_file"}}},
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
    assert msgs[3]["meta"]["status"] == "completed"
    assert msgs[4]["role"] == "system"


def test_merge_dedupes_exact_and_prefix_user_chunks(tmp_path: Path) -> None:
    """Hub echo + ACP full message must not double user text."""
    path = tmp_path / "updates.jsonl"
    lines = [
        _line("user_message_chunk", content={"type": "text", "text": "hello"}),
        _line("user_message_chunk", content={"type": "text", "text": "hello"}),
        _line("user_message_chunk", content={"type": "text", "text": "hel"}),
        _line("user_message_chunk", content={"type": "text", "text": "hello world"}),
        _line("agent_message_chunk", content={"type": "text", "text": "ok"}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    msgs = parse_updates_jsonl(path, max_messages=200)
    assert msgs[0]["role"] == "user"
    assert msgs[0]["text"] == "hello world"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["text"] == "ok"


def test_tool_status_completed_with_thought_between(tmp_path: Path) -> None:
    """Updates after other events must still merge into the same toolCallId."""
    path = tmp_path / "updates.jsonl"
    lines = [
        _line(
            "tool_call",
            toolCallId="t-42",
            title="read_file",
            rawInput={"target_file": "hub/history.py"},
            **{"_meta": {"x.ai/tool": {"label": "Read", "kind": "read", "name": "read_file"}}},
        ),
        _line("agent_thought_chunk", content={"type": "text", "text": "looking…"}),
        _line(
            "tool_call_update",
            toolCallId="t-42",
            title="Read `hub/history.py`",
            status="completed",
            content=[
                {
                    "type": "content",
                    "content": {"type": "text", "text": "found 25 matches"},
                }
            ],
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    msgs = parse_updates_jsonl(path)
    tools = [m for m in msgs if m["role"] == "tool"]
    assert len(tools) == 1
    assert tools[0]["meta"]["status"] == "completed"
    assert tools[0]["meta"]["toolCallId"] == "t-42"
    assert "found 25 matches" in (tools[0]["meta"].get("summary") or tools[0]["meta"].get("detail") or "")
    assert tools[0]["text"] == "Read `hub/history.py`"


def test_plan_parses_and_last_plan_wins(tmp_path: Path) -> None:
    path = tmp_path / "updates.jsonl"
    lines = [
        _line(
            "plan",
            entries=[
                {"content": "Explore codebase", "priority": "high", "status": "pending"},
                {"content": "Fix merge", "priority": "medium", "status": "pending"},
            ],
        ),
        _line("agent_thought_chunk", content={"type": "text", "text": "planning"}),
        _line(
            "plan",
            entries=[
                {"content": "Explore codebase", "priority": "high", "status": "completed"},
                {"content": "Fix merge", "priority": "medium", "status": "in_progress"},
                {"content": "Verify tests", "priority": "low", "status": "pending"},
            ],
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    msgs = parse_updates_jsonl(path)
    plans = [m for m in msgs if m["role"] == "plan"]
    assert len(plans) == 1
    entries = plans[0]["meta"]["entries"]
    assert len(entries) == 3
    assert entries[0]["status"] == "completed"
    assert entries[1]["status"] == "running"  # in_progress normalized
    assert entries[2]["status"] == "pending"
    assert entries[0]["content"] == "Explore codebase"


def test_tool_detail_not_multiline_json_dump(tmp_path: Path) -> None:
    path = tmp_path / "updates.jsonl"
    huge = {
        "target_file": "D:/Projects/Grok Remote Hub/hub/history.py",
        "offset": 1,
        "limit": 500,
        "extra": "x" * 200,
    }
    lines = [
        _line(
            "tool_call",
            toolCallId="big-1",
            title="read_file",
            rawInput=huge,
            **{"_meta": {"x.ai/tool": {"label": "Read", "kind": "read", "name": "read_file"}}},
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    msgs = parse_updates_jsonl(path)
    tool = msgs[0]
    detail = tool["meta"].get("detail") or ""
    summary = tool["meta"].get("summary") or ""
    assert "\n" not in detail
    assert "\n" not in summary
    assert "{" not in detail or detail.count("{") == 0  # path, not JSON object dump
    assert "history.py" in summary or "history.py" in detail or "history.py" in tool["text"]
    assert len(detail) <= 160
    assert tool["meta"]["label"] == "Read"


def test_normalize_status() -> None:
    assert normalize_status("completed") == "completed"
    assert normalize_status("in_progress") == "running"
    assert normalize_status({"status": "failed"}) == "failed"
    assert normalize_status(None) == "pending"


def test_tool_label_and_summary() -> None:
    update = {
        "title": "read_file",
        "rawInput": {"target_file": "a.py"},
        "_meta": {"x.ai/tool": {"label": "Read", "kind": "read", "name": "read_file"}},
    }
    assert tool_label(update) == "Read"
    assert tool_summary(update) == "a.py"
    assert "\n" not in tool_summary({"rawInput": {"command": "ls -la " + "x" * 200}})


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


def test_skips_hook_execution(tmp_path: Path) -> None:
    path = tmp_path / "updates.jsonl"
    path.write_text(
        "\n".join(
            [
                _line("hook_execution", hook="something"),
                _line("user_message_chunk", content={"type": "text", "text": "hi"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    msgs = parse_updates_jsonl(path)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"


def test_extract_text_nested_content_shapes() -> None:
    """Parity with JS extractText: nested content dict/array/text."""
    assert _extract_text("plain") == "plain"
    assert _extract_text({"type": "text", "text": "hello"}) == "hello"
    assert (
        _extract_text(
            {
                "type": "content",
                "content": {"type": "text", "text": "nested"},
            }
        )
        == "nested"
    )
    assert (
        _extract_text(
            [
                {"type": "content", "content": {"type": "text", "text": "a"}},
                {"type": "content", "content": {"type": "text", "text": "b"}},
            ]
        )
        == "ab"
    )
    assert _extract_text({"content": [{"text": "x"}, {"text": "y"}]}) == "xy"


def test_subagent_events_as_system(tmp_path: Path) -> None:
    path = tmp_path / "updates.jsonl"
    path.write_text(
        "\n".join(
            [
                _line("subagent_spawned", sessionId="child-1"),
                _line("subagent_finished", sessionId="child-1"),
                _line("user_message_chunk", content={"type": "text", "text": "hi"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    msgs = parse_updates_jsonl(path)
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "system", "user"]
    assert "subagent spawned" in msgs[0]["text"]
    assert "child-1" in msgs[0]["text"]
    assert "subagent finished" in msgs[1]["text"]
