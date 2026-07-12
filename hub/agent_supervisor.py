from __future__ import annotations

import asyncio
import logging
import os
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
