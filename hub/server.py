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
from hub.config import Config, PROJECT_ROOT, TAILSCALE_EXE
from hub.fs_browser import (
    FsBrowserError,
    content_type_for,
    list_dir as fs_list_dir,
    read_text as fs_read_text,
    resolve_file_for_read,
    write_text as fs_write_text,
)
from hub.history import load_session_history
from hub.projects import ProjectError, create_project
from hub.prompt_queue import PromptQueue
from hub.session_index import (
    delete_session,
    find_session,
    list_projects,
    rename_session,
    scan_sessions,
    stamp_hub_origin,
)
from hub.billing_usage import fetch_credits_usage
from hub.session_signals import read_session_signals, read_signals_file, find_signals_path
from hub.session_policy import (
    STUCK_TURN_SECONDS,
    cwd_key,
    load_remote_sessions,
    needs_fresh_agent_session,
    resolve_live_session_id,
    save_remote_sessions,
)
from hub.session_tailer import EventDedupe, SessionTailer
from hub.skills_index import list_skills
from hub.version_info import HUB_VERSION, get_cli_version, structural_compat

log = logging.getLogger("hub.server")

LAST_REMOTE_SESSION_FILE = PROJECT_ROOT / "logs" / "last-remote-session.txt"
REMOTE_SESSIONS_FILE = PROJECT_ROOT / "data" / "remote-sessions.json"
REMOTE_SESSION_SYSTEM_NOTE = (
    "Live remote session for this project. Desktop TUI history is separate."
)
REMOTE_SESSION_SAME_NOTE = "Live remote session"
NO_OUTPUT_USER_MSG = (
    "Agent produced no output. Started a fresh remote session — send again."
)
MID_TURN_STALL_USER_MSG = (
    "Agent stalled mid-turn (no activity). Turn cleared — you can send again."
)
MAX_TURN_USER_MSG = (
    "Turn hit max duration and was cleared — you can send again."
)


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
        # Background WS work (prompt/cancel) so the receive loop can process pings.
        self._bg_tasks: set[asyncio.Task] = set()
        self.supervisor = AgentSupervisor(config, self.secret, on_status=self._on_agent_status)
        self.acp = AcpClient(
            config,
            self.secret,
            on_message=self._on_acp_message,
            on_connection=self._on_acp_connection,
        )
        self.acp.on_user_question = self._on_user_question
        self._acp_dedupe = EventDedupe(maxlen=2000)
        self.tailer = SessionTailer(
            config.sessions_root,
            on_event=self._on_disk_event,
            poll_interval=0.25,
        )
        # Session ids created via session/new in this hub process (safe to prompt).
        # Restored from remote-sessions.json on start so multi-turn survives hub restart.
        self.acp_created_sessions: set[str] = set()
        # cwd (casefold) -> last hub-created agent session id for remote prompts.
        self.remote_agent_session: dict[str, str] = {}
        self.remote_sessions_path = REMOTE_SESSIONS_FILE
        self._load_remote_sessions_map()
        # FIFO prompts while a turn is running (data only; no ws refs).
        self._prompt_queue = PromptQueue(max_size=10)
        self._prompt_queue_lock = asyncio.Lock()
        self._app: web.Application | None = None
        self._status_resync_task: asyncio.Task | None = None
        self._last_broadcast_force_clear: str | None = None
        self.cli_version: str | None = None
        self.compat: dict[str, Any] = {
            "ok": False,
            "level": "structural",
            "productTag": "remote-stream",
            "hubVersion": HUB_VERSION,
            "cliVersion": None,
            "checks": {},
            "issues": ["compat not run yet"],
        }

    def refresh_compat(self) -> dict[str, Any]:
        """Re-run structural compatibility checks (no model calls)."""
        if self.cli_version is None:
            self.cli_version = get_cli_version(self.config.grok_bin)
        self.compat = structural_compat(
            self.config,
            agent_up=self.agent_status == "up",
            acp_connected=self.acp_connected,
            cli_version=self.cli_version,
        )
        return self.compat

    def _hub_session_ids(self, limit: int = 50) -> list[str]:
        ids = list(self.acp_created_sessions)
        # Prefer most recently recorded remote sessions first when possible
        recent = list(self.remote_agent_session.values())
        ordered: list[str] = []
        seen: set[str] = set()
        for sid in reversed(recent):
            if sid in self.acp_created_sessions and sid not in seen:
                ordered.append(sid)
                seen.add(sid)
        for sid in ids:
            if sid not in seen:
                ordered.append(sid)
                seen.add(sid)
        return ordered[:limit]

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
        compat = self.compat or {}
        issues = list(compat.get("issues") or [])
        return {
            "type": "status",
            "agent": agent,
            "bind": self.bind_mode,
            "tailscaleIp": self.tailscale_ip,
            "acpConnected": self.acp_connected,
            "loadedSessionId": self.acp.loaded_session_id,
            "turnRunning": self.acp.turn_running,
            "turnSessionId": self.acp.turn_session_id,
            "promptQueueLength": len(self._prompt_queue),
            "hubVersion": HUB_VERSION,
            "cliVersion": self.cli_version or compat.get("cliVersion"),
            "compatOk": bool(compat.get("ok")),
            "compatIssues": issues[:8],
            "productTag": "remote-stream",
            "hubSessionIds": self._hub_session_ids(50),
        }

    async def _on_agent_status(self, status: str) -> None:
        self.agent_status = status
        await self.broadcast(self.status_payload())

    async def _on_acp_connection(self, connected: bool) -> None:
        self.acp_connected = connected
        if not connected:
            sid = self.acp.disconnect_turn_session_id
            self.acp.disconnect_turn_session_id = None
            if not sid:
                sid = self.acp.turn_session_id
            if sid:
                await self.broadcast(
                    {
                        "type": "turn",
                        "sessionId": sid,
                        "state": "idle",
                        "error": "ACP disconnected",
                    },
                    session_id=sid,
                )
        await self.broadcast(self.status_payload())

    async def _on_acp_message(self, msg: dict[str, Any]) -> None:
        session_id = self._session_id_from_acp(msg)
        await self._emit_acp(session_id, msg)

    async def _on_user_question(self, payload: dict[str, Any]) -> None:
        """Fan out agent ask_user_question to all connected UIs."""
        await self.broadcast(
            {
                "type": "user_question",
                "requestId": payload.get("requestId"),
                "sessionId": payload.get("sessionId"),
                "questions": payload.get("questions") or [],
                "toolCallId": payload.get("toolCallId"),
            }
        )

    async def _ws_user_question_answer(
        self, ws: web.WebSocketResponse, payload: dict[str, Any]
    ) -> None:
        """Resolve a pending ACP ask_user_question from the web UI."""
        from hub.acp_ask_user import build_accepted_result

        request_id = str(payload.get("requestId") or "")
        if not request_id:
            await ws.send_str(
                json.dumps({"type": "error", "message": "user_question_answer missing requestId"})
            )
            return
        outcome = str(payload.get("outcome") or "accepted").lower()
        answers = payload.get("answers") or {}
        if not isinstance(answers, dict):
            answers = {}
        if outcome == "cancelled":
            ok = self.acp.cancel_user_question(request_id)
        else:
            # answers: {qid: [str, ...]}
            ok = self.acp.answer_user_question(request_id, build_accepted_result(answers))
        await self.broadcast(
            {
                "type": "user_question_resolved",
                "requestId": request_id,
                "outcome": "cancelled" if outcome == "cancelled" else "accepted",
                "ok": ok,
            }
        )
        if not ok:
            await ws.send_str(
                json.dumps(
                    {
                        "type": "error",
                        "message": f"No pending user question for requestId={request_id}",
                    }
                )
            )


    async def _on_disk_event(self, session_id: str, msg: dict[str, Any]) -> None:
        """Disk tailer path: CLI (or any process) wrote updates.jsonl."""
        sid = self._session_id_from_acp(msg) or session_id
        await self._emit_acp(sid, msg)

    async def _emit_acp(self, session_id: str | None, msg: dict[str, Any]) -> None:
        """Dedupe hub ACP + disk events, then side-effects + WS fanout."""
        if session_id and not self._acp_dedupe.should_emit(session_id, msg):
            return

        method = msg.get("method") or ""
        if method in ("session/update", "_x.ai/session/update"):
            update = (msg.get("params") or {}).get("update") or {}
            kind = update.get("sessionUpdate") or ""
            if kind == "available_commands_update" and session_id:
                cmds = (
                    update.get("availableCommands")
                    or update.get("available_commands")
                    or []
                )
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

    def _any_subscribed(self, session_id: str) -> bool:
        for subs in self.subscriptions.values():
            if session_id in subs:
                return True
        return False

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
        always = (
            "status",
            "sessions",
            "error",
            "hello",
            "session_switch",
            "queued",
            "queue",
            "user_question",
            "user_question_resolved",
        )
        scoped = ("acp", "history", "commands", "turn", "system")
        for ws in list(self.clients):
            if session_id:
                subs = self.subscriptions.get(ws) or set()
                # status / sessions / error / session_switch always go to all
                if payload.get("type") not in always:
                    if session_id not in subs and payload.get("type") in scoped:
                        continue
            try:
                await ws.send_str(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self._drop_client(ws)

    async def _drop_client(self, ws: web.WebSocketResponse) -> None:
        self.clients.discard(ws)
        self.subscriptions.pop(ws, set())
        try:
            await ws.close()
        except Exception:
            pass
        # Keep tailing for process lifetime so iOS/Safari reconnects never miss
        # lines written while the socket was down (offsets stay mid-file).

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

    @staticmethod
    def _cwd_key(cwd: str) -> str:
        return cwd_key(cwd)

    def _load_remote_sessions_map(self) -> None:
        """Restore remote_agent_session + acp_created_sessions from disk."""
        mapping = load_remote_sessions(self.remote_sessions_path)
        self.remote_agent_session = dict(mapping)
        for sid in mapping.values():
            if sid:
                self.acp_created_sessions.add(str(sid))
        if mapping:
            log.info(
                "Loaded %d remote session mapping(s) from %s",
                len(mapping),
                self.remote_sessions_path,
            )

    def _persist_remote_sessions_map(self) -> None:
        try:
            save_remote_sessions(self.remote_sessions_path, self.remote_agent_session)
        except OSError as exc:
            log.debug("failed to write remote-sessions.json: %s", exc)

    def _record_hub_session(self, session_id: str, cwd: str) -> None:
        """Track a hub-created agent session and persist remote map + last id."""
        sid = str(session_id)
        self.acp_created_sessions.add(sid)
        key = self._cwd_key(cwd)
        if key:
            self.remote_agent_session[key] = sid
            self._persist_remote_sessions_map()
        try:
            path = LAST_REMOTE_SESSION_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(sid + "\n", encoding="utf-8")
        except OSError as exc:
            log.debug("failed to write last-remote-session.txt: %s", exc)

    def _hub_remote_ids(self) -> set[str]:
        return set(self.remote_agent_session.values())

    def _scan_sessions(self) -> list:
        return scan_sessions(
            self.config.sessions_root,
            limit=self.config.max_sessions,
            hub_remote_ids=self._hub_remote_ids(),
        )

    async def _stamp_origin_with_retry(self, session_id: str, origin: str) -> None:
        """Best-effort hub_origin write; summary may appear shortly after session/new."""
        for delay in (0, 0.4, 1.0):
            if delay:
                await asyncio.sleep(delay)
            try:
                if stamp_hub_origin(self.config.sessions_root, session_id, origin):
                    return
            except Exception as exc:
                log.debug(
                    "stamp_hub_origin error session=%s origin=%s: %s",
                    session_id,
                    origin,
                    exc,
                )
        log.debug(
            "stamp_hub_origin gave up session=%s origin=%s", session_id, origin
        )

    async def _ensure_hub_agent_session(
        self,
        view_session_id: str,
        cwd: str,
        ws: web.WebSocketResponse | None = None,
        *,
        notify_switch: bool = True,
    ) -> tuple[str, bool, str]:
        """Return (live_session_id, switched, reason) safe for session/prompt.

        If view_session_id was never created by this hub, reuse cwd remote or
        session/new. Optionally notify UI via session_switch and subscribe ws.
        """
        live, needs_new, reason = resolve_live_session_id(
            view_session_id,
            cwd,
            self.acp_created_sessions,
            self.remote_agent_session,
        )
        if not needs_new and live:
            agent_sid = live
            # After hub restart / ACP reconnect, ensure agent has hub session loaded.
            if self.acp.connected and self.acp.loaded_session_id != agent_sid:
                try:
                    await self.acp.session_load(agent_sid, cwd)
                except Exception:
                    log.info(
                        "Reusing remote session failed load; creating new (cwd=%s)",
                        cwd,
                    )
                    agent_sid = await self.acp.session_new(cwd)
                    self._record_hub_session(agent_sid, cwd)
                    asyncio.create_task(
                        self._stamp_origin_with_retry(agent_sid, "attach")
                    )
                    reason = "need_session_new"
            log.info(
                "Reuse hub remote session %s for view %s cwd=%s reason=%s",
                agent_sid,
                view_session_id,
                cwd,
                reason,
            )
        else:
            agent_sid = await self.acp.session_new(cwd)
            self._record_hub_session(agent_sid, cwd)
            asyncio.create_task(self._stamp_origin_with_retry(agent_sid, "attach"))
            reason = "need_session_new"
            log.info(
                "Created hub remote session %s for view %s cwd=%s",
                agent_sid,
                view_session_id,
                cwd,
            )

        switched = agent_sid != view_session_id
        switch_reason = "hub_session"
        if switched:
            switch_reason = (
                "cli_or_foreign_session"
                if reason in ("reuse_cwd", "need_session_new", "cli_or_foreign_session")
                else reason
            )
            if notify_switch:
                switch = {
                    "type": "session_switch",
                    "from": view_session_id,
                    "to": agent_sid,
                    "reason": switch_reason,
                    "cwd": cwd,
                    "message": REMOTE_SESSION_SYSTEM_NOTE,
                }
                await self.broadcast(switch)
                if ws is not None:
                    self.subscriptions.setdefault(ws, set()).add(agent_sid)
                items = self._scan_sessions()
                await self.broadcast(
                    {"type": "sessions", "items": [s.to_dict() for s in items]}
                )
                await self.broadcast(self.status_payload())
        else:
            switch_reason = "hub_session"

        return agent_sid, switched, switch_reason

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth_middleware])
        app["hub"] = self
        app.router.add_get("/health", self.handle_health)
        app.router.add_get("/api/compat", self.handle_compat)
        app.router.add_post("/api/compat/refresh", self.handle_compat_refresh)
        app.router.add_get("/api/sessions", self.handle_sessions)
        app.router.add_get("/api/sessions/{id}/history", self.handle_history)
        app.router.add_get("/api/sessions/{id}/usage", self.handle_session_usage)
        app.router.add_get("/api/usage/plan", self.handle_usage_plan)
        app.router.add_post("/api/sessions", self.handle_new_session)
        app.router.add_patch("/api/sessions/{id}", self.handle_rename_session)
        app.router.add_delete("/api/sessions/{id}", self.handle_delete_session)
        app.router.add_post("/api/sessions/{id}/load", self.handle_load_session)
        app.router.add_post("/api/sessions/{id}/attach", self.handle_attach_session)
        app.router.add_post("/api/admin/reset-turn", self.handle_reset_turn)
        app.router.add_get("/api/projects", self.handle_projects)
        app.router.add_post("/api/projects", self.handle_create_project)
        app.router.add_get("/api/skills", self.handle_skills)
        app.router.add_get("/api/fs/list", self.handle_fs_list)
        app.router.add_get("/api/fs/read", self.handle_fs_read)
        app.router.add_get("/api/fs/raw", self.handle_fs_raw)
        app.router.add_put("/api/fs/write", self.handle_fs_write)
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
        # CLI version is cheap and useful even if agent is slow
        self.cli_version = await asyncio.to_thread(get_cli_version, self.config.grok_bin)
        await self.supervisor.start()
        await self.supervisor.wait_until_up(timeout=25.0)
        await self.acp.start()
        await self.tailer.start()
        # Give ACP a moment to connect
        for _ in range(40):
            if self.acp.connected:
                break
            await asyncio.sleep(0.25)
        # In-memory turn state is fresh on start; still clear any stuck flag if present.
        if self.acp.turn_running and self.acp.is_turn_stuck(STUCK_TURN_SECONDS):
            sid = self.acp.turn_session_id
            self.acp.force_clear_turn(
                f"startup clear stuck turn age>{STUCK_TURN_SECONDS}s"
            )
            log.warning("Startup force-cleared stuck turn session=%s", sid)
        self.refresh_compat()
        self._status_resync_task = asyncio.create_task(
            self._status_resync_loop(), name="hub-status-resync"
        )
        log.info(
            "compat structural ok=%s hub=%s cli=%s issues=%s",
            self.compat.get("ok"),
            HUB_VERSION,
            self.cli_version,
            self.compat.get("issues"),
        )

    async def _on_cleanup(self, app: web.Application) -> None:
        task = self._status_resync_task
        self._status_resync_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.tailer.stop()
        await self.acp.stop()
        await self.supervisor.stop()

    async def _status_resync_loop(self) -> None:
        """While a turn is running, re-broadcast status so clients re-sync turnRunning.

        Also surfaces watchdog force-clears that may not have gone through WS prompt.
        """
        try:
            while True:
                await asyncio.sleep(10.0)
                reason = self.acp.last_force_clear_reason
                if reason and reason != self._last_broadcast_force_clear:
                    sid = self.acp.last_force_clear_session
                    self._last_broadcast_force_clear = reason
                    if sid and not self.acp.turn_running:
                        err = MID_TURN_STALL_USER_MSG
                        low = reason.lower()
                        if "max turn" in low:
                            err = MAX_TURN_USER_MSG
                        elif "no acp session/update" in low:
                            err = NO_OUTPUT_USER_MSG
                        try:
                            await self._broadcast_turn(sid, "idle", err)
                        except Exception:
                            log.debug("status resync turn idle broadcast failed", exc_info=True)
                if self.acp.turn_running:
                    # Hard safety: wall-clock stuck even if watchdog task died
                    if self.acp.is_turn_stuck(STUCK_TURN_SECONDS):
                        age = self.acp.turn_age_seconds()
                        if age is not None and age >= STUCK_TURN_SECONDS:
                            # Prefer max-turn only via acp watchdog; here just re-broadcast.
                            pass
                    try:
                        await self.broadcast(self.status_payload())
                    except Exception:
                        log.debug("status resync broadcast failed", exc_info=True)
        except asyncio.CancelledError:
            return

    async def handle_index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(Path(self.config.static_dir) / "index.html")

    async def handle_health(self, request: web.Request) -> web.Response:
        compat = self.compat or {}
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
            "turnRunning": self.acp.turn_running,
            "turnSessionId": self.acp.turn_session_id,
            "turnAgeSeconds": self.acp.turn_age_seconds(),
            "hubVersion": HUB_VERSION,
            "cliVersion": self.cli_version or compat.get("cliVersion"),
            "compatOk": bool(compat.get("ok")),
            "compatIssues": list(compat.get("issues") or [])[:8],
            "productTag": "remote-stream",
        }
        return web.json_response(body)

    async def handle_compat(self, request: web.Request) -> web.Response:
        return web.json_response(self.compat or {})

    async def handle_compat_refresh(self, request: web.Request) -> web.Response:
        # Re-probe CLI version in case binary was upgraded while hub is running
        self.cli_version = await asyncio.to_thread(get_cli_version, self.config.grok_bin)
        compat = self.refresh_compat()
        await self.broadcast(self.status_payload())
        return web.json_response(compat)

    async def handle_reset_turn(self, request: web.Request) -> web.Response:
        """Force-clear a stuck turn so clients can send again."""
        sid = self.acp.turn_session_id
        was = self.acp.turn_running
        cleared = self.acp.force_clear_turn("admin POST /api/admin/reset-turn")
        if cleared:
            self._last_broadcast_force_clear = self.acp.last_force_clear_reason
        if sid:
            await self.broadcast(
                {
                    "type": "turn",
                    "sessionId": sid,
                    "state": "idle",
                    "error": "Turn reset by admin",
                },
                session_id=sid,
            )
        # Always push full status so every client re-syncs turnRunning=false.
        await self.broadcast(self.status_payload())
        return web.json_response(
            {"ok": True, "cleared": cleared, "wasRunning": was, "sessionId": sid}
        )

    async def handle_sessions(self, request: web.Request) -> web.Response:
        items = self._scan_sessions()
        return web.json_response({"items": [s.to_dict() for s in items]})

    async def handle_rename_session(self, request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid json"}, status=400)
        title = body.get("title")
        if not isinstance(title, str) or not title.strip():
            return web.json_response({"error": "title required"}, status=400)
        updated = await asyncio.to_thread(
            rename_session, self.config.sessions_root, session_id, title
        )
        if not updated:
            return web.json_response({"error": "session not found"}, status=404)
        items = self._scan_sessions()
        await self.broadcast({"type": "sessions", "items": [s.to_dict() for s in items]})
        return web.json_response({"item": updated.to_dict()})

    async def handle_delete_session(self, request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        ok = await asyncio.to_thread(
            delete_session, self.config.sessions_root, session_id
        )
        if not ok:
            return web.json_response({"error": "session not found"}, status=404)

        self.acp_created_sessions.discard(session_id)
        removed_keys = [
            key
            for key, sid in list(self.remote_agent_session.items())
            if sid == session_id
        ]
        for key in removed_keys:
            self.remote_agent_session.pop(key, None)
        if removed_keys:
            self._persist_remote_sessions_map()

        try:
            if LAST_REMOTE_SESSION_FILE.is_file():
                last = LAST_REMOTE_SESSION_FILE.read_text(encoding="utf-8").strip()
                if last == session_id:
                    LAST_REMOTE_SESSION_FILE.write_text("", encoding="utf-8")
        except OSError as exc:
            log.debug("failed to clear last-remote-session.txt: %s", exc)

        items = self._scan_sessions()
        await self.broadcast({"type": "sessions", "items": [s.to_dict() for s in items]})
        await self.broadcast(self.status_payload())
        return web.json_response({"ok": True})

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

    async def handle_session_usage(self, request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        normalized = read_session_signals(self.config.sessions_root, session_id)
        path = find_signals_path(self.config.sessions_root, session_id)
        raw = read_signals_file(path) if path else None
        raw_subset: dict[str, Any] = {}
        if isinstance(raw, dict):
            for key in (
                "contextWindowUsage",
                "contextTokensUsed",
                "contextWindowTokens",
                "monthlyUsagePercent",
                "monthly_usage_percent",
                "usageMonthlyPercent",
                "isMonthly",
                "usagePeriod",
                "usage_period",
            ):
                if key in raw:
                    raw_subset[key] = raw[key]
            usage = raw.get("usage")
            if isinstance(usage, dict):
                raw_subset["usage"] = usage
        plan = await asyncio.to_thread(fetch_credits_usage)
        # Nested plan only — never surface tokens or auth material
        plan_safe = {
            "weeklyPercent": plan.get("weeklyPercent"),
            "periodType": plan.get("periodType"),
            "periodStart": plan.get("periodStart"),
            "periodEnd": plan.get("periodEnd"),
            "product": plan.get("product"),
            "available": bool(plan.get("available")),
            "error": plan.get("error"),
        }
        return web.json_response(
            {
                "sessionId": session_id,
                **normalized,
                "plan": plan_safe,
                "raw": raw_subset,
            }
        )

    async def handle_usage_plan(self, request: web.Request) -> web.Response:
        plan = await asyncio.to_thread(fetch_credits_usage)
        return web.json_response(
            {
                "weeklyPercent": plan.get("weeklyPercent"),
                "periodType": plan.get("periodType"),
                "periodStart": plan.get("periodStart"),
                "periodEnd": plan.get("periodEnd"),
                "product": plan.get("product"),
                "available": bool(plan.get("available")),
                "error": plan.get("error"),
            }
        )

    async def handle_projects(self, request: web.Request) -> web.Response:
        sessions = self._scan_sessions()
        items = list_projects(self.config.projects_root, sessions)
        return web.json_response({"items": items})

    async def handle_skills(self, request: web.Request) -> web.Response:
        try:
            items = await asyncio.to_thread(list_skills)
        except Exception:
            log.exception("list_skills failed")
            return web.json_response({"items": [], "error": "skills scan failed"}, status=500)
        return web.json_response({"items": items})

    async def handle_create_project(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        name = body.get("name")
        path = body.get("path")
        try:
            result = create_project(self.config.projects_root, name=name, path=path)
        except ProjectError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except OSError as exc:
            log.exception("create project failed")
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response(result)

    async def handle_fs_list(self, request: web.Request) -> web.Response:
        root = (request.query.get("root") or "").strip()
        if not root:
            return web.json_response({"error": "root required"}, status=400)
        path = request.query.get("path") or ""
        try:
            result = fs_list_dir(self.config.projects_root, root, path)
        except FsBrowserError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception:
            log.exception("fs list failed")
            return web.json_response({"error": "internal error"}, status=500)
        return web.json_response(result)

    async def handle_fs_read(self, request: web.Request) -> web.Response:
        root = (request.query.get("root") or "").strip()
        if not root:
            return web.json_response({"error": "root required"}, status=400)
        path = request.query.get("path") or ""
        try:
            result = fs_read_text(self.config.projects_root, root, path)
        except FsBrowserError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception:
            log.exception("fs read failed")
            return web.json_response({"error": "internal error"}, status=500)
        return web.json_response(result)

    async def handle_fs_raw(self, request: web.Request) -> web.Response:
        root = (request.query.get("root") or "").strip()
        if not root:
            return web.json_response({"error": "root required"}, status=400)
        path = request.query.get("path") or ""
        if not str(path).strip():
            return web.json_response({"error": "path required"}, status=400)
        try:
            file_path = resolve_file_for_read(
                self.config.projects_root, root, path
            )
            size = file_path.stat().st_size
            if size > 15_000_000:
                return web.json_response({"error": "file too large"}, status=413)
            ctype = content_type_for(file_path)
            return web.FileResponse(
                path=file_path,
                headers={
                    "Content-Type": ctype,
                    "Cache-Control": "private, max-age=60",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except FsBrowserError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception:
            log.exception("fs raw failed")
            return web.json_response({"error": "internal error"}, status=500)

    async def handle_fs_write(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid json"}, status=400)
        root = body.get("root")
        if root is None or not str(root).strip():
            return web.json_response({"error": "root required"}, status=400)
        path = body.get("path")
        if path is None or not str(path).strip():
            return web.json_response({"error": "path required"}, status=400)
        content = body.get("content", "")
        if content is None:
            content = ""
        try:
            result = fs_write_text(
                self.config.projects_root, str(root).strip(), str(path).strip(), content
            )
        except FsBrowserError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception:
            log.exception("fs write failed")
            return web.json_response({"error": "internal error"}, status=500)
        return web.json_response(result)

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
            self._record_hub_session(session_id, cwd)
            asyncio.create_task(self._stamp_origin_with_retry(session_id, "user"))
        except Exception as exc:
            log.exception("session/new failed")
            return web.json_response({"error": str(exc)}, status=500)
        await self.broadcast(self.status_payload())
        items = self._scan_sessions()
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
        # Only allow session/load for hub-created sessions (never foreign/CLI).
        if needs_fresh_agent_session(session_id, self.acp_created_sessions):
            return web.json_response(
                {
                    "error": "foreign session; use POST /api/sessions/{id}/attach for live remote",
                    "code": "foreign_session",
                },
                status=400,
            )
        if self.acp.turn_running and self.acp.is_turn_stuck():
            stuck_sid = self.acp.turn_session_id
            self.acp.force_clear_turn("auto-clear stuck turn before session load")
            if stuck_sid:
                await self.broadcast(
                    {
                        "type": "turn",
                        "sessionId": stuck_sid,
                        "state": "idle",
                        "error": "Turn cleared (stuck)",
                    },
                    session_id=stuck_sid,
                )
            await self.broadcast(self.status_payload())
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
        cmds = list(self.acp.available_commands or [])
        if cmds:
            await self.broadcast(
                {
                    "type": "commands",
                    "sessionId": session_id,
                    "commands": cmds,
                },
                session_id=session_id,
            )
        return web.json_response(
            {"sessionId": session_id, "loaded": True, "commands": cmds}
        )

    async def handle_attach_session(self, request: web.Request) -> web.Response:
        """Attach for chat: ensure live hub session for cwd (no prompt required).

        Returns view vs live ids so the client can subscribe to the live stream
        immediately (attach-on-open, not switch-on-first-send).
        """
        view_session_id = request.match_info["id"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        session = find_session(self.config.sessions_root, view_session_id)
        cwd = (body.get("cwd") or (session.cwd if session else "") or "").strip()
        if not cwd:
            return web.json_response({"error": "cwd required"}, status=400)
        if not self.acp.connected:
            return web.json_response({"error": "agent not connected"}, status=503)

        if self.acp.turn_running and self.acp.is_turn_stuck():
            stuck_sid = self.acp.turn_session_id
            self.acp.force_clear_turn("auto-clear stuck turn before attach")
            if stuck_sid:
                await self.broadcast(
                    {
                        "type": "turn",
                        "sessionId": stuck_sid,
                        "state": "idle",
                        "error": "Turn cleared (stuck)",
                    },
                    session_id=stuck_sid,
                )
            await self.broadcast(self.status_payload())

        try:
            live_id, switched, reason = await self._ensure_hub_agent_session(
                view_session_id,
                cwd,
                ws=None,
                notify_switch=True,
            )
        except Exception as exc:
            log.exception("attach failed view=%s", view_session_id)
            return web.json_response({"error": str(exc)}, status=500)

        cmds = list(self.acp.available_commands or [])
        if cmds:
            await self.broadcast(
                {
                    "type": "commands",
                    "sessionId": live_id,
                    "commands": cmds,
                },
                session_id=live_id,
            )

        message = REMOTE_SESSION_SYSTEM_NOTE if switched else REMOTE_SESSION_SAME_NOTE
        return web.json_response(
            {
                "viewSessionId": view_session_id,
                "liveSessionId": live_id,
                "switched": switched,
                "reason": reason,
                "cwd": cwd,
                "message": message,
                "commands": cmds,
            }
        )

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.clients.add(ws)
        self.subscriptions[ws] = set()
        await ws.send_str(json.dumps(self.status_payload()))
        items = self._scan_sessions()
        await ws.send_str(json.dumps({"type": "sessions", "items": [s.to_dict() for s in items]}))
        # Push cached agent slash commands so reconnect/new clients get real list
        # without waiting for attach or a later available_commands_update.
        if self.acp.available_commands:
            await ws.send_str(
                json.dumps(
                    {
                        "type": "commands",
                        "sessionId": None,
                        "commands": self.acp.available_commands,
                    }
                )
            )

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
                session = find_session(self.config.sessions_root, sid)
                path = Path(session.path) if session else None
                # Push history to this socket first, then start/resume tail (atomic catch-up).
                messages = load_session_history(
                    self.config.sessions_root,
                    sid,
                    session_path=path,
                    max_messages=self.config.max_history_messages,
                )
                try:
                    await ws.send_str(
                        json.dumps(
                            {"type": "history", "sessionId": sid, "messages": messages},
                            default=str,
                        )
                    )
                except Exception:
                    log.debug("failed to send history on subscribe session=%s", sid, exc_info=True)
                if self.acp.available_commands:
                    try:
                        await ws.send_str(
                            json.dumps(
                                {
                                    "type": "commands",
                                    "sessionId": sid,
                                    "commands": self.acp.available_commands,
                                }
                            )
                        )
                    except Exception:
                        log.debug(
                            "failed to send commands on subscribe session=%s",
                            sid,
                            exc_info=True,
                        )
                await self.tailer.ensure_watching(sid, path)
            return
        if typ == "unsubscribe":
            sid = str(payload.get("sessionId") or "")
            if sid:
                self.subscriptions.setdefault(ws, set()).discard(sid)
                # Never stop_watching on unsubscribe: keep tail + offsets alive for reconnect.
            return
        if typ == "prompt":
            # Never block the WS receive loop on long agent turns. Blocking here
            # starves ping/pong and drops mobile/desktop clients mid-turn
            # (keepalive ping timeout). ACP serializes turns via its own lock.
            task = asyncio.create_task(
                self._ws_prompt_safe(ws, payload),
                name=f"hub-prompt-{payload.get('sessionId', '')!s}"[:48],
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
            return
        if typ == "cancel":
            task = asyncio.create_task(
                self._ws_cancel_safe(ws, payload),
                name="hub-cancel",
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
            return
        if typ == "user_question_answer":
            await self._ws_user_question_answer(ws, payload)
            return
        await ws.send_str(json.dumps({"type": "error", "message": f"unknown type: {typ}"}))

    async def _ws_prompt_safe(self, ws: web.WebSocketResponse, payload: dict[str, Any]) -> None:
        try:
            await self._ws_prompt(ws, payload)
        except Exception:
            log.exception("background prompt task failed")
            try:
                sid = str(payload.get("sessionId") or "")
                if not ws.closed:
                    await ws.send_str(
                        json.dumps(
                            {
                                "type": "error",
                                "message": "Prompt failed unexpectedly. You can send again.",
                            }
                        )
                    )
                if sid:
                    await self._broadcast_turn(sid, sid, "idle", "Prompt failed unexpectedly")
            except Exception:
                pass

    async def _ws_cancel_safe(self, ws: web.WebSocketResponse, payload: dict[str, Any]) -> None:
        try:
            await self._ws_cancel(ws, payload)
        except Exception:
            log.exception("background cancel task failed")

    def _is_no_output_error(self, exc: BaseException) -> bool:
        msg = str(exc).lower()
        return "no acp session/update" in msg or (
            "force-cleared" in msg and "session/update" in msg
        )

    def _is_mid_turn_stall_error(self, exc: BaseException) -> bool:
        msg = str(exc).lower()
        return "mid-turn stall" in msg

    def _is_max_turn_error(self, exc: BaseException) -> bool:
        msg = str(exc).lower()
        return "max turn duration" in msg

    async def _broadcast_turn(
        self,
        session_id: str,
        state: str,
        error: str | None = None,
        also_session_id: str | None = None,
    ) -> None:
        """Broadcast turn state; optionally also to a view session id (unlock UI)."""
        payload = {
            "type": "turn",
            "sessionId": session_id,
            "state": state,
            "error": error,
        }
        await self.broadcast(payload, session_id=session_id)
        if also_session_id and also_session_id != session_id:
            also = {
                "type": "turn",
                "sessionId": also_session_id,
                "state": state,
                "error": error,
            }
            await self.broadcast(also, session_id=also_session_id)

    async def _ws_prompt(self, ws: web.WebSocketResponse, payload: dict[str, Any]) -> None:
        view_session_id = str(payload.get("sessionId") or "")
        text = str(payload.get("text") or "")
        if not view_session_id or not text.strip():
            await ws.send_str(json.dumps({"type": "error", "message": "sessionId and text required"}))
            return
        if not self.acp.connected:
            await ws.send_str(json.dumps({"type": "error", "message": "agent not connected"}))
            await ws.send_str(
                json.dumps(
                    {
                        "type": "turn",
                        "sessionId": view_session_id,
                        "state": "idle",
                        "error": "agent not connected",
                    }
                )
            )
            return

        # Auto-clear only when watchdog would (mid-turn stall / max wall / no-output)
        if self.acp.turn_running and self.acp.is_turn_stuck():
            stuck_sid = self.acp.turn_session_id
            self.acp.force_clear_turn("auto-clear stuck turn before new prompt")
            if stuck_sid:
                await self._broadcast_turn(
                    stuck_sid, "idle", "Turn cleared (stuck)", also_session_id=view_session_id
                )
            await self.broadcast(self.status_payload())

        cwd_raw = str(payload.get("cwd") or "")
        # While a turn is active, queue instead of rejecting (TUI-like).
        if self.acp.turn_running:
            await self._enqueue_prompt(ws, view_session_id, text, cwd_raw)
            return

        await self._execute_prompt(
            view_session_id, text, cwd_raw, ws=ws, echo_user=True
        )
        await self._drain_prompt_queue()

    async def _enqueue_prompt(
        self,
        ws: web.WebSocketResponse,
        view_session_id: str,
        text: str,
        cwd_raw: str,
    ) -> None:
        """Queue a prompt while a turn is running. Echoes user text immediately."""
        async with self._prompt_queue_lock:
            position = self._prompt_queue.try_enqueue(
                {
                    "view_session_id": view_session_id,
                    "text": text,
                    "cwd": cwd_raw,
                }
            )
            if position is None:
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "error",
                            "message": "Queue full (max 10). Wait for turns to finish.",
                        }
                    )
                )
                return
            queue_length = len(self._prompt_queue)

        await self.broadcast(
            {
                "type": "queued",
                "sessionId": view_session_id,
                "text": text,
                "position": position,
                "queueLength": queue_length,
            }
        )
        # Unscoped echo: view id may differ from live selection; all clients get it.
        # Client accepts user_message_chunk when turnRunning or session matches.
        await self.broadcast(
            {
                "type": "acp",
                "sessionId": view_session_id,
                "message": {
                    "method": "session/update",
                    "params": {
                        "sessionId": view_session_id,
                        "update": {
                            "sessionUpdate": "user_message_chunk",
                            "content": {"type": "text", "text": text},
                        },
                    },
                },
            }
        )
        await self.broadcast(self.status_payload())

    async def _execute_prompt(
        self,
        view_session_id: str,
        text: str,
        cwd_raw: str,
        *,
        ws: web.WebSocketResponse | None = None,
        echo_user: bool = True,
    ) -> None:
        """Run one prompt turn. Does not enqueue; caller drains the queue after."""
        session = find_session(self.config.sessions_root, view_session_id)
        cwd = (cwd_raw or (session.cwd if session else "") or "").strip()
        if not cwd and self.acp.loaded_session_id != view_session_id:
            err = "cwd unknown for session"
            if ws is not None and not ws.closed:
                await ws.send_str(json.dumps({"type": "error", "message": err}))
            await self._broadcast_turn(view_session_id, "idle", err)
            await self.broadcast(self.status_payload())
            return

        session_id = view_session_id
        # Hub-owned session for prompts (CLI / foreign ids cannot be prompted)
        try:
            session_id, _switched, _reason = await self._ensure_hub_agent_session(
                view_session_id, cwd, ws=ws, notify_switch=True
            )
        except Exception as exc:
            log.exception("ensure hub agent session failed view=%s", view_session_id)
            if ws is not None and not ws.closed:
                await ws.send_str(json.dumps({"type": "error", "message": str(exc)}))
            await self._broadcast_turn(
                view_session_id, "idle", str(exc), also_session_id=session_id
            )
            await self.broadcast(self.status_payload())
            return

        if ws is not None:
            self.subscriptions.setdefault(ws, set()).add(session_id)
        log.info(
            "WS prompt start session=%s view=%s hub_created=%s",
            session_id,
            view_session_id,
            session_id in self.acp_created_sessions,
        )
        # Running on live id; also unlock view selection if still on foreign id
        await self._broadcast_turn(
            session_id, "running", None, also_session_id=view_session_id
        )
        # Echo user message as acp-shaped update so all clients see it immediately
        # (skip when item was already echoed at enqueue time)
        if echo_user:
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

        # Hub-created sessions are already loaded via session/new; never load foreign ids.
        allow_load = session_id in self.acp_created_sessions
        try:
            await self.acp.session_prompt(
                session_id,
                text,
                cwd=cwd or None,
                allow_load=allow_load,
            )
            log.info("WS prompt end session=%s ok", session_id)
            await self._broadcast_turn(
                session_id, "idle", None, also_session_id=view_session_id
            )
        except Exception as exc:
            log.exception("prompt failed session=%s", session_id)
            if self.acp.turn_running and self.acp.turn_session_id == session_id:
                self.acp.force_clear_turn(f"prompt exception: {exc}")

            err_msg = str(exc)
            # Hang with zero output: drop cached remote, create fresh for next send
            if self._is_no_output_error(exc):
                err_msg = NO_OUTPUT_USER_MSG
                key = self._cwd_key(cwd)
                if key and self.remote_agent_session.get(key) == session_id:
                    self.remote_agent_session.pop(key, None)
                    self._persist_remote_sessions_map()
                self.acp_created_sessions.discard(session_id)
                try:
                    fresh = await self.acp.session_new(cwd)
                    self._record_hub_session(fresh, cwd)
                    asyncio.create_task(self._stamp_origin_with_retry(fresh, "attach"))
                    switch = {
                        "type": "session_switch",
                        "from": session_id,
                        "to": fresh,
                        "reason": "no_output_retry",
                        "cwd": cwd,
                        "message": NO_OUTPUT_USER_MSG,
                    }
                    await self.broadcast(switch)
                    if ws is not None:
                        self.subscriptions.setdefault(ws, set()).add(fresh)
                    items = self._scan_sessions()
                    await self.broadcast(
                        {"type": "sessions", "items": [s.to_dict() for s in items]}
                    )
                    log.info(
                        "no-output: prepared fresh remote session %s (was %s)",
                        fresh,
                        session_id,
                    )
                except Exception as create_exc:
                    log.warning("no-output: fresh session create failed: %s", create_exc)
            elif self._is_mid_turn_stall_error(exc):
                err_msg = MID_TURN_STALL_USER_MSG
            elif self._is_max_turn_error(exc):
                err_msg = MAX_TURN_USER_MSG
            elif "force-cleared" in str(exc).lower():
                err_msg = MID_TURN_STALL_USER_MSG

            if self.acp.last_force_clear_reason:
                self._last_broadcast_force_clear = self.acp.last_force_clear_reason

            await self._broadcast_turn(
                session_id, "idle", err_msg, also_session_id=view_session_id
            )
            await self.broadcast({"type": "error", "message": err_msg})
        finally:
            # Always re-assert idle unlock path via status (success already idled above)
            if self.acp.turn_running and self.acp.turn_session_id == session_id:
                self.acp.force_clear_turn("prompt finally safeguard")
                await self._broadcast_turn(
                    session_id, "idle", None, also_session_id=view_session_id
                )
        await self.broadcast(self.status_payload())

    async def _drain_prompt_queue(self) -> None:
        """Run queued prompts FIFO until empty or a turn is still running."""
        while True:
            async with self._prompt_queue_lock:
                if self.acp.turn_running or not self._prompt_queue:
                    return
                item = self._prompt_queue.pop()
                if item is None:
                    return
                remaining = len(self._prompt_queue)
            await self.broadcast(
                {
                    "type": "queue",
                    "queueLength": remaining,
                    "sessionId": item.get("view_session_id") or "",
                }
            )
            try:
                await self._execute_prompt(
                    str(item.get("view_session_id") or ""),
                    str(item.get("text") or ""),
                    str(item.get("cwd") or ""),
                    ws=None,
                    echo_user=False,
                )
            except Exception:
                log.exception("queued prompt failed")
                # continue draining remaining items

    async def _ws_cancel(self, ws: web.WebSocketResponse, payload: dict[str, Any]) -> None:
        session_id = str(payload.get("sessionId") or "")
        if not session_id:
            return
        # Stop cancels active turn and drops any waiting queued prompts.
        async with self._prompt_queue_lock:
            self._prompt_queue.clear()
        await self.broadcast({"type": "queue", "queueLength": 0, "sessionId": session_id})
        try:
            await self.acp.session_cancel(session_id)
        except Exception as exc:
            log.warning("session_cancel raised session=%s: %s — force-clearing", session_id, exc)
            try:
                self.acp.force_clear_turn(f"user cancel fallback: {exc}")
            except Exception:
                log.exception("force_clear_turn after cancel failure")
            try:
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "error",
                            "message": (
                                f"Stop: agent cancel failed ({exc}); "
                                "turn force-cleared locally."
                            ),
                        }
                    )
                )
            except Exception:
                pass
        # Always broadcast idle + status so clients unlock (turnRunning: false).
        await self.broadcast(
            {"type": "turn", "sessionId": session_id, "state": "idle", "error": None},
            session_id=session_id,
        )
        await self.broadcast(self.status_payload())


def create_app(config: Config | None = None) -> web.Application:
    cfg = config or __import__("hub.config", fromlist=["load_config"]).load_config()
    hub = Hub(cfg)
    hosts, mode, ts_ip = resolve_bind_hosts(cfg)
    hub.bind_hosts = hosts
    hub.bind_host = ts_ip if ts_ip and ts_ip in hosts else hosts[0]
    hub.bind_mode = mode
    hub.tailscale_ip = ts_ip
    return hub.build_app()
