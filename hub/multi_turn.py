"""Pure multi-project concurrent turn helpers (no I/O).

Hub gate for multi-cwd live turns and session-list status flags.
Multi-process agent pool is a future scale-out if the agent serializes turns;
this module still ensures the hub does not globally block different projects.
"""

from __future__ import annotations

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
