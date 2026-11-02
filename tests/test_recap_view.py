"""build_session_recap pure helper + disk path."""

from __future__ import annotations

import json
from pathlib import Path

from hub.recap_view import (
    build_session_recap,
    build_session_recap_from_disk,
)


def test_recap_from_messages_last_user_and_assistant() -> None:
    msgs = [
        {"role": "user", "text": "First question"},
        {"role": "assistant", "text": "First answer"},
        {"role": "tool", "text": "read_file"},
        {"role": "user", "text": "What next?"},
        {"role": "assistant", "text": "Ship the recap endpoint."},
    ]
    out = build_session_recap(msgs, max_chars=800)
    assert out["empty"] is False
    assert out["lastUser"] == "What next?"
    assert out["lastAssistant"] == "Ship the recap endpoint."
    assert "What next?" in out["summary"]
    assert "Ship the recap endpoint." in out["summary"]


def test_recap_empty_messages() -> None:
    out = build_session_recap([], max_chars=200)
    assert out["empty"] is True
    assert out["summary"] == ""
    assert out["lastUser"] is None
    assert out["lastAssistant"] is None


def test_recap_truncates_to_max_chars() -> None:
    long_a = "A" * 2000
    msgs = [
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": long_a},
    ]
    out = build_session_recap(msgs, max_chars=120)
    assert len(out["summary"]) <= 120
    assert out["empty"] is False


def test_recap_from_disk(tmp_path: Path) -> None:
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    session_dir = tmp_path / "proj" / sid
    session_dir.mkdir(parents=True)
    lines = [
        json.dumps(
            {
                "timestamp": 1,
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": "Disk user"},
                    },
                },
            }
        ),
        json.dumps(
            {
                "timestamp": 2,
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "Disk assistant"},
                    },
                },
            }
        ),
    ]
    (session_dir / "updates.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = build_session_recap_from_disk(tmp_path, sid, session_path=session_dir)
    assert out["empty"] is False
    assert out["lastUser"] == "Disk user"
    assert out["lastAssistant"] == "Disk assistant"
    assert out["sessionId"] == sid
