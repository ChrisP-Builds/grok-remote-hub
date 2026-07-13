from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from hub.follow import (
    Colors,
    StreamState,
    format_message_line,
    handle_live_line,
    handle_live_update,
    resolve_session,
)
from hub.session_index import SessionInfo


def _acp_line(session_update: str, **extra) -> str:
    update = {"sessionUpdate": session_update, **extra}
    return json.dumps(
        {
            "method": "session/update",
            "params": {"sessionId": "s1", "update": update},
        }
    )


def test_format_message_line_roles() -> None:
    c = Colors(enabled=False)
    assert format_message_line({"role": "user", "text": "hi", "meta": {}}, False, c) == "You: hi"
    assert format_message_line({"role": "assistant", "text": "yo", "meta": {}}, False, c) == "Grok: yo"
    assert format_message_line({"role": "thought", "text": "hmm", "meta": {}}, False, c) is None
    assert "thought" in (format_message_line({"role": "thought", "text": "hmm", "meta": {}}, True, c) or "")
    assert format_message_line(
        {"role": "tool", "text": "Read a.py", "meta": {"label": "Read a.py", "status": "completed"}},
        False,
        c,
    ) is None
    tool = format_message_line(
        {"role": "tool", "text": "Read a.py", "meta": {"label": "Read a.py", "status": "completed"}},
        True,
        c,
    )
    assert tool is not None and "Read a.py" in tool and "completed" in tool


def test_handle_live_line_streams_assistant() -> None:
    out = StringIO()
    state = StreamState()
    c = Colors(enabled=False)
    handle_live_line(
        _acp_line("agent_message_chunk", content={"type": "text", "text": "Hel"}),
        state,
        verbose=False,
        colors=c,
        out=out,
    )
    handle_live_line(
        _acp_line("agent_message_chunk", content={"type": "text", "text": "lo"}),
        state,
        verbose=False,
        colors=c,
        out=out,
    )
    assert out.getvalue() == "Grok: Hello"
    assert state.open_role == "assistant"


def test_handle_live_skips_thought_unless_verbose() -> None:
    out = StringIO()
    state = StreamState()
    c = Colors(enabled=False)
    handle_live_update(
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "secret"}},
        state,
        verbose=False,
        colors=c,
        out=out,
    )
    assert out.getvalue() == ""
    handle_live_update(
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "secret"}},
        state,
        verbose=True,
        colors=c,
        out=out,
    )
    assert "thought" in out.getvalue()


def test_resolve_session_by_id_and_cwd(tmp_path: Path) -> None:
    # Layout: sessions_root / encoded_cwd / uuid / summary.json
    sid = "019f493c-af12-7652-a6d8-bf645c10921c"
    cwd = str(tmp_path / "proj")
    Path(cwd).mkdir()
    sess = tmp_path / "sessions" / "encoded" / sid
    sess.mkdir(parents=True)
    summary = {
        "generated_title": "Demo project test",
        "updated_at": "2026-07-09T12:00:00Z",
        "num_chat_messages": 2,
        "info": {"id": sid, "cwd": cwd},
        "current_model_id": "test",
    }
    (sess / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (sess / "updates.jsonl").write_text("{}\n", encoding="utf-8")

    root = tmp_path / "sessions"
    by_id = resolve_session(root, session_id=sid)
    assert by_id is not None
    assert by_id.sessionId == sid
    assert by_id.title == "Demo project test"

    by_cwd = resolve_session(root, cwd=cwd)
    assert by_cwd is not None
    assert by_cwd.sessionId == sid

    recent = resolve_session(root)
    assert recent is not None
    assert recent.sessionId == sid

    assert resolve_session(root, session_id="00000000-0000-0000-0000-000000000000") is None


def test_resolve_prefers_most_recent_for_cwd(tmp_path: Path) -> None:
    cwd = str(tmp_path / "app")
    Path(cwd).mkdir()
    root = tmp_path / "sessions"
    old_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    new_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    for sid, ts in ((old_id, "2026-01-01T00:00:00Z"), (new_id, "2026-07-09T00:00:00Z")):
        d = root / "enc" / sid
        d.mkdir(parents=True)
        (d / "summary.json").write_text(
            json.dumps(
                {
                    "generated_title": sid[:8],
                    "updated_at": ts,
                    "num_chat_messages": 1,
                    "info": {"id": sid, "cwd": cwd},
                }
            ),
            encoding="utf-8",
        )
        (d / "updates.jsonl").write_text("{}\n", encoding="utf-8")

    info = resolve_session(root, cwd=cwd)
    assert info is not None
    assert info.sessionId == new_id


def test_session_info_path_usable() -> None:
    """Sanity: SessionInfo path field is what follow uses for updates.jsonl."""
    info = SessionInfo(
        sessionId="x",
        title="t",
        cwd="c",
        updatedAt="2026-01-01T00:00:00Z",
        modelId="",
        path=r"D:\fake\session",
    )
    assert Path(info.path).name == "session" or "session" in info.path
