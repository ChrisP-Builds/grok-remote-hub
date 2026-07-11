"""Hub / CLI version awareness and structural compatibility smoke checks."""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path
from typing import Any

from hub import __version__ as HUB_VERSION

__all__ = [
    "HUB_VERSION",
    "get_cli_version",
    "parse_version_first_line",
    "structural_compat",
]


def parse_version_first_line(text: str | None) -> str | None:
    """Return the first non-empty line of version output, or None."""
    if not text:
        return None
    for line in str(text).splitlines():
        s = line.strip()
        if s:
            return s
    return None


def get_cli_version(grok_bin: str) -> str | None:
    """Run ``grok --version`` and return the first line (timeout 5s)."""
    if not grok_bin:
        return None
    try:
        proc = subprocess.run(
            [grok_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return parse_version_first_line(out)


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _sessions_root_readable(path: Path) -> bool:
    try:
        if not path.is_dir():
            return False
        return os.access(path, os.R_OK)
    except OSError:
        return False


def _static_present(static_dir: Path) -> bool:
    try:
        if not static_dir.is_dir():
            return False
        return (
            (static_dir / "index.html").is_file()
            and (static_dir / "app.js").is_file()
            and (static_dir / "app.css").is_file()
        )
    except OSError:
        return False


def structural_compat(
    config: Any,
    *,
    agent_up: bool,
    acp_connected: bool,
    cli_version: str | None = None,
) -> dict[str, Any]:
    """Free structural smoke: versions, agent/ACP, sessions root, static files.

    Does not run model prompts (``canPrompt`` stays ``"unknown"``).
    ``sessionNew`` is true when ACP is already connected (initialize done).
    """
    if cli_version is None:
        cli_version = get_cli_version(getattr(config, "grok_bin", "") or "")

    agent_bind = str(getattr(config, "agent_bind", "127.0.0.1") or "127.0.0.1")
    agent_port = int(getattr(config, "agent_port", 2419) or 2419)
    sessions_root = Path(getattr(config, "sessions_root", Path.home() / ".grok" / "sessions"))
    static_dir = Path(getattr(config, "static_dir", Path("static")))

    agent_port_open = _port_open(agent_bind, agent_port)
    sessions_ok = _sessions_root_readable(sessions_root)
    static_ok = _static_present(static_dir)
    agent_ok = bool(agent_up) or agent_port_open
    acp_ok = bool(acp_connected)

    checks: dict[str, Any] = {
        "cliVersion": cli_version,
        "hubVersion": HUB_VERSION,
        "agentUp": bool(agent_up),
        "agentPortOpen": agent_port_open,
        "acpConnected": acp_ok,
        "sessionNew": acp_ok,  # initialize already done when ACP is connected
        "canPrompt": "unknown",  # structural only; no model call
        "sessionsRootReadable": sessions_ok,
        "staticPresent": static_ok,
    }

    issues: list[str] = []
    if not cli_version:
        issues.append("cli version unavailable")
    if not agent_ok:
        issues.append("agent process/port down")
    if not acp_ok:
        issues.append("acp not connected")
    if not sessions_ok:
        issues.append("sessions root not readable")
    if not static_ok:
        issues.append("static UI files missing")

    return {
        "ok": len(issues) == 0,
        "level": "structural",
        "productTag": "remote-stream",
        "hubVersion": HUB_VERSION,
        "cliVersion": cli_version,
        "checks": checks,
        "issues": issues,
    }
