"""Structural checks for external hub health watchdog + client hard recovery."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def test_watch_hub_script_exists_and_contracts() -> None:
    path = ROOT / "watch-hub.ps1"
    assert path.is_file(), "watch-hub.ps1 must exist at repo root"
    text = path.read_text(encoding="utf-8")
    assert "MaxFails" in text
    assert "CooldownMinutes" in text
    assert "restart-hub.ps1" in text
    assert "KeepAgent" in text
    assert "/health" in text
    assert "watch-hub-state.json" in text
    assert "Invoke-RestMethod" in text
    assert "TimeoutSec" in text
    # Always exit 0 for scheduled-task quiet
    assert "exit 0" in text


def test_install_startup_registers_watch_task() -> None:
    path = ROOT / "install-startup.ps1"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "GrokRemoteHubWatch" in text
    assert "GrokRemoteHub" in text
    assert "watch-hub.ps1" in text
    assert "start-hub.ps1" in text
    # Silent launch for shipped UX (no console flash every 2 minutes)
    assert "watch-hub-hidden.vbs" in text
    assert "wscript" in text.lower()
    assert "start-hub-hidden.vbs" in text


def test_silent_vbs_launchers_exist() -> None:
    watch_vbs = (ROOT / "watch-hub-hidden.vbs").read_text(encoding="utf-8", errors="replace")
    start_vbs = (ROOT / "start-hub-hidden.vbs").read_text(encoding="utf-8", errors="replace")
    assert "watch-hub.ps1" in watch_vbs
    assert "WindowStyle" in watch_vbs or ", 0," in watch_vbs or "Run cmd, 0" in watch_vbs
    assert "sh.Run" in watch_vbs
    assert "start-hub.ps1" in start_vbs
    assert "sh.Run" in start_vbs


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
    assert "watch-hub" in text.lower()
    readme = (ROOT / "docs" / "adr" / "README.md").read_text(encoding="utf-8")
    assert "019" in readme
    assert "watchdog" in readme.lower() or "watch-hub" in readme.lower()
