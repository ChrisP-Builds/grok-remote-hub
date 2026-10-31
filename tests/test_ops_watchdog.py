"""Structural checks for external hub health watchdog + client hard recovery."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def test_watchdog_module_exists_and_contracts() -> None:
    path = ROOT / "hub" / "watchdog.py"
    assert path.is_file(), "hub/watchdog.py must exist"
    text = path.read_text(encoding="utf-8")
    assert "watch-hub-state.json" in text
    assert "KeepAgent" in text
    assert "/health" in text
    assert "CREATE_NO_WINDOW" in text or "0x08000000" in text
    assert "max" in text.lower() and "fail" in text.lower()
    assert "cooldown" in text.lower()
    assert "daemon" in text.lower() or "run_daemon" in text or "interval" in text
    assert "watchdog.pid" in text
    assert "restart-hub.ps1" in text
    assert "__main__" in text


def test_watchdog_pure_helpers(tmp_path: Path) -> None:
    from hub import watchdog as wd

    assert wd.parse_bind_port("bind_port = 9999\n") == 9999
    assert wd.parse_bind_port("  bind_port= 8788 ") == 8788
    assert wd.parse_bind_port("# no port") == 8787
    assert wd.parse_bind_port("") == 8787

    assert wd.cooldown_elapsed(None, 5) is True
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace(
        "+00:00", "Z"
    )
    recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace(
        "+00:00", "Z"
    )
    assert wd.cooldown_elapsed(past, 5) is True
    assert wd.cooldown_elapsed(recent, 5) is False
    assert wd.cooldown_elapsed("not-a-date", 5) is True

    state_path = tmp_path / "watch-hub-state.json"
    state = wd.default_state()
    state["consecutiveFails"] = 2
    state["lastError"] = "boom"
    state["mode"] = "daemon"
    state["pid"] = 12345
    state["pollIntervalSeconds"] = 60
    state["lastCheckAt"] = wd.utc_now_iso()
    wd.write_state(state_path, state)
    loaded = wd.read_state(state_path)
    assert loaded["consecutiveFails"] == 2
    assert loaded["lastError"] == "boom"
    assert loaded["mode"] == "daemon"
    assert loaded["pid"] == 12345
    assert loaded["pollIntervalSeconds"] == 60
    assert loaded["lastOkAt"] is None
    assert loaded["lastRestartAt"] is None

    missing = wd.read_state(tmp_path / "nope.json")
    assert missing["consecutiveFails"] == 0
    assert set(missing).issuperset(
        {"consecutiveFails", "lastRestartAt", "lastOkAt", "lastError"}
    )


def test_watch_hub_script_wrapper() -> None:
    path = ROOT / "watch-hub.ps1"
    assert path.is_file(), "watch-hub.ps1 must exist at repo root"
    text = path.read_text(encoding="utf-8")
    assert "MaxFails" in text
    assert "CooldownMinutes" in text
    assert "hub.watchdog" in text
    assert "--once" in text
    assert "exit 0" in text


def test_install_startup_registers_watch_task() -> None:
    path = ROOT / "install-startup.ps1"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "GrokRemoteHubWatch" in text
    assert "GrokRemoteHub" in text
    assert "start-hub.ps1" in text
    assert "watchdog-hidden.vbs" in text
    assert "wscript" in text.lower()
    assert "start-hub-hidden.vbs" in text
    assert "AtLogOn" in text or "at logon" in text.lower()
    assert "pythonw" in text.lower() or "hub.watchdog" in text
    # Primary is long-lived logon daemon, not every-2-minute PS poll
    assert "RepetitionInterval" not in text
    assert "every 2 minutes" not in text.lower() or "not every-2-minute" in text.lower()


def test_silent_vbs_launchers_exist() -> None:
    wd_vbs = (ROOT / "watchdog-hidden.vbs").read_text(encoding="utf-8", errors="replace")
    start_vbs = (ROOT / "start-hub-hidden.vbs").read_text(encoding="utf-8", errors="replace")
    assert "hub.watchdog" in wd_vbs
    assert "pythonw" in wd_vbs.lower()
    assert ", 0," in wd_vbs or "Run cmd, 0" in wd_vbs or "sh.Run" in wd_vbs
    assert "sh.Run" in wd_vbs
    assert "start-hub.ps1" in start_vbs
    assert "sh.Run" in start_vbs
    # Legacy one-shot VBS may remain; new task uses watchdog-hidden.vbs
    legacy = ROOT / "watch-hub-hidden.vbs"
    if legacy.is_file():
        legacy_text = legacy.read_text(encoding="utf-8", errors="replace")
        assert "watch-hub" in legacy_text.lower() or "hub.watchdog" in legacy_text


def test_start_hub_ensures_watchdog() -> None:
    text = (ROOT / "start-hub.ps1").read_text(encoding="utf-8")
    assert "Get-HubProcesses" in text
    assert "hub.watchdog" in text or "hub\\.watchdog" in text
    assert "Start-WatchdogSilent" in text or "watchdog" in text.lower()
    assert "pythonw" in text.lower() or "Win32_Process" in text
    # Must not treat watchdog as main hub process
    assert "notmatch" in text.lower() or "hub\\.watchdog" in text


def test_stop_hub_excludes_watchdog_from_hub_kill_then_stops_watchdog() -> None:
    text = (ROOT / "stop-hub.ps1").read_text(encoding="utf-8")
    assert "hub.watchdog" in text or "hub\\.watchdog" in text
    assert "watchdog.pid" in text
    # Full stop still stops watchdog
    assert "Watchdog stopped" in text
    # KeepAgent (restart) path must not kill the watchdog process itself
    assert "Keeping watchdog" in text
    assert "-not $KeepAgent" in text
    # bind_port from config.toml (not sole hardcoded 8787 path)
    assert "bind_port" in text
    assert "Select-String" in text
    assert "LocalPort $port" in text or "LocalPort `$port" in text
    assert "$port" in text


def test_main_has_connection_reset_handler() -> None:
    src = (ROOT / "hub" / "__main__.py").read_text(encoding="utf-8")
    assert "_ignore_proactor_reset" in src
    assert "ConnectionResetError" in src
    assert "set_exception_handler" in src
    assert "10054" in src


def test_app_js_hard_reload_after_reconnect_exhaustion() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "_didHardReload" in js
    assert "maybeHardRecoverFromDeadHub" in js
    assert "location.reload()" in js
    assert "_hubUnreachableSince" in js
    # Thresholds from design
    assert "HARD_RECONNECT_ATTEMPTS" in js or "reconnectAttempt >=" in js
    assert "30000" in js or "UNREACHABLE_MS" in js
    # bootId hard reload still present
    assert "grh.bootReload." in js
    # Force connect when health ok but WS down
    assert "connectWs()" in js
    assert "_lastForceConnectAt" in js


def test_start_hub_waits_for_port_after_kill() -> None:
    text = (ROOT / "start-hub.ps1").read_text(encoding="utf-8")
    assert "port free" in text.lower() or "portFreeDeadline" in text or "AddSeconds(5)" in text
    assert "Get-HubProcesses" in text


def test_restart_hub_accepts_keep_agent() -> None:
    text = (ROOT / "restart-hub.ps1").read_text(encoding="utf-8")
    assert "KeepAgent" in text


def test_adr_019_external_watchdog() -> None:
    path = ROOT / "docs" / "adr" / "019-external-hub-health-watchdog.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "Accepted" in text
    assert "KeepAgent" in text
    assert "watch-hub" in text.lower() or "hub.watchdog" in text
    assert "pythonw" in text.lower() or "hub.watchdog" in text
    readme = (ROOT / "docs" / "adr" / "README.md").read_text(encoding="utf-8")
    assert "019" in readme
    assert "watchdog" in readme.lower() or "watch-hub" in readme.lower()
