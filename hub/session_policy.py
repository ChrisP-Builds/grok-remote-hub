"""Pure policy helpers for hub-owned remote agent sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Dead on arrival: no ACP update after prompt accepted.
NO_OUTPUT_SECONDS = 60.0

# Silence after activity — only for truly dead agents (tools can be quiet minutes).
MID_TURN_STALL_SECONDS = 600.0  # 10 min

# Hard wall (align with session/prompt request timeout; long agentic like TUI).
MAX_TURN_SECONDS = 1800.0  # 30 min

# Wall used only when no activity signal is available; new-prompt stuck uses
# should_force_clear_turn (mid-turn stall or max wall), not a short healthy timer.
STUCK_TURN_SECONDS = 1800.0

# Client: soft warn only; never auto reset-turn / unlock.
CLIENT_STALL_WARN_SECONDS = 120.0
CLIENT_STALL_UNLOCK_SECONDS = 0  # 0 = disabled auto-unlock


def should_force_clear_turn(
    saw_update: bool,
    age_since_start: float,
    age_since_activity: float,
    *,
    no_output_seconds: float = NO_OUTPUT_SECONDS,
    mid_turn_stall_seconds: float = MID_TURN_STALL_SECONDS,
    max_turn_seconds: float = MAX_TURN_SECONDS,
) -> str | None:
    """Return a force-clear reason, or None if the turn should keep running.

    Pure policy for the continuous stall watchdog:
    - max turn duration (even with activity)
    - zero ACP updates after prompt (foreign/hung load path)
    - mid-turn stall after last ACP activity
    """
    if age_since_start < 0:
        age_since_start = 0.0
    if age_since_activity < 0:
        age_since_activity = 0.0

    if max_turn_seconds > 0 and age_since_start >= max_turn_seconds:
        return (
            f"max turn duration ({age_since_start:.1f}s >= {max_turn_seconds}s)"
        )
    if (
        no_output_seconds > 0
        and not saw_update
        and age_since_start >= no_output_seconds
    ):
        return (
            f"no ACP session/update for {age_since_start:.1f}s after prompt "
            f"(threshold={no_output_seconds}s)"
        )
    if (
        mid_turn_stall_seconds > 0
        and saw_update
        and age_since_activity >= mid_turn_stall_seconds
    ):
        return (
            f"mid-turn stall ({age_since_activity:.1f}s since last ACP activity, "
            f"threshold={mid_turn_stall_seconds}s)"
        )
    return None


def is_turn_stuck_for_new_prompt(
    saw_update: bool,
    age_since_start: float,
    age_since_activity: float,
    *,
    no_output_seconds: float = NO_OUTPUT_SECONDS,
    mid_turn_stall_seconds: float = MID_TURN_STALL_SECONDS,
    max_turn_seconds: float = MAX_TURN_SECONDS,
) -> bool:
    """True when a running turn may be force-cleared before accepting a new prompt.

    Matches watchdog force-clear: no-output, mid-turn stall, or max wall —
    never after a short wall of healthy activity.
    """
    return (
        should_force_clear_turn(
            saw_update,
            age_since_start,
            age_since_activity,
            no_output_seconds=no_output_seconds,
            mid_turn_stall_seconds=mid_turn_stall_seconds,
            max_turn_seconds=max_turn_seconds,
        )
        is not None
    )


def needs_fresh_agent_session(session_id: str | None, created_set: set[str] | frozenset[str]) -> bool:
    """True when this hub process never created the session via session/new.

    CLI-originated (or other-process) sessions must not receive session/prompt
    via session/load; agent serve hangs with zero session/update. View history
    is fine; prompting requires a hub-managed agent session.
    """
    if not session_id:
        return True
    sid = str(session_id).strip()
    if not sid:
        return True
    return sid not in created_set


def cwd_key(cwd: str | None) -> str:
    """Normalize cwd for map keys (casefold, backslash, strip trailing sep)."""
    return str(cwd or "").replace("/", "\\").rstrip("\\").casefold()


def resolve_live_session_id(
    view_session_id: str | None,
    cwd: str | None,
    created_set: set[str] | frozenset[str],
    remote_by_cwd: dict[str, str],
) -> tuple[str | None, bool, str]:
    """Resolve a reusable live hub session without calling ACP.

    Returns (live_session_id | None, needs_session_new, reason).

    - If view is hub-created: reuse view (needs_new=False, reason=hub_session).
    - Else if cwd has a hub-created remote: reuse it (needs_new=False, reason=reuse_cwd).
    - Else: needs session/new (live_id=None, needs_new=True, reason=need_session_new).
    """
    view = str(view_session_id or "").strip() or None
    if view and not needs_fresh_agent_session(view, created_set):
        return view, False, "hub_session"

    key = cwd_key(cwd)
    if key:
        existing = remote_by_cwd.get(key)
        if existing and not needs_fresh_agent_session(existing, created_set):
            return str(existing), False, "reuse_cwd"

    return None, True, "need_session_new"


def load_remote_sessions(path: Path | str) -> dict[str, str]:
    """Load cwd_key -> session_id map from JSON. Empty dict if missing/invalid."""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    # Accept either {"byCwd": {...}} or flat map
    src: Any = raw.get("byCwd") if "byCwd" in raw else raw
    if not isinstance(src, dict):
        return {}
    for k, v in src.items():
        key = cwd_key(str(k))
        sid = str(v or "").strip()
        if key and sid:
            out[key] = sid
    return out


def save_remote_sessions(path: Path | str, mapping: dict[str, str]) -> None:
    """Persist cwd_key -> session_id map. Creates parent dirs as needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean: dict[str, str] = {}
    for k, v in (mapping or {}).items():
        key = cwd_key(str(k))
        sid = str(v or "").strip()
        if key and sid:
            clean[key] = sid
    payload = {"byCwd": clean}
    p.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
