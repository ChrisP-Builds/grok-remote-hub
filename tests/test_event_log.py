"""Unit tests for the per-session event log and the resume decision (ADR-029)."""

from __future__ import annotations

import json

from hub.event_log import (
    SEQUENCED_TYPES,
    EventLog,
    decide_resume,
    replay_frame,
    serialize,
)


def _acp(session_id: str, text: str) -> dict:
    return {
        "type": "acp",
        "sessionId": session_id,
        "message": {"method": "session/update", "params": {"update": {"text": text}}},
    }


def test_sequenced_types_match_adr() -> None:
    assert SEQUENCED_TYPES == {"acp", "turn", "system", "terminal_out", "compact", "usage", "commands"}
    assert "history" not in SEQUENCED_TYPES


def test_append_stamps_seq_and_returns_the_logged_text() -> None:
    log = EventLog()
    first = _acp("a", "one")
    second = _acp("b", "two")
    logged = log.append("a", first)
    assert logged.seq == 1 and first["seq"] == 1
    assert logged.text == json.dumps(first, default=str) == serialize(first)
    assert log.append("b", second).seq == 2 and second["seq"] == 2
    assert log.seq == 2


def test_head_is_zero_for_unknown_and_latest_after_append() -> None:
    log = EventLog()
    assert log.head("nope") == 0
    log.append("a", _acp("a", "x"))
    log.append("b", _acp("b", "y"))
    log.append("a", _acp("a", "z"))
    assert log.head("a") == 3
    assert log.head("b") == 2


def test_since_returns_only_newer_events_of_that_session_in_order() -> None:
    log = EventLog()
    texts = [log.append("a", _acp("a", f"a{i}")).text for i in range(3)]
    log.append("b", _acp("b", "other"))
    log.append("a", _acp("a", "a3"))
    got = log.since("a", 1)
    assert got is not None
    assert [e.seq for e in got] == [2, 3, 5]
    assert [e.text for e in got][:2] == texts[1:]
    assert all(json.loads(e.text)["sessionId"] == "a" for e in got)
    assert log.since("a", 5) == []
    assert [e.seq for e in log.since("a", 0) or []] == [1, 2, 3, 5]


def test_since_for_unknown_session_is_empty_not_a_gap() -> None:
    log = EventLog()
    assert log.since("never", 0) == []
    log.append("a", _acp("a", "x"))
    assert log.since("never", 1) == []


def test_since_rejects_future_and_negative_seq() -> None:
    log = EventLog()
    log.append("a", _acp("a", "x"))
    assert log.since("a", 2) is None
    assert log.since("never", 1) == []
    assert log.since("never", 2) is None
    assert log.since("a", -1) is None


def test_event_cap_evicts_oldest_and_reports_a_gap_below_it() -> None:
    log = EventLog(max_events=3)
    for i in range(5):
        log.append("a", _acp("a", str(i)))
    assert log.head("a") == 5
    assert log.since("a", 0) is None
    assert log.since("a", 1) is None
    assert [e.seq for e in log.since("a", 2) or []] == [3, 4, 5]
    assert log.since("a", 5) == []


def test_byte_cap_evicts_oldest_but_always_keeps_the_newest_event() -> None:
    log = EventLog(max_bytes=200)
    small = log.append("a", _acp("a", "s"))
    assert len(small.text) < 200
    big = log.append("a", _acp("a", "x" * 500))
    assert len(big.text) > 200
    assert log.since("a", 1) is not None
    assert [e.seq for e in log.since("a", 1) or []] == [2]
    assert log.since("a", 0) is None
    log.append("a", _acp("a", "t"))
    assert [e.seq for e in log.since("a", 2) or []] == [3]
    assert log.since("a", 1) is None


def test_session_lru_eviction_keeps_gap_knowledge() -> None:
    log = EventLog(max_sessions=2)
    log.append("a", _acp("a", "1"))
    log.append("a", _acp("a", "2"))
    log.append("b", _acp("b", "3"))
    log.append("c", _acp("c", "4"))
    assert log.head("a") == 2
    assert log.since("a", 0) is None
    assert log.since("a", 1) is None
    assert log.since("a", 2) == []
    log.append("a", _acp("a", "5"))
    assert [e.seq for e in log.since("a", 2) or []] == [5]
    assert log.since("a", 1) is None
    assert log.head("b") == 3
    assert log.since("b", 3) is None or log.since("b", 3) == []


def test_lru_order_is_by_last_append_not_first() -> None:
    log = EventLog(max_sessions=2)
    log.append("a", _acp("a", "1"))
    log.append("b", _acp("b", "2"))
    log.append("a", _acp("a", "3"))
    log.append("c", _acp("c", "4"))
    assert log.since("b", 0) is None
    assert [e.seq for e in log.since("a", 0) or []] == [1, 3]


def test_decide_resume_without_a_request_is_fallback_none() -> None:
    log = EventLog()
    log.append("a", _acp("a", "1"))
    for since, boot in ((None, None), (None, "boot"), (1, None), (1, ""), ("1", "boot"), (True, "boot"), (1.5, "boot"), (-1, "boot")):
        d = decide_resume(log, "a", since, boot, "boot")
        assert d.replay is False and d.reason == "none", (since, boot)
        assert d.events == []


def test_decide_resume_boot_mismatch_and_gap() -> None:
    log = EventLog(max_events=1)
    log.append("a", _acp("a", "1"))
    log.append("a", _acp("a", "2"))
    stale = decide_resume(log, "a", 1, "old", "boot")
    assert stale.replay is False and stale.reason == "boot" and stale.since_seq == 1
    gap = decide_resume(log, "a", 0, "boot", "boot")
    assert gap.replay is False and gap.reason == "gap"
    future = decide_resume(log, "a", 99, "boot", "boot")
    assert future.replay is False and future.reason == "gap"


def test_decide_resume_ok_returns_exactly_the_missed_events() -> None:
    log = EventLog()
    log.append("a", _acp("a", "1"))
    missed = [log.append("a", _acp("a", "2")), log.append("a", _acp("a", "3"))]
    log.append("b", _acp("b", "4"))
    d = decide_resume(log, "a", 1, "boot", "boot")
    assert d.replay is True and d.reason == "" and d.since_seq == 1
    assert d.events == missed
    current = decide_resume(log, "a", 3, "boot", "boot")
    assert current.replay is True and current.events == []


def test_replay_frame_is_one_json_message_with_payloads_verbatim() -> None:
    log = EventLog()
    log.append("a", _acp("a", "0"))
    payloads = [_acp("a", "1"), {"type": "turn", "sessionId": "a", "state": "idle", "error": None}]
    events = [log.append("a", p) for p in payloads]
    frame = replay_frame("a", 1, events)
    for e in events:
        assert e.text in frame
    parsed = json.loads(frame)
    assert parsed["type"] == "replay" and parsed["sessionId"] == "a"
    assert parsed["fromSeq"] == 1 and parsed["toSeq"] == 3 and parsed["count"] == 2
    assert parsed["events"] == payloads
    assert [e["seq"] for e in parsed["events"]] == [2, 3]


def test_replay_frame_empty_range() -> None:
    parsed = json.loads(replay_frame("a", 7, []))
    assert parsed == {"type": "replay", "sessionId": "a", "fromSeq": 7, "toSeq": 7, "count": 0, "events": []}
