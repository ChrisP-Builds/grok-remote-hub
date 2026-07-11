"""Minimal ACP client terminal/* implementation for the hub."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any

log = logging.getLogger("hub.acp.terminal")

DEFAULT_OUTPUT_BYTE_LIMIT = 1_000_000
WAIT_EXIT_CAP_SECONDS = 120.0


class ManagedTerminal:
    def __init__(
        self,
        terminal_id: str,
        process: asyncio.subprocess.Process,
        output_byte_limit: int,
    ):
        self.terminal_id = terminal_id
        self.process = process
        self.output_byte_limit = max(0, output_byte_limit)
        self._buf = bytearray()
        self._truncated = False
        self._lock = asyncio.Lock()
        self._exit_code: int | None = None
        self._signal: str | None = None
        self._done = asyncio.Event()
        self._pump_task: asyncio.Task | None = None

    def start_pump(self) -> None:
        self._pump_task = asyncio.create_task(self._pump(), name=f"term-pump-{self.terminal_id[:8]}")

    async def _pump(self) -> None:
        proc = self.process
        try:
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(8192)
                if not chunk:
                    break
                async with self._lock:
                    self._append(chunk)
            rc = await proc.wait()
            self._exit_code = int(rc) if rc is not None else None
            if rc and rc < 0:
                # POSIX signal encoding when available
                self._signal = str(-rc)
                self._exit_code = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("terminal pump error %s: %s", self.terminal_id, exc)
            try:
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
            except Exception:
                pass
            if self._exit_code is None and proc.returncode is not None:
                self._exit_code = int(proc.returncode)
        finally:
            self._done.set()

    def _append(self, chunk: bytes) -> None:
        if self.output_byte_limit <= 0:
            self._truncated = True
            self._buf.clear()
            return
        self._buf.extend(chunk)
        if len(self._buf) > self.output_byte_limit:
            # Truncate from the beginning at a character boundary.
            overflow = len(self._buf) - self.output_byte_limit
            del self._buf[:overflow]
            # Drop partial UTF-8 lead if needed
            while self._buf and (self._buf[0] & 0xC0) == 0x80:
                del self._buf[0]
            self._truncated = True

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            text = bytes(self._buf).decode("utf-8", errors="replace")
            truncated = self._truncated
        exit_status = None
        if self._done.is_set():
            exit_status = {
                "exitCode": self._exit_code,
                "signal": self._signal,
            }
        return {
            "output": text,
            "truncated": truncated,
            "exitStatus": exit_status,
        }

    async def wait_for_exit(self, timeout: float = WAIT_EXIT_CAP_SECONDS) -> dict[str, Any]:
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            await self.kill()
            try:
                await asyncio.wait_for(self._done.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
        return {
            "exitCode": self._exit_code,
            "signal": self._signal,
        }

    async def kill(self) -> None:
        proc = self.process
        if proc.returncode is not None:
            return
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception as exc:
            log.debug("kill terminal %s: %s", self.terminal_id, exc)

    async def release(self) -> None:
        await self.kill()
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._pump_task = None
        # Close pipes
        try:
            if self.process.stdout:
                self.process.stdout.close()
        except Exception:
            pass
        self._done.set()


class TerminalManager:
    """terminalId -> ManagedTerminal."""

    def __init__(self) -> None:
        self._terms: dict[str, ManagedTerminal] = {}

    async def create(self, params: dict[str, Any]) -> dict[str, Any]:
        command = params.get("command")
        if not command or not isinstance(command, str):
            raise ValueError("terminal/create requires string command")
        args = params.get("args") or []
        if not isinstance(args, list):
            raise ValueError("args must be a list of strings")
        args = [str(a) for a in args]
        cwd = params.get("cwd")
        if cwd is not None and cwd != "":
            cwd = str(cwd)
            if not os.path.isabs(cwd):
                raise ValueError("cwd must be an absolute path")
        else:
            cwd = None

        env = os.environ.copy()
        raw_env = params.get("env") or []
        if isinstance(raw_env, list):
            for item in raw_env:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                value = item.get("value")
                if name is not None and value is not None:
                    env[str(name)] = str(value)
        elif isinstance(raw_env, dict):
            for k, v in raw_env.items():
                env[str(k)] = str(v)

        limit = params.get("outputByteLimit")
        if limit is None:
            limit = DEFAULT_OUTPUT_BYTE_LIMIT
        try:
            limit_i = int(limit)
        except (TypeError, ValueError):
            limit_i = DEFAULT_OUTPUT_BYTE_LIMIT

        # Prefer exec when we have a real executable + args. Use shell when the
        # command string looks like a pipeline / bare shell builtin (dir, Get-ChildItem).
        use_shell = (not args) and (
            any(ch in command for ch in ("|", ">", "<", "&", ";", "\n"))
            or (" " in command and not os.path.isfile(command))
        )
        if use_shell:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
            )
        else:
            try:
                proc = await asyncio.create_subprocess_exec(
                    command,
                    *args,
                    cwd=cwd,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    stdin=asyncio.subprocess.DEVNULL,
                )
            except FileNotFoundError:
                # Windows often needs shell for bare commands like `dir`
                shell_cmd = command if not args else " ".join([command, *args])
                proc = await asyncio.create_subprocess_shell(
                    shell_cmd,
                    cwd=cwd,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    stdin=asyncio.subprocess.DEVNULL,
                )

        term_id = f"term_{uuid.uuid4().hex[:16]}"
        managed = ManagedTerminal(term_id, proc, limit_i)
        managed.start_pump()
        self._terms[term_id] = managed
        log.info("terminal created id=%s cmd=%s", term_id, command)
        return {"terminalId": term_id}

    def get(self, terminal_id: str) -> ManagedTerminal:
        term = self._terms.get(terminal_id)
        if term is None:
            raise KeyError(f"unknown terminalId: {terminal_id}")
        return term

    async def output(self, terminal_id: str) -> dict[str, Any]:
        return await self.get(terminal_id).snapshot()

    async def wait_for_exit(self, terminal_id: str) -> dict[str, Any]:
        return await self.get(terminal_id).wait_for_exit()

    async def kill(self, terminal_id: str) -> dict[str, Any]:
        await self.get(terminal_id).kill()
        return {}

    async def release(self, terminal_id: str) -> dict[str, Any]:
        term = self._terms.pop(terminal_id, None)
        if term is not None:
            await term.release()
        return {}

    async def close_all(self) -> None:
        ids = list(self._terms.keys())
        for tid in ids:
            try:
                await self.release(tid)
            except Exception:
                pass
