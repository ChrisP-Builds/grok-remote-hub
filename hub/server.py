from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import secrets
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from hub.acp_client import AcpClient
from hub.agent_supervisor import AgentSupervisor
from hub.config import Config, PROJECT_ROOT, TAILSCALE_EXE
from hub.fs_browser import (
    RAW_MAX_BYTES,
    FsBrowserError,
    content_disposition_attachment,
    content_type_for,
    list_dir as fs_list_dir,
    read_text as fs_read_text,
    resolve_file_for_read,
    write_text as fs_write_text,
    write_upload_bytes as fs_write_upload_bytes,
)
from hub.site_preview import (
    SitePreviewError,
    SitePreviewManager,
    build_preview_plan,
)
from hub.history import load_session_history
from hub.plan_view import PlanViewError, apply_plan_action, read_session_plan
from hub.projects import ProjectError, create_project, list_project_browse
from hub.multi_turn import (
    STATUS_IDLE,
    can_start_concurrent_turn,
    merge_session_flags,
)
from hub.prompt_queue import PromptQueue
from hub.session_index import (
    delete_session,
    find_session,
    list_projects,
    read_hub_origin,
    rename_session,
    scan_sessions,
    stamp_hub_origin,
)
from hub.billing_usage import fetch_credits_usage
from hub.status_view import (
    ACP_HEAL_MAX_ATTEMPTS,
    map_agent_status,
    should_attempt_acp_heal,
)
from hub.session_signals import read_session_signals, read_signals_file, find_signals_path
from hub.session_policy import (
    CONTEXT_SOFT_MESSAGE,
    STUCK_TURN_SECONDS,
    context_budget_level,
    cwd_key,
    is_hub_resume_candidate,
    is_no_output_error_message,
    load_hub_session_ids,
    load_remote_sessions,
    resolve_ensure_action,
    save_remote_sessions,
    should_auto_retry_no_output,
    turn_telemetry,
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
    "Agent produced no output. Same session kept — send again."
)
NO_OUTPUT_RECOVERING_MSG = "Recovering session — retrying…"
NO_OUTPUT_RETRY_FAILED_MSG = (
    "Agent still not responding after automatic retry. Try again in a moment."
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
        # Process identity: changes on every hub process start (restart detection).
        self.boot_id = secrets.token_hex(8)
        self.started_at = time.time()
        self.bind_host = "127.0.0.1"
        self.bind_hosts: list[str] = ["127.0.0.1"]
        self.bind_mode = "local"
        self.tailscale_ip: str | None = None
        self.agent_status = "down"
        self.acp_connected = False
        # ACP self-heal: reconnect when process is up but WebSocket is down.
        self._acp_heal_attempts = 0
        self._acp_heal_max = ACP_HEAL_MAX_ATTEMPTS
        self._acp_disconnected_at: float | None = None  # monotonic
        self._acp_heal_last_error: str | None = None
        self._acp_heal_in_progress = False
        self._agent_restart_in_progress = False
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
        self.acp.on_terminal_out = self._on_terminal_out
        self._acp_dedupe = EventDedupe(maxlen=2000)
        self.tailer = SessionTailer(
            config.sessions_root,
            on_event=self._on_disk_event,
            poll_interval=0.25,
        )
        # Session ids created via session/new (or resumed via session/load) in this process.
        # Process-local only; not seeded from disk (disk ids need session/load first).
        self.acp_created_sessions: set[str] = set()
        # Durable hub-owned ids from remote-sessions.json hubIds (resume candidates).
        self.hub_owned_session_ids: set[str] = set()
        # cwd (casefold) -> last hub-created agent session id for remote prompts.
        self.remote_agent_session: dict[str, str] = {}
        self.remote_sessions_path = REMOTE_SESSIONS_FILE
        self._load_remote_sessions_map()
        # Per-cwd FIFO queues while that project has a turn running (data only).
        self._prompt_queues: dict[str, PromptQueue] = {}
        self._prompt_queue_lock = asyncio.Lock()
        self._max_concurrent_turns = max(1, int(getattr(config, "max_concurrent_turns", 3) or 3))
        self._app: web.Application | None = None
        self._status_resync_task: asyncio.Task | None = None
        self.site_preview = SitePreviewManager()
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
        """Process-live ids first, then durable disk hub-owned / remote-map values.

        Client uses this for isHubCreatedSession after restart (before attach).
        """
        recent = list(self.remote_agent_session.values())
        ordered: list[str] = []
        seen: set[str] = set()
        # Live first: prefer recently recorded cwd map values that are process-live
        for sid in reversed(recent):
            if sid in self.acp_created_sessions and sid not in seen:
                ordered.append(sid)
                seen.add(sid)
        for sid in self.acp_created_sessions:
            if sid not in seen:
                ordered.append(sid)
                seen.add(sid)
        # Disk resume candidates so UI marks hub-owned before attach
        for sid in reversed(recent):
            if sid not in seen:
                ordered.append(sid)
                seen.add(sid)
        for sid in sorted(self.hub_owned_session_ids):
            if sid not in seen:
                ordered.append(sid)
                seen.add(sid)
        return ordered[:limit]

    def _queue_total_length(self) -> int:
        return sum(len(q) for q in self._prompt_queues.values())

    def _session_flags_map(self, extra_ids: list[str] | None = None) -> dict[str, str]:
        ids = list(self._hub_session_ids(50))
        for sid in self.acp.turn_session_ids:
            if sid not in ids:
                ids.append(sid)
        for sid in self.acp.sessions_with_pending_questions():
            if sid not in ids:
                ids.append(sid)
        if extra_ids:
            for sid in extra_ids:
                if sid and sid not in ids:
                    ids.append(sid)
        return merge_session_flags(
            ids,
            active_sessions=set(self.acp.turn_session_ids),
            pending_question_sessions=self.acp.sessions_with_pending_questions(),
        )

    def _sessions_items_with_status(self) -> list[dict[str, Any]]:
        items = self._scan_sessions()
        flags = self._session_flags_map([s.sessionId for s in items])
        out: list[dict[str, Any]] = []
        for s in items:
            d = s.to_dict()
            d["liveStatus"] = flags.get(s.sessionId, STATUS_IDLE)
            out.append(d)
        return out

    def _map_agent_from_live(self) -> tuple[dict[str, str | bool], dict[str, Any]]:
        """Quality-aware agent status from process flag + ACP wire liveness."""
        live = self.acp.acp_liveness_snapshot()
        mapped = map_agent_status(
            self.agent_status == "up",
            self.acp_connected,
            consecutive_send_failures=int(live.get("consecutive_send_failures") or 0),
            seconds_since_recv=live.get("seconds_since_recv"),
            has_pending=bool(live.get("has_pending")),
        )
        return mapped, live

    def _live_turns_payload(self) -> list[dict[str, Any]]:
        """liveTurns entries with age/silence/sawUpdate/ttfb telemetry."""
        now = time.monotonic()
        out: list[dict[str, Any]] = []
        for sid in self.acp.turn_session_ids:
            meta = self.acp.active_turns.get(sid) or {}
            tel = turn_telemetry(
                started_at=meta.get("started_at"),
                last_activity=meta.get("last_activity"),
                saw_update=bool(meta.get("saw_update")),
                now=now,
                first_update_at=meta.get("first_update_at"),
            )
            out.append(
                {
                    "sessionId": sid,
                    "state": "running",
                    "ageSeconds": tel["ageSeconds"],
                    "silenceSeconds": tel["silenceSeconds"],
                    "sawUpdate": tel["sawUpdate"],
                    "ttfbSeconds": tel["ttfbSeconds"],
                }
            )
        return out

    def _primary_turn_telemetry(self) -> dict[str, Any]:
        """Telemetry for primary (most recent) active turn; empty when idle."""
        sid = self.acp.turn_session_id
        if not sid:
            return {
                "ageSeconds": None,
                "silenceSeconds": None,
                "sawUpdate": False,
                "ttfbSeconds": None,
            }
        meta = self.acp.active_turns.get(sid) or {}
        return turn_telemetry(
            started_at=meta.get("started_at"),
            last_activity=meta.get("last_activity"),
            saw_update=bool(meta.get("saw_update")),
            now=time.monotonic(),
            first_update_at=meta.get("first_update_at"),
        )

    def _capacity_payload(self) -> dict[str, Any]:
        busy = list(self.acp.turn_session_ids)
        return {
            "activeTurnCount": len(busy),
            "maxConcurrentTurns": self._max_concurrent_turns,
            "busySessionIds": busy,
        }

    def _resolve_loaded_session_dir(self, session_id: str) -> Path | None:
        """Cheap path resolve for primary loaded session (no full scan).

        Layout: sessions_root / <encoded_cwd> / <session_id> /
        Best effort only; returns None when unknown.
        """
        sid = str(session_id or "").strip()
        root = Path(self.config.sessions_root)
        if not sid or not root.is_dir():
            return None
        try:
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                candidate = child / sid
                if candidate.is_dir():
                    return candidate
        except OSError:
            return None
        return None

    def _context_budget_payload(self) -> dict[str, Any] | None:
        """Soft context budget for primary loadedSessionId only. Omit if unknown."""
        sid = self.acp.loaded_session_id
        if not sid:
            return None
        updates_bytes: int | None = None
        total_tokens: int | None = None
        try:
            session_dir = self._resolve_loaded_session_dir(str(sid))
            if session_dir is not None:
                updates = session_dir / "updates.jsonl"
                if updates.is_file():
                    updates_bytes = int(updates.stat().st_size)
                # Optional tokens from signals.json when present (skip if hard).
                signals_path = session_dir / "signals.json"
                if signals_path.is_file():
                    try:
                        raw = json.loads(signals_path.read_text(encoding="utf-8"))
                        if isinstance(raw, dict):
                            tok = raw.get("contextTokensUsed")
                            if tok is not None and not isinstance(tok, bool):
                                total_tokens = int(tok)
                    except (
                        OSError,
                        json.JSONDecodeError,
                        TypeError,
                        ValueError,
                        UnicodeError,
                    ):
                        pass
        except OSError:
            pass
        level = context_budget_level(
            updates_bytes=updates_bytes,
            total_tokens=total_tokens,
        )
        return {
            "level": level,
            "updatesBytes": updates_bytes,
            "message": CONTEXT_SOFT_MESSAGE if level == "soft" else "",
        }

    def status_payload(self) -> dict[str, Any]:
        mapped, live = self._map_agent_from_live()
        compat = self.compat or {}
        issues = list(compat.get("issues") or [])
        live_turns = self._live_turns_payload()
        primary_tel = self._primary_turn_telemetry()
        capacity = self._capacity_payload()
        context_budget = self._context_budget_payload()
        pending_q = sorted(self.acp.sessions_with_pending_questions())
        session_flags = self._session_flags_map()
        heal_exhausted = (
            self._acp_heal_attempts >= self._acp_heal_max
            and not bool(mapped["acpConnected"])
            and mapped["agentProcess"] == "up"
        )
        body: dict[str, Any] = {
            "type": "status",
            "agent": mapped["agent"],
            "agentProcess": mapped["agentProcess"],
            "agentDetail": mapped["agentDetail"],
            "acpQuality": mapped.get("acpQuality", "down"),
            "bind": self.bind_mode,
            "tailscaleIp": self.tailscale_ip,
            "acpConnected": mapped["acpConnected"],
            "acpConsecutiveSendFailures": int(
                live.get("consecutive_send_failures") or 0
            ),
            "acpLastRecvAgeSeconds": live.get("seconds_since_recv"),
            "acpLastSendOkAgeSeconds": live.get("seconds_since_send_ok"),
            "acpHealAttempts": self._acp_heal_attempts,
            "acpHealError": self._acp_heal_last_error if heal_exhausted else None,
            "loadedSessionId": self.acp.loaded_session_id,
            "turnRunning": self.acp.turn_running,
            "turnSessionId": self.acp.turn_session_id,
            "turnAgeSeconds": primary_tel["ageSeconds"],
            "turnSilenceSeconds": primary_tel["silenceSeconds"],
            "liveTurns": live_turns,
            "pendingQuestionSessions": pending_q,
            "sessionFlags": session_flags,
            "maxConcurrentTurns": self._max_concurrent_turns,
            "activeTurnCount": capacity["activeTurnCount"],
            "capacity": capacity,
            "promptQueueLength": self._queue_total_length(),
            "hubVersion": HUB_VERSION,
            "cliVersion": self.cli_version or compat.get("cliVersion"),
            "compatOk": bool(compat.get("ok")),
            "compatIssues": issues[:8],
            "productTag": "remote-stream",
            "hubSessionIds": self._hub_session_ids(50),
            "bootId": self.boot_id,
            "startedAt": self._started_at_iso(),
        }
        if context_budget is not None:
            body["contextBudget"] = context_budget
        return body

    def _started_at_iso(self) -> str:
        return (
            datetime.fromtimestamp(self.started_at, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _now_iso() -> str:
        return (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )

    async def _emit_error(
        self,
        message: str,
        *,
        session_id: str | None = None,
        ws: web.WebSocketResponse | None = None,
        level: str = "warning",
    ) -> None:
        """Log + send a client-facing error (WS unicast or broadcast)."""
        msg = str(message or "Error")
        log_fn = log.error if level == "error" else log.warning
        log_fn("client_error session=%s: %s", session_id or "-", msg)
        payload: dict[str, Any] = {
            "type": "error",
            "message": msg,
            "sessionId": session_id,
            "at": self._now_iso(),
        }
        if ws is not None and not ws.closed:
            try:
                await ws.send_str(json.dumps(payload, default=str))
            except Exception:
                log.debug("failed to send error to ws session=%s", session_id or "-", exc_info=True)
            return
        await self.broadcast(payload)

    async def _on_agent_status(self, status: str) -> None:
        self.agent_status = status
        if status != "up":
            # Process down: do not auto-heal; reset budget for next process-up.
            self._acp_heal_attempts = 0
            self._acp_heal_last_error = None
        await self.broadcast(self.status_payload())

    async def _on_acp_connection(self, connected: bool) -> None:
        self.acp_connected = connected
        if connected:
            if self._acp_heal_attempts or self._acp_heal_last_error:
                log.info(
                    "ACP heal: connected; reset heal attempts (was n=%s)",
                    self._acp_heal_attempts,
                )
            self._acp_heal_attempts = 0
            self._acp_disconnected_at = None
            self._acp_heal_last_error = None
        else:
            if self._acp_disconnected_at is None:
                self._acp_disconnected_at = time.monotonic()
            # Prior process-local session/new ids are dead after ACP drop.
            # Keep remote_agent_session for UI badges; next ensure will session/new.
            if self.acp_created_sessions:
                log.info(
                    "ACP disconnected; clearing %d agent-live session id(s)",
                    len(self.acp_created_sessions),
                )
            self.acp_created_sessions.clear()
            sids = list(self.acp.disconnect_turn_session_ids or [])
            self.acp.disconnect_turn_session_ids = []
            sid = self.acp.disconnect_turn_session_id
            self.acp.disconnect_turn_session_id = None
            if sid and sid not in sids:
                sids.append(sid)
            if not sids and self.acp.turn_session_id:
                sids.append(self.acp.turn_session_id)
            for cleared in sids:
                await self.broadcast(
                    {
                        "type": "turn",
                        "sessionId": cleared,
                        "state": "idle",
                        "error": "ACP disconnected",
                    },
                    session_id=cleared,
                )
        await self.broadcast(self.status_payload())

    async def _on_acp_message(self, msg: dict[str, Any]) -> None:
        session_id = self._session_id_from_acp(msg)
        await self._emit_acp(session_id, msg)

    async def _on_user_question(self, payload: dict[str, Any]) -> None:
        """Fan out agent ask_user_question to all connected UIs."""
        session_id = payload.get("sessionId")
        if not session_id:
            log.warning(
                "user_question missing sessionId requestId=%s",
                payload.get("requestId"),
            )
        await self.broadcast(
            {
                "type": "user_question",
                "requestId": payload.get("requestId"),
                "sessionId": session_id,
                "questions": payload.get("questions") or [],
                "toolCallId": payload.get("toolCallId"),
            }
        )
        # Refresh sessionFlags / pendingQuestionSessions so rail shows question.
        await self.broadcast(self.status_payload())

    async def _on_terminal_out(self, payload: dict[str, Any]) -> None:
        """Fan out hub-hosted terminal/* pump deltas to subscribed UIs."""
        session_id = payload.get("sessionId")
        await self.broadcast(
            {
                "type": "terminal_out",
                "terminalId": payload.get("terminalId"),
                "delta": payload.get("delta") or "",
                "sessionId": session_id,
            },
            session_id=session_id if isinstance(session_id, str) else None,
        )

    async def _ws_user_question_answer(
        self, ws: web.WebSocketResponse, payload: dict[str, Any]
    ) -> None:
        """Resolve a pending ACP ask_user_question from the web UI."""
        from hub.acp_ask_user import build_accepted_result

        request_id = str(payload.get("requestId") or "")
        if not request_id:
            await self._emit_error(
                "user_question_answer missing requestId", ws=ws
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
        # Clear question flag in sessionFlags for the rail.
        await self.broadcast(self.status_payload())
        if not ok:
            await self._emit_error(
                f"No pending user question for requestId={request_id}",
                ws=ws,
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
        scoped = ("acp", "history", "commands", "turn", "system", "terminal_out")
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
        """Restore remote_agent_session + hub_owned_session_ids; never seed agent-live set.

        Disk ids from a prior hub/agent process are not live in this process.
        acp_created_sessions stays process-local until session/new or successful load.
        """
        mapping = load_remote_sessions(self.remote_sessions_path)
        self.remote_agent_session = dict(mapping)
        self.hub_owned_session_ids = load_hub_session_ids(self.remote_sessions_path)
        # Do NOT add disk ids to acp_created_sessions.
        if mapping or self.hub_owned_session_ids:
            log.info(
                "Restored remote map for UI only (%d mapping(s), %d hubId(s) from %s); "
                "agent-live set starts empty.",
                len(mapping),
                len(self.hub_owned_session_ids),
                self.remote_sessions_path,
            )

    def _persist_remote_sessions_map(self) -> None:
        try:
            save_remote_sessions(
                self.remote_sessions_path,
                self.remote_agent_session,
                hub_ids=self.hub_owned_session_ids,
            )
        except OSError as exc:
            log.debug("failed to write remote-sessions.json: %s", exc)

    def _record_hub_session(self, session_id: str, cwd: str) -> None:
        """Track a hub-created agent session and persist remote map + hubIds + last id."""
        sid = str(session_id)
        self.acp_created_sessions.add(sid)
        self.hub_owned_session_ids.add(sid)
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
        return set(self.remote_agent_session.values()) | set(self.hub_owned_session_ids)

    def _resume_map_ids(self) -> set[str]:
        """Remote map values plus durable hubIds (resume candidates after restart)."""
        return set(self.remote_agent_session.values()) | set(self.hub_owned_session_ids)

    def _stamp_origin_sync(self, session_id: str, origin: str) -> bool:
        """Immediate best-effort hub_origin stamp (summary may not exist yet)."""
        try:
            return stamp_hub_origin(self.config.sessions_root, session_id, origin)
        except Exception as exc:
            log.debug(
                "stamp_hub_origin sync error session=%s origin=%s: %s",
                session_id,
                origin,
                exc,
            )
            return False

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

    async def _try_session_load(self, session_id: str, cwd: str) -> bool:
        """Attempt session/load with hard timeout. True on success."""
        try:
            await asyncio.wait_for(
                self.acp.session_load(session_id, cwd),
                timeout=20.0,
            )
            return True
        except Exception as exc:
            log.warning("session/load failed for %s: %s", session_id, exc)
            return False

    async def _load_or_fallback_session(
        self,
        target: str,
        cwd: str,
        *,
        view_session_id: str,
        view_origin: str | None,
    ) -> str:
        """Load target; on fail retry once, then try view if different, else session/new.

        Prefers not abandoning the intended continuity id. When load fails for byCwd
        while view is a different hub resume candidate, tries load view before new.
        """
        view = str(view_session_id or "").strip() or None

        async def _succeed(sid: str, how: str) -> str:
            self._record_hub_session(sid, cwd)
            self._stamp_origin_sync(sid, "attach")
            asyncio.create_task(self._stamp_origin_with_retry(sid, "attach"))
            log.info(
                "Resumed hub session via %s %s for view %s cwd=%s",
                how,
                sid,
                view_session_id,
                cwd,
            )
            return sid

        if await self._try_session_load(target, cwd):
            return await _succeed(target, "session/load")

        # Retry once after short delay (transient agent/load races).
        log.warning(
            "session/load first attempt failed for %s; retrying after 0.5s",
            target,
        )
        await asyncio.sleep(0.5)
        if await self._try_session_load(target, cwd):
            return await _succeed(target, "session/load retry")

        # byCwd load failed while view is a different hub resume candidate → try view.
        if (
            view
            and view != target
            and is_hub_resume_candidate(
                view,
                created_set=self.acp_created_sessions,
                remote_map_ids=self._resume_map_ids(),
                hub_origin=view_origin,
            )
        ):
            log.warning(
                "session/load failed for target %s; trying view resume candidate %s",
                target,
                view,
            )
            if await self._try_session_load(view, cwd):
                return await _succeed(view, "session/load view-fallback")

        # Only mint new when continuity load is exhausted.
        log.error(
            "session/load exhausted for target=%s view=%s cwd=%s; "
            "falling back to session/new (continuity abandoned)",
            target,
            view,
            cwd,
        )
        agent_sid = await self.acp.session_new(cwd)
        self._record_hub_session(agent_sid, cwd)
        self._stamp_origin_sync(agent_sid, "attach")
        asyncio.create_task(self._stamp_origin_with_retry(agent_sid, "attach"))
        log.info(
            "Created hub remote session %s for view %s cwd=%s (resume_failed)",
            agent_sid,
            view_session_id,
            cwd,
        )
        return agent_sid

    async def _ensure_hub_agent_session(
        self,
        view_session_id: str,
        cwd: str,
        ws: web.WebSocketResponse | None = None,
        *,
        notify_switch: bool = True,
    ) -> tuple[str, bool, str]:
        """Return (live_session_id, switched, reason) safe for session/prompt.

        Process-live: reuse. Hub-owned after restart: session/load same id.
        Foreign/CLI or load failure: session/new. Optionally notify session_switch.
        """
        view_origin = read_hub_origin(self.config.sessions_root, view_session_id)
        key = self._cwd_key(cwd)
        remote_id = self.remote_agent_session.get(key) if key else None
        remote_origin = (
            read_hub_origin(self.config.sessions_root, remote_id) if remote_id else ""
        )
        target, action, reason = resolve_ensure_action(
            view_session_id,
            cwd,
            self.acp_created_sessions,
            self.remote_agent_session,
            view_hub_origin=view_origin or None,
            remote_hub_origin=remote_origin or None,
            hub_owned_ids=self.hub_owned_session_ids,
        )

        if action == "reuse" and target:
            agent_sid = target
            # Keep byCwd aligned with the working session (view wins).
            self._record_hub_session(agent_sid, cwd)
            log.info(
                "Reuse hub remote session %s for view %s cwd=%s reason=%s",
                agent_sid,
                view_session_id,
                cwd,
                reason,
            )
        elif action == "load" and target:
            agent_sid = await self._load_or_fallback_session(
                target,
                cwd,
                view_session_id=view_session_id,
                view_origin=view_origin or None,
            )
            if agent_sid != target:
                reason = "resume_failed"
        else:
            agent_sid = await self.acp.session_new(cwd)
            self._record_hub_session(agent_sid, cwd)
            self._stamp_origin_sync(agent_sid, "attach")
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
            if reason == "resume_failed":
                switch_reason = "resume_failed"
            elif reason in (
                "reuse_cwd",
                "resume_cwd",
                "need_session_new",
                "cli_or_foreign_session",
            ):
                switch_reason = "cli_or_foreign_session"
            else:
                switch_reason = reason
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
                    {"type": "sessions", "items": self._sessions_items_with_status()}
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
        app.router.add_get("/api/sessions/{id}/plan", self.handle_session_plan)
        app.router.add_post("/api/sessions/{id}/plan/action", self.handle_session_plan_action)
        app.router.add_get("/api/sessions/{id}/usage", self.handle_session_usage)
        app.router.add_get("/api/usage/plan", self.handle_usage_plan)
        app.router.add_post("/api/sessions", self.handle_new_session)
        app.router.add_patch("/api/sessions/{id}", self.handle_rename_session)
        app.router.add_delete("/api/sessions/{id}", self.handle_delete_session)
        app.router.add_post("/api/sessions/{id}/load", self.handle_load_session)
        app.router.add_post("/api/sessions/{id}/attach", self.handle_attach_session)
        app.router.add_post("/api/admin/reset-turn", self.handle_reset_turn)
        app.router.add_post("/api/admin/reconnect-acp", self.handle_reconnect_acp)
        app.router.add_post("/api/admin/restart-agent", self.handle_restart_agent)
        app.router.add_get("/api/projects", self.handle_projects)
        app.router.add_get("/api/projects/browse", self.handle_projects_browse)
        app.router.add_post("/api/projects", self.handle_create_project)
        app.router.add_get("/api/skills", self.handle_skills)
        app.router.add_get("/api/fs/list", self.handle_fs_list)
        app.router.add_get("/api/fs/read", self.handle_fs_read)
        app.router.add_get("/api/fs/raw", self.handle_fs_raw)
        app.router.add_put("/api/fs/write", self.handle_fs_write)
        app.router.add_post("/api/fs/upload", self.handle_fs_upload)
        app.router.add_post("/api/preview/start", self.handle_preview_start)
        app.router.add_post("/api/preview/stop", self.handle_preview_stop)
        app.router.add_get("/api/preview/status", self.handle_preview_status)
        # Register before static / so preview paths are not swallowed.
        app.router.add_get("/preview-site", self.handle_preview_site)
        app.router.add_get("/preview-site/", self.handle_preview_site)
        app.router.add_get("/preview-site/{path:.*}", self.handle_preview_site)
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
        if self.acp.turn_running:
            for sid in list(self.acp.turn_session_ids):
                if self.acp.is_turn_stuck(session_id=sid):
                    self.acp.force_clear_turn(
                        f"startup clear stuck turn age>{STUCK_TURN_SECONDS}s",
                        session_id=sid,
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
        self.site_preview.stop()
        await self.tailer.stop()
        await self.acp.stop()
        # Leave agent serve up across hub bounce (code deploy / restart-hub).
        # Full agent teardown: stop-hub.ps1 -KillAgent (default for stop alone).
        await self.supervisor.stop(kill_agent=False)

    async def _status_resync_loop(self) -> None:
        """While a turn is running, re-broadcast status so clients re-sync turnRunning.

        Near-streaming cadence (1s) while any turn is active; slower when idle.
        Also surfaces watchdog force-clears that may not have gone through WS prompt.
        Triggers ACP self-heal when agent process is up but ACP is disconnected.
        """
        try:
            while True:
                active = bool(self.acp.turn_running) or bool(self.acp.turn_session_ids)
                # 1.0s while turns run so rail pills stay fresh; 5–10s when idle.
                await asyncio.sleep(1.0 if active else 8.0)
                try:
                    await self._maybe_heal_acp()
                except Exception:
                    log.debug("ACP heal tick failed", exc_info=True)
                reason = self.acp.last_force_clear_reason
                if reason and reason != self._last_broadcast_force_clear:
                    sid = self.acp.last_force_clear_session
                    self._last_broadcast_force_clear = reason
                    if sid and not self.acp.is_session_active(sid):
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
                if self.acp.turn_running or bool(self.acp.turn_session_ids):
                    try:
                        await self.broadcast(self.status_payload())
                    except Exception:
                        log.debug("status resync broadcast failed", exc_info=True)
        except asyncio.CancelledError:
            return

    async def _maybe_heal_acp(self) -> None:
        """Reconnect ACP when agent process is up but chat-usable ACP is down.

        Uses quality-adjusted acpConnected (false for zombie/stale half-dead
        sockets) so heal runs without a separate quality branch. Capped
        attempts with backoff; never spawns extra agent processes.
        """
        mapped, _live = self._map_agent_from_live()
        chat_acp = bool(mapped["acpConnected"])
        if self._acp_disconnected_at is None and (
            self.agent_status == "up" and not chat_acp
        ):
            self._acp_disconnected_at = time.monotonic()
        disconnected_for: float | None = None
        if self._acp_disconnected_at is not None:
            disconnected_for = time.monotonic() - self._acp_disconnected_at
        if not should_attempt_acp_heal(
            agent_process_up=self.agent_status == "up",
            acp_connected=chat_acp,
            heal_in_progress=self._acp_heal_in_progress,
            attempts=self._acp_heal_attempts,
            disconnected_for_s=disconnected_for,
            max_attempts=self._acp_heal_max,
        ):
            return
        self._acp_heal_in_progress = True
        n = self._acp_heal_attempts + 1
        self._acp_heal_attempts = n
        log.info(
            "ACP heal: reconnect attempt n=%s/%s (agent up, acp down for %.1fs)",
            n,
            self._acp_heal_max,
            disconnected_for if disconnected_for is not None else -1.0,
        )
        try:
            await self.acp.reconnect(timeout=10.0)
            if self.acp.connected or self.acp_connected:
                log.info("ACP heal: reconnect ok n=%s", n)
                self._acp_heal_last_error = None
            else:
                self._acp_heal_last_error = "reconnect returned without connection"
                log.warning("ACP heal: failed n=%s: %s", n, self._acp_heal_last_error)
        except Exception as exc:
            self._acp_heal_last_error = str(exc)
            log.warning("ACP heal: failed n=%s: %s", n, exc)
        finally:
            self._acp_heal_in_progress = False
            if (
                self._acp_heal_attempts >= self._acp_heal_max
                and not self.acp_connected
            ):
                log.warning(
                    "ACP heal: exhausted attempts n=%s; last error=%s "
                    "(restart with KillAgent if agent hung)",
                    self._acp_heal_attempts,
                    self._acp_heal_last_error,
                )
                try:
                    await self.broadcast(self.status_payload())
                except Exception:
                    log.debug("ACP heal exhausted status broadcast failed", exc_info=True)

    async def handle_index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(Path(self.config.static_dir) / "index.html")

    async def handle_health(self, request: web.Request) -> web.Response:
        compat = self.compat or {}
        mapped, live = self._map_agent_from_live()
        primary_tel = self._primary_turn_telemetry()
        capacity = self._capacity_payload()
        context_budget = self._context_budget_payload()
        body: dict[str, Any] = {
            "ok": True,
            "agent": mapped["agent"],
            "agentProcess": mapped["agentProcess"],
            "agentDetail": mapped["agentDetail"],
            "acpQuality": mapped.get("acpQuality", "down"),
            "acpConnected": mapped["acpConnected"],
            "acpConsecutiveSendFailures": int(
                live.get("consecutive_send_failures") or 0
            ),
            "acpLastRecvAgeSeconds": live.get("seconds_since_recv"),
            "acpLastSendOkAgeSeconds": live.get("seconds_since_send_ok"),
            "acpHealAttempts": self._acp_heal_attempts,
            "acpHealError": (
                self._acp_heal_last_error
                if (
                    self._acp_heal_attempts >= self._acp_heal_max
                    and not bool(mapped["acpConnected"])
                    and mapped["agentProcess"] == "up"
                )
                else None
            ),
            "bind": self.bind_mode,
            "host": self.bind_host,
            "hosts": list(self.bind_hosts),
            "port": self.config.bind_port,
            "tailscaleIp": self.tailscale_ip,
            "loadedSessionId": self.acp.loaded_session_id,
            "turnRunning": self.acp.turn_running,
            "turnSessionId": self.acp.turn_session_id,
            "turnAgeSeconds": primary_tel["ageSeconds"],
            "turnSilenceSeconds": primary_tel["silenceSeconds"],
            "liveTurns": self._live_turns_payload(),
            "activeTurnCount": capacity["activeTurnCount"],
            "maxConcurrentTurns": self._max_concurrent_turns,
            "capacity": capacity,
            "pendingQuestionSessions": sorted(
                self.acp.sessions_with_pending_questions()
            ),
            "hubVersion": HUB_VERSION,
            "cliVersion": self.cli_version or compat.get("cliVersion"),
            "compatOk": bool(compat.get("ok")),
            "compatIssues": list(compat.get("issues") or [])[:8],
            "productTag": "remote-stream",
            "bootId": self.boot_id,
            "startedAt": self._started_at_iso(),
        }
        if context_budget is not None:
            body["contextBudget"] = context_budget
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
        """Force-clear stuck turn(s) so clients can send again.

        Optional JSON body ``sessionId`` clears only that turn; otherwise all.
        """
        body: dict[str, Any] = {}
        try:
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}
        target = str(body.get("sessionId") or "").strip() or None
        sids = [target] if target else list(self.acp.turn_session_ids)
        was = self.acp.turn_running
        cleared = self.acp.force_clear_turn(
            "admin POST /api/admin/reset-turn", session_id=target
        )
        if cleared:
            self._last_broadcast_force_clear = self.acp.last_force_clear_reason
        for sid in sids:
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
        # Always push full status so every client re-syncs turn state.
        await self.broadcast(self.status_payload())
        return web.json_response(
            {
                "ok": True,
                "cleared": cleared,
                "wasRunning": was,
                "sessionId": target or (sids[0] if sids else None),
                "sessionIds": sids,
            }
        )

    async def handle_reconnect_acp(self, request: web.Request) -> web.Response:
        """Manual ACP WebSocket reconnect (no KillAgent, no new agent process).

        Resets the auto-heal attempt budget so operators can retry after exhaustion.
        """
        if self.agent_status != "up":
            return web.json_response(
                {"ok": False, "error": "agent process down", "acpConnected": False},
                status=503,
            )
        if self._acp_heal_in_progress:
            return web.json_response(
                {
                    "ok": False,
                    "error": "ACP heal already in progress",
                    "acpConnected": self.acp_connected,
                },
                status=409,
            )
        self._acp_heal_attempts = 0
        self._acp_heal_last_error = None
        if self._acp_disconnected_at is None and not self.acp_connected:
            self._acp_disconnected_at = time.monotonic()
        log.info("ACP heal: manual reconnect via POST /api/admin/reconnect-acp")
        self._acp_heal_in_progress = True
        err: str | None = None
        try:
            await self.acp.reconnect(timeout=10.0)
            if not (self.acp.connected or self.acp_connected):
                err = "reconnect returned without connection"
                self._acp_heal_last_error = err
        except Exception as exc:
            err = str(exc)
            self._acp_heal_last_error = err
            log.warning("ACP heal: manual reconnect failed: %s", exc)
        finally:
            self._acp_heal_in_progress = False
        await self.broadcast(self.status_payload())
        ok = bool(self.acp_connected)
        body: dict[str, Any] = {
            "ok": ok,
            "acpConnected": ok,
            "agentProcess": self.agent_status,
        }
        if err and not ok:
            body["error"] = err
        return web.json_response(body, status=200 if ok else 502)

    async def handle_restart_agent(self, request: web.Request) -> web.Response:
        """KillAgent-style agent process restart without restarting the hub.

        Force-clears turns, closes ACP, kills the serve process (including
        attached listeners), waits for supervisor respawn, reconnects ACP.
        """
        if self._agent_restart_in_progress:
            return web.json_response(
                {
                    "ok": False,
                    "error": "agent restart already in progress",
                    "acpConnected": self.acp_connected,
                    "agentProcess": self.agent_status,
                },
                status=409,
            )
        self._agent_restart_in_progress = True
        log.warning("Admin restart-agent: begin (KillAgent-style, hub stays up)")
        err: str | None = None
        try:
            # 1) Drop in-flight turns so clients unlock after kill.
            try:
                sids = list(self.acp.turn_session_ids)
                cleared = self.acp.force_clear_turn(
                    "admin POST /api/admin/restart-agent"
                )
                if cleared:
                    self._last_broadcast_force_clear = self.acp.last_force_clear_reason
                for sid in sids:
                    if sid:
                        await self.broadcast(
                            {
                                "type": "turn",
                                "sessionId": sid,
                                "state": "idle",
                                "error": "Agent restart — turn stopped",
                            },
                            session_id=sid,
                        )
            except Exception as exc:
                log.warning("restart-agent: force_clear_turn failed: %s", exc)

            # 2) Close ACP websocket before killing the process.
            try:
                await self.acp._close_ws()
            except Exception as exc:
                log.warning("restart-agent: ACP close failed: %s", exc)

            # 3) Reset heal state so reconnect budget is fresh after spawn.
            self._acp_heal_attempts = 0
            self._acp_heal_last_error = None
            self._acp_disconnected_at = None
            self._acp_heal_in_progress = False

            # 4) Force-kill listener / pid and wait for supervisor respawn.
            up = await self.supervisor.force_restart(wait_up_timeout=40.0)
            if not up:
                err = "agent process did not come back up within timeout"
                log.error("restart-agent: %s", err)
                await self.broadcast(self.status_payload())
                return web.json_response(
                    {
                        "ok": False,
                        "error": err,
                        "acpConnected": bool(self.acp_connected),
                        "agentProcess": self.agent_status,
                    },
                    status=504,
                )

            # 5) Reconnect ACP to the new serve process.
            try:
                await self.acp.reconnect(timeout=15.0)
            except Exception as exc:
                err = f"agent up but ACP reconnect failed: {exc}"
                self._acp_heal_last_error = str(exc)
                if self._acp_disconnected_at is None:
                    self._acp_disconnected_at = time.monotonic()
                log.warning("restart-agent: %s", err)

            ok = bool(self.acp_connected) and self.agent_status == "up"
            if ok:
                self._acp_heal_attempts = 0
                self._acp_heal_last_error = None
                self._acp_disconnected_at = None
                log.info("restart-agent: success agentProcess=up acpConnected=true")
            else:
                if not err:
                    err = "agent up but ACP not connected"
                log.warning("restart-agent: partial failure: %s", err)

            await self.broadcast(self.status_payload())
            body: dict[str, Any] = {
                "ok": ok,
                "acpConnected": bool(self.acp_connected),
                "agentProcess": self.agent_status,
            }
            if err and not ok:
                body["error"] = err
            return web.json_response(body, status=200 if ok else 502)
        finally:
            self._agent_restart_in_progress = False

    async def handle_sessions(self, request: web.Request) -> web.Response:
        return web.json_response({"items": self._sessions_items_with_status()})

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
        await self.broadcast({"type": "sessions", "items": self._sessions_items_with_status()})
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
        await self.broadcast({"type": "sessions", "items": self._sessions_items_with_status()})
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

    async def handle_session_plan(self, request: web.Request) -> web.Response:
        """Read plan.md + plan_mode.json for Hub plan viewer."""
        session_id = request.match_info["id"]
        session = find_session(self.config.sessions_root, session_id)
        path = session.path if session else None
        try:
            payload = await asyncio.to_thread(
                read_session_plan,
                self.config.sessions_root,
                session_id,
                path,
            )
        except PlanViewError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        return web.json_response(payload)

    async def handle_session_plan_action(self, request: web.Request) -> web.Response:
        """Write plan_mode.json via Hub handshake (approve / request_changes / quit)."""
        session_id = request.match_info["id"]
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError, ValueError):
            return web.json_response({"error": "invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be a JSON object"}, status=400)
        action = body.get("action")
        if not isinstance(action, str) or not action.strip():
            return web.json_response(
                {"error": "action required (approve|request_changes|quit)"},
                status=400,
            )
        session = find_session(self.config.sessions_root, session_id)
        path = session.path if session else None
        try:
            payload = await asyncio.to_thread(
                apply_plan_action,
                self.config.sessions_root,
                session_id,
                action.strip(),
                path,
            )
        except PlanViewError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        try:
            await self.broadcast(
                {
                    "type": "plan",
                    "sessionId": session_id,
                    "awaitingApproval": payload.get("awaitingApproval"),
                    "state": payload.get("state"),
                    "action": payload.get("action"),
                },
                session_id=session_id,
            )
        except Exception:
            log.debug("plan action broadcast failed", exc_info=True)
        return web.json_response(payload)

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

    async def handle_projects_browse(self, request: web.Request) -> web.Response:
        path = request.query.get("path") or ""
        try:
            result = await asyncio.to_thread(
                list_project_browse, self.config.projects_root, path
            )
        except ProjectError as exc:
            msg = str(exc)
            status = 404 if "not found" in msg.lower() else 400
            return web.json_response({"error": msg}, status=status)
        except OSError as exc:
            log.exception("projects browse failed")
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response(result)

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
            if size > RAW_MAX_BYTES:
                return web.json_response({"error": "file too large"}, status=413)
            ctype = content_type_for(file_path)
            headers = {
                "Content-Type": ctype,
                "Cache-Control": "private, max-age=60",
                "X-Content-Type-Options": "nosniff",
            }
            download = (request.query.get("download") or "").strip().lower()
            if download in ("1", "true", "yes"):
                headers["Content-Disposition"] = content_disposition_attachment(
                    file_path.name
                )
            return web.FileResponse(
                path=file_path,
                headers=headers,
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

    async def handle_fs_upload(self, request: web.Request) -> web.Response:
        """Binary media upload into session cwd (default uploads/). Multipart preferred."""
        root = ""
        rel_dir = "uploads"
        filename = ""
        data = b""
        content_type: str | None = None

        ctype = (request.content_type or "").lower()
        try:
            if "multipart/" in ctype:
                reader = await request.multipart()
                while True:
                    part = await reader.next()
                    if part is None:
                        break
                    name = part.name or ""
                    if name == "root":
                        root = (await part.text()).strip()
                    elif name == "path":
                        text = (await part.text()).strip()
                        if text:
                            rel_dir = text
                    elif name == "filename":
                        filename = (await part.text()).strip()
                    elif name == "file":
                        if part.filename and not filename:
                            filename = part.filename
                        # Do not log body; read once into memory under size caps downstream.
                        data = await part.read(decode=False)
                        part_ct = part.headers.get("Content-Type")
                        if part_ct:
                            content_type = part_ct
            else:
                root = (request.query.get("root") or "").strip()
                path_q = (request.query.get("path") or "").strip()
                if path_q:
                    rel_dir = path_q
                filename = (request.query.get("filename") or "").strip()
                data = await request.read()
                if request.content_type:
                    content_type = request.content_type
        except Exception:
            log.exception("fs upload parse failed")
            return web.json_response({"error": "invalid upload"}, status=400)

        if not root or not str(root).strip():
            return web.json_response({"error": "root required"}, status=400)
        if not filename:
            filename = "upload.bin"
        try:
            result = fs_write_upload_bytes(
                self.config.projects_root,
                str(root).strip(),
                rel_dir,
                filename,
                data,
                content_type=content_type,
            )
        except FsBrowserError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception:
            log.exception("fs upload failed")
            return web.json_response({"error": "internal error"}, status=500)
        return web.json_response(result)

    async def handle_preview_start(self, request: web.Request) -> web.Response:
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
        try:
            plan = build_preview_plan(
                self.config.projects_root, str(root).strip(), str(path).strip()
            )
            self.site_preview.start(plan)
        except SitePreviewError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception:
            log.exception("preview start failed")
            return web.json_response({"error": "internal error"}, status=500)
        entry = plan["entry_url_path"]
        return web.json_response(
            {
                "ok": True,
                "siteRoot": str(plan["site_root"]),
                "entryPath": entry,
                "previewUrl": f"/preview-site/{entry}",
                "hubUrl": "/preview-site/",
            }
        )

    async def handle_preview_stop(self, request: web.Request) -> web.Response:
        self.site_preview.stop()
        return web.json_response({"ok": True})

    async def handle_preview_status(self, request: web.Request) -> web.Response:
        return web.json_response(self.site_preview.status())

    async def handle_preview_site(self, request: web.Request) -> web.Response:
        if not self.site_preview.active:
            return web.Response(
                text="no active preview",
                status=404,
                content_type="text/plain",
                headers={"Cache-Control": "no-store"},
            )
        # Prefer named path param; fall back to path after /preview-site/
        url_path = request.match_info.get("path")
        if url_path is None:
            raw = request.path or ""
            prefix = "/preview-site"
            if raw.startswith(prefix):
                url_path = raw[len(prefix) :].lstrip("/")
            else:
                url_path = ""
        file_path = self.site_preview.resolve_file(url_path or "")
        if file_path is None:
            return web.Response(
                text="not found",
                status=404,
                content_type="text/plain",
                headers={"Cache-Control": "no-store"},
            )
        ctype, _ = mimetypes.guess_type(str(file_path))
        if not ctype:
            ctype = "application/octet-stream"
        # HTML: charset so scripts/CSS parse reliably
        if ctype == "text/html":
            ctype = "text/html; charset=utf-8"
        return web.FileResponse(
            path=file_path,
            headers={
                "Content-Type": ctype,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

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
            self._stamp_origin_sync(session_id, "user")
            asyncio.create_task(self._stamp_origin_with_retry(session_id, "user"))
        except Exception as exc:
            log.exception("session/new failed")
            return web.json_response({"error": str(exc)}, status=500)
        await self.broadcast(self.status_payload())
        items = self._scan_sessions()
        await self.broadcast({"type": "sessions", "items": self._sessions_items_with_status()})
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
        # Allow session/load for process-live or hub resume candidates (never pure CLI).
        hub_origin = read_hub_origin(self.config.sessions_root, session_id)
        remote_map_ids = self._resume_map_ids()
        if not is_hub_resume_candidate(
            session_id,
            created_set=self.acp_created_sessions,
            remote_map_ids=remote_map_ids,
            hub_origin=hub_origin or None,
        ):
            return web.json_response(
                {
                    "error": "foreign session; use POST /api/sessions/{id}/attach for live remote",
                    "code": "foreign_session",
                },
                status=400,
            )
        # Only block/clear when THIS session is mid-turn; other projects may run.
        if self.acp.is_session_active(session_id) and self.acp.is_turn_stuck(
            session_id=session_id
        ):
            self.acp.force_clear_turn(
                "auto-clear stuck turn before session load", session_id=session_id
            )
            await self.broadcast(
                {
                    "type": "turn",
                    "sessionId": session_id,
                    "state": "idle",
                    "error": "Turn cleared (stuck)",
                },
                session_id=session_id,
            )
            await self.broadcast(self.status_payload())
        if self.acp.is_session_active(session_id):
            return web.json_response(
                {"error": "turn in progress on this session; wait until idle"},
                status=409,
            )
        try:
            await self.acp.session_load(session_id, cwd)
            self._record_hub_session(session_id, cwd)
            self._stamp_origin_sync(session_id, "attach")
            asyncio.create_task(self._stamp_origin_with_retry(session_id, "attach"))
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

        # Attach must not be blocked by other projects' turns.
        # Clear only a stuck turn on the view/live session if needed.
        for check_sid in (view_session_id,):
            if self.acp.is_session_active(check_sid) and self.acp.is_turn_stuck(
                session_id=check_sid
            ):
                self.acp.force_clear_turn(
                    "auto-clear stuck turn before attach", session_id=check_sid
                )
                await self.broadcast(
                    {
                        "type": "turn",
                        "sessionId": check_sid,
                        "state": "idle",
                        "error": "Turn cleared (stuck)",
                    },
                    session_id=check_sid,
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
        await ws.send_str(json.dumps({"type": "sessions", "items": self._sessions_items_with_status()}))
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
            await self._emit_error("invalid json", ws=ws)
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
        await self._emit_error(f"unknown type: {typ}", ws=ws)

    async def _ws_prompt_safe(self, ws: web.WebSocketResponse, payload: dict[str, Any]) -> None:
        try:
            await self._ws_prompt(ws, payload)
        except Exception:
            log.exception("background prompt task failed")
            try:
                sid = str(payload.get("sessionId") or "")
                await self._emit_error(
                    "Prompt failed unexpectedly. You can send again.",
                    session_id=sid or None,
                    ws=ws,
                    level="error",
                )
                if sid:
                    await self._broadcast_turn(
                        sid, "idle", "Prompt failed unexpectedly"
                    )
            except Exception:
                pass

    async def _ws_cancel_safe(self, ws: web.WebSocketResponse, payload: dict[str, Any]) -> None:
        try:
            await self._ws_cancel(ws, payload)
        except Exception:
            log.exception("background cancel task failed")

    def _is_no_output_error(self, exc: BaseException) -> bool:
        return is_no_output_error_message(exc)

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
        if error:
            log.warning(
                "turn_error session=%s state=%s: %s",
                session_id or "-",
                state,
                error,
            )
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
        # Immediate status so client liveTurns/sessionFlags/pills update on start/end.
        try:
            await self.broadcast(self.status_payload())
        except Exception:
            log.debug("status broadcast after turn failed", exc_info=True)

    def _resolve_prompt_cwd_key(
        self, view_session_id: str, cwd_raw: str
    ) -> tuple[str, str]:
        """Return (cwd, cwd_key) for queue / concurrency gating."""
        session = find_session(self.config.sessions_root, view_session_id)
        cwd = (cwd_raw or (session.cwd if session else "") or "").strip()
        # Prefer live remote session id mapping key when known
        key = self._cwd_key(cwd) if cwd else ""
        if not key:
            for k, sid in self.remote_agent_session.items():
                if sid == view_session_id or sid == self.acp.loaded_session_id:
                    key = k
                    break
        if not key:
            key = f"session:{view_session_id}"
        return cwd, key

    async def _ws_prompt(self, ws: web.WebSocketResponse, payload: dict[str, Any]) -> None:
        view_session_id = str(payload.get("sessionId") or "")
        text = str(payload.get("text") or "")
        if not view_session_id or not text.strip():
            await self._emit_error(
                "sessionId and text required",
                session_id=view_session_id or None,
                ws=ws,
            )
            return
        if not self.acp.connected:
            await self._emit_error(
                "agent not connected",
                session_id=view_session_id or None,
                ws=ws,
            )
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

        cwd_raw = str(payload.get("cwd") or "")
        cwd, cwd_key = self._resolve_prompt_cwd_key(view_session_id, cwd_raw)

        # Resolve likely live id for active-turn checks (view may map to hub remote).
        live_guess = view_session_id
        if cwd_key and self.remote_agent_session.get(cwd_key):
            live_guess = self.remote_agent_session[cwd_key]
        elif view_session_id in self.acp_created_sessions:
            live_guess = view_session_id

        # Auto-clear stuck turn only for this session (not other projects).
        for check_sid in {view_session_id, live_guess}:
            if self.acp.is_session_active(check_sid) and self.acp.is_turn_stuck(
                session_id=check_sid
            ):
                self.acp.force_clear_turn(
                    "auto-clear stuck turn before new prompt", session_id=check_sid
                )
                await self._broadcast_turn(
                    check_sid, "idle", "Turn cleared (stuck)", also_session_id=view_session_id
                )
                await self.broadcast(self.status_payload())

        # This session already running → per-cwd queue (TUI-like follow-ups).
        if self.acp.is_session_active(view_session_id) or self.acp.is_session_active(
            live_guess
        ):
            await self._enqueue_prompt(ws, view_session_id, text, cwd_raw, cwd_key)
            return

        active_map = self.acp.active_by_session_cwd()
        # Prefer cwd_key from active map when missing
        ok, reason = can_start_concurrent_turn(
            live_guess,
            cwd_key,
            active_by_session=active_map,
            max_concurrent=self._max_concurrent_turns,
        )
        if not ok:
            if reason == "max_concurrent":
                await self._emit_error(
                    (
                        f"Max concurrent project turns "
                        f"({self._max_concurrent_turns}). "
                        "Wait for a project to finish."
                    ),
                    session_id=view_session_id,
                    ws=ws,
                )
                return
            # same_cwd_busy: queue for this project
            await self._enqueue_prompt(ws, view_session_id, text, cwd_raw, cwd_key)
            return

        # Different project (or free capacity): execute immediately in parallel.
        await self._execute_prompt(
            view_session_id, text, cwd_raw, ws=ws, echo_user=True, cwd_key=cwd_key
        )
        await self._drain_prompt_queues()

    async def _enqueue_prompt(
        self,
        ws: web.WebSocketResponse,
        view_session_id: str,
        text: str,
        cwd_raw: str,
        cwd_key: str | None = None,
    ) -> None:
        """Queue a prompt for one project cwd. Echoes user text immediately."""
        if not cwd_key:
            _, cwd_key = self._resolve_prompt_cwd_key(view_session_id, cwd_raw)
        async with self._prompt_queue_lock:
            q = self._prompt_queues.get(cwd_key)
            if q is None:
                q = PromptQueue(max_size=10)
                self._prompt_queues[cwd_key] = q
            position = q.try_enqueue(
                {
                    "view_session_id": view_session_id,
                    "text": text,
                    "cwd": cwd_raw,
                    "cwd_key": cwd_key,
                }
            )
            if position is None:
                await self._emit_error(
                    "Queue full (max 10). Wait for turns to finish.",
                    session_id=view_session_id,
                    ws=ws,
                )
                return
            queue_length = self._queue_total_length()

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

    async def _heal_session_for_no_output_retry(
        self, session_id: str, cwd: str
    ) -> bool:
        """session/load same id; reconnect ACP once if needed. Never session/new."""
        if await self._try_session_load(session_id, cwd):
            self._record_hub_session(session_id, cwd)
            self._stamp_origin_sync(session_id, "attach")
            log.info("no-output heal: session/load ok session=%s", session_id)
            return True
        log.warning(
            "no-output heal: session/load failed session=%s; reconnecting ACP",
            session_id,
        )
        try:
            await self.acp.reconnect(timeout=10.0)
        except Exception as exc:
            log.warning("no-output heal: ACP reconnect failed: %s", exc)
            return False
        if await self._try_session_load(session_id, cwd):
            self._record_hub_session(session_id, cwd)
            self._stamp_origin_sync(session_id, "attach")
            log.info(
                "no-output heal: session/load ok after reconnect session=%s",
                session_id,
            )
            return True
        log.warning(
            "no-output heal: session/load still failed session=%s", session_id
        )
        return False

    async def _no_output_auto_retry(
        self,
        *,
        session_id: str,
        view_session_id: str,
        text: str,
        cwd: str,
        cwd_key: str,
    ) -> bool:
        """Force-clear, heal same session, re-prompt once. True if retry succeeded."""
        if self.acp.is_session_active(session_id):
            self.acp.force_clear_turn(
                "no-output recovery: clear stuck turn, same session",
                session_id=session_id,
            )
        log.warning(
            "no-output: auto-retry starting session=%s (no session/new, no map rewrite)",
            session_id,
        )
        await self._broadcast_turn(
            session_id,
            "running",
            NO_OUTPUT_RECOVERING_MSG,
            also_session_id=view_session_id,
        )
        await self.broadcast(self.status_payload())

        healed = await self._heal_session_for_no_output_retry(
            session_id, cwd or ""
        )
        if not healed:
            log.warning(
                "no-output heal incomplete session=%s; still retrying prompt",
                session_id,
            )

        try:
            await self.acp.session_prompt(
                session_id,
                text,
                cwd=cwd or None,
                allow_load=False,
                cwd_key=cwd_key,
                no_output_seconds=90,
            )
            log.info("no-output: auto-retry ok session=%s", session_id)
            await self._broadcast_turn(
                session_id, "idle", None, also_session_id=view_session_id
            )
            return True
        except Exception as retry_exc:
            log.exception(
                "no-output: auto-retry failed session=%s", session_id
            )
            if self.acp.is_session_active(session_id):
                self.acp.force_clear_turn(
                    f"no-output auto-retry failed: {retry_exc}",
                    session_id=session_id,
                )
            if self._is_no_output_error(retry_exc):
                err_msg = NO_OUTPUT_RETRY_FAILED_MSG
            elif self._is_mid_turn_stall_error(retry_exc):
                err_msg = MID_TURN_STALL_USER_MSG
            elif self._is_max_turn_error(retry_exc):
                err_msg = MAX_TURN_USER_MSG
            elif "force-cleared" in str(retry_exc).lower():
                err_msg = MID_TURN_STALL_USER_MSG
            else:
                err_msg = NO_OUTPUT_RETRY_FAILED_MSG
            if self.acp.last_force_clear_reason:
                self._last_broadcast_force_clear = (
                    self.acp.last_force_clear_reason
                )
            await self._broadcast_turn(
                session_id, "idle", err_msg, also_session_id=view_session_id
            )
            await self._emit_error(
                err_msg, session_id=session_id, level="warning"
            )
            return False

    async def _execute_prompt(
        self,
        view_session_id: str,
        text: str,
        cwd_raw: str,
        *,
        ws: web.WebSocketResponse | None = None,
        echo_user: bool = True,
        cwd_key: str = "",
        _auto_retry: bool = True,
    ) -> None:
        """Run one prompt turn. Does not enqueue; caller drains queues after."""
        session = find_session(self.config.sessions_root, view_session_id)
        cwd = (cwd_raw or (session.cwd if session else "") or "").strip()
        if not cwd and self.acp.loaded_session_id != view_session_id:
            err = "cwd unknown for session"
            await self._emit_error(err, session_id=view_session_id, ws=ws)
            await self._broadcast_turn(view_session_id, "idle", err)
            await self.broadcast(self.status_payload())
            return

        if not cwd_key:
            cwd_key = self._cwd_key(cwd) if cwd else f"session:{view_session_id}"

        session_id = view_session_id
        # Hub-owned session for prompts (CLI / foreign ids cannot be prompted)
        try:
            session_id, _switched, _reason = await self._ensure_hub_agent_session(
                view_session_id, cwd, ws=ws, notify_switch=True
            )
        except Exception as exc:
            log.exception("ensure hub agent session failed view=%s", view_session_id)
            await self._emit_error(
                str(exc), session_id=view_session_id, ws=ws, level="error"
            )
            await self._broadcast_turn(
                view_session_id, "idle", str(exc), also_session_id=session_id
            )
            await self.broadcast(self.status_payload())
            return

        if ws is not None:
            self.subscriptions.setdefault(ws, set()).add(session_id)
        log.info(
            "WS prompt start session=%s view=%s hub_created=%s active=%d",
            session_id,
            view_session_id,
            session_id in self.acp_created_sessions,
            len(self.acp.turn_session_ids),
        )
        # Running on live id; also unlock view selection if still on foreign id
        await self._broadcast_turn(
            session_id, "running", None, also_session_id=view_session_id
        )
        await self.broadcast(self.status_payload())
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

        # ensure path session/new'd if needed; multi-session agent holds id without load.
        try:
            await self.acp.session_prompt(
                session_id,
                text,
                cwd=cwd or None,
                allow_load=False,
                cwd_key=cwd_key,
            )
            log.info("WS prompt end session=%s ok", session_id)
            await self._broadcast_turn(
                session_id, "idle", None, also_session_id=view_session_id
            )
        except Exception as exc:
            log.exception("prompt failed session=%s", session_id)
            if self.acp.is_session_active(session_id):
                self.acp.force_clear_turn(
                    f"prompt exception: {exc}", session_id=session_id
                )

            # Hang with zero output: heal + auto-retry once on same session.
            if should_auto_retry_no_output(exc, already_retried=not _auto_retry):
                await self._no_output_auto_retry(
                    session_id=session_id,
                    view_session_id=view_session_id,
                    text=text,
                    cwd=cwd,
                    cwd_key=cwd_key,
                )
            else:
                err_msg = str(exc)
                if self._is_no_output_error(exc):
                    err_msg = (
                        NO_OUTPUT_RETRY_FAILED_MSG
                        if not _auto_retry
                        else NO_OUTPUT_USER_MSG
                    )
                    if self.acp.is_session_active(session_id):
                        self.acp.force_clear_turn(
                            "no-output recovery: clear stuck turn, same session",
                            session_id=session_id,
                        )
                    log.warning(
                        "no-output: kept session %s (no session/new, no map rewrite)",
                        session_id,
                    )
                elif self._is_mid_turn_stall_error(exc):
                    err_msg = MID_TURN_STALL_USER_MSG
                elif self._is_max_turn_error(exc):
                    err_msg = MAX_TURN_USER_MSG
                elif "force-cleared" in str(exc).lower():
                    err_msg = MID_TURN_STALL_USER_MSG

                if self.acp.last_force_clear_reason:
                    self._last_broadcast_force_clear = (
                        self.acp.last_force_clear_reason
                    )

                await self._broadcast_turn(
                    session_id, "idle", err_msg, also_session_id=view_session_id
                )
                await self._emit_error(
                    err_msg, session_id=session_id, level="warning"
                )
        finally:
            # Always re-assert idle unlock path via status (success already idled above)
            if self.acp.is_session_active(session_id):
                self.acp.force_clear_turn(
                    "prompt finally safeguard", session_id=session_id
                )
                await self._broadcast_turn(
                    session_id, "idle", None, also_session_id=view_session_id
                )
        await self.broadcast(self.status_payload())

    async def _drain_prompt_queues(self) -> None:
        """Start queued prompts whose cwd/session can run now (multi-project)."""
        while True:
            item: dict[str, Any] | None = None
            async with self._prompt_queue_lock:
                # Drop empty queues
                empty_keys = [k for k, q in self._prompt_queues.items() if len(q) == 0]
                for k in empty_keys:
                    self._prompt_queues.pop(k, None)
                if not self._prompt_queues:
                    return
                active_map = self.acp.active_by_session_cwd()
                for qkey, q in list(self._prompt_queues.items()):
                    if len(q) == 0:
                        continue
                    # Peek without pop
                    peek = q._items[0] if q._items else None
                    if peek is None:
                        continue
                    view_sid = str(peek.get("view_session_id") or "")
                    item_key = str(peek.get("cwd_key") or qkey)
                    # Resolve live id if mapped
                    live = self.remote_agent_session.get(item_key) or view_sid
                    ok, reason = can_start_concurrent_turn(
                        live,
                        item_key,
                        active_by_session=active_map,
                        max_concurrent=self._max_concurrent_turns,
                    )
                    if not ok:
                        continue
                    if reason == "already_active":
                        # Session still mid-turn; keep queued
                        continue
                    item = q.pop()
                    if len(q) == 0:
                        self._prompt_queues.pop(qkey, None)
                    break
                remaining = self._queue_total_length()
            if item is None:
                return
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
                    cwd_key=str(item.get("cwd_key") or ""),
                )
            except Exception:
                log.exception("queued prompt failed")
                # continue draining remaining startable items

    # Back-compat name used by older call sites / tests.
    async def _drain_prompt_queue(self) -> None:
        await self._drain_prompt_queues()

    async def _ws_cancel(self, ws: web.WebSocketResponse, payload: dict[str, Any]) -> None:
        session_id = str(payload.get("sessionId") or "")
        if not session_id:
            return
        # Stop cancels only this session's turn + its project queue.
        _, cwd_key = self._resolve_prompt_cwd_key(session_id, "")
        # Also match by remote map reverse lookup
        for k, sid in self.remote_agent_session.items():
            if sid == session_id:
                cwd_key = k
                break
        async with self._prompt_queue_lock:
            q = self._prompt_queues.pop(cwd_key, None)
            if q is not None:
                q.clear()
            # Also drop queue items targeting this session under any key
            for k, qq in list(self._prompt_queues.items()):
                kept: list[dict[str, Any]] = []
                for it in list(qq._items):
                    if str(it.get("view_session_id") or "") == session_id:
                        continue
                    kept.append(it)
                qq._items = kept
                if len(qq) == 0:
                    self._prompt_queues.pop(k, None)
            remaining = self._queue_total_length()
        await self.broadcast(
            {"type": "queue", "queueLength": remaining, "sessionId": session_id}
        )
        try:
            await self.acp.session_cancel(session_id)
        except Exception as exc:
            log.warning("session_cancel raised session=%s: %s — force-clearing", session_id, exc)
            try:
                self.acp.force_clear_turn(
                    f"user cancel fallback: {exc}", session_id=session_id
                )
            except Exception:
                log.exception("force_clear_turn after cancel failure")
            try:
                await self._emit_error(
                    (
                        f"Stop: agent cancel failed ({exc}); "
                        "turn force-cleared locally."
                    ),
                    session_id=session_id,
                    ws=ws,
                )
            except Exception:
                pass
        # Always broadcast idle + status so clients unlock this session.
        await self.broadcast(
            {"type": "turn", "sessionId": session_id, "state": "idle", "error": None},
            session_id=session_id,
        )
        await self.broadcast(self.status_payload())
        # Other projects may have queued work that can start now.
        await self._drain_prompt_queues()


def create_app(config: Config | None = None) -> web.Application:
    cfg = config or __import__("hub.config", fromlist=["load_config"]).load_config()
    hub = Hub(cfg)
    hosts, mode, ts_ip = resolve_bind_hosts(cfg)
    hub.bind_hosts = hosts
    hub.bind_host = ts_ip if ts_ip and ts_ip in hosts else hosts[0]
    hub.bind_mode = mode
    hub.tailscale_ip = ts_ip
    return hub.build_app()
