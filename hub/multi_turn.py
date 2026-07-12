"""Pure multi-project concurrent turn helpers (no I/O).

Hub gate for multi-cwd live turns and session-list status flags.
Multi-process agent pool is a future scale-out if the agent serializes turns;
this module still ensures the hub does not globally block different projects.
"""

from __future__ import annotations

from typing import Iterable

STATUS_WORKING = "working"
STATUS_QUESTION = "question"
STATUS_IDLE = "idle"

__all__ = (
    "STATUS_WORKING",
    "STATUS_QUESTION",
    "STATUS_IDLE",
    "session_status_flag",
    "can_start_concurrent_turn",
    "merge_session_flags",
    "LiveTurnRegistry",
)


def session_status_flag(*, turn_running: bool, has_pending_question: bool) -> str:
    """Return mutually exclusive status: question wins over working; else idle."""
    if has_pending_question:
        return STATUS_QUESTION
    if turn_running:
        return STATUS_WORKING
    return STATUS_IDLE


def can_start_concurrent_turn(
    session_id: str,
    cwd_key: str,
    *,
    active_by_session: dict[str, str],
    max_concurrent: int,
) -> tuple[bool, str]:
    """Decide whether a new live turn may start for session_id / cwd_key.

    Returns (allowed, reason):
    - already_active: this session already has a turn (caller treats as busy/queue)
    - ok: may start immediately
    - same_cwd_busy: another session with the same cwd_key is active
    - max_concurrent: at capacity for a new project turn
    """
    sid = str(session_id or "")
    key = str(cwd_key or "")
    active = {str(k): str(v) for k, v in (active_by_session or {}).items()}
    cap = max(1, int(max_concurrent))

    if sid and sid in active:
        return True, "already_active"

    if key:
        for other_sid, other_key in active.items():
            if other_sid != sid and other_key == key:
                return False, "same_cwd_busy"

    if len(active) >= cap:
        return False, "max_concurrent"

    return True, "ok"


def merge_session_flags(
    session_ids: list[str],
    *,
    active_sessions: set[str],
    pending_question_sessions: set[str],
) -> dict[str, str]:
    """Map each session id to working | question | idle."""
    active = {str(s) for s in (active_sessions or set())}
    pending = {str(s) for s in (pending_question_sessions or set())}
    out: dict[str, str] = {}
    for raw in session_ids or []:
        sid = str(raw)
        out[sid] = session_status_flag(
            turn_running=sid in active,
            has_pending_question=sid in pending,
        )
    return out


class LiveTurnRegistry:
    """In-memory tracker for active turns and pending user questions."""

    def __init__(self) -> None:
        # session_id -> cwd_key
        self._active: dict[str, str] = {}
        self._pending_questions: set[str] = set()

    def start_turn(self, session_id: str, cwd_key: str = "") -> None:
        sid = str(session_id or "")
        if not sid:
            return
        self._active[sid] = str(cwd_key or "")

    def end_turn(self, session_id: str) -> None:
        sid = str(session_id or "")
        if sid:
            self._active.pop(sid, None)

    def set_question(self, session_id: str) -> None:
        sid = str(session_id or "")
        if sid:
            self._pending_questions.add(sid)

    def clear_question(self, session_id: str) -> None:
        sid = str(session_id or "")
        if sid:
            self._pending_questions.discard(sid)

    def clear_all(self) -> None:
        self._active.clear()
        self._pending_questions.clear()

    def is_active(self, session_id: str) -> bool:
        return str(session_id or "") in self._active

    def active_count(self) -> int:
        return len(self._active)

    def active_by_session(self) -> dict[str, str]:
        return dict(self._active)

    def active_session_ids(self) -> set[str]:
        return set(self._active.keys())

    def pending_question_sessions(self) -> set[str]:
        return set(self._pending_questions)

    def flag_for(self, session_id: str) -> str:
        sid = str(session_id or "")
        return session_status_flag(
            turn_running=sid in self._active,
            has_pending_question=sid in self._pending_questions,
        )

    def flags_for(self, session_ids: Iterable[str]) -> dict[str, str]:
        return merge_session_flags(
            [str(s) for s in session_ids],
            active_sessions=self.active_session_ids(),
            pending_question_sessions=self.pending_question_sessions(),
        )
