"""Unit + structural tests for hub-hosted terminal/* live output streaming."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
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


def test_kill_awaited_stops_only_the_commands_the_agent_waits_on() -> None:
    """Stop must end a command blocking the turn, but a background server keeps running."""
    sleeper = {"command": sys.executable, "args": ["-c", "import time; time.sleep(60)"]}

    async def run() -> None:
        mgr = TerminalManager()
        blocking = (await mgr.create({**sleeper, "sessionId": "s-stop"}))["terminalId"]
        background = (await mgr.create({**sleeper, "sessionId": "s-stop"}))["terminalId"]
        other = (await mgr.create({**sleeper, "sessionId": "s-other"}))["terminalId"]
        other_wait = asyncio.create_task(mgr.wait_for_exit(other))
        waiting = asyncio.create_task(mgr.wait_for_exit(blocking))
        await asyncio.sleep(0.2)
        try:
            assert await mgr.kill_awaited("s-stop") == 1
            exit_info = await asyncio.wait_for(waiting, timeout=10.0)
            assert exit_info["exitCode"] != 0 or exit_info["signal"]
            assert mgr.get(background).process.returncode is None
            assert mgr.get(other).process.returncode is None
        finally:
            other_wait.cancel()
            procs = [mgr.get(t).process for t in (blocking, background, other)]
            await mgr.close_all()
            await asyncio.gather(*(p.wait() for p in procs))

    asyncio.run(run())


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _force_kill_pid(pid: int) -> None:
    import os
    import signal
    import subprocess

    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=0x08000000)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def _first_pid_in_output(mgr: TerminalManager, tid: str, timeout: float = 15.0) -> int:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for line in (await mgr.output(tid))["output"].splitlines():
            if line.strip().isdigit():
                return int(line.strip())
        await asyncio.sleep(0.05)
    raise AssertionError("child never printed its pid: " + (await mgr.output(tid))["output"])


async def _until_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if not _pid_alive(pid):
            return True
        await asyncio.sleep(0.05)
    return not _pid_alive(pid)


CHILD_THAT_SLEEPS = "import os, time; print(os.getpid(), flush=True); time.sleep(60)"


def test_kill_awaited_ends_the_whole_tree_of_a_shell_command() -> None:
    """`npm test` style commands are shell grandchildren; killing only the shell left them running
    and holding the output pipe, so wait_for_exit sat at its 120 s cap after Stop."""
    quoted = f'"{sys.executable}" -c "{CHILD_THAT_SLEEPS}"'
    # A space in the command routes it through the platform shell, as the agent's own commands are.
    command = f"& {quoted}" if sys.platform == "win32" else quoted

    async def run() -> None:
        mgr = TerminalManager()
        tid = (await mgr.create({"command": command, "sessionId": "s-tree"}))["terminalId"]
        child_pid = None
        try:
            waiting = asyncio.create_task(mgr.wait_for_exit(tid))
            child_pid = await _first_pid_in_output(mgr, tid)
            assert _pid_alive(child_pid)
            assert await mgr.kill_awaited("s-tree") == 1
            exit_info = await asyncio.wait_for(waiting, timeout=10.0)
            assert exit_info["exitCode"] != 0 or exit_info["signal"]
            assert await _until_dead(child_pid), "grandchild survived Stop"
        finally:
            if child_pid is not None and _pid_alive(child_pid):
                _force_kill_pid(child_pid)
            await mgr.close_all()

    asyncio.run(run())


def test_wait_for_exit_returns_when_the_process_exits_even_if_a_child_keeps_the_pipe() -> None:
    """A command that leaves a background child behind must not pin wait_for_exit to the output pipe."""
    spawn_and_exit = (
        "import os, subprocess, sys; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "print(p.pid, flush=True)"
    )

    async def run() -> None:
        mgr = TerminalManager()
        tid = (await mgr.create({"command": sys.executable, "args": ["-c", spawn_and_exit], "sessionId": "s-bg"}))[
            "terminalId"
        ]
        child_pid = None
        try:
            child_pid = await _first_pid_in_output(mgr, tid)
            started = asyncio.get_running_loop().time()
            exit_info = await asyncio.wait_for(mgr.wait_for_exit(tid), timeout=10.0)
            assert asyncio.get_running_loop().time() - started < 8.0
            assert exit_info["exitCode"] == 0
            assert (await mgr.output(tid))["exitStatus"] == {"exitCode": 0, "signal": None}
            assert _pid_alive(child_pid), "a background child nobody waits on must keep running"
        finally:
            if child_pid is not None:
                _force_kill_pid(child_pid)
            await mgr.close_all()

    asyncio.run(run())


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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shell selection")
def test_windows_shell_commands_run_in_powershell_like_grok(tmp_path) -> None:
    """Grok's own Shell tool runs PowerShell on Windows; cmd.exe rejects agent cmdlets."""
    (tmp_path / "keepme").mkdir()

    async def run() -> tuple[dict, str]:
        tm = TerminalManager()
        tid = (await tm.create({"command": "Remove-Item -Recurse -Force .\\keepme; Get-Date -Format yyyy", "cwd": str(tmp_path)}))["terminalId"]
        exit_info = await tm.wait_for_exit(tid)
        out = (await tm.output(tid)).get("output", "")
        await tm.close_all()
        return exit_info, out

    exit_info, out = asyncio.run(run())
    assert exit_info["exitCode"] == 0, out
    assert not (tmp_path / "keepme").exists()
    assert out.strip()[-4:].isdigit()