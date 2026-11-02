from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from hub.config import Config

log = logging.getLogger("hub.agent")

StatusCallback = Callable[[str], Awaitable[None] | None]

# Windows process-creation flags for a hub-independent agent process.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# Experiment: leader lock under ~/.grok (see logs/exp-leader/FINDINGS.md).
_LEADER_LOCK_NAME = "leader.lock"
_LEADER_PIPE_PREFIX = "grok-leader"


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _windows_spawn_flags() -> int:
    """Flags so agent is not torn down with the hub process tree."""
    return (
        _CREATE_NEW_PROCESS_GROUP
        | _DETACHED_PROCESS
        | _CREATE_NO_WINDOW
        | _CREATE_BREAKAWAY_FROM_JOB
    )


def leader_lock_path() -> Path:
    """Path to grok leader lock file (~/.grok/leader.lock)."""
    return Path.home() / ".grok" / _LEADER_LOCK_NAME


def pid_is_alive(pid: int) -> bool:
    """True if *pid* looks like a live OS process (best-effort, cross-platform)."""
    if pid is None or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return int(code.value) == STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def leader_named_pipe_present() -> bool:
    """True if a Windows named pipe matching grok-leader* exists."""
    if sys.platform != "win32":
        return False
    try:
        # \\.\pipe\ listing
        for name in os.listdir(r"\\.\pipe"):
            if _LEADER_PIPE_PREFIX in str(name).lower():
                return True
    except Exception:
        pass
    return False


def leader_is_ready(
    *,
    pid: int | None = None,
    lock_path: Path | None = None,
) -> bool:
    """Leader readiness: live pid, lock pid, or leader named pipe.

    Used by AgentSupervisor is_up/wait for agent_backend=leader.
    """
    if pid is not None and pid_is_alive(pid):
        return True
    path = lock_path if lock_path is not None else leader_lock_path()
    try:
        if path.is_file():
            raw = path.read_text(encoding="utf-8", errors="replace").strip()
            # lock may be bare pid or key=value; take first integer token
            for tok in raw.replace("=", " ").replace(",", " ").split():
                if tok.isdigit():
                    if pid_is_alive(int(tok)):
                        return True
                    break
    except OSError:
        pass
    if leader_named_pipe_present():
        return True
    return False


class AgentSupervisor:
    """Owns agent backend lifecycle with restart backoff.

    Default backend is ``agent serve`` (TCP WS). Opt-in ``agent_backend=leader``
    spawns ``grok agent leader`` and treats lock/pipe as readiness (experiment).

    By default, hub stop leaves a surviving agent process running so code
    deploys / hub bounce do not cold-kill serve. Full agent teardown only when
    stop(kill_agent=True) or ops scripts pass -KillAgent.
    """

    def __init__(self, config: Config, secret: str, on_status: StatusCallback | None = None):
        self.config = config
        self.secret = secret
        self.on_status = on_status
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._started_by_us = False
        self._agent_pid: int | None = None
        self.status: str = "down"  # up | down
        self._log_path: Path | None = None

    @property
    def uses_leader(self) -> bool:
        return bool(getattr(self.config, "uses_leader_backend", False))

    @property
    def pid_path(self) -> Path:
        return self.config.log_dir / "agent.pid"

    @property
    def is_up(self) -> bool:
        return self.status == "up"

    async def _emit(self, status: str) -> None:
        self.status = status
        if self.on_status:
            result = self.on_status(status)
            if asyncio.iscoroutine(result):
                await result

    def _write_pid_file(self, pid: int) -> None:
        try:
            self.config.log_dir.mkdir(parents=True, exist_ok=True)
            self.pid_path.write_text(str(pid) + "\n", encoding="ascii")
            self._agent_pid = pid
        except OSError as exc:
            log.warning("Failed to write agent pid file: %s", exc)

    def _clear_pid_file(self) -> None:
        self._agent_pid = None
        try:
            if self.pid_path.is_file():
                self.pid_path.unlink()
        except OSError:
            pass

    def _read_pid_file(self) -> int | None:
        try:
            if not self.pid_path.is_file():
                return None
            raw = self.pid_path.read_text(encoding="ascii").strip()
            if raw.isdigit():
                return int(raw)
        except OSError:
            pass
        return None

    async def start(self) -> None:
        self._stop.clear()
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        self._log_path = self.config.log_dir / f"agent-{day}.log"
        self._task = asyncio.create_task(self._run_loop(), name="agent-supervisor")

    async def stop(self, kill_agent: bool = False) -> None:
        """Stop supervisor loop. Leave agent process up unless kill_agent=True.

        Default False: hub exit / restart keeps agent serve (phone continuity).
        """
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if kill_agent:
            await self._kill_proc()
        else:
            # Drop handle without terminating; next hub attaches via open port.
            self._proc = None
            log.info(
                "Hub supervisor stop: keeping agent (kill_agent=False); "
                "pid=%s port=%s",
                self._agent_pid or self._read_pid_file(),
                self.config.agent_port,
            )
        await self._emit("down")

    async def _kill_proc(self) -> None:
        """Kill only an agent process this supervisor started (or recorded pid)."""
        proc = self._proc
        self._proc = None
        pid: int | None = None
        if proc is not None and self._started_by_us:
            pid = proc.pid
            try:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
            except ProcessLookupError:
                pass
            except Exception as exc:
                log.warning("Failed to stop agent process: %s", exc)
        elif self._started_by_us:
            pid = self._agent_pid or self._read_pid_file()
            if pid is not None:
                await self._kill_pid(pid)
        # Attached external agent (_started_by_us False): never kill.
        self._clear_pid_file()
        self._started_by_us = False

    async def _kill_pid(self, pid: int) -> None:
        try:
            if sys.platform == "win32":
                # Prefer taskkill so tree children of grok launcher die too.
                proc = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            else:
                os.kill(pid, 15)
                await asyncio.sleep(0.5)
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
        except ProcessLookupError:
            pass
        except Exception as exc:
            log.warning("Failed to kill agent pid %s: %s", pid, exc)

    async def _pids_listening_on_port(self, port: int) -> list[int]:
        """PIDs with a TCP LISTEN socket on ``port`` (any local bind)."""
        found: list[int] = []
        try:
            if sys.platform == "win32":
                proc = await asyncio.create_subprocess_exec(
                    "netstat",
                    "-ano",
                    "-p",
                    "tcp",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await proc.communicate()
                text = out.decode("utf-8", errors="replace")
                needle = f":{int(port)}"
                for line in text.splitlines():
                    upper = line.upper()
                    if "LISTENING" not in upper:
                        continue
                    parts = line.split()
                    if len(parts) < 5:
                        continue
                    local = parts[1] if len(parts) > 1 else ""
                    if not local.endswith(needle):
                        # Also match 0.0.0.0:port / [::]:port style already via endswith
                        if needle not in local:
                            continue
                        # Require port as full local-port segment (avoid :24190)
                        if not (
                            local.endswith(needle)
                            or local.endswith(f"]{needle}")
                        ):
                            continue
                    try:
                        pid = int(parts[-1])
                    except ValueError:
                        continue
                    if pid > 0 and pid not in found:
                        found.append(pid)
            else:
                # Prefer ss; fall back to lsof.
                for cmd in (
                    ("ss", "-lptn", f"sport = :{int(port)}"),
                    ("lsof", "-ti", f":{int(port)}", "-sTCP:LISTEN"),
                ):
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            *cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        out, _ = await proc.communicate()
                    except FileNotFoundError:
                        continue
                    text = out.decode("utf-8", errors="replace")
                    if cmd[0] == "lsof":
                        for tok in text.split():
                            if tok.isdigit():
                                pid = int(tok)
                                if pid > 0 and pid not in found:
                                    found.append(pid)
                    else:
                        # ss: users:(("name",pid=123,fd=4))
                        for m in re.finditer(r"pid=(\d+)", text):
                            pid = int(m.group(1))
                            if pid > 0 and pid not in found:
                                found.append(pid)
                    if found:
                        break
        except Exception as exc:
            log.warning("Failed to enumerate listeners on port %s: %s", port, exc)
        return found

    def _backend_ready(self) -> bool:
        """Process-level readiness for the configured backend."""
        if self.uses_leader:
            return leader_is_ready(
                pid=self._agent_pid or self._read_pid_file(),
                lock_path=leader_lock_path(),
            )
        return _port_open(self.config.agent_bind, self.config.agent_port)

    async def force_kill_agent(self) -> bool:
        """Kill agent regardless of attach vs hub-spawned ownership.

        Serve: hard kill of the port holder.
        Leader: ``grok leader kill`` plus recorded pid tree.
        Returns True if at least one kill was attempted.
        """
        if self.uses_leader:
            return await self._force_kill_leader()

        pids: list[int] = []
        seen: set[int] = set()

        def _add(pid: int | None) -> None:
            if pid is None or pid <= 0 or pid in seen:
                return
            seen.add(pid)
            pids.append(pid)

        if self._proc is not None and self._proc.pid:
            _add(self._proc.pid)
        _add(self._agent_pid)
        _add(self._read_pid_file())
        for pid in await self._pids_listening_on_port(self.config.agent_port):
            _add(pid)

        if not pids:
            log.warning(
                "force_kill_agent: no pid/port listener for agent port %s",
                self.config.agent_port,
            )
            self._proc = None
            self._clear_pid_file()
            # Ensure spawn loop can create a new serve if port is free.
            self._started_by_us = False
            if self.status != "down":
                await self._emit("down")
            return False

        log.warning(
            "force_kill_agent: killing pids=%s port=%s (attached_or_owned)",
            pids,
            self.config.agent_port,
        )
        for pid in pids:
            await self._kill_pid(pid)

        self._proc = None
        self._clear_pid_file()
        # After external attach kill, loop must be free to spawn.
        self._started_by_us = False
        if self.status != "down":
            await self._emit("down")
        return True

    async def _force_kill_leader(self) -> bool:
        """Kill leader process tree carefully (leader kill CLI + pid)."""
        attempted = False
        pids: list[int] = []
        seen: set[int] = set()

        def _add(pid: int | None) -> None:
            if pid is None or pid <= 0 or pid in seen:
                return
            seen.add(pid)
            pids.append(pid)

        if self._proc is not None and self._proc.pid:
            _add(self._proc.pid)
        _add(self._agent_pid)
        _add(self._read_pid_file())
        try:
            lock = leader_lock_path()
            if lock.is_file():
                raw = lock.read_text(encoding="utf-8", errors="replace").strip()
                for tok in raw.replace("=", " ").replace(",", " ").split():
                    if tok.isdigit():
                        _add(int(tok))
                        break
        except OSError:
            pass

        # Prefer grok's own cleanup first (clears lock).
        try:
            proc = await asyncio.create_subprocess_exec(
                self.config.grok_bin,
                "leader",
                "kill",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            attempted = True
            log.warning("force_kill_agent(leader): ran grok leader kill")
        except Exception as exc:
            log.warning("force_kill_agent(leader): leader kill failed: %s", exc)

        if pids:
            log.warning("force_kill_agent(leader): killing pids=%s", pids)
            for pid in pids:
                await self._kill_pid(pid)
            attempted = True

        if not attempted:
            log.warning("force_kill_agent(leader): no pid and leader kill failed")

        self._proc = None
        self._clear_pid_file()
        self._started_by_us = False
        if self.status != "down":
            await self._emit("down")
        return attempted

    async def force_restart(self, wait_up_timeout: float = 40.0) -> bool:
        """Kill backend, then wait until process readiness is up again.

        Serve: TCP port. Leader: lock/pipe/pid. ACP not yet verified.
        """
        if self.uses_leader:
            log.warning(
                "force_restart(leader): begin wait_up=%.1fs",
                wait_up_timeout,
            )
        else:
            log.warning(
                "force_restart: begin port=%s wait_up=%.1fs",
                self.config.agent_port,
                wait_up_timeout,
            )
        await self.force_kill_agent()
        # Allow the OS to release the bind / lock before polling / spawn.
        await asyncio.sleep(0.4)
        up = await self.wait_until_up(timeout=wait_up_timeout)
        if up:
            if self.uses_leader:
                log.info(
                    "force_restart(leader): ready again "
                    "(process/lock/pipe only; ACP not yet verified)"
                )
            else:
                log.info(
                    "force_restart: agent port %s is up again "
                    "(TCP only; ACP not yet verified)",
                    self.config.agent_port,
                )
        else:
            if self.uses_leader:
                log.error(
                    "force_restart(leader): still down after %.1fs",
                    wait_up_timeout,
                )
            else:
                log.error(
                    "force_restart: agent port %s still down after %.1fs",
                    self.config.agent_port,
                    wait_up_timeout,
                )
        return up

    async def wait_until_up(self, timeout: float = 30.0) -> bool:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if self._backend_ready():
                if self.status != "up":
                    await self._emit("up")
                return True
            await asyncio.sleep(0.25)
        return False

    async def _run_loop(self) -> None:
        if self.uses_leader:
            await self._run_loop_leader()
            return
        await self._run_loop_serve()

    async def _run_loop_serve(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if _port_open(self.config.agent_bind, self.config.agent_port):
                    self._started_by_us = False
                    self._proc = None
                    log.info(
                        "Agent already listening on %s:%s (attach, not spawn)",
                        self.config.agent_bind,
                        self.config.agent_port,
                    )
                    await self._emit("up")
                    while not self._stop.is_set() and _port_open(
                        self.config.agent_bind, self.config.agent_port
                    ):
                        await asyncio.sleep(2.0)
                    if self._stop.is_set():
                        break
                    log.warning("External agent port closed; will restart")
                    await self._emit("down")
                    continue

                await self._spawn()
                if self._proc is None and self._agent_pid is None:
                    await self._emit("down")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue

                # Wait until port open or process dies
                up = await self.wait_until_up(timeout=20.0)
                if not up:
                    log.error("Agent failed to open port")
                    await self._kill_proc()
                    await self._emit("down")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue

                backoff = 1.0
                await self._emit("up")
                if self._proc is not None:
                    code = await self._proc.wait()
                    self._proc = None
                    log.warning("Agent process exited with code %s", code)
                else:
                    # Detached without wait handle: poll port until down.
                    while not self._stop.is_set() and _port_open(
                        self.config.agent_bind, self.config.agent_port
                    ):
                        await asyncio.sleep(2.0)
                    if self._stop.is_set():
                        break
                    log.warning("Agent port closed (detached serve)")
                self._clear_pid_file()
                await self._emit("down")
                if self._stop.is_set():
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Agent supervisor error: %s", exc)
                await self._emit("down")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _run_loop_leader(self) -> None:
        """Spawn/attach loop for agent_backend=leader (no TCP port)."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if leader_is_ready(
                    pid=self._agent_pid or self._read_pid_file(),
                    lock_path=leader_lock_path(),
                ):
                    # Existing leader (CLI or prior hub): attach, do not spawn.
                    if not self._started_by_us:
                        self._proc = None
                    log.info(
                        "Leader already ready (lock/pipe/pid); attach, not spawn"
                    )
                    await self._emit("up")
                    while not self._stop.is_set() and leader_is_ready(
                        pid=self._agent_pid or self._read_pid_file(),
                        lock_path=leader_lock_path(),
                    ):
                        await asyncio.sleep(2.0)
                    if self._stop.is_set():
                        break
                    log.warning("Leader went down; will restart")
                    await self._emit("down")
                    continue

                await self._spawn_leader()
                if self._proc is None and self._agent_pid is None:
                    await self._emit("down")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue

                up = await self.wait_until_up(timeout=20.0)
                if not up:
                    log.error("Leader failed to become ready (lock/pipe)")
                    await self._kill_proc()
                    await self._emit("down")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue

                backoff = 1.0
                await self._emit("up")
                if self._proc is not None:
                    code = await self._proc.wait()
                    self._proc = None
                    log.warning("Leader process exited with code %s", code)
                else:
                    while not self._stop.is_set() and leader_is_ready(
                        pid=self._agent_pid or self._read_pid_file(),
                        lock_path=leader_lock_path(),
                    ):
                        await asyncio.sleep(2.0)
                    if self._stop.is_set():
                        break
                    log.warning("Leader readiness lost (detached)")
                self._clear_pid_file()
                await self._emit("down")
                if self._stop.is_set():
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Leader supervisor error: %s", exc)
                await self._emit("down")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _spawn_detached(self, cmd: list[str], log_label: str) -> None:
        """Shared detached spawn for serve and leader backends."""
        self._log_path = self._log_path or (self.config.log_dir / "agent.log")
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        log_f = open(self._log_path, "a", encoding="utf-8", errors="replace")
        try:
            kwargs: dict = {
                "stdout": log_f,
                "stderr": asyncio.subprocess.STDOUT,
                "stdin": asyncio.subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                flags = _windows_spawn_flags()
                try:
                    self._proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        creationflags=flags,
                        **kwargs,
                    )
                except OSError as exc:
                    log.warning(
                        "Detached spawn with breakaway failed (%s); retry without breakaway",
                        exc,
                    )
                    flags = (
                        _CREATE_NEW_PROCESS_GROUP
                        | _DETACHED_PROCESS
                        | _CREATE_NO_WINDOW
                    )
                    self._proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        creationflags=flags,
                        **kwargs,
                    )
            else:
                kwargs["start_new_session"] = True
                self._proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)

            self._started_by_us = True
            if self._proc is not None and self._proc.pid:
                self._write_pid_file(self._proc.pid)
                log.info(
                    "%s spawned pid=%s (detached=%s)",
                    log_label,
                    self._proc.pid,
                    sys.platform == "win32",
                )
        except FileNotFoundError:
            log.error("grok binary not found: %s", self.config.grok_bin)
            log_f.close()
            self._proc = None
            self._started_by_us = False
            self._clear_pid_file()
        except Exception:
            log_f.close()
            raise

    def _agent_options(self) -> list[str]:
        """`grok agent` options that precede the mode. Gate plugin: ADR-027."""
        opts = ["--always-approve"]
        plugin_dir = Path(self.config.gate_plugin_dir)
        if (plugin_dir / "hooks" / "hooks.json").is_file():
            opts += ["--plugin-dir", str(plugin_dir)]
        return opts

    async def _spawn(self) -> None:
        bind = f"{self.config.agent_bind}:{self.config.agent_port}"
        cmd = [
            self.config.grok_bin,
            "agent",
            *self._agent_options(),
            "serve",
            "--bind",
            bind,
            "--secret",
            self.secret,
        ]
        log.info(
            "Starting agent: %s agent %s serve --bind %s --secret <redacted>",
            self.config.grok_bin,
            " ".join(cmd[2 : cmd.index("serve")]),
            bind,
        )
        await self._spawn_detached(cmd, "Agent")

    async def _spawn_leader(self) -> None:
        """Spawn ``grok agent leader`` detached (experiment backend)."""
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        debug_file = self.config.log_dir / f"agent-leader-{day}.log"
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.config.grok_bin,
            "agent",
            *self._agent_options(),
            "leader",
            "--no-exit-on-disconnect",
            "--relay-on-demand",
            "--debug-file",
            str(debug_file),
        ]
        log.info(
            "Starting leader: %s agent %s leader "
            "--no-exit-on-disconnect --relay-on-demand --debug-file %s",
            self.config.grok_bin,
            " ".join(cmd[2 : cmd.index("leader")]),
            debug_file.name,
        )
        self._log_path = self.config.log_dir / f"agent-{day}.log"
        await self._spawn_detached(cmd, "Leader")
