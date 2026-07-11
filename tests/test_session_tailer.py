from __future__ import annotations

import asyncio
import json
from pathlib import Path

from hub.session_tailer import (
    EventDedupe,
    SessionTailer,
    extract_event_id,
    parse_updates_line,
    stable_event_key,
)


def test_extract_event_id_from_params_meta() -> None:
    msg = {
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}},
            "_meta": {"eventId": "s1-42", "totalTokens": 1},
        },
    }
    assert extract_event_id(msg) == "s1-42"


def test_extract_event_id_from_update_meta() -> None:
    msg = {
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call",
                "_meta": {"eventId": "nested-9"},
            },
        },
    }
    assert extract_event_id(msg) == "nested-9"


def test_extract_event_id_top_level() -> None:
    msg = {"_meta": {"eventId": "top-1"}, "method": "session/update", "params": {}}
    assert extract_event_id(msg) == "top-1"


def test_extract_event_id_missing() -> None:
    msg = {
        "method": "session/update",
        "params": {"sessionId": "s1", "update": {"sessionUpdate": "plan"}},
    }
    assert extract_event_id(msg) is None


def test_stable_event_key_prefers_event_id() -> None:
    msg = {
        "timestamp": 999,
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "x"}},
            "_meta": {"eventId": "e-1"},
        },
    }
    assert stable_event_key(msg) == "id:e-1"


def test_stable_event_key_hash_ignores_timestamp() -> None:
    a = {
        "timestamp": 1,
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "Hello"},
            },
        },
    }
    b = {
        "timestamp": 2,
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "Hello"},
            },
        },
    }
    assert stable_event_key(a) == stable_event_key(b)
    assert stable_event_key(a).startswith("h:")


def test_parse_updates_line_message_shape() -> None:
    line = json.dumps(
        {
            "timestamp": 100,
            "method": "session/update",
            "params": {
                "sessionId": "abc",
                "update": {"sessionUpdate": "turn_completed"},
                "_meta": {"eventId": "abc-1"},
            },
        }
    )
    msg = parse_updates_line(line)
    assert msg is not None
    assert msg["method"] == "session/update"
    assert msg["params"]["sessionId"] == "abc"
    assert extract_event_id(msg) == "abc-1"


def test_parse_updates_line_invalid() -> None:
    assert parse_updates_line("") is None
    assert parse_updates_line("not-json") is None
    assert parse_updates_line("[]") is None


def test_event_dedupe_by_event_id() -> None:
    dedupe = EventDedupe(maxlen=10)
    msg_acp = {
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "a"}},
            "_meta": {"eventId": "same-id"},
        },
    }
    msg_disk = {
        "timestamp": 5,
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "a"}},
            "_meta": {"eventId": "same-id"},
        },
    }
    assert dedupe.should_emit("s1", msg_acp) is True
    assert dedupe.should_emit("s1", msg_disk) is False


def test_event_dedupe_by_hash_without_event_id() -> None:
    dedupe = EventDedupe(maxlen=10)
    msg1 = {
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "hi"}},
        },
    }
    msg2 = {
        "timestamp": 99,
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "hi"}},
        },
    }
    assert dedupe.should_emit("s1", msg1) is True
    assert dedupe.should_emit("s1", msg2) is False


def test_event_dedupe_maxlen_eviction() -> None:
    dedupe = EventDedupe(maxlen=2)
    def msg(n: int) -> dict:
        return {
            "method": "session/update",
            "params": {
                "sessionId": "s1",
                "update": {"sessionUpdate": "x", "content": {"type": "text", "text": str(n)}},
                "_meta": {"eventId": f"e{n}"},
            },
        }

    assert dedupe.should_emit("s1", msg(1)) is True
    assert dedupe.should_emit("s1", msg(2)) is True
    assert dedupe.should_emit("s1", msg(3)) is True
    # e1 should be evicted
    assert dedupe.should_emit("s1", msg(1)) is True


def test_session_tailer_reads_new_lines(tmp_path: Path) -> None:
    async def _run() -> None:
        session_dir = tmp_path / "sess-1"
        session_dir.mkdir()
        updates = session_dir / "updates.jsonl"
        updates.write_text("", encoding="utf-8")

        received: list[tuple[str, dict]] = []

        async def on_event(session_id: str, msg: dict) -> None:
            received.append((session_id, msg))

        tailer = SessionTailer(tmp_path, on_event=on_event, poll_interval=0.05)
        await tailer.start()
        try:
            await tailer.ensure_watching("sess-1", session_dir)
            with updates.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "timestamp": 1,
                            "method": "session/update",
                            "params": {
                                "sessionId": "sess-1",
                                "update": {
                                    "sessionUpdate": "agent_message_chunk",
                                    "content": {"type": "text", "text": "live"},
                                },
                                "_meta": {"eventId": "live-1"},
                            },
                        }
                    )
                    + "\n"
                )
            for _ in range(40):
                if received:
                    break
                await asyncio.sleep(0.05)
            assert len(received) == 1
            sid, msg = received[0]
            assert sid == "sess-1"
            assert msg["method"] == "session/update"
            assert extract_event_id(msg) == "live-1"
            assert msg["params"]["update"]["content"]["text"] == "live"
        finally:
            await tailer.stop()

    asyncio.run(_run())


def test_session_tailer_seek_end_skips_history(tmp_path: Path) -> None:
    async def _run() -> None:
        session_dir = tmp_path / "sess-2"
        session_dir.mkdir()
        updates = session_dir / "updates.jsonl"
        old = {
            "timestamp": 1,
            "method": "session/update",
            "params": {
                "sessionId": "sess-2",
                "update": {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": "old"},
                },
                "_meta": {"eventId": "old-1"},
            },
        }
        updates.write_text(json.dumps(old) + "\n", encoding="utf-8")

        received: list[dict] = []

        async def on_event(session_id: str, msg: dict) -> None:
            received.append(msg)

        tailer = SessionTailer(tmp_path, on_event=on_event, poll_interval=0.05)
        await tailer.start()
        try:
            await tailer.ensure_watching("sess-2", session_dir)
            await asyncio.sleep(0.2)
            assert received == []
        finally:
            await tailer.stop()

    asyncio.run(_run())


def _acp_line(session_id: str, text: str, event_id: str) -> str:
    return (
        json.dumps(
            {
                "timestamp": 1,
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": text},
                    },
                    "_meta": {"eventId": event_id},
                },
            }
        )
        + "\n"
    )


def test_session_tailer_resumes_offset_after_stop(tmp_path: Path) -> None:
    """Lines written while unsubscribed must be delivered on resubscribe.

    Root bug: open_at_end after stop_watching skipped mid-disconnect writes.
    """

    async def _run() -> None:
        session_dir = tmp_path / "sess-resume"
        session_dir.mkdir()
        updates = session_dir / "updates.jsonl"
        # Pre-existing history (should be skipped on first open_at_end)
        updates.write_text(
            _acp_line("sess-resume", "hist-1", "h1") + _acp_line("sess-resume", "hist-2", "h2"),
            encoding="utf-8",
        )

        received: list[str] = []

        async def on_event(session_id: str, msg: dict) -> None:
            text = (
                ((msg.get("params") or {}).get("update") or {}).get("content") or {}
            ).get("text")
            if text:
                received.append(str(text))

        tailer = SessionTailer(tmp_path, on_event=on_event, poll_interval=0.05)
        await tailer.start()
        try:
            # 1-2: first watch opens at EOF; history not replayed
            await tailer.ensure_watching("sess-resume", session_dir)
            await asyncio.sleep(0.15)
            assert received == []

            # 3-4: append while watching → callback gets it
            with updates.open("a", encoding="utf-8") as f:
                f.write(_acp_line("sess-resume", "live-1", "live-1"))
            for _ in range(40):
                if "live-1" in received:
                    break
                await asyncio.sleep(0.05)
            assert "live-1" in received
            assert tailer.get_offset("sess-resume") is not None

            # 5: stop watch, append while stopped (the disconnect gap)
            await tailer.stop_watching("sess-resume")
            assert not tailer.is_watching("sess-resume")
            with updates.open("a", encoding="utf-8") as f:
                f.write(_acp_line("sess-resume", "while-stopped", "stopped-1"))

            # 6: resubscribe must resume stored offset and emit the gap line
            await tailer.ensure_watching("sess-resume", session_dir)
            for _ in range(40):
                if "while-stopped" in received:
                    break
                await asyncio.sleep(0.05)
            assert "while-stopped" in received, (
                f"expected catch-up of line written while stopped; got {received!r}"
            )
            # Must not replay pre-watch history
            assert "hist-1" not in received
            assert "hist-2" not in received
        finally:
            await tailer.stop()

    asyncio.run(_run())


def test_session_tailer_offset_clamps_when_file_shrinks(tmp_path: Path) -> None:
    async def _run() -> None:
        session_dir = tmp_path / "sess-shrink"
        session_dir.mkdir()
        updates = session_dir / "updates.jsonl"
        updates.write_text(_acp_line("sess-shrink", "a", "a1"), encoding="utf-8")

        received: list[str] = []

        async def on_event(session_id: str, msg: dict) -> None:
            text = (
                ((msg.get("params") or {}).get("update") or {}).get("content") or {}
            ).get("text")
            if text:
                received.append(str(text))

        tailer = SessionTailer(tmp_path, on_event=on_event, poll_interval=0.05)
        await tailer.start()
        try:
            await tailer.ensure_watching("sess-shrink", session_dir)
            with updates.open("a", encoding="utf-8") as f:
                f.write(_acp_line("sess-shrink", "b", "b1"))
            for _ in range(40):
                if "b" in received:
                    break
                await asyncio.sleep(0.05)
            assert "b" in received
            await tailer.stop_watching("sess-shrink")

            # Replace with shorter file
            updates.write_text(_acp_line("sess-shrink", "new", "n1"), encoding="utf-8")
            await tailer.ensure_watching("sess-shrink", session_dir)
            await asyncio.sleep(0.15)
            # Clamped resume should not blow up; new content after resume is live-only
            with updates.open("a", encoding="utf-8") as f:
                f.write(_acp_line("sess-shrink", "after", "a2"))
            for _ in range(40):
                if "after" in received:
                    break
                await asyncio.sleep(0.05)
            assert "after" in received
        finally:
            await tailer.stop()

    asyncio.run(_run())
