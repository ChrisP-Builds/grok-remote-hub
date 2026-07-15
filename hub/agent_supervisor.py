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


class AgentSupervisor:
    """Owns `grok agent serve` lifecycle with restart backoff.

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

    async def force_kill_agent(self) -> bool:
        """Kill agent regardless of attach vs hub-spawned ownership.

        Hung ACP with process still listening needs a hard kill of the port
        holder, not only processes where ``_started_by_us`` is True.
        Returns True if at least one kill was attempted.
        """
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

    async def force_restart(self, wait_up_timeout: float = 40.0) -> bool:
        """Kill whatever holds the agent port, then wait until it is open again.

        Relies on the supervisor ``_run_loop`` spawn path after the listener dies.
        """
        log.warning(
            "force_restart: begin port=%s wait_up=%.1fs",
            self.config.agent_port,
            wait_up_timeout,
        )
        await self.force_kill_agent()
        # Allow the OS to release the bind before polling / spawn.
        await asyncio.sleep(0.4)
        up = await self.wait_until_up(timeout=wait_up_timeout)
        if up:
            log.info(
                "force_restart: agent port %s is up again",
                self.config.agent_port,
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
            if _port_open(self.config.agent_bind, self.config.agent_port):
                if self.status != "up":
                    await self._emit("up")
                return True
            await asyncio.sleep(0.25)
        return False

    async def _run_loop(self) -> None:
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

    async def _spawn(self) -> None:
        bind = f"{self.config.agent_bind}:{self.config.agent_port}"
        # --always-approve is a `grok agent` option (before mode), not a serve flag
        cmd = [
            self.config.grok_bin,
            "agent",
            "--always-approve",
            "serve",
            "--bind",
            bind,
            "--secret",
            self.secret,
        ]
        log.info(
            "Starting agent: %s agent --always-approve serve --bind %s --secret <redacted>",
            self.config.grok_bin,
            bind,
        )
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
                # Detached so hub force-kill / job teardown does not take agent.
                flags = _windows_spawn_flags()
                try:
                    self._proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        creationflags=flags,
                        **kwargs,
                    )
                except OSError as exc:
                    # Breakaway may be denied; retry without it.
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
                # Start new session so SIGHUP to hub does not kill agent.
                kwargs["start_new_session"] = True
                self._proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)

            self._started_by_us = True
            if self._proc is not None and self._proc.pid:
                self._write_pid_file(self._proc.pid)
                log.info("Agent spawned pid=%s (detached=%s)", self._proc.pid, sys.platform == "win32")
        except FileNotFoundError:
            log.error("grok binary not found: %s", self.config.grok_bin)
            log_f.close()
            self._proc = None
            self._started_by_us = False
            self._clear_pid_file()
        except Exception:
            log_f.close()
            raise
