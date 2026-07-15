"""Unit tests for structured ACP trace ring buffer."""

from __future__ import annotations

import json
from pathlib import Path

from hub.acp_trace import AcpTrace, session_id_slice


def test_emit_and_snapshot_order(tmp_path: Path) -> None:
    tr = AcpTrace(log_dir=tmp_path, capacity=10)
    tr.emit("connect", host="127.0.0.1")
    tr.emit("initialize_ok")
    tr.emit("send_ok", method="session/prompt")
    events = tr.snapshot(100)
    assert len(events) == 3
    assert [e["event"] for e in events] == [
        "connect",
        "initialize_ok",
        "send_ok",
    ]
    assert "ts" in events[0]
    assert events[0]["host"] == "127.0.0.1"


def test_ring_capacity_drops_oldest() -> None:
    tr = AcpTrace(log_dir=None, capacity=3)
    for i in range(5):
        tr.emit("recv", seq=i)
    events = tr.snapshot(100)
    assert len(events) == 3
    assert [e["seq"] for e in events] == [2, 3, 4]


def test_snapshot_n_limits() -> None:
    tr = AcpTrace(capacity=50)
    for i in range(10):
        tr.emit("quality", n=i)
    assert len(tr.snapshot(3)) == 3
    assert tr.snapshot(3)[-1]["n"] == 9
    assert tr.snapshot(0) == []


def test_emit_never_raises_on_bad_fields(tmp_path: Path) -> None:
    tr = AcpTrace(log_dir=tmp_path, capacity=5)

    class Bad:
        def __str__(self) -> str:
            raise RuntimeError("nope")

    # Should not throw even with awkward values.
    rec = tr.emit("probe_fail", weird=Bad(), nested={"a": [1, 2, float("nan")]})
    assert rec["event"] == "probe_fail"
    snap = tr.snapshot(1)
    assert len(snap) == 1


def test_redacts_secret_like_fields() -> None:
    tr = AcpTrace(capacity=5)
    tr.emit("connect", token="super-secret", prompt="full user text")
    e = tr.snapshot(1)[0]
    assert e["token"] == "[redacted]"
    assert e["prompt"] == "[redacted]"


def test_truncates_long_strings() -> None:
    tr = AcpTrace(capacity=5)
    long = "x" * 500
    tr.emit("recv", method=long)
    e = tr.snapshot(1)[0]
    assert len(e["method"]) < 220
    assert e["method"].endswith("…")


def test_jsonl_written(tmp_path: Path) -> None:
    tr = AcpTrace(log_dir=tmp_path, capacity=10)
    tr.emit("heal_ok", attempts=1)
    files = list(tmp_path.glob("acp-trace-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "heal_ok"
    assert row["attempts"] == 1


def test_clear() -> None:
    tr = AcpTrace(capacity=5)
    tr.emit("connect")
    tr.clear()
    assert tr.snapshot(10) == []


def test_session_id_slice() -> None:
    assert session_id_slice(None) is None
    assert session_id_slice("abc") == "abc"
    assert session_id_slice("abcdefghijklmnop", 8) == "abcdefgh"
