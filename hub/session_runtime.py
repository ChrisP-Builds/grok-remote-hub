"""One runtime-state object per ACP session id, shared by Hub and AcpClient (ADR-030)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator, MutableMapping
from dataclasses import dataclass, field
from typing import Any, TypeVar


@dataclass
class BackgroundActivity:
    """Current background episode; stamps are monotonic seconds."""

    last_at: float
    started_at: float
    kind: str | None = None
    rich_at: float | None = None


@dataclass
class LoadState:
    """session/load single-flight and quiet-period fanout suppress (ADR-016)."""

    inflight: asyncio.Future | None = None
    loading: bool = False
    suppress_count: int = 0
    release_handle: asyncio.TimerHandle | None = None
    suppress_deadline: float | None = None

    def is_active(self) -> bool:
        return (
            self.loading
            or self.inflight is not None
            or self.release_handle is not None
            or self.suppress_deadline is not None
            or self.suppress_count > 0
        )


@dataclass
class CompactState:
    """Hub-owned /compact gate deadline and stashed agent token counts."""

    gate_until: float | None = None
    agent_tokens: dict[str, int | None] | None = None


@dataclass
class SessionRuntime:
    session_id: str
    turn: dict[str, Any] | None = None
    turn_end_waiters: list[asyncio.Future] = field(default_factory=list)
    stall_watchdog: asyncio.Task | None = None
    end_turn_lock: asyncio.Lock | None = None
    background: BackgroundActivity | None = None
    pending_question_ids: set[str] = field(default_factory=set)
    load: LoadState = field(default_factory=LoadState)
    warm: bool = False
    compact: CompactState = field(default_factory=CompactState)
    ops_bg_phase: str | None = None
    bg_status_debounce_until: float | None = None

    def is_busy(self) -> bool:
        """Work still in progress for this session; a delete must not erase it."""
        watchdog = self.stall_watchdog
        lock = self.end_turn_lock
        return (
            self.turn is not None
            or (watchdog is not None and not watchdog.done())
            or (lock is not None and lock.locked())
            or self.background is not None
            or bool(self.pending_question_ids)
            or self.load.is_active()
        )

    def is_live(self, now: float) -> bool:
        """Busy, or holding a fact that only an explicit clear or an expiry may drop."""
        return (
            self.is_busy()
            or self.warm
            or self.ops_bg_phase is not None
            or self.compact.agent_tokens is not None
            or _deadline_ahead(self.compact.gate_until, now)
            or _deadline_ahead(self.bg_status_debounce_until, now)
        )


def _deadline_ahead(deadline: float | None, now: float) -> bool:
    return deadline is not None and deadline > now


class SessionRuntimes:
    """Registry of SessionRuntime by session id. Ids are stored as given; empty ids are refused."""

    def __init__(self) -> None:
        self._by_id: dict[str, SessionRuntime] = {}

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, session_id: object) -> bool:
        return session_id in self._by_id

    def get(self, session_id: str | None) -> SessionRuntime | None:
        if not session_id:
            return None
        return self._by_id.get(str(session_id))

    def get_or_create(self, session_id: str) -> SessionRuntime:
        sid = str(session_id or "")
        if not sid.strip():
            raise ValueError("session id required")
        rt = self._by_id.get(sid)
        if rt is None:
            rt = SessionRuntime(sid)
            self._by_id[sid] = rt
        return rt

    def drop(self, session_id: str) -> SessionRuntime | None:
        return self._by_id.pop(str(session_id or ""), None)

    def items(self) -> list[tuple[str, SessionRuntime]]:
        return list(self._by_id.items())

    def prune(
        self, session_id: str | None = None, *, now: float | None = None
    ) -> list[str]:
        """Drop entries with nothing live: one session, or every session when id is None."""
        ts = time.monotonic() if now is None else float(now)
        if session_id is not None:
            rt = self._by_id.get(str(session_id))
            if rt is None or rt.is_live(ts):
                return []
            del self._by_id[rt.session_id]
            return [rt.session_id]
        dropped = [sid for sid, rt in self._by_id.items() if not rt.is_live(ts)]
        for sid in dropped:
            del self._by_id[sid]
        return dropped


_V = TypeVar("_V")


class RuntimeFieldMap(MutableMapping[str, _V]):
    """Dict-shaped live view of one SessionRuntime field; a None field means absent."""

    def __init__(
        self,
        runtimes: SessionRuntimes,
        read: Callable[[SessionRuntime], _V | None],
        write: Callable[[SessionRuntime, _V | None], None],
    ) -> None:
        self._runtimes = runtimes
        self._read = read
        self._write = write

    def __getitem__(self, session_id: str) -> _V:
        rt = self._runtimes.get(session_id)
        value = self._read(rt) if rt is not None else None
        if value is None:
            raise KeyError(session_id)
        return value

    def __setitem__(self, session_id: str, value: _V) -> None:
        if value is None:
            self.pop(session_id, None)
            return
        self._write(self._runtimes.get_or_create(session_id), value)

    def __delitem__(self, session_id: str) -> None:
        rt = self._runtimes.get(session_id)
        if rt is None or self._read(rt) is None:
            raise KeyError(session_id)
        self._write(rt, None)
        self._runtimes.prune(rt.session_id)

    def __iter__(self) -> Iterator[str]:
        return (
            sid for sid, rt in self._runtimes.items() if self._read(rt) is not None
        )

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({dict(self)!r})"


def turn_map(runtimes: SessionRuntimes) -> RuntimeFieldMap[dict[str, Any]]:
    def _write(rt: SessionRuntime, meta: dict[str, Any] | None) -> None:
        ended = rt.turn is not None and rt.turn is not meta
        rt.turn = meta
        if ended:
            waiters, rt.turn_end_waiters = rt.turn_end_waiters, []
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(None)

    return RuntimeFieldMap(runtimes, read=lambda rt: rt.turn, write=_write)


def compact_gate_map(runtimes: SessionRuntimes) -> RuntimeFieldMap[float]:
    def _write(rt: SessionRuntime, deadline: float | None) -> None:
        rt.compact.gate_until = deadline

    return RuntimeFieldMap(runtimes, read=lambda rt: rt.compact.gate_until, write=_write)
