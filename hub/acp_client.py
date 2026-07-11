from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from websockets.asyncio.client import connect as ws_connect

from hub.acp_fs import read_text_file, write_text_file
from hub.acp_permissions import pick_permission_option
from hub.acp_terminal import TerminalManager
from hub.config import Config
from hub.session_policy import (
    MAX_TURN_SECONDS,
    MID_TURN_STALL_SECONDS,
    NO_OUTPUT_SECONDS,
    STUCK_TURN_SECONDS,
    is_turn_stuck_for_new_prompt,
    should_force_clear_turn,
)

log = logging.getLogger("hub.acp")

MessageCallback = Callable[[dict[str, Any]], Awaitable[None] | None]

# Re-export for callers/tests that still import from acp_client.
__all__ = (
    "AcpClient",
    "NO_OUTPUT_SECONDS",
    "MID_TURN_STALL_SECONDS",
    "MAX_TURN_SECONDS",
    "STUCK_TURN_SECONDS",
    "is_turn_stuck_for_new_prompt",
    "should_force_clear_turn",
    "pick_permission_option",
)


class AcpClient:
    """Sole ACP WebSocket client to grok agent serve."""

    def __init__(
        self,
        config: Config,
        secret: str,
        on_message: MessageCallback | None = None,
        on_connection: Callable[[bool], Awaitable[None] | None] | None = None,
    ):
        self.config = config
        self.secret = secret
        self.on_message = on_message
        self.on_connection = on_connection
        self._ws: Any = None
        self._maintain_task: asyncio.Task | None = None
        self._recv_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._stop = asyncio.Event()
        self.connected = False
        self.loaded_session_id: str | None = None
        self.available_commands: list[dict[str, Any]] = []
        self.turn_running = False
        self.turn_session_id: str | None = None
        self.turn_started_at: float | None = None
        # Last session/update (or prompt start) monotonic time for hang detection.
        self.last_activity_at: float | None = None
        # True once any session/update arrived during the current turn.
        self.turn_saw_update: bool = False
        self._stall_watchdog: asyncio.Task | None = None
        # Last watchdog/admin force-clear (Hub may broadcast idle from these).
        self.last_force_clear_reason: str | None = None
        self.last_force_clear_session: str | None = None
        # Session id cleared on ACP disconnect; Hub may broadcast turn idle once.
        self.disconnect_turn_session_id: str | None = None
        # Client-side terminal/* processes for advertised terminal capability.
        self._terminals = TerminalManager()

    def turn_age_seconds(self) -> float | None:
        if self.turn_started_at is None:
            return None
        return time.monotonic() - self.turn_started_at

    def is_turn_stuck(self, threshold: float | None = None) -> bool:
        """True if a running turn is dead enough to force-clear for a new prompt.

        Activity-aware (TUI-aligned): healthy long turns are not stuck.
        Uses no-output / mid-turn stall / max wall from session_policy.
        ``threshold`` is accepted for call-site compatibility and ignored;
        force-clear policy is centralized in is_turn_stuck_for_new_prompt.
        """
        del threshold  # API compat; policy is activity-aware, not short wall.
        if not self.turn_running:
            return False
        age = self.turn_age_seconds()
        # Running without a timestamp is already inconsistent; treat as stuck.
        if age is None:
            return True
        activity_at = self.last_activity_at
        age_activity = (
            (time.monotonic() - activity_at) if activity_at is not None else age
        )
        return is_turn_stuck_for_new_prompt(
            self.turn_saw_update,
            age,
            age_activity,
        )

    def force_clear_turn(self, reason: str) -> bool:
        """Force-clear turn state so a new prompt can run. Returns True if cleared.

        Also fails any pending ACP request futures so a blocked session_prompt
        can exit its lock and allow a subsequent prompt.
        """
        if not self.turn_running and self.turn_session_id is None and not self._pending:
            return False
        age = self.turn_age_seconds()
        cleared_sid = self.turn_session_id
        log.warning(
            "Force-clearing turn (session=%s age=%s pending=%d): %s",
            cleared_sid,
            f"{age:.1f}s" if age is not None else "unknown",
            len(self._pending),
            reason,
        )
        self.last_force_clear_reason = reason
        self.last_force_clear_session = cleared_sid
        # Unblock waiters (e.g. long session/prompt) so the lock is released
        for req_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(TimeoutError(f"Turn force-cleared: {reason}"))
            self._pending.pop(req_id, None)
        self.turn_running = False
        self.turn_session_id = None
        self.turn_started_at = None
        self.last_activity_at = None
        self.turn_saw_update = False
        self._cancel_stall_watchdog()
        if cleared_sid and "acp disconnect" in reason.lower():
            self.disconnect_turn_session_id = cleared_sid
        return True

    def _cancel_stall_watchdog(self) -> None:
        wd = self._stall_watchdog
        self._stall_watchdog = None
        if wd is not None and not wd.done():
            wd.cancel()

    # Back-compat alias for older call sites / tests.
    def _cancel_no_output_watchdog(self) -> None:
        self._cancel_stall_watchdog()

    def note_activity(self) -> None:
        """Record ACP session/update activity for hang detection."""
        self.last_activity_at = time.monotonic()
        if self.turn_running:
            self.turn_saw_update = True

    def _url(self) -> str:
        key = quote(self.secret, safe="")
        return f"{self.config.agent_ws_url}?server-key={key}"

    async def start(self) -> None:
        self._stop.clear()
        self._maintain_task = asyncio.create_task(self._maintain(), name="acp-client")

    async def stop(self) -> None:
        self._stop.set()
        if self._maintain_task:
            self._maintain_task.cancel()
            try:
                await self._maintain_task
            except asyncio.CancelledError:
                pass
            self._maintain_task = None
        await self._close_ws()

    async def _set_connected(self, value: bool) -> None:
        self.connected = value
        if self.on_connection:
            result = self.on_connection(value)
            if asyncio.iscoroutine(result):
                await result

    async def _close_ws(self) -> None:
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        self._recv_task = None
        ws = self._ws
        self._ws = None
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError("ACP connection closed"))
        self._pending.clear()
        try:
            await self._terminals.close_all()
        except Exception:
            pass
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self.connected:
            await self._set_connected(False)

    async def _recv_loop(self, ws: Any) -> None:
        try:
            async for raw in ws:
                if self._stop.is_set():
                    break
                await self._handle_raw(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("ACP recv loop ended: %s", exc)
            # Fail pending requests so waiters unblock
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(ConnectionError(f"ACP recv ended: {exc}"))
            self._pending.clear()

    async def _maintain(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                url = self._url()
                log.info("Connecting ACP to %s:%s", self.config.agent_bind, self.config.agent_port)
                async with ws_connect(url, open_timeout=10, max_size=16 * 1024 * 1024) as ws:
                    self._ws = ws
                    # Reader must run before initialize/request awaits responses
                    self._recv_task = asyncio.create_task(self._recv_loop(ws), name="acp-recv")
                    await self._initialize()
                    await self._set_connected(True)
                    backoff = 1.0
                    log.info("ACP connected and initialized")
                    # Stay until recv ends or stop
                    await self._recv_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("ACP connection error: %s", exc)
            finally:
                self._ws = None
                self.loaded_session_id = None
                if self.turn_running or self.turn_session_id or self._pending:
                    self.force_clear_turn("acp disconnected")
                else:
                    self.turn_running = False
                    self.turn_session_id = None
                    self.turn_started_at = None
                    self.last_activity_at = None
                    self.turn_saw_update = False
                    self._cancel_stall_watchdog()
                if self._recv_task and not self._recv_task.done():
                    self._recv_task.cancel()
                    try:
                        await self._recv_task
                    except asyncio.CancelledError:
                        pass
                self._recv_task = None
                for fut in list(self._pending.values()):
                    if not fut.done():
                        fut.set_exception(ConnectionError("ACP connection closed"))
                self._pending.clear()
                if self.connected:
                    await self._set_connected(False)
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)

    async def _initialize(self) -> None:
        await self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True,
                },
                "clientInfo": {"name": "grok-remote-hub", "version": "0.1.0"},
            },
            timeout=30.0,
        )

    async def _handle_raw(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("Non-JSON ACP frame: %s", raw[:200])
            return

        if not isinstance(msg, dict):
            return

        msg_id = msg.get("id")
        method = msg.get("method")

        # JSON-RPC response to a hub-originated request
        if msg_id is not None and ("result" in msg or "error" in msg) and not method:
            fut = self._pending.pop(msg_id, None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(str(msg["error"])))
                else:
                    fut.set_result(msg.get("result"))
            await self._fanout(msg)
            return

        # Agent -> client request (method + id, no result/error)
        if (
            msg_id is not None
            and method
            and "result" not in msg
            and "error" not in msg
        ):
            await self._handle_client_request(msg)
            await self._fanout(msg)
            return

        # Notifications / other
        await self._fanout(msg)
        await self._track_update(msg)

    async def _reply_result(self, msg_id: Any, result: Any) -> None:
        await self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})

    async def _reply_error(
        self, msg_id: Any, code: int, message: str, data: Any = None
    ) -> None:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        await self._send({"jsonrpc": "2.0", "id": msg_id, "error": err})

    async def _handle_client_request(self, msg: dict[str, Any]) -> None:
        """Respond to agent-initiated JSON-RPC requests (fs, terminal, permission)."""
        msg_id = msg.get("id")
        method = str(msg.get("method") or "")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        method_l = method.lower()
        # Client RPCs count as turn activity (avoids mid-turn stall during tools).
        self.note_activity()

        try:
            if "permission" in method_l or method_l.endswith("request_permission"):
                await self._handle_permission(msg_id, params)
                return

            if method in ("fs/read_text_file", "fs/readTextFile"):
                result = await asyncio.to_thread(read_text_file, params)
                await self._reply_result(msg_id, result)
                return

            if method in ("fs/write_text_file", "fs/writeTextFile"):
                result = await asyncio.to_thread(write_text_file, params)
                await self._reply_result(msg_id, result)
                return

            if method in ("terminal/create", "terminal/create_terminal"):
                result = await self._terminals.create(params)
                await self._reply_result(msg_id, result)
                return

            if method == "terminal/output":
                tid = str(params.get("terminalId") or params.get("terminal_id") or "")
                result = await self._terminals.output(tid)
                await self._reply_result(msg_id, result)
                return

            if method in ("terminal/wait_for_exit", "terminal/waitForExit"):
                tid = str(params.get("terminalId") or params.get("terminal_id") or "")
                result = await self._terminals.wait_for_exit(tid)
                await self._reply_result(msg_id, result)
                return

            if method == "terminal/kill":
                tid = str(params.get("terminalId") or params.get("terminal_id") or "")
                result = await self._terminals.kill(tid)
                await self._reply_result(msg_id, result)
                return

            if method == "terminal/release":
                tid = str(params.get("terminalId") or params.get("terminal_id") or "")
                result = await self._terminals.release(tid)
                await self._reply_result(msg_id, result)
                return

            log.error("Unknown ACP client method from agent: %s id=%s", method, msg_id)
            await self._reply_error(
                msg_id,
                -32801,
                f"Method not found: {method}",
            )
        except KeyError as exc:
            log.warning("ACP client request %s: %s", method, exc)
            await self._reply_error(msg_id, -32000, str(exc))
        except FileNotFoundError as exc:
            log.warning("ACP client request %s: %s", method, exc)
            await self._reply_error(msg_id, -32000, str(exc))
        except ValueError as exc:
            log.warning("ACP client request %s: %s", method, exc)
            await self._reply_error(msg_id, -32602, str(exc))
        except Exception as exc:
            log.exception("ACP client request %s failed: %s", method, exc)
            await self._reply_error(msg_id, -32000, f"{type(exc).__name__}: {exc}")

    async def _handle_permission(self, msg_id: Any, params: dict[str, Any]) -> None:
        options = params.get("options") or []
        if not isinstance(options, list):
            options = []
        option_id = pick_permission_option(options)
        tool_call = params.get("toolCall") or params.get("tool_call") or {}
        tool_name = ""
        if isinstance(tool_call, dict):
            tool_name = str(
                tool_call.get("title")
                or tool_call.get("kind")
                or tool_call.get("toolCallId")
                or tool_call.get("tool_call_id")
                or ""
            )
        log.info(
            "permission auto-approved tool=%s optionId=%s",
            tool_name or "?",
            option_id,
        )
        await self._reply_result(
            msg_id,
            {"outcome": {"outcome": "selected", "optionId": option_id}},
        )

    async def _track_update(self, msg: dict[str, Any]) -> None:
        method = msg.get("method") or ""
        if method not in ("session/update", "_x.ai/session/update"):
            return
        self.note_activity()
        params = msg.get("params") or {}
        update = params.get("update") or {}
        kind = update.get("sessionUpdate") or ""
        if kind == "available_commands_update":
            cmds = update.get("availableCommands") or update.get("available_commands") or []
            if isinstance(cmds, list):
                self.available_commands = cmds
        if kind in ("turn_completed", "task_completed", "prompt_complete"):
            self.turn_running = False
            self.turn_session_id = None
            self.turn_started_at = None
            self.last_activity_at = None
            self.turn_saw_update = False
            self._cancel_stall_watchdog()

    async def _fanout(self, msg: dict[str, Any]) -> None:
        if not self.on_message:
            return
        result = self.on_message(msg)
        if asyncio.iscoroutine(result):
            await result

    async def _send(self, payload: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("ACP not connected")
        await ws.send(json.dumps(payload))

    async def request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 120.0
    ) -> Any:
        if self._ws is None:
            raise ConnectionError("ACP not connected")
        req_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        await self._send(payload)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except Exception:
            pending = self._pending.pop(req_id, None)
            if pending is not None and not pending.done():
                pending.cancel()
            raise

    async def session_new(self, cwd: str) -> str:
        async with self._lock:
            result = await self.request(
                "session/new",
                {"cwd": cwd, "mcpServers": []},
                timeout=60.0,
            )
            session_id = (result or {}).get("sessionId") or (result or {}).get("session_id")
            if not session_id:
                raise RuntimeError(f"session/new missing sessionId: {result!r}")
            self.loaded_session_id = str(session_id)
            return str(session_id)

    async def session_load(self, session_id: str, cwd: str) -> Any:
        async with self._lock:
            if self.turn_running:
                if self.is_turn_stuck():
                    self.force_clear_turn("stuck turn before session/load")
                else:
                    raise RuntimeError("Turn in progress; cannot load another session")
            result = await self.request(
                "session/load",
                {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
                timeout=60.0,
            )
            self.loaded_session_id = session_id
            return result

    async def _stall_watchdog_loop(
        self, session_id: str, no_output_seconds: float
    ) -> None:
        """Continuous monitor: no-output, mid-turn stall, and max turn duration.

        Unlike the old one-shot no-output loop, this never stops after the first
        session/update — mid-turn hangs must still force-clear.
        """
        try:
            while self.turn_running and self.turn_session_id == session_id:
                await asyncio.sleep(1.0)
                if not self.turn_running or self.turn_session_id != session_id:
                    return
                started = self.turn_started_at
                if started is None:
                    continue
                now = time.monotonic()
                age_start = now - started
                activity_at = self.last_activity_at
                age_activity = (now - activity_at) if activity_at is not None else age_start
                reason = should_force_clear_turn(
                    self.turn_saw_update,
                    age_start,
                    age_activity,
                    no_output_seconds=no_output_seconds,
                    mid_turn_stall_seconds=MID_TURN_STALL_SECONDS,
                    max_turn_seconds=MAX_TURN_SECONDS,
                )
                if reason:
                    self.force_clear_turn(reason)
                    return
        except asyncio.CancelledError:
            return

    async def session_prompt(
        self,
        session_id: str,
        text: str,
        cwd: str | None = None,
        *,
        allow_load: bool = True,
        no_output_seconds: float | None = None,
    ) -> Any:
        """Send session/prompt. By default loads session if not loaded.

        allow_load=False: only prompt if already loaded (hub-owned sessions
        are created via session/new and must not session/load CLI ids).

        no_output_seconds: hang threshold; None uses NO_OUTPUT_SECONDS.
        """
        thr = NO_OUTPUT_SECONDS if no_output_seconds is None else no_output_seconds
        async with self._lock:
            if self.turn_running:
                if self.is_turn_stuck():
                    self.force_clear_turn("stuck turn before new prompt")
                else:
                    raise RuntimeError("Agent is busy with another turn")
            if self.loaded_session_id != session_id:
                if not allow_load:
                    raise RuntimeError(
                        f"Session {session_id} not loaded; hub must session/new first"
                    )
                if not cwd:
                    raise RuntimeError("Session not loaded; cwd required to load")
                await self.request(
                    "session/load",
                    {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
                    timeout=60.0,
                )
                self.loaded_session_id = session_id

            self.turn_running = True
            self.turn_session_id = session_id
            self.turn_started_at = time.monotonic()
            self.last_activity_at = time.monotonic()
            self.turn_saw_update = False
            self._cancel_stall_watchdog()
            # Always run continuous stall watchdog for mid-turn / max duration.
            self._stall_watchdog = asyncio.create_task(
                self._stall_watchdog_loop(session_id, thr),
                name=f"acp-stall-{session_id[:8]}",
            )
            log.info("Prompt start session=%s", session_id)
            try:
                # Match request timeout to MAX_TURN_SECONDS (TUI-length agentic turns).
                result = await self.request(
                    "session/prompt",
                    {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": text}],
                    },
                    timeout=float(MAX_TURN_SECONDS),
                )
                log.info("Prompt end session=%s ok", session_id)
                return result
            except Exception as exc:
                log.warning("Prompt end session=%s error: %s", session_id, exc)
                raise
            finally:
                self._cancel_stall_watchdog()
                self.turn_running = False
                self.turn_session_id = None
                self.turn_started_at = None
                self.last_activity_at = None
                self.turn_saw_update = False

    async def session_cancel(self, session_id: str) -> None:
        for method in ("session/cancel", "session/prompt/cancel", "x.ai/session/cancel"):
            try:
                await self.request(method, {"sessionId": session_id}, timeout=10.0)
                self.turn_running = False
                self.turn_session_id = None
                self.turn_started_at = None
                self.last_activity_at = None
                self.turn_saw_update = False
                self._cancel_stall_watchdog()
                return
            except Exception as exc:
                log.debug("Cancel via %s failed: %s", method, exc)
        raise RuntimeError("Cancel not supported by agent")
