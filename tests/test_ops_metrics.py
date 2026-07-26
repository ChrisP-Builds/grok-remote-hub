"""Unit + structural tests for process-local OpsMetrics and diagnostics wiring."""

from __future__ import annotations

import json
from pathlib import Path

from hub.ops_metrics import DEFAULT_COUNTERS, OpsMetrics

ROOT = Path(__file__).resolve().parents[1]


def test_default_counters_present_as_zero() -> None:
    m = OpsMetrics(log_dir=None)
    snap = m.snapshot_counters()
    for name in DEFAULT_COUNTERS:
        assert name in snap
        assert snap[name] == 0


def test_inc_and_snapshot() -> None:
    m = OpsMetrics()
    m.inc("ws_connect")
    m.inc("ws_connect", 2)
    m.inc("prompt_ok")
    c = m.snapshot_counters()
    assert c["ws_connect"] == 3
    assert c["prompt_ok"] == 1
    assert c["ws_disconnect"] == 0


def test_emit_and_snapshot_events_order() -> None:
    m = OpsMetrics(capacity=10)
    m.emit("hub_start", bootId="abc")
    m.emit("ensure", action="reuse")
    m.emit("force_clear", reason="stall")
    events = m.snapshot_events(100)
    assert len(events) == 3
    assert [e["event"] for e in events] == [
        "hub_start",
        "ensure",
        "force_clear",
    ]
    assert "ts" in events[0]
    assert events[0]["bootId"] == "abc"


def test_ring_capacity_drops_oldest() -> None:
    m = OpsMetrics(capacity=3)
    for i in range(5):
        m.emit("tick", n=i)
    events = m.snapshot_events(100)
    assert len(events) == 3
    assert [e["n"] for e in events] == [2, 3, 4]


def test_snapshot_n_limits() -> None:
    m = OpsMetrics(capacity=50)
    for i in range(10):
        m.emit("e", n=i)
    assert len(m.snapshot_events(3)) == 3
    assert m.snapshot_events(3)[-1]["n"] == 9
    assert m.snapshot_events(0) == []


def test_redacts_secret_and_prompt_fields() -> None:
    m = OpsMetrics(capacity=5)
    m.emit("x", token="super-secret", prompt="full user text", text="blob")
    e = m.snapshot_events(1)[0]
    assert e["token"] == "[redacted]"
    assert e["prompt"] == "[redacted]"
    assert e["text"] == "[redacted]"


def test_truncates_long_strings() -> None:
    m = OpsMetrics(capacity=5)
    long = "x" * 500
    m.emit("recv", method=long)
    e = m.snapshot_events(1)[0]
    assert len(e["method"]) < 220
    assert e["method"].endswith("…")


def test_inc_emit_never_raise() -> None:
    m = OpsMetrics(capacity=5)

    class Bad:
        def __str__(self) -> str:
            raise RuntimeError("nope")

    m.inc("ws_connect")
    rec = m.emit("probe", weird=Bad(), nested={"a": [1, 2, float("nan")]})
    assert rec["event"] == "probe"
    assert len(m.snapshot_events(1)) == 1


def test_jsonl_written(tmp_path: Path) -> None:
    m = OpsMetrics(log_dir=tmp_path, capacity=10)
    m.emit("hub_start", bootId="xyz")
    files = list(tmp_path.glob("ops-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "hub_start"
    assert row["bootId"] == "xyz"


def test_snapshot_combined() -> None:
    m = OpsMetrics()
    m.inc("agent_restart")
    m.emit("agent_restart", reason="admin")
    snap = m.snapshot(5)
    assert snap["counters"]["agent_restart"] == 1
    assert len(snap["events"]) == 1
    assert snap["events"][0]["event"] == "agent_restart"


def test_clear() -> None:
    m = OpsMetrics()
    m.inc("ws_connect", 5)
    m.emit("x")
    m.clear()
    assert m.snapshot_counters()["ws_connect"] == 0
    assert m.snapshot_events(10) == []


def test_server_registers_diagnostics_route() -> None:
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    assert "/api/admin/diagnostics" in src
    assert "handle_diagnostics" in src
    assert "OpsMetrics" in src
    assert "self.ops" in src


def test_health_includes_ops_fields() -> None:
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    assert "opsCounters" in src
    assert "opsRecent" in src
    assert "uptimeSeconds" in src
    assert '"pid"' in src or "os.getpid()" in src
    # Shared builder used by /health
    assert "def _health_body" in src


def test_status_payload_has_ops_counters_not_events_flood() -> None:
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    # status_payload should expose counters; full event ring is health/diagnostics.
    idx = src.find("def status_payload")
    assert idx > 0
    end = src.find("\n    def ", idx + 1)
    chunk = src[idx : end if end > idx else idx + 8000]
    assert "opsCounters" in chunk
    assert "opsRecent" not in chunk  # keep WS status lean


def test_adr_020_ops_metrics() -> None:
    path = ROOT / "docs" / "adr" / "020-ops-metrics-and-diagnostics.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "Accepted" in text
    assert "OpsMetrics" in text
    assert "diagnostics" in text.lower()
    readme = (ROOT / "docs" / "adr" / "README.md").read_text(encoding="utf-8")
    assert "020" in readme
    assert "ops" in readme.lower() or "diagnostics" in readme.lower()


def test_app_js_pill_boot_uptime_tooltip() -> None:
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "boot " in js
    assert "uptimeSeconds" in js or "startedAt" in js
