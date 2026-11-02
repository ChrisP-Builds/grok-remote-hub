"""Hub-wide sequence numbers and a bounded per-session event log for reconnect replay. ADR-029."""

from __future__ import annotations

import json
from collections import OrderedDict, deque
from typing import Any, NamedTuple

SEQUENCED_TYPES = frozenset({"acp", "turn", "system", "terminal_out", "compact", "usage", "commands"})

MAX_EVENTS_PER_SESSION = 4000
MAX_BYTES_PER_SESSION = 4 * 1024 * 1024
MAX_SESSIONS = 64


def serialize(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)


class LoggedEvent(NamedTuple):
    seq: int
    text: str


class _SessionLog:
    __slots__ = ("events", "size")

    def __init__(self) -> None:
        self.events: deque[LoggedEvent] = deque()
        self.size = 0


class EventLog:
    def __init__(
        self,
        *,
        max_events: int = MAX_EVENTS_PER_SESSION,
        max_bytes: int = MAX_BYTES_PER_SESSION,
        max_sessions: int = MAX_SESSIONS,
    ) -> None:
        self.max_events = max(1, int(max_events))
        self.max_bytes = max(1, int(max_bytes))
        self.max_sessions = max(1, int(max_sessions))
        self._seq = 0
        self._sessions: OrderedDict[str, _SessionLog] = OrderedDict()
        # Highest seq dropped per session; outlives the session entry so gaps stay detectable.
        self._evicted: dict[str, int] = {}

    @property
    def seq(self) -> int:
        return self._seq

    def append(self, session_id: str, payload: dict[str, Any]) -> LoggedEvent:
        self._seq += 1
        payload["seq"] = self._seq
        event = LoggedEvent(self._seq, serialize(payload))
        slog = self._sessions.get(session_id)
        if slog is None:
            slog = self._sessions[session_id] = _SessionLog()
        else:
            self._sessions.move_to_end(session_id)
        slog.events.append(event)
        slog.size += len(event.text)
        self._trim(session_id, slog)
        self._evict_sessions()
        return event

    def head(self, session_id: str) -> int:
        slog = self._sessions.get(session_id)
        if slog is not None and slog.events:
            return slog.events[-1].seq
        return self._evicted.get(session_id, 0)

    def since(self, session_id: str, after_seq: int) -> list[LoggedEvent] | None:
        """Events with seq > after_seq, or None when the log cannot prove the range is complete."""
        if after_seq < 0 or after_seq > self._seq:
            return None
        if self._evicted.get(session_id, 0) > after_seq:
            return None
        slog = self._sessions.get(session_id)
        if slog is None:
            return []
        newer: list[LoggedEvent] = []
        for event in reversed(slog.events):
            if event.seq <= after_seq:
                break
            newer.append(event)
        newer.reverse()
        return newer

    def _trim(self, session_id: str, slog: _SessionLog) -> None:
        while len(slog.events) > 1 and (len(slog.events) > self.max_events or slog.size > self.max_bytes):
            old = slog.events.popleft()
            slog.size -= len(old.text)
            self._note_evicted(session_id, old.seq)

    def _evict_sessions(self) -> None:
        while len(self._sessions) > self.max_sessions:
            session_id, slog = self._sessions.popitem(last=False)
            self._note_evicted(session_id, slog.events[-1].seq)

    def _note_evicted(self, session_id: str, seq: int) -> None:
        if seq > self._evicted.get(session_id, 0):
            self._evicted[session_id] = seq


class ResumeDecision(NamedTuple):
    replay: bool
    reason: str
    since_seq: int
    events: list[LoggedEvent]


def _parse_seq(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def decide_resume(
    log: EventLog, session_id: str, since_seq: Any, boot_id: Any, hub_boot_id: str
) -> ResumeDecision:
    """Replay only for a well-formed request from this boot whose range the log still holds."""
    since = _parse_seq(since_seq)
    if since is None or not isinstance(boot_id, str) or not boot_id:
        return ResumeDecision(False, "none", 0, [])
    if boot_id != hub_boot_id:
        return ResumeDecision(False, "boot", since, [])
    events = log.since(session_id, since)
    if events is None:
        return ResumeDecision(False, "gap", since, [])
    return ResumeDecision(True, "", since, events)


def replay_frame(session_id: str, since_seq: int, events: list[LoggedEvent]) -> str:
    """One frame; fromSeq echoes the request (exclusive), toSeq is the last seq included."""
    to_seq = events[-1].seq if events else since_seq
    head = json.dumps(
        {"type": "replay", "sessionId": session_id, "fromSeq": since_seq, "toSeq": to_seq, "count": len(events)}
    )
    return head[:-1] + ',"events":[' + ",".join(e.text for e in events) + "]}"
