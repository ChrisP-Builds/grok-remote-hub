"""Tests for hub version info and structural compatibility checks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import hub
from hub.version_info import (
    HUB_VERSION,
    get_cli_version,
    parse_version_first_line,
    structural_compat,
)

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def test_hub_version_exported() -> None:
    assert HUB_VERSION == hub.__version__
    assert hub.__version__ == "0.4.0"


def test_parse_version_first_line() -> None:
    assert parse_version_first_line("grok 0.2.93 (abc) [stable]\nmore") == "grok 0.2.93 (abc) [stable]"
    assert parse_version_first_line("\n\n  v1.2.3  \n") == "v1.2.3"
    assert parse_version_first_line("") is None
    assert parse_version_first_line(None) is None
    assert parse_version_first_line("   \n  ") is None


def test_get_cli_version_missing_binary() -> None:
    assert get_cli_version("") is None
    assert get_cli_version("definitely-not-a-real-grok-binary-xyz") is None


def test_structural_compat_ok_when_healthy(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    cfg = SimpleNamespace(
        grok_bin="grok",
        agent_bind="127.0.0.1",
        agent_port=1,  # unlikely open; agent_up overrides for agentUp check
        sessions_root=sessions,
        static_dir=STATIC,
    )
    result = structural_compat(
        cfg,
        agent_up=True,
        acp_connected=True,
        cli_version="grok 0.2.93 (test)",
    )
    assert result["ok"] is True
    assert result["level"] == "structural"
    assert result["productTag"] == "remote-stream"
    assert result["hubVersion"] == "0.4.0"
    assert result["cliVersion"] == "grok 0.2.93 (test)"
    assert result["checks"]["agentUp"] is True
    assert result["checks"]["acpConnected"] is True
    assert result["checks"]["sessionNew"] is True
    assert result["checks"]["canPrompt"] == "unknown"
    assert result["checks"]["sessionsRootReadable"] is True
    assert result["checks"]["staticPresent"] is True
    assert result["issues"] == []


def test_structural_compat_issues_when_agent_down(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    cfg = SimpleNamespace(
        grok_bin="grok",
        agent_bind="127.0.0.1",
        agent_port=1,
        sessions_root=sessions,
        static_dir=STATIC,
    )
    result = structural_compat(
        cfg,
        agent_up=False,
        acp_connected=False,
        cli_version="",  # force unavailable (None would re-probe PATH)
    )
    assert result["ok"] is False
    issues = result["issues"]
    assert any("cli version" in i for i in issues)
    assert any("agent" in i for i in issues)
    assert any("acp" in i for i in issues)
    assert result["checks"]["canPrompt"] == "unknown"
    assert result["checks"]["sessionNew"] is False


def test_structural_compat_missing_static(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    empty_static = tmp_path / "static"
    empty_static.mkdir()
    cfg = SimpleNamespace(
        grok_bin="grok",
        agent_bind="127.0.0.1",
        agent_port=1,
        sessions_root=sessions,
        static_dir=empty_static,
    )
    result = structural_compat(
        cfg,
        agent_up=True,
        acp_connected=True,
        cli_version="0.1.0",
    )
    assert result["ok"] is False
    assert any("static" in i for i in result["issues"])
