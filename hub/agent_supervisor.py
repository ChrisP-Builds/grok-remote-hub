from __future__ import annotations

import asyncio
import logging
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from hub.config import Config

log = logging.getLogger("hub.agent")

StatusCallback = Callable[[str], Awaitable[None] | None]


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class AgentSupervisor:
    """Owns `grok agent serve` lifecycle with restart backoff."""

    def __init__(self, config: Config, secret: str, on_status: StatusCallback | None = None):
        self.config = config
        self.secret = secret
        self.on_status = on_status
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._started_by_us = False
        self.status: str = "down"  # up | down
        self._log_path: Path | None = None

    @property
    def is_up(self) -> bool:
        return self.status == "up"

    async def _emit(self, status: str) -> None:
        self.status = status
        if self.on_status:
            result = self.on_status(status)
            if asyncio.iscoroutine(result):
                await result

    async def start(self) -> None:
        self._stop.clear()
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        self._log_path = self.config.log_dir / f"agent-{day}.log"
        self._task = asyncio.create_task(self._run_loop(), name="agent-supervisor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._kill_proc()
        await self._emit("down")

    async def _kill_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if not proc or not self._started_by_us:
            return
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
                    log.info(
                        "Agent already listening on %s:%s",
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
                if self._proc is None:
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
                assert self._proc is not None
                code = await self._proc.wait()
                self._proc = None
                log.warning("Agent process exited with code %s", code)
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
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_f,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
            )
            self._started_by_us = True
        except FileNotFoundError:
            log.error("grok binary not found: %s", self.config.grok_bin)
            log_f.close()
            self._proc = None
            self._started_by_us = False
        except Exception:
            log_f.close()
            raise
