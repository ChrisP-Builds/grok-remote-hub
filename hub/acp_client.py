from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from websockets.asyncio.client import connect as ws_connect

from hub.acp_ask_user import (
    build_accepted_result,
    build_cancelled_result,
    normalize_questions,
)
from hub.acp_fs import read_text_file, write_text_file
from hub.acp_permissions import pick_permission_option
from hub.acp_terminal import TerminalManager
from hub.acp_trace import AcpTrace, session_id_slice
from hub.config import Config
from hub.session_policy import (
    MAX_TURN_SECONDS,
    MID_TURN_STALL_SECONDS,
    NO_OUTPUT_SECONDS,
    STUCK_TURN_SECONDS,
    apply_turn_activity,
    is_turn_stuck_for_new_prompt,
    should_force_clear_turn,
    should_skip_session_load,
)
from hub.status_view import (
    ACP_PROBE_INTERVAL_S,
    ACP_PROBE_SILENCE_S,
    ACP_PROBE_TIMEOUT_S,
    LOAD_SUPPRESS_MAX_S,
    LOAD_SUPPRESS_QUIET_S,
    load_suppress_should_release,
    should_probe_acp_liveness,
    should_suppress_session_load_fanout,
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
    """Sole ACP WebSocket client to grok agent serve.

    Multi-session concurrent turns: the hub does not globally block one project
    because another is mid-turn. Concurrent session/prompt awaits share one
    connection; the send lock is held only for id assignment + wire send, not
    across prompt futures. If the agent serializes turns internally, a future
    multi-process agent pool is the scale-out path — the hub gate still allows
    multi-cwd concurrency.
    """

    def __init__(
        self,
        config: Config,
        secret: str,
        on_message: MessageCallback | None = None,
        on_connection: Callable[[bool], Awaitable[None] | None] | None = None,
        *,
        log_dir: Any = None,
        trace: AcpTrace | None = None,
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
        # req_id -> session_id for prompt (and other session-scoped) RPCs
        self._pending_session: dict[int, str] = {}
        self._next_id = 1
        self._stop = asyncio.Event()
        self.connected = False
        self.loaded_session_id: str | None = None
        # session ids successfully session/new or session/load'd this process (ACP up).
        self._warm_sessions: set[str] = set()
        # Single-flight: sid -> Future while session/load is in progress.
        self._load_inflight: dict[str, asyncio.Future] = {}
        # session/load: suppress historical session/update fanout (UI strobe).
        self._loading_sessions: set[str] = set()
        # Optional once-per-load suppress counts for trace (sid -> count).
        self._load_suppress_counts: dict[str, int] = {}
        # Delayed release handles for load suppress (sid -> Handle).
        self._load_release_handles: dict[str, asyncio.TimerHandle] = {}
        # Quiet-period load suppress: max wall deadline (monotonic) per sid.
        self._load_suppress_deadline: dict[str, float] = {}
        self.available_commands: list[dict[str, Any]] = []
        # session_id -> {started_at, last_activity, saw_update, first_update_at,
        #                cwd_key, prompt_req_id?}
        self.active_turns: dict[str, dict[str, Any]] = {}
        self._stall_watchdogs: dict[str, asyncio.Task] = {}
        # Single-flight locks for end_turn (cancel-then-clear) per session.
        self._end_turn_locks: dict[str, asyncio.Lock] = {}
        # Last cancel RPC method that succeeded (tried first next time).
        self._cancel_method: str | None = None
        # Last watchdog/admin force-clear (Hub may broadcast idle from these).
        self.last_force_clear_reason: str | None = None
        self.last_force_clear_session: str | None = None
        # Session ids cleared on ACP disconnect; Hub may broadcast turn idle once.
        self.disconnect_turn_session_ids: list[str] = []
        # Back-compat single field (last cleared on disconnect).
        self.disconnect_turn_session_id: str | None = None
        # Client-side terminal/* processes for advertised terminal capability.
        self._terminals = TerminalManager()
        # Pending _x.ai/ask_user_question futures keyed by str(msg_id).
        self._pending_user_questions: dict[str, asyncio.Future] = {}
        # request_id -> session_id for pending questions
        self._pending_user_question_sessions: dict[str, str] = {}
        # Hub sets this to fan out user_question events to the web UI.
        self.on_user_question: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
        # Hub sets this to fan out live terminal/* pump deltas to the web UI.
        self.on_terminal_out: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
        # Wire terminal pump → hub fanout
        self._terminals.on_output = self._on_terminal_output_chunk
        # ACP wire liveness (half-open / zombie detection for status quality).
        self.last_send_ok_at: float | None = None
        self.last_recv_at: float | None = None
        self.last_send_error_at: float | None = None
        self.consecutive_send_failures: int = 0
        self._force_unhealthy_scheduled: bool = False
        # Proactive WS ping probe timestamps (monotonic).
        self.last_probe_at: float | None = None
        self.last_probe_ok_at: float | None = None
        self._probe_in_progress: bool = False
        # Structured lifecycle trace (memory + optional JSONL).
        if trace is not None:
            self.trace = trace
        else:
            resolved_log = log_dir
            if resolved_log is None:
                resolved_log = getattr(config, "log_dir", None)
            self.trace = AcpTrace(log_dir=resolved_log)

    def _trace(self, event: str, **fields: Any) -> None:
        tr = getattr(self, "trace", None)
        if tr is None:
            return
        try:
            tr.emit(event, **fields)
        except Exception:
            log.debug("ACP trace emit failed", exc_info=True)

    def acp_liveness_snapshot(self) -> dict[str, Any]:
        """Ages (seconds or None) and pending-RPC flag for status quality mapping."""
        now = time.monotonic()

        def _age(ts: float | None) -> float | None:
            if ts is None:
                return None
            return now - float(ts)

        return {
            "consecutive_send_failures": int(self.consecutive_send_failures),
            "seconds_since_send_ok": _age(self.last_send_ok_at),
            "seconds_since_recv": _age(self.last_recv_at),
            "seconds_since_send_error": _age(self.last_send_error_at),
            "has_pending": bool(self._pending),
        }

    def _schedule_force_unhealthy(self, reason: str = "send_failures") -> None:
        """Close half-dead WS so maintain reconnects; never await under send lock."""
        if self._force_unhealthy_scheduled:
            return
        self._force_unhealthy_scheduled = True
        self._trace(
            "force_unhealthy",
            reason=reason,
            consecutive_send_failures=self.consecutive_send_failures,
            **{k: v for k, v in self.acp_liveness_snapshot().items()},
        )
        if reason == "send_failures" or "zombie" in reason.lower():
            self._trace(
                "zombie",
                reason=reason,
                consecutive_send_failures=self.consecutive_send_failures,
            )
        # Immediate false so status is not chat-ready while close is scheduled.
        # Notify hub now: _close_ws only calls on_connection when connected was True.
        was_connected = self.connected
        self.connected = False
        if was_connected and self.on_connection is not None:
            try:
                result = self.on_connection(False)
                if asyncio.iscoroutine(result):
                    asyncio.get_running_loop().create_task(
                        result, name="acp-zombie-on-conn"
                    )
            except Exception:
                log.debug("ACP zombie on_connection notify failed", exc_info=True)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._force_unhealthy_scheduled = False
            return
        loop.create_task(self._force_unhealthy(reason), name="acp-force-unhealthy")

    async def _force_unhealthy(self, reason: str = "send_failures") -> None:
        try:
            log.warning(
                "ACP unhealthy (%s) after %s consecutive send failures; closing WS",
                reason,
                self.consecutive_send_failures,
            )
            await self._close_ws()
        except Exception:
            log.debug("ACP force unhealthy close failed", exc_info=True)
        finally:
            self._force_unhealthy_scheduled = False

    async def maybe_probe_liveness(self) -> bool | None:
        """Ping ACP WebSocket when idle silence suggests a half-open wire.

        Returns True on probe ok, False on fail, None if skipped.
        On fail: force unhealthy so heal can reconnect (no KillAgent).
        """
        if self._probe_in_progress:
            return None
        if not self.connected or self._ws is None:
            return None
        live = self.acp_liveness_snapshot()
        now = time.monotonic()
        since_probe: float | None = None
        if self.last_probe_at is not None:
            since_probe = now - float(self.last_probe_at)
        if not should_probe_acp_liveness(
            connected=bool(self.connected and self._ws is not None),
            has_pending=bool(live.get("has_pending")),
            seconds_since_recv=live.get("seconds_since_recv"),
            seconds_since_probe=since_probe,
            silence_s=ACP_PROBE_SILENCE_S,
            interval_s=ACP_PROBE_INTERVAL_S,
        ):
            return None
        ws = self._ws
        if ws is None:
            return None
        self._probe_in_progress = True
        self.last_probe_at = now
        self._trace(
            "probe_start",
            seconds_since_recv=live.get("seconds_since_recv"),
            consecutive_send_failures=live.get("consecutive_send_failures"),
        )
        try:
            # websockets: ping() returns a future that resolves on pong.
            pong_waiter = await ws.ping()
            await asyncio.wait_for(pong_waiter, timeout=ACP_PROBE_TIMEOUT_S)
            self.last_recv_at = time.monotonic()
            self.last_probe_ok_at = self.last_recv_at
            self._trace(
                "probe_ok",
                seconds_since_recv=0.0,
            )
            return True
        except Exception as exc:
            log.warning(
                "ACP liveness probe failed (recv_age=%s): %s",
                live.get("seconds_since_recv"),
                exc,
            )
            self._trace(
                "probe_fail",
                error=str(exc)[:200],
                seconds_since_recv=live.get("seconds_since_recv"),
                consecutive_send_failures=self.consecutive_send_failures,
            )
            self.consecutive_send_failures = max(
                int(self.consecutive_send_failures) + 1, 2
            )
            self.last_send_error_at = time.monotonic()
            self._schedule_force_unhealthy(reason="probe_fail")
            return False
        finally:
            self._probe_in_progress = False

    # --- back-compat properties over multi-session active_turns ---

    @property
    def turn_running(self) -> bool:
        return bool(self.active_turns)

    @turn_running.setter
    def turn_running(self, value: bool) -> None:
        """Back-compat: setting False clears all; True is a no-op without session."""
        if not value:
            self.active_turns.clear()

    @property
    def turn_session_id(self) -> str | None:
        """Primary / most recently started active session id (back-compat)."""
        if not self.active_turns:
            return None
        best_sid: str | None = None
        best_t = -1.0
        for sid, meta in self.active_turns.items():
            t = float(meta.get("started_at") or 0.0)
            if t >= best_t:
                best_t = t
                best_sid = sid
        return best_sid

    @turn_session_id.setter
    def turn_session_id(self, value: str | None) -> None:
        if value is None and not self.active_turns:
            return
        if value is None:
            return
        # Back-compat single-session assign without full register
        sid = str(value)
        if sid not in self.active_turns:
            now = time.monotonic()
            self.active_turns[sid] = {
                "started_at": now,
                "last_activity": now,
                "saw_update": False,
                "first_update_at": None,
                "cwd_key": "",
            }

    @property
    def turn_session_ids(self) -> list[str]:
        return list(self.active_turns.keys())

    @property
    def turn_started_at(self) -> float | None:
        sid = self.turn_session_id
        if not sid:
            return None
        meta = self.active_turns.get(sid) or {}
        return meta.get("started_at")

    @turn_started_at.setter
    def turn_started_at(self, value: float | None) -> None:
        sid = self.turn_session_id
        if sid and sid in self.active_turns and value is not None:
            self.active_turns[sid]["started_at"] = value

    @property
    def last_activity_at(self) -> float | None:
        sid = self.turn_session_id
        if not sid:
            return None
        meta = self.active_turns.get(sid) or {}
        return meta.get("last_activity")

    @last_activity_at.setter
    def last_activity_at(self, value: float | None) -> None:
        sid = self.turn_session_id
        if sid and sid in self.active_turns and value is not None:
            self.active_turns[sid]["last_activity"] = value

    @property
    def turn_saw_update(self) -> bool:
        sid = self.turn_session_id
        if not sid:
            return False
        meta = self.active_turns.get(sid) or {}
        return bool(meta.get("saw_update"))

    @turn_saw_update.setter
    def turn_saw_update(self, value: bool) -> None:
        sid = self.turn_session_id
        if sid and sid in self.active_turns:
            self.active_turns[sid]["saw_update"] = bool(value)

    def active_by_session_cwd(self) -> dict[str, str]:
        """session_id -> cwd_key for concurrency gating."""
        return {
            sid: str(meta.get("cwd_key") or "")
            for sid, meta in self.active_turns.items()
        }

    def is_session_active(self, session_id: str) -> bool:
        return str(session_id or "") in self.active_turns

    def sessions_with_pending_questions(self) -> set[str]:
        return set(self._pending_user_question_sessions.values())

    def turn_age_seconds(self, session_id: str | None = None) -> float | None:
        sid = session_id or self.turn_session_id
        if not sid:
            return None
        meta = self.active_turns.get(sid)
        if not meta:
            return None
        started = meta.get("started_at")
        if started is None:
            return None
        return time.monotonic() - float(started)

    def is_turn_stuck(
        self, threshold: float | None = None, session_id: str | None = None
    ) -> bool:
        """True if a running turn is dead enough to force-clear for a new prompt.

        Activity-aware (TUI-aligned): healthy long turns are not stuck.
        Uses no-output / mid-turn stall / max wall from session_policy.
        ``threshold`` is accepted for call-site compatibility and ignored;
        force-clear policy is centralized in is_turn_stuck_for_new_prompt.
        """
        del threshold  # API compat; policy is activity-aware, not short wall.
        sid = session_id or self.turn_session_id
        if not sid or sid not in self.active_turns:
            if session_id is not None:
                return False
            # Any stuck active turn (global check)
            return any(self.is_turn_stuck(session_id=s) for s in list(self.active_turns))
        meta = self.active_turns[sid]
        started = meta.get("started_at")
        if started is None:
            return True
        age = time.monotonic() - float(started)
        activity_at = meta.get("last_activity")
        age_activity = (
            (time.monotonic() - float(activity_at))
            if activity_at is not None
            else age
        )
        return is_turn_stuck_for_new_prompt(
            bool(meta.get("saw_update")),
            age,
            age_activity,
        )

    def force_clear_turn(self, reason: str, session_id: str | None = None) -> bool:
        """Force-clear turn state. If session_id given, clear only that turn.

        Without session_id, clear all (disconnect / admin). Fails pending ACP
        futures scoped to cleared session(s) (or all when clearing all).
        """
        if session_id is not None:
            return self._force_clear_one(str(session_id), reason)

        if (
            not self.active_turns
            and not self._pending
            and not self._pending_user_questions
        ):
            return False

        age = self.turn_age_seconds()
        cleared_sid = self.turn_session_id
        log.warning(
            "Force-clearing all turns (primary=%s age=%s pending=%d active=%d): %s",
            cleared_sid,
            f"{age:.1f}s" if age is not None else "unknown",
            len(self._pending),
            len(self.active_turns),
            reason,
        )
        self._trace(
            "turn_force_clear",
            reason=reason,
            sessionId=session_id_slice(cleared_sid),
            active=len(self.active_turns),
            pending=len(self._pending),
        )
        self.last_force_clear_reason = reason
        self.last_force_clear_session = cleared_sid
        for req_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(TimeoutError(f"Turn force-cleared: {reason}"))
            self._pending.pop(req_id, None)
        self._pending_session.clear()
        self._cancel_all_pending_user_questions()
        cleared_ids = list(self.active_turns.keys())
        self.active_turns.clear()
        self._cancel_all_stall_watchdogs()
        if cleared_ids and "acp disconnect" in reason.lower():
            self.disconnect_turn_session_ids = cleared_ids
            self.disconnect_turn_session_id = cleared_ids[0]
        return True

    def _force_clear_one(self, session_id: str, reason: str) -> bool:
        sid = str(session_id or "")
        has_turn = sid in self.active_turns
        has_q = any(s == sid for s in self._pending_user_question_sessions.values())
        scoped_reqs = [rid for rid, s in self._pending_session.items() if s == sid]
        if not has_turn and not has_q and not scoped_reqs:
            return False
        meta = self.active_turns.get(sid) or {}
        started = meta.get("started_at")
        age = (time.monotonic() - float(started)) if started is not None else None
        log.warning(
            "Force-clearing turn (session=%s age=%s): %s",
            sid,
            f"{age:.1f}s" if age is not None else "unknown",
            reason,
        )
        self._trace(
            "turn_force_clear",
            reason=reason,
            sessionId=session_id_slice(sid),
            ageSeconds=age,
        )
        self.last_force_clear_reason = reason
        self.last_force_clear_session = sid
        for req_id in scoped_reqs:
            fut = self._pending.pop(req_id, None)
            self._pending_session.pop(req_id, None)
            if fut is not None and not fut.done():
                fut.set_exception(TimeoutError(f"Turn force-cleared: {reason}"))
        self._cancel_pending_user_questions_for_session(sid)
        self.active_turns.pop(sid, None)
        self._cancel_stall_watchdog(sid)
        if "acp disconnect" in reason.lower():
            self.disconnect_turn_session_id = sid
            if sid not in self.disconnect_turn_session_ids:
                self.disconnect_turn_session_ids.append(sid)
        return True

    def _cancel_stall_watchdog(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._cancel_all_stall_watchdogs()
            return
        wd = self._stall_watchdogs.pop(str(session_id), None)
        if wd is not None and not wd.done():
            wd.cancel()

    def _cancel_all_stall_watchdogs(self) -> None:
        for sid, wd in list(self._stall_watchdogs.items()):
            if wd is not None and not wd.done():
                wd.cancel()
        self._stall_watchdogs.clear()

    # Back-compat alias for older call sites / tests.
    def _cancel_no_output_watchdog(self) -> None:
        self._cancel_all_stall_watchdogs()

    def note_activity(
        self,
        session_id: str | None = None,
        *,
        update_kind: str | None = None,
    ) -> None:
        """Record ACP session/update activity for hang detection.

        Always refreshes last_activity / saw_update (including user_message_chunk)
        so stall detection sees the agent alive. Freezes first_update_at only for
        kinds that count toward agent TTFB (not user echo / available_commands).
        """
        now = time.monotonic()

        def _touch(sid: str) -> None:
            meta = self.active_turns[sid]
            started = meta.get("started_at")
            newly = apply_turn_activity(meta, now=now, update_kind=update_kind)
            if newly:
                ttfb = None
                if started is not None:
                    ttfb = float(now) - float(started)
                    if ttfb < 0:
                        ttfb = 0.0
                self._trace(
                    "first_agent_update",
                    sessionId=session_id_slice(sid),
                    kind=str(update_kind or "") or None,
                    ttfbSeconds=ttfb,
                )

        if session_id and session_id in self.active_turns:
            _touch(session_id)
            return
        # Fan out to all active turns when session unknown (tool RPCs, etc.)
        for sid in list(self.active_turns):
            _touch(sid)

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

    async def reconnect(self, timeout: float = 10.0) -> None:
        """Close the current ACP WebSocket so the maintain loop reconnects.

        Waits until connected (and initialized) or raises TimeoutError.
        Used by no-output heal; does not session/new.
        """
        was_connected = self.connected
        log.info("ACP reconnect requested (was_connected=%s)", was_connected)
        self._trace("disconnect", reason="reconnect_requested", was_connected=was_connected)
        try:
            await self._close_ws()
        except Exception as exc:
            log.warning("ACP reconnect close failed: %s", exc)
        # If maintain died (e.g. prior cancel race) or was never started, revive it.
        if self._maintain_task is None or self._maintain_task.done():
            if not self._stop.is_set():
                log.warning(
                    "ACP maintain not running; restarting loop for reconnect"
                )
                self._maintain_task = asyncio.create_task(
                    self._maintain(), name="acp-client"
                )
        deadline = time.monotonic() + max(0.5, float(timeout))
        # If already mid-reconnect, wait for the next connected edge.
        while time.monotonic() < deadline:
            if self.connected and self._ws is not None:
                log.info("ACP reconnect complete")
                return
            await asyncio.sleep(0.15)
        raise TimeoutError(f"ACP reconnect timed out after {timeout}s")

    async def _set_connected(self, value: bool) -> None:
        self.connected = value
        if self.on_connection:
            result = self.on_connection(value)
            if asyncio.iscoroutine(result):
                await result

    async def _close_ws(self) -> None:
        # Close socket first so recv can exit cleanly; cancel only if stuck.
        ws = self._ws
        self._ws = None
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError("ACP connection closed"))
        self._pending.clear()
        self._pending_session.clear()
        self._cancel_all_pending_user_questions()
        try:
            await self._terminals.close_all()
        except Exception:
            pass
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        recv = self._recv_task
        self._recv_task = None
        if recv is not None and not recv.done():
            recv.cancel()
            try:
                await recv
            except asyncio.CancelledError:
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
            self._pending_session.clear()
            self._cancel_all_pending_user_questions()

    async def _maintain(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                url = self._url()
                log.info("Connecting ACP to %s:%s", self.config.agent_bind, self.config.agent_port)
                self._trace(
                    "connect",
                    host=self.config.agent_bind,
                    port=self.config.agent_port,
                )
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
                # Re-raise only when this task is stopping/cancelled.
                # Awaiting a cancelled recv (reconnect path) must not kill maintain.
                if self._stop.is_set():
                    raise
                cur = asyncio.current_task()
                cancelling = 0
                if cur is not None and hasattr(cur, "cancelling"):
                    try:
                        cancelling = int(cur.cancelling())  # type: ignore[attr-defined]
                    except Exception:
                        cancelling = 0
                if cancelling:
                    raise
                log.info("ACP recv cancelled (reconnect); maintain continues")
            except Exception as exc:
                log.warning("ACP connection error: %s", exc)
                self._trace("disconnect", reason="connection_error", error=str(exc)[:200])
            finally:
                self._ws = None
                self.loaded_session_id = None
                self._warm_sessions.clear()
                for _fut in self._load_inflight.values():
                    if not _fut.done():
                        _fut.set_exception(ConnectionError("ACP disconnected"))
                self._load_inflight.clear()
                # Cancel method may no longer apply after reconnect.
                self._cancel_method = None
                if (
                    self.active_turns
                    or self._pending
                    or self._pending_user_questions
                ):
                    # WS already dead: cancel RPC cannot work; unlock locally.
                    self.force_clear_turn("acp disconnected")
                else:
                    self.active_turns.clear()
                    self._cancel_all_stall_watchdogs()
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
                self._pending_session.clear()
                self._cancel_all_pending_user_questions()
                if self.connected:
                    self._trace("disconnect", reason="ws_closed")
                    await self._set_connected(False)
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)

    async def _initialize(self) -> None:
        try:
            await self.request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": True, "writeTextFile": True},
                        "terminal": True,
                    },
                    "clientInfo": {"name": "grok-remote-hub", "version": "0.4.0"},
                },
                timeout=30.0,
            )
            self._trace("initialize_ok")
        except Exception as exc:
            self._trace("initialize_fail", error=str(exc)[:200])
            raise

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

        # Any successfully parsed inbound message counts as recv liveness.
        self.last_recv_at = time.monotonic()

        msg_id = msg.get("id")
        method = msg.get("method")

        # JSON-RPC response to a hub-originated request
        if msg_id is not None and ("result" in msg or "error" in msg) and not method:
            kind = "error" if "error" in msg else "result"
            self._trace("recv", kind=kind, id=msg_id)
            fut = self._pending.pop(msg_id, None)
            self._pending_session.pop(msg_id, None)
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
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            sid = params.get("sessionId") or params.get("session_id")
            self._trace(
                "recv",
                method=str(method)[:80],
                kind="client_request",
                sessionId=session_id_slice(str(sid) if sid else None),
            )
            await self._handle_client_request(msg)
            await self._fanout(msg)
            return

        # Notifications / other — trace interesting methods only
        sid = None
        update_kind = None
        if method:
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            if isinstance(params, dict):
                sid = params.get("sessionId") or params.get("session_id")
                upd = params.get("update") if isinstance(params.get("update"), dict) else {}
                update_kind = (
                    upd.get("sessionUpdate")
                    or upd.get("session_update")
                    or params.get("sessionUpdate")
                )
                if not sid and isinstance(upd, dict):
                    sid = upd.get("sessionId") or upd.get("session_id")
            method_s = str(method)
            interesting = (
                method_s in (
                    "session/update",
                    "_x.ai/session/update",
                    "_x.ai/session_notification",
                    "x.ai/session_notification",
                )
                or "compact" in method_s.lower()
            )
            if interesting:
                self._trace(
                    "recv",
                    method=method_s[:80],
                    kind=str(update_kind)[:80] if update_kind else "notification",
                    sessionId=session_id_slice(str(sid) if sid else None),
                )

        sid_str = str(sid) if sid else None
        method_str = str(method) if method else None
        kind_str = str(update_kind) if update_kind else None
        suppress = should_suppress_session_load_fanout(
            loading_session_ids=self._loading_sessions,
            session_id=sid_str,
            method=method_str,
            update_kind=kind_str,
            active_turn_session_ids=frozenset(self.active_turns.keys()),
        )
        if suppress:
            # Count for a single end-of-load trace (avoid per-message flood).
            key = sid_str or next(iter(self._loading_sessions), "")
            if key:
                self._load_suppress_counts[key] = (
                    self._load_suppress_counts.get(key, 0) + 1
                )
                # Each suppressed frame resets the quiet-period release timer.
                self._rearm_load_suppress_release(key)
            await self._track_update(msg)
            return

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
        sid = (
            params.get("sessionId")
            or params.get("session_id")
            or None
        )
        self.note_activity(str(sid) if sid else None)

        try:
            if "permission" in method_l or method_l.endswith("request_permission"):
                await self._handle_permission(msg_id, params)
                return

            # Spawn task so the ACP recv loop keeps processing while the UI answers.
            if "ask_user_question" in method_l:
                asyncio.create_task(
                    self._handle_ask_user_question(msg_id, params),
                    name=f"acp-ask-user-{msg_id!s}"[:48],
                )
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

    def answer_user_question(self, request_id: str, result: dict[str, Any]) -> bool:
        """Resolve a pending ask_user_question with an ACP result dict."""
        key = str(request_id or "")
        if not key:
            return False
        fut = self._pending_user_questions.get(key)
        if fut is None or fut.done():
            return False
        fut.set_result(result)
        return True

    def cancel_user_question(self, request_id: str) -> bool:
        """Resolve a pending ask_user_question as cancelled."""
        return self.answer_user_question(request_id, build_cancelled_result())

    def _cancel_all_pending_user_questions(self) -> None:
        """Complete all pending user questions with cancelled (disconnect/force-clear)."""
        for key, fut in list(self._pending_user_questions.items()):
            if not fut.done():
                fut.set_result(build_cancelled_result())
            self._pending_user_questions.pop(key, None)
        self._pending_user_question_sessions.clear()

    def _cancel_pending_user_questions_for_session(self, session_id: str) -> None:
        sid = str(session_id or "")
        for key, s in list(self._pending_user_question_sessions.items()):
            if s != sid:
                continue
            fut = self._pending_user_questions.pop(key, None)
            self._pending_user_question_sessions.pop(key, None)
            if fut is not None and not fut.done():
                fut.set_result(build_cancelled_result())

    async def _handle_ask_user_question(
        self, msg_id: Any, params: dict[str, Any]
    ) -> None:
        """Wait for web UI answer, then reply to the agent (runs off the recv path)."""
        questions = normalize_questions(params)
        if not questions:
            try:
                await self._reply_result(msg_id, build_accepted_result({}))
            except Exception as exc:
                log.warning("ask_user_question empty reply failed: %s", exc)
            return

        key = str(msg_id)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_user_questions[key] = fut

        session_id = (
            params.get("sessionId")
            or params.get("session_id")
            or self.turn_session_id
            or self.loaded_session_id
        )
        sid_str = str(session_id) if session_id else ""
        if sid_str:
            self._pending_user_question_sessions[key] = sid_str
        tool_call_id = (
            params.get("toolCallId")
            or params.get("tool_call_id")
            or ""
        )
        payload: dict[str, Any] = {
            "requestId": key,
            "sessionId": sid_str or None,
            "questions": questions,
            "toolCallId": str(tool_call_id) if tool_call_id else None,
        }
        log.info(
            "ask_user_question request id=%s session=%s n=%d",
            key,
            payload.get("sessionId") or "?",
            len(questions),
        )
        try:
            if self.on_user_question:
                cb = self.on_user_question(payload)
                if asyncio.iscoroutine(cb):
                    await cb
            result = await asyncio.wait_for(fut, timeout=1800.0)
            await self._reply_result(msg_id, result)
            log.info("ask_user_question answered id=%s", key)
        except asyncio.TimeoutError:
            log.warning("ask_user_question timed out id=%s", key)
            try:
                await self._reply_result(msg_id, build_cancelled_result())
            except Exception as exc:
                log.warning("ask_user_question timeout reply failed: %s", exc)
        except Exception as exc:
            log.exception("ask_user_question failed id=%s: %s", key, exc)
            try:
                await self._reply_result(msg_id, build_cancelled_result())
            except Exception:
                pass
        finally:
            pending = self._pending_user_questions.pop(key, None)
            self._pending_user_question_sessions.pop(key, None)
            if pending is not None and not pending.done():
                pending.cancel()

    async def _track_update(self, msg: dict[str, Any]) -> None:
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        sid = None
        update_pre: dict[str, Any] = {}
        if isinstance(params, dict):
            sid = params.get("sessionId") or params.get("session_id")
            raw_update = params.get("update") or {}
            if isinstance(raw_update, dict):
                update_pre = raw_update
            if not sid and update_pre:
                sid = update_pre.get("sessionId") or update_pre.get("session_id")
        sid_str = str(sid) if sid else None
        method_str = str(method) if method else None

        # Compact / session notifications count as activity so stall watchdog
        # does not mis-fire while compaction runs mid-session.
        if method in (
            "_x.ai/session_notification",
            "x.ai/session_notification",
        ):
            kind_n = str(
                update_pre.get("sessionUpdate")
                or update_pre.get("session_update")
                or params.get("sessionUpdate")
                or ""
            )
            suppress_n = should_suppress_session_load_fanout(
                loading_session_ids=self._loading_sessions,
                session_id=sid_str,
                method=method_str,
                update_kind=kind_n or None,
                active_turn_session_ids=frozenset(self.active_turns.keys()),
            )
            if kind_n.startswith("auto_compact_") and not suppress_n:
                self.note_activity(sid_str, update_kind=kind_n or None)
            return

        if method not in ("session/update", "_x.ai/session/update"):
            return
        update = update_pre if update_pre else (params.get("update") or {})
        kind = update.get("sessionUpdate") or ""
        # available_commands still applied during load (helper allows this kind).
        if kind == "available_commands_update":
            cmds = update.get("availableCommands") or update.get("available_commands") or []
            if isinstance(cmds, list):
                self.available_commands = cmds
        suppress = should_suppress_session_load_fanout(
            loading_session_ids=self._loading_sessions,
            session_id=sid_str,
            method=method_str,
            update_kind=str(kind) if kind else None,
            active_turn_session_ids=frozenset(self.active_turns.keys()),
        )
        if suppress:
            return
        self.note_activity(sid_str, update_kind=str(kind) if kind else None)
        if kind in ("turn_completed", "task_completed", "prompt_complete"):
            if sid_str and sid_str in self.active_turns:
                self.active_turns.pop(sid_str, None)
                self._cancel_stall_watchdog(sid_str)
            elif not sid_str and len(self.active_turns) == 1:
                only = next(iter(self.active_turns))
                self.active_turns.pop(only, None)
                self._cancel_stall_watchdog(only)

    def _on_terminal_output_chunk(
        self, terminal_id: str, delta: str, session_id: str | None
    ) -> None:
        """TerminalManager on_output: note activity + fan out to hub UI."""
        if not delta:
            return
        sid = session_id or self.turn_session_id or self.loaded_session_id
        sid_str = str(sid) if sid else None
        self.note_activity(sid_str)
        if not self.on_terminal_out:
            return
        payload: dict[str, Any] = {
            "terminalId": terminal_id,
            "delta": delta,
            "sessionId": sid_str,
        }
        try:
            result = self.on_terminal_out(payload)
            if asyncio.iscoroutine(result):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    return
                loop.create_task(result, name=f"hub-term-out-{terminal_id[:8]}")
        except Exception:
            log.debug("on_terminal_out failed id=%s", terminal_id, exc_info=True)

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
        method = payload.get("method")
        try:
            await ws.send(json.dumps(payload))
        except Exception as exc:
            self.consecutive_send_failures += 1
            self.last_send_error_at = time.monotonic()
            self._trace(
                "send_fail",
                method=str(method)[:80] if method else None,
                consecutive_failures=self.consecutive_send_failures,
                error=str(exc)[:200],
            )
            if self.consecutive_send_failures >= 2:
                self._schedule_force_unhealthy(reason="send_failures")
            raise
        self.last_send_ok_at = time.monotonic()
        self.consecutive_send_failures = 0
        if method:
            self._trace(
                "send_ok",
                method=str(method)[:80],
                consecutive_failures=0,
            )

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 120.0,
        *,
        session_id: str | None = None,
    ) -> Any:
        """Send JSON-RPC and await result.

        Lock is held only for id assignment + wire send so concurrent RPCs
        (multi-session prompts) can await futures in parallel.
        """
        if self._ws is None:
            raise ConnectionError("ACP not connected")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        async with self._lock:
            req_id = self._next_id
            self._next_id += 1
            self._pending[req_id] = fut
            if session_id:
                self._pending_session[req_id] = str(session_id)
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params or {},
            }
            await self._send(payload)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except Exception:
            pending = self._pending.pop(req_id, None)
            self._pending_session.pop(req_id, None)
            if pending is not None and not pending.done():
                pending.cancel()
            raise

    def forget_warm_session(self, session_id: str) -> None:
        """Drop warm/loaded cache so next session/load is a real agent RPC.

        Used by no-output heal: agent silence means warm skip is unsafe.
        """
        sid = str(session_id or "").strip()
        if not sid:
            return
        self._warm_sessions.discard(sid)
        if self.loaded_session_id == sid:
            self.loaded_session_id = None

    async def session_new(self, cwd: str) -> str:
        result = await self.request(
            "session/new",
            {"cwd": cwd, "mcpServers": []},
            timeout=60.0,
        )
        session_id = (result or {}).get("sessionId") or (result or {}).get("session_id")
        if not session_id:
            raise RuntimeError(f"session/new missing sessionId: {result!r}")
        sid = str(session_id)
        self.loaded_session_id = sid
        self._warm_sessions.add(sid)
        return sid

    async def session_load(self, session_id: str, cwd: str) -> Any:
        sid = str(session_id)
        # Already loaded in this process: skip agent session/load (not model prefill).
        if should_skip_session_load(self._warm_sessions, sid):
            log.info("session/load skip already warm sid=%s", sid[:8])
            self.loaded_session_id = sid
            return {"skipped": True, "sessionId": sid}
        # Concurrent attach/ensure: join in-flight load for same sid (one RPC).
        inflight = self._load_inflight.get(sid)
        if inflight is not None and not inflight.done():
            return await inflight
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._load_inflight[sid] = fut
        try:
            # Only block load of this session if it is mid-turn (not other projects).
            if session_id in self.active_turns:
                if self.is_turn_stuck(session_id=session_id):
                    await self.end_turn(session_id, "stuck turn before session/load")
                else:
                    raise RuntimeError("Turn in progress; cannot load this session")
            # Cancel any prior delayed release for this sid before re-arming.
            self.release_load_suppress(sid)
            self._loading_sessions.add(sid)
            self._load_suppress_counts.setdefault(sid, 0)
            try:
                result = await self.request(
                    "session/load",
                    {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
                    timeout=60.0,
                    session_id=session_id,
                )
                self.loaded_session_id = session_id
                self._warm_sessions.add(sid)
            finally:
                # Quiet-period release: agent may flush historical frames for many
                # seconds after the RPC result. Keep sid in _loading_sessions; each
                # suppressed frame rearms the quiet timer until silence or max hold.
                now = time.monotonic()
                self._load_suppress_deadline[sid] = now + LOAD_SUPPRESS_MAX_S
                prev = self._load_release_handles.pop(sid, None)
                if prev is not None:
                    prev.cancel()

                def _delayed_release(s: str = sid) -> None:
                    self._load_release_handles.pop(s, None)
                    self.release_load_suppress(s)

                self._load_release_handles[sid] = loop.call_later(
                    LOAD_SUPPRESS_QUIET_S, _delayed_release
                )
            if not fut.done():
                fut.set_result(result)
            return result
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            if self._load_inflight.get(sid) is fut:
                self._load_inflight.pop(sid, None)

    def _rearm_load_suppress_release(self, session_id: str) -> None:
        """Reset quiet-period timer after a suppressed frame; release if past max."""
        sid = str(session_id or "").strip()
        if not sid or sid not in self._loading_sessions:
            return
        now = time.monotonic()
        deadline = self._load_suppress_deadline.get(sid)
        # No deadline yet (mid session/load before finally): do not force-release.
        # Missing deadline must not be treated as held_s=max.
        if deadline is not None:
            held_s = now - (deadline - LOAD_SUPPRESS_MAX_S)
            if load_suppress_should_release(
                quiet_elapsed_s=0.0,  # rearm means frame just arrived
                quiet_s=LOAD_SUPPRESS_QUIET_S,
                held_s=held_s,
                max_s=LOAD_SUPPRESS_MAX_S,
            ):
                self.release_load_suppress(sid)
                return
        prev = self._load_release_handles.pop(sid, None)
        if prev is not None:
            prev.cancel()
        remaining = (
            (deadline - now) if deadline is not None else LOAD_SUPPRESS_MAX_S
        )
        delay = min(LOAD_SUPPRESS_QUIET_S, max(0.0, remaining))
        if delay <= 0:
            self.release_load_suppress(sid)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.release_load_suppress(sid)
            return

        def _delayed_release(s: str = sid) -> None:
            self._load_release_handles.pop(s, None)
            self.release_load_suppress(s)

        self._load_release_handles[sid] = loop.call_later(delay, _delayed_release)


    async def wait_load_suppress_settled(
        self, session_id: str, *, timeout: float | None = None
    ) -> None:
        """Wait until load-replay quiet suppress ends for session_id.

        If sid is not in ``_loading_sessions``, return immediately. Otherwise
        poll until it leaves or timeout (default ``LOAD_SUPPRESS_MAX_S``).
        On timeout, force-release so callers never hang. Does not force-release
        early on the success path (quiet timer expires naturally).
        """
        sid = str(session_id or "").strip()
        if not sid or sid not in self._loading_sessions:
            return
        thr = LOAD_SUPPRESS_MAX_S if timeout is None else float(timeout)
        deadline = time.monotonic() + max(0.0, thr)
        while sid in self._loading_sessions:
            if time.monotonic() >= deadline:
                self.release_load_suppress(sid)
                return
            await asyncio.sleep(0.05)

    def release_load_suppress(self, session_id: str | None = None) -> None:
        """Immediately stop load-replay suppress so live prompts get activity.

        Discards sid from ``_loading_sessions``, clears quiet/max deadline,
        emits count trace if any, and cancels a pending delayed release.
        Safe if already released. When session_id is None, release all.
        """
        if session_id is None:
            sids = (
                list(self._loading_sessions)
                or list(self._load_release_handles)
                or list(self._load_suppress_deadline)
            )
            for s in sids:
                self.release_load_suppress(s)
            return
        sid = str(session_id).strip()
        if not sid:
            return
        handle = self._load_release_handles.pop(sid, None)
        if handle is not None:
            handle.cancel()
        self._load_suppress_deadline.pop(sid, None)
        count = self._load_suppress_counts.pop(sid, 0)
        was_loading = sid in self._loading_sessions
        self._loading_sessions.discard(sid)
        if count:
            self._trace(
                "load_replay_suppressed",
                sessionId=session_id_slice(sid),
                count=count,
            )
        elif was_loading:
            # No frames counted; still clear quietly.
            pass

    async def _stall_watchdog_loop(
        self, session_id: str, no_output_seconds: float
    ) -> None:
        """Continuous monitor: no-output, mid-turn stall, and max turn duration.

        Per-session: only watches the given session_id.
        """
        try:
            while session_id in self.active_turns:
                await asyncio.sleep(1.0)
                if session_id not in self.active_turns:
                    return
                meta = self.active_turns.get(session_id) or {}
                started = meta.get("started_at")
                if started is None:
                    continue
                now = time.monotonic()
                age_start = now - float(started)
                activity_at = meta.get("last_activity")
                age_activity = (
                    (now - float(activity_at)) if activity_at is not None else age_start
                )
                reason = should_force_clear_turn(
                    bool(meta.get("saw_update")),
                    age_start,
                    age_activity,
                    no_output_seconds=no_output_seconds,
                    mid_turn_stall_seconds=MID_TURN_STALL_SECONDS,
                    max_turn_seconds=MAX_TURN_SECONDS,
                )
                if reason:
                    # Cancel agent first (bounded), then local clear — never
                    # unlock-only or fire-and-forget cancel after clear.
                    await self.end_turn(session_id, reason)
                    return
        except asyncio.CancelledError:
            return

    def _register_active_turn(self, session_id: str, cwd_key: str = "") -> None:
        now = time.monotonic()
        self.active_turns[session_id] = {
            "started_at": now,
            "last_activity": now,
            "saw_update": False,
            "first_update_at": None,
            "cwd_key": str(cwd_key or ""),
        }

    def _clear_active_turn(self, session_id: str) -> None:
        self.active_turns.pop(session_id, None)
        self._cancel_stall_watchdog(session_id)

    async def session_compact(
        self,
        session_id: str,
        context: str | None = None,
    ) -> Any:
        """Sole ACP compact entry for hub/CLI remote parity.

        Uses method ``_x.ai/compact_conversation`` only (``x.ai/…`` is not found
        on agent serve). This is the same agent compaction engine the TUI slash
        ``/compact`` drives via serve; session/prompt of ``/compact`` does not
        work. Unknown extra fields are ignored by the agent; only pass context
        when set.
        """
        params: dict[str, Any] = {"sessionId": session_id}
        ctx = (context or "").strip()
        if ctx:
            params["context"] = ctx
        return await self.request(
            "_x.ai/compact_conversation",
            params,
            timeout=180.0,
            session_id=session_id,
        )

    async def session_prompt(
        self,
        session_id: str,
        text: str,
        cwd: str | None = None,
        *,
        allow_load: bool = True,
        no_output_seconds: float | None = None,
        cwd_key: str = "",
    ) -> Any:
        """Send session/prompt for a session that already exists in the agent.

        Hub path: allow_load=False after session/new (multi-session prompt by id
        without session/load). session/load of foreign or post-restart disk ids
        hangs with zero session/update.

        allow_load=True: rare explicit load path; still dangerous for CLI ids.

        Concurrent multi-session: another session running does not raise busy.
        Only this session already active (and not stuck) raises busy.

        no_output_seconds: hang threshold; None uses NO_OUTPUT_SECONDS.
        """
        thr = NO_OUTPUT_SECONDS if no_output_seconds is None else no_output_seconds

        if session_id in self.active_turns:
            if self.is_turn_stuck(session_id=session_id):
                await self.end_turn(session_id, "stuck turn before new prompt")
            else:
                raise RuntimeError("Agent is busy with another turn")

        # Hub pre-prompt timing (load vs reuse); no wait_until_up / sleep on this path.
        t0 = time.monotonic()
        did_load = False

        if self.loaded_session_id != session_id:
            if allow_load:
                if not cwd:
                    raise RuntimeError("Session not loaded; cwd required to load")
                # Prefer session_load so warm-set skip applies (not model prefill).
                await self.session_load(session_id, cwd)
                self.loaded_session_id = session_id
                did_load = True
            else:
                # Multi-session agent: prompt by id without session/load.
                # Caller must have session/new'd this id in this process.
                self.loaded_session_id = session_id

        key = cwd_key or ""
        # Historical load-replay must quiet-suppress fully before the live turn.
        if session_id in self._loading_sessions:
            await self.wait_load_suppress_settled(session_id)
        # Idempotent if quiet timer already released suppress.
        self.release_load_suppress(session_id)
        self._register_active_turn(session_id, key)
        self._cancel_stall_watchdog(session_id)
        # Always run continuous stall watchdog for mid-turn / max duration.
        self._stall_watchdogs[session_id] = asyncio.create_task(
            self._stall_watchdog_loop(session_id, thr),
            name=f"acp-stall-{session_id[:8]}",
        )
        pre_send_ms = (time.monotonic() - t0) * 1000.0
        log.info(
            "Prompt start session=%s (active=%d) pre_send_ms=%.1f load=%s",
            session_id,
            len(self.active_turns),
            pre_send_ms,
            did_load,
        )
        self._trace(
            "prompt_start",
            sessionId=session_id_slice(session_id),
            active=len(self.active_turns),
        )
        self._trace(
            "prompt_send",
            sessionId=session_id_slice(session_id),
            preSendMs=round(pre_send_ms, 2),
            load=did_load,
        )
        try:
            # Match request timeout to MAX_TURN_SECONDS (TUI-length agentic turns).
            # Await is outside any global lock so other sessions can prompt.
            result = await self.request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout=float(MAX_TURN_SECONDS),
                session_id=session_id,
            )
            log.info("Prompt end session=%s ok", session_id)
            self._trace("prompt_end", sessionId=session_id_slice(session_id), ok=True)
            return result
        except Exception as exc:
            log.warning("Prompt end session=%s error: %s", session_id, exc)
            self._trace(
                "prompt_end",
                sessionId=session_id_slice(session_id),
                ok=False,
                error=str(exc)[:200],
            )
            raise
        finally:
            self._clear_active_turn(session_id)

    async def notify_agent_cancel(self, session_id: str) -> bool:
        """Tell the agent to abort its in-flight turn. Does not force_clear locally.

        Prefers the last successful cancel method; short per-method timeout so
        shotgun cancel stays bounded (~2s total with end_turn). Returns True if
        any method succeeded. Logs a warning if all fail.
        """
        methods: list[str] = [
            "session/cancel",
            "session/prompt/cancel",
            "x.ai/session/cancel",
            "_x.ai/session/cancel",
        ]
        cached = self._cancel_method
        if cached and cached in methods:
            methods = [cached] + [m for m in methods if m != cached]
        last_exc: Exception | None = None
        # Short per-method timeout: total cancel budget stays ~2s under end_turn.
        per_method_timeout = 1.5
        for method in methods:
            try:
                await self.request(
                    method,
                    {"sessionId": session_id},
                    timeout=per_method_timeout,
                    session_id=session_id,
                )
                self._cancel_method = method
                log.info(
                    "Agent cancel ok via %s session=%s", method, session_id[:12]
                )
                return True
            except Exception as exc:
                last_exc = exc
                if cached and method == cached:
                    # Cached method failed; stop preferring it until one succeeds.
                    self._cancel_method = None
                log.debug("Cancel via %s failed: %s", method, exc)
        log.warning(
            "Agent cancel unsupported/failed session=%s: %s",
            session_id,
            last_exc or "no method succeeded",
        )
        return False

    async def end_turn(
        self,
        session_id: str | None,
        reason: str,
        *,
        cancel_timeout: float = 2.0,
    ) -> dict:
        """Cancel agent then force-clear local turn. Single-flight per session.

        Order is always cancel-before-clear so the agent releases the old prompt
        before the hub unlocks. Cancel is bounded by cancel_timeout; clear runs
        even if cancel fails (WS dead, unsupported method, etc.).
        """
        sid_key = str(session_id) if session_id is not None else "__all__"
        lock = self._end_turn_locks.get(sid_key)
        if lock is None:
            lock = asyncio.Lock()
            self._end_turn_locks[sid_key] = lock

        async with lock:
            cancelled = False
            if session_id is not None:
                try:
                    cancelled = bool(
                        await asyncio.wait_for(
                            self.notify_agent_cancel(str(session_id)),
                            timeout=float(cancel_timeout),
                        )
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "end_turn: cancel timed out session=%s reason=%s",
                        str(session_id)[:12],
                        reason,
                    )
                    cancelled = False
                except Exception as exc:
                    log.warning(
                        "end_turn: cancel failed session=%s: %s",
                        str(session_id)[:12],
                        exc,
                    )
                    cancelled = False
            else:
                # All sessions: best-effort cancel each active id within budget.
                sids = list(self.active_turns.keys())
                if sids:
                    per = max(0.3, float(cancel_timeout) / max(len(sids), 1))
                    any_ok = False
                    for sid in sids:
                        try:
                            ok = await asyncio.wait_for(
                                self.notify_agent_cancel(sid),
                                timeout=per,
                            )
                            any_ok = any_ok or bool(ok)
                        except Exception:
                            pass
                    cancelled = any_ok

            cleared = self.force_clear_turn(reason, session_id=session_id)
            if session_id is not None and not cancelled:
                log.warning(
                    "end_turn: agent cancel failed session=%s force_cleared=%s reason=%s",
                    str(session_id)[:12],
                    cleared,
                    reason,
                )
            return {
                "cancelled": cancelled,
                "cleared": cleared,
                "reason": reason,
            }

    async def session_cancel(self, session_id: str) -> None:
        """Cancel agent turn for one session and unlock local hub turn state.

        Only clears this session's turn and its pending user questions.
        Other projects' turns are left running.
        """
        self._cancel_pending_user_questions_for_session(session_id)
        await self.end_turn(session_id, "user cancel")
        # Never raise after local force-clear: UI must unlock on Stop.
