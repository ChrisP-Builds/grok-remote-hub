"""Unit + structural tests for hub-hosted terminal/* live output streaming."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hub.acp_terminal import ManagedTerminal, TerminalManager, utf8_delta

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
HUB = ROOT / "hub"


def test_utf8_delta_ascii() -> None:
    carry = bytearray()
    assert utf8_delta(b"hello", carry) == "hello"
    assert carry == b""


def test_utf8_delta_empty() -> None:
    carry = bytearray()
    assert utf8_delta(b"", carry) == ""
    assert carry == b""


def test_utf8_delta_split_multibyte() -> None:
    """€ is U+20AC → UTF-8 e2 82 ac; split across chunks."""
    euro = "€".encode("utf-8")
    assert euro == b"\xe2\x82\xac"
    carry = bytearray()
    assert utf8_delta(euro[:1], carry) == ""
    assert bytes(carry) == b"\xe2"
    assert utf8_delta(euro[1:2], carry) == ""
    assert bytes(carry) == b"\xe2\x82"
    assert utf8_delta(euro[2:], carry) == "€"
    assert carry == b""


def test_utf8_delta_invalid_replaced() -> None:
    carry = bytearray()
    # Lone continuation byte is invalid → replacement char
    out = utf8_delta(b"\x80ok", carry)
    assert "\ufffd" in out or out.endswith("ok")
    assert "ok" in out
    assert carry == b""


def test_utf8_delta_carry_then_ascii() -> None:
    carry = bytearray()
    utf8_delta(b"\xe2", carry)
    # Incomplete lead abandoned when followed by ASCII that completes? No —
    # carry stays until enough bytes or we flush. Next chunk completes.
    out = utf8_delta(b"\x82\xacHi", carry)
    assert out == "€Hi"
    assert carry == b""


def test_managed_terminal_notify_output_invokes_callback() -> None:
    seen: list[tuple[str, str, str | None]] = []

    def on_out(tid: str, delta: str, sid: str | None) -> None:
        seen.append((tid, delta, sid))

    proc = SimpleNamespace(stdout=None, returncode=0)
    mt = ManagedTerminal(
        "term_abc",
        proc,  # type: ignore[arg-type]
        1_000_000,
        session_id="sess-1",
        on_output=on_out,
    )
    mt.notify_output("line1\n")
    assert seen == [("term_abc", "line1\n", "sess-1")]


def test_manager_on_output_forwarded_via_notify() -> None:
    seen: list[tuple[str, str, str | None]] = []
    mgr = TerminalManager()
    mgr.on_output = lambda tid, d, sid: seen.append((tid, d, sid))

    proc = SimpleNamespace(stdout=None, returncode=0)
    mt = ManagedTerminal(
        "term_xyz",
        proc,  # type: ignore[arg-type]
        1000,
        session_id="s2",
        on_output=mgr._forward_output,
    )
    mgr._terms["term_xyz"] = mt
    mt.notify_output("hello")
    assert seen == [("term_xyz", "hello", "s2")]


def test_create_passes_session_id_and_fires_on_output() -> None:
    """Integration-light: real subprocess, sessionId stored, on_output called."""

    async def _run() -> None:
        seen: list[dict[str, Any]] = []

        def on_out(tid: str, delta: str, sid: str | None) -> None:
            seen.append({"terminalId": tid, "delta": delta, "sessionId": sid})

        mgr = TerminalManager()
        mgr.on_output = on_out
        # Cross-platform: same interpreter prints one line then exits.
        import sys

        result = await mgr.create(
            {
                "command": sys.executable,
                "args": ["-c", "print('pump-ok', flush=True)"],
                "sessionId": "session-create-1",
            }
        )
        tid = result["terminalId"]
        assert tid.startswith("term_")
        term = mgr.get(tid)
        assert term.session_id == "session-create-1"
        await term.wait_for_exit(timeout=15.0)
        await asyncio.sleep(0.05)
        joined = "".join(x["delta"] for x in seen if x["terminalId"] == tid)
        assert "pump-ok" in joined
        assert all(
            x["sessionId"] == "session-create-1"
            for x in seen
            if x["terminalId"] == tid
        )
        await mgr.release(tid)

    asyncio.run(_run())


def test_js_handles_terminal_out() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'type === "terminal_out"' in js
    assert "function onTerminalOut" in js
    assert "term:" in js
    assert "TERM_OUT_MAX_CHARS" in js
    assert "streamBuffers.terminals" in js or "terminals: new Map()" in js
    assert 'label: "terminal"' in js or 'label: "terminal"' in js.replace(" ", "")
    assert "noteTermLineActivity" in js[js.find("function onTerminalOut") :]


def test_server_broadcasts_terminal_out() -> None:
    server = (HUB / "server.py").read_text(encoding="utf-8")
    assert '"type": "terminal_out"' in server or "'type': 'terminal_out'" in server
    assert "on_terminal_out" in server
    assert "_on_terminal_out" in server
    assert "terminal_out" in server
    # Scoped fanout like acp
    assert "terminal_out" in server[server.find("scoped") : server.find("scoped") + 200]


def test_acp_client_wires_on_terminal_out() -> None:
    client = (HUB / "acp_client.py").read_text(encoding="utf-8")
    assert "on_terminal_out" in client
    assert "_on_terminal_output_chunk" in client
    assert "self._terminals.on_output" in client


def test_css_terminal_tool_body() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "[data-terminal=" in css
    assert "var(--font)" in css
    idx = css.find("[data-terminal=")
    chunk = css[idx : idx + 180]
    assert "pre-wrap" in chunk
