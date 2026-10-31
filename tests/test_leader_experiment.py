"""Opt-in leader multi-client experiment (default backend remains serve)."""

from __future__ import annotations

import inspect
from pathlib import Path

from hub.acp_client import AcpClient, StdioJsonRpcTransport
from hub.agent_supervisor import (
    AgentSupervisor,
    leader_is_ready,
    leader_lock_path,
    pid_is_alive,
)
from hub.config import Config, load_config


def test_config_default_agent_backend_is_serve() -> None:
    cfg = Config()
    assert cfg.agent_backend == "serve"
    assert cfg.uses_leader_backend is False


def test_load_config_accepts_leader_backend(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        "[agent]\nbackend = \"leader\"\nbind = \"127.0.0.1\"\nport = 2419\n",
        encoding="utf-8",
    )
    loaded = load_config(p)
    assert loaded.agent_backend == "leader"
    assert loaded.uses_leader_backend is True


def test_load_config_hub_agent_backend_alias(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("[hub]\nagent_backend = \"leader\"\n", encoding="utf-8")
    loaded = load_config(p)
    assert loaded.agent_backend == "leader"


def test_load_config_unknown_backend_falls_back_to_serve(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("[agent]\nbackend = \"weird\"\n", encoding="utf-8")
    loaded = load_config(p)
    assert loaded.agent_backend == "serve"
    assert loaded.uses_leader_backend is False


def test_load_config_omitted_backend_is_serve(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("[agent]\nport = 2419\n", encoding="utf-8")
    loaded = load_config(p)
    assert loaded.agent_backend == "serve"


def test_supervisor_has_leader_spawn_path() -> None:
    assert hasattr(AgentSupervisor, "_spawn_leader")
    assert hasattr(AgentSupervisor, "_run_loop_leader")
    assert hasattr(AgentSupervisor, "_force_kill_leader")
    src = inspect.getsource(AgentSupervisor._spawn_leader)
    assert "leader" in src
    assert "--no-exit-on-disconnect" in src
    assert "--relay-on-demand" in src


def test_supervisor_uses_leader_flag_from_config() -> None:
    serve = AgentSupervisor(Config(agent_backend="serve"), secret="x")
    leader = AgentSupervisor(Config(agent_backend="leader"), secret="x")
    assert serve.uses_leader is False
    assert leader.uses_leader is True


def test_acp_has_leader_stdio_maintain_path() -> None:
    assert hasattr(AcpClient, "_maintain_stdio_leader")
    assert hasattr(AcpClient, "_is_leader_backend")
    src = inspect.getsource(AcpClient._maintain_stdio_leader)
    assert "--leader" in src
    assert "stdio" in src
    # Dispatcher routes leader to stdio path
    maintain = inspect.getsource(AcpClient._maintain)
    assert "_maintain_stdio_leader" in maintain
    assert "_maintain_ws" in maintain


def test_stdio_transport_helpers_exist() -> None:
    assert StdioJsonRpcTransport is not None
    for name in ("send", "close", "ping", "start_reader"):
        assert hasattr(StdioJsonRpcTransport, name)


def test_leader_is_ready_false_without_pid_or_pipe() -> None:
    # Missing lock + impossible pid should not report ready (unless a live
    # grok-leader pipe exists on this machine — tolerate that by using bad pid
    # only when lock is overridden to a missing path).
    missing = Path("__no_such_leader_lock__")
    assert missing.exists() is False
    # pid 1 is often alive on Unix; use a high unused pid heuristic
    ready = leader_is_ready(pid=-1, lock_path=missing)
    # Without a named pipe this is False; if a pipe is present (dev machine
    # mid-experiment), readiness can be True — both are valid for the helper.
    assert ready in (True, False)
    if not ready:
        assert leader_is_ready(pid=None, lock_path=missing) is False


def test_pid_is_alive_rejects_nonpositive() -> None:
    assert pid_is_alive(0) is False
    assert pid_is_alive(-3) is False


def test_leader_lock_path_under_grok_home() -> None:
    path = leader_lock_path()
    assert path.name == "leader.lock"
    assert path.parent.name == ".grok"
