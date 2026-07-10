from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from hub.acp_client import AcpClient
from hub.agent_supervisor import AgentSupervisor
from hub.config import Config, TAILSCALE_EXE
from hub.history import load_session_history
from hub.session_index import find_session, list_projects, scan_sessions

log = logging.getLogger("hub.server")


def resolve_bind_hosts(config: Config) -> tuple[list[str], str, str | None]:
    """Return (hosts, bind_mode, tailscale_ip).

    When Tailscale is available, bind BOTH 127.0.0.1 and the Tailscale IP so
    desktop localhost and phone-over-tailnet work. Never binds 0.0.0.0.
    Agent remains on 127.0.0.1 only (separate config).
    """
    if config.bind_host:
        host = config.bind_host.strip()
        if host in ("0.0.0.0", "::", "[::]"):
            raise ValueError("bind_host must not be a wildcard (0.0.0.0 / ::)")
        if _looks_like_tailscale(host):
            # Explicit Tailscale IP: also bind localhost for desktop UX
            return ["127.0.0.1", host], "tailscale", host
        return [host], "local", None

    ip = _tailscale_ip()
    if ip:
        return ["127.0.0.1", ip], "tailscale", ip
    return ["127.0.0.1"], "local", None


def resolve_bind_host(config: Config) -> tuple[str, str, str | None]:
    """Compat: return (primary_host, bind_mode, tailscale_ip).

    primary_host is Tailscale IP when dual-binding, else the sole host.
    """
    hosts, mode, ts = resolve_bind_hosts(config)
    primary = ts if ts and ts in hosts else hosts[0]
    return primary, mode, ts


def _looks_like_tailscale(host: str) -> bool:
    return bool(re.match(r"^100\.\d+\.\d+\.\d+$", host))


def _tailscale_ip() -> str | None:
    candidates = [str(TAILSCALE_EXE), "tailscale"]
    for exe in candidates:
        try:
            proc = subprocess.run(
                [exe, "ip", "-4"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                line = proc.stdout.strip().splitlines()[0].strip()
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", line):
                    return line
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return None


class Hub:
    def __init__(self, config: Config):
        self.config = config
        self.secret = config.ensure_agent_secret()
        self.bind_host = "127.0.0.1"
        self.bind_hosts: list[str] = ["127.0.0.1"]
        self.bind_mode = "local"
        self.tailscale_ip: str | None = None
        self.agent_status = "down"
        self.acp_connected = False
        self.clients: set[web.WebSocketResponse] = set()
        self.subscriptions: dict[web.WebSocketResponse, set[str]] = {}
        self.supervisor = AgentSupervisor(config, self.secret, on_status=self._on_agent_status)
        self.acp = AcpClient(
            config,
            self.secret,
            on_message=self._on_acp_message,
            on_connection=self._on_acp_connection,
        )
        self._app: web.Application | None = None

    def status_payload(self) -> dict[str, Any]:
        agent = "up" if self.agent_status == "up" and self.acp_connected else (
            "up" if self.agent_status == "up" else "down"
        )
        # Prefer reporting agent down if process/port is down; if port up but ACP not yet connected, still down-ish
        if self.agent_status != "up":
            agent = "down"
        elif not self.acp_connected:
            agent = "down"
        else:
            agent = "up"
        return {
            "type": "status",
            "agent": agent,
            "bind": self.bind_mode,
            "tailscaleIp": self.tailscale_ip,
            "acpConnected": self.acp_connected,
            "loadedSessionId": self.acp.loaded_session_id,
            "turnRunning": self.acp.turn_running,
            "turnSessionId": self.acp.turn_session_id,
        }

    async def _on_agent_status(self, status: str) -> None:
        self.agent_status = status
        await self.broadcast(self.status_payload())

    async def _on_acp_connection(self, connected: bool) -> None:
        self.acp_connected = connected
        await self.broadcast(self.status_payload())

    async def _on_acp_message(self, msg: dict[str, Any]) -> None:
        session_id = self._session_id_from_acp(msg)
        # Commands update
        method = msg.get("method") or ""
        if method in ("session/update", "_x.ai/session/update"):
            update = (msg.get("params") or {}).get("update") or {}
            kind = update.get("sessionUpdate") or ""
            if kind == "available_commands_update" and session_id:
                cmds = update.get("availableCommands") or []
                await self.broadcast(
                    {"type": "commands", "sessionId": session_id, "commands": cmds},
                    session_id=session_id,
                )
            if kind in ("turn_completed", "task_completed", "prompt_complete") and session_id:
                await self.broadcast(
                    {"type": "turn", "sessionId": session_id, "state": "idle", "error": None},
                    session_id=session_id,
                )

        await self.broadcast(
            {"type": "acp", "sessionId": session_id, "message": msg},
            session_id=session_id,
        )

    @staticmethod
    def _session_id_from_acp(msg: dict[str, Any]) -> str | None:
        params = msg.get("params") or {}
        if isinstance(params, dict):
            sid = params.get("sessionId") or params.get("session_id")
            if sid:
                return str(sid)
            update = params.get("update") or {}
            if isinstance(update, dict) and update.get("sessionId"):
                return str(update["sessionId"])
        return None

    async def broadcast(self, payload: dict[str, Any], session_id: str | None = None) -> None:
        data = json.dumps(payload, default=str)
        dead: list[web.WebSocketResponse] = []
        for ws in list(self.clients):
            if session_id:
                subs = self.subscriptions.get(ws) or set()
                # status / sessions / error always go to all
                if payload.get("type") not in ("status", "sessions", "error", "hello"):
                    if session_id not in subs and payload.get("type") in (
                        "acp",
                        "history",
                        "commands",
                        "turn",
                    ):
                        continue
            try:
                await ws.send_str(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self._drop_client(ws)

    async def _drop_client(self, ws: web.WebSocketResponse) -> None:
        self.clients.discard(ws)
        self.subscriptions.pop(ws, None)
        try:
            await ws.close()
        except Exception:
            pass

    def check_auth(self, request: web.Request) -> bool:
        token = self.config.hub_token
        if not token:
            return True
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer ") and auth[7:].strip() == token:
            return True
        if request.rel_url.query.get("token") == token:
            return True
        return False

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth_middleware])
        app["hub"] = self
        app.router.add_get("/health", self.handle_health)
        app.router.add_get("/api/sessions", self.handle_sessions)
        app.router.add_get("/api/sessions/{id}/history", self.handle_history)
        app.router.add_post("/api/sessions", self.handle_new_session)
        app.router.add_post("/api/sessions/{id}/load", self.handle_load_session)
        app.router.add_get("/api/projects", self.handle_projects)
        app.router.add_get("/ws", self.handle_ws)
        static_dir = Path(self.config.static_dir)
        if static_dir.is_dir():
            app.router.add_get("/", self.handle_index)
            app.router.add_static("/", static_dir, show_index=False)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        self._app = app
        return app

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        # Static assets and health still respect token if configured
        if request.path == "/health":
            return await handler(request)
        if not self.check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def _on_startup(self, app: web.Application) -> None:
        await self.supervisor.start()
        await self.supervisor.wait_until_up(timeout=25.0)
        await self.acp.start()
        # Give ACP a moment to connect
        for _ in range(40):
            if self.acp.connected:
                break
            await asyncio.sleep(0.25)

    async def _on_cleanup(self, app: web.Application) -> None:
        await self.acp.stop()
        await self.supervisor.stop()

    async def handle_index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(Path(self.config.static_dir) / "index.html")

    async def handle_health(self, request: web.Request) -> web.Response:
        body = {
            "ok": True,
            "agent": self.agent_status,
            "acpConnected": self.acp_connected,
            "bind": self.bind_mode,
            "host": self.bind_host,
            "hosts": list(self.bind_hosts),
            "port": self.config.bind_port,
            "tailscaleIp": self.tailscale_ip,
            "loadedSessionId": self.acp.loaded_session_id,
        }
        return web.json_response(body)

    async def handle_sessions(self, request: web.Request) -> web.Response:
        items = scan_sessions(self.config.sessions_root, limit=self.config.max_sessions)
        return web.json_response({"items": [s.to_dict() for s in items]})

    async def handle_history(self, request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        session = find_session(self.config.sessions_root, session_id)
        path = session.path if session else None
        messages = load_session_history(
            self.config.sessions_root,
            session_id,
            session_path=path,
            max_messages=self.config.max_history_messages,
        )
        return web.json_response({"sessionId": session_id, "messages": messages})

    async def handle_projects(self, request: web.Request) -> web.Response:
        sessions = scan_sessions(self.config.sessions_root, limit=self.config.max_sessions)
        items = list_projects(self.config.projects_root, sessions)
        return web.json_response({"items": items})

    async def handle_new_session(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        cwd = (body.get("cwd") or "").strip()
        if not cwd:
            return web.json_response({"error": "cwd required"}, status=400)
        if not self.acp.connected:
            return web.json_response({"error": "agent not connected"}, status=503)
        try:
            session_id = await self.acp.session_new(cwd)
        except Exception as exc:
            log.exception("session/new failed")
            return web.json_response({"error": str(exc)}, status=500)
        await self.broadcast(self.status_payload())
        items = scan_sessions(self.config.sessions_root, limit=self.config.max_sessions)
        await self.broadcast({"type": "sessions", "items": [s.to_dict() for s in items]})
        return web.json_response({"sessionId": session_id, "cwd": cwd})

    async def handle_load_session(self, request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        cwd = (body.get("cwd") or "").strip()
        if not cwd:
            session = find_session(self.config.sessions_root, session_id)
            if session:
                cwd = session.cwd
        if not cwd:
            return web.json_response({"error": "cwd required"}, status=400)
        if not self.acp.connected:
            return web.json_response({"error": "agent not connected"}, status=503)
        if self.acp.turn_running:
            return web.json_response(
                {"error": "turn in progress; wait until idle to switch sessions"},
                status=409,
            )
        try:
            await self.acp.session_load(session_id, cwd)
        except Exception as exc:
            log.exception("session/load failed")
            return web.json_response({"error": str(exc)}, status=500)
        await self.broadcast(self.status_payload())
        if self.acp.available_commands:
            await self.broadcast(
                {
                    "type": "commands",
                    "sessionId": session_id,
                    "commands": self.acp.available_commands,
                },
                session_id=session_id,
            )
        return web.json_response({"sessionId": session_id, "loaded": True})

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.clients.add(ws)
        self.subscriptions[ws] = set()
        await ws.send_str(json.dumps(self.status_payload()))
        items = scan_sessions(self.config.sessions_root, limit=self.config.max_sessions)
        await ws.send_str(json.dumps({"type": "sessions", "items": [s.to_dict() for s in items]}))

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle_ws_message(ws, msg.data)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            await self._drop_client(ws)
        return ws

    async def _handle_ws_message(self, ws: web.WebSocketResponse, data: str) -> None:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            await ws.send_str(json.dumps({"type": "error", "message": "invalid json"}))
            return
        if not isinstance(payload, dict):
            return
        typ = payload.get("type")
        if typ == "hello":
            await ws.send_str(json.dumps(self.status_payload()))
            return
        if typ == "subscribe":
            sid = str(payload.get("sessionId") or "")
            if sid:
                self.subscriptions.setdefault(ws, set()).add(sid)
            return
        if typ == "unsubscribe":
            sid = str(payload.get("sessionId") or "")
            if sid:
                self.subscriptions.setdefault(ws, set()).discard(sid)
            return
        if typ == "prompt":
            await self._ws_prompt(ws, payload)
            return
        if typ == "cancel":
            await self._ws_cancel(ws, payload)
            return
        await ws.send_str(json.dumps({"type": "error", "message": f"unknown type: {typ}"}))

    async def _ws_prompt(self, ws: web.WebSocketResponse, payload: dict[str, Any]) -> None:
        session_id = str(payload.get("sessionId") or "")
        text = str(payload.get("text") or "")
        if not session_id or not text.strip():
            await ws.send_str(json.dumps({"type": "error", "message": "sessionId and text required"}))
            return
        if not self.acp.connected:
            await ws.send_str(json.dumps({"type": "error", "message": "agent not connected"}))
            return
        if self.acp.turn_running:
            await ws.send_str(
                json.dumps(
                    {
                        "type": "error",
                        "message": "Agent is busy. Wait for the current turn to finish.",
                    }
                )
            )
            return

        session = find_session(self.config.sessions_root, session_id)
        cwd = (payload.get("cwd") or (session.cwd if session else "") or "").strip()
        if not cwd and self.acp.loaded_session_id != session_id:
            await ws.send_str(json.dumps({"type": "error", "message": "cwd unknown for session"}))
            return

        self.subscriptions.setdefault(ws, set()).add(session_id)
        await self.broadcast(
            {"type": "turn", "sessionId": session_id, "state": "running", "error": None},
            session_id=session_id,
        )
        # Echo user message as acp-shaped update so all clients see it immediately
        await self.broadcast(
            {
                "type": "acp",
                "sessionId": session_id,
                "message": {
                    "method": "session/update",
                    "params": {
                        "sessionId": session_id,
                        "update": {
                            "sessionUpdate": "user_message_chunk",
                            "content": {"type": "text", "text": text},
                        },
                    },
                },
            },
            session_id=session_id,
        )
        try:
            await self.acp.session_prompt(session_id, text, cwd=cwd or None)
            await self.broadcast(
                {"type": "turn", "sessionId": session_id, "state": "idle", "error": None},
                session_id=session_id,
            )
        except Exception as exc:
            log.exception("prompt failed")
            await self.broadcast(
                {
                    "type": "turn",
                    "sessionId": session_id,
                    "state": "idle",
                    "error": str(exc),
                },
                session_id=session_id,
            )
            await self.broadcast({"type": "error", "message": str(exc)})
        await self.broadcast(self.status_payload())

    async def _ws_cancel(self, ws: web.WebSocketResponse, payload: dict[str, Any]) -> None:
        session_id = str(payload.get("sessionId") or "")
        if not session_id:
            return
        try:
            await self.acp.session_cancel(session_id)
            await self.broadcast(
                {"type": "turn", "sessionId": session_id, "state": "idle", "error": None},
                session_id=session_id,
            )
        except Exception as exc:
            await ws.send_str(
                json.dumps(
                    {
                        "type": "error",
                        "message": f"Cancel failed: {exc}. Wait for the turn to finish.",
                    }
                )
            )


def create_app(config: Config | None = None) -> web.Application:
    cfg = config or __import__("hub.config", fromlist=["load_config"]).load_config()
    hub = Hub(cfg)
    hosts, mode, ts_ip = resolve_bind_hosts(cfg)
    hub.bind_hosts = hosts
    hub.bind_host = ts_ip if ts_ip and ts_ip in hosts else hosts[0]
    hub.bind_mode = mode
    hub.tailscale_ip = ts_ip
    return hub.build_app()
