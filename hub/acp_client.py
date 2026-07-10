from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from websockets.asyncio.client import connect as ws_connect

from hub.config import Config

log = logging.getLogger("hub.acp")

MessageCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


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
                self.turn_running = False
                self.turn_session_id = None
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
        if msg_id is not None and ("result" in msg or "error" in msg):
            fut = self._pending.pop(msg_id, None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(str(msg["error"])))
                else:
                    fut.set_result(msg.get("result"))
            await self._fanout(msg)
            return

        await self._fanout(msg)
        await self._track_update(msg)

        method = msg.get("method")
        if method and msg_id is not None and "result" not in msg:
            if "permission" in method.lower() or method.endswith("request_permission"):
                try:
                    await self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "result": {
                                "outcome": {"outcome": "selected", "optionId": "allow-always"}
                            },
                        }
                    )
                except Exception:
                    try:
                        await self._send(
                            {"jsonrpc": "2.0", "id": msg_id, "result": {"approved": True}}
                        )
                    except Exception as exc:
                        log.debug("Permission auto-reply failed: %s", exc)

    async def _track_update(self, msg: dict[str, Any]) -> None:
        method = msg.get("method") or ""
        if method not in ("session/update", "_x.ai/session/update"):
            return
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
            self._pending.pop(req_id, None)
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
                raise RuntimeError("Turn in progress; cannot load another session")
            result = await self.request(
                "session/load",
                {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
                timeout=60.0,
            )
            self.loaded_session_id = session_id
            return result

    async def session_prompt(self, session_id: str, text: str, cwd: str | None = None) -> Any:
        async with self._lock:
            if self.turn_running:
                raise RuntimeError("Agent is busy with another turn")
            if self.loaded_session_id != session_id:
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
            try:
                result = await self.request(
                    "session/prompt",
                    {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": text}],
                    },
                    timeout=600.0,
                )
                return result
            finally:
                self.turn_running = False
                self.turn_session_id = None

    async def session_cancel(self, session_id: str) -> None:
        for method in ("session/cancel", "session/prompt/cancel", "x.ai/session/cancel"):
            try:
                await self.request(method, {"sessionId": session_id}, timeout=10.0)
                self.turn_running = False
                self.turn_session_id = None
                return
            except Exception as exc:
                log.debug("Cancel via %s failed: %s", method, exc)
        raise RuntimeError("Cancel not supported by agent")
