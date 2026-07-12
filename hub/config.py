from __future__ import annotations

import os
import secrets
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
DEFAULT_SESSIONS_ROOT = Path.home() / ".grok" / "sessions"
# Portable default; override in config.toml (see config.example.toml).
DEFAULT_PROJECTS_ROOT = Path.home() / "Projects"
DEFAULT_AGENT_SECRET_PATH = PROJECT_ROOT / "data" / "agent.secret"
DEFAULT_GROK_BIN_CANDIDATES = (
    Path.home() / ".grok" / "bin" / "grok.exe",
    Path.home() / ".grok" / "bin" / "grok",
)
TAILSCALE_EXE = Path(r"C:\Program Files\Tailscale\tailscale.exe")


def _default_grok_bin() -> str:
    found = shutil.which("grok")
    if found:
        return found
    for candidate in DEFAULT_GROK_BIN_CANDIDATES:
        if candidate.is_file():
            return str(candidate)
    return "grok"


@dataclass
class Config:
    bind_host: str = ""
    bind_port: int = 8787
    agent_bind: str = "127.0.0.1"
    agent_port: int = 2419
    grok_bin: str = field(default_factory=_default_grok_bin)
    sessions_root: Path = field(default_factory=lambda: DEFAULT_SESSIONS_ROOT)
    projects_root: Path = field(default_factory=lambda: DEFAULT_PROJECTS_ROOT)
    hub_token: str = ""
    agent_secret_path: Path = field(default_factory=lambda: DEFAULT_AGENT_SECRET_PATH)
    log_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "logs")
    static_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "static")
    max_sessions: int = 80
    max_history_messages: int = 800
    # Concurrent live turns across different project cwds (default 3).
    max_concurrent_turns: int = 3

    @property
    def agent_ws_url(self) -> str:
        return f"ws://{self.agent_bind}:{self.agent_port}/ws"

    def ensure_agent_secret(self) -> str:
        path = self.agent_secret_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            secret = path.read_text(encoding="utf-8").strip()
            if secret:
                return secret
        secret = secrets.token_urlsafe(32)
        path.write_text(secret + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return secret


def _as_path(value: object, default: Path) -> Path:
    if value is None or value == "":
        return default
    return Path(str(value)).expanduser()


def load_config(path: Path | None = None) -> Config:
    cfg_path = path or DEFAULT_CONFIG_PATH
    data: dict = {}
    if cfg_path.is_file():
        with cfg_path.open("rb") as f:
            data = tomllib.load(f) or {}

    hub = data.get("hub", data)
    agent = data.get("agent", {})

    return Config(
        bind_host=str(hub.get("bind_host", "")),
        bind_port=int(hub.get("bind_port", 8787)),
        agent_bind=str(agent.get("bind", hub.get("agent_bind", "127.0.0.1"))),
        agent_port=int(agent.get("port", hub.get("agent_port", 2419))),
        grok_bin=str(hub.get("grok_bin", agent.get("grok_bin", _default_grok_bin()))),
        sessions_root=_as_path(hub.get("sessions_root"), DEFAULT_SESSIONS_ROOT),
        projects_root=_as_path(hub.get("projects_root"), DEFAULT_PROJECTS_ROOT),
        hub_token=str(hub.get("hub_token", "") or ""),
        agent_secret_path=_as_path(
            hub.get("agent_secret_path", agent.get("secret_path")),
            DEFAULT_AGENT_SECRET_PATH,
        ),
        log_dir=_as_path(hub.get("log_dir"), PROJECT_ROOT / "logs"),
        static_dir=_as_path(hub.get("static_dir"), PROJECT_ROOT / "static"),
        max_sessions=int(hub.get("max_sessions", 80)),
        max_history_messages=int(hub.get("max_history_messages", 800)),
        max_concurrent_turns=int(hub.get("max_concurrent_turns", 3)),
    )
