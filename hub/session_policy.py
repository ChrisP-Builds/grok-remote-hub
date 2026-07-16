"""Pure policy helpers for hub-owned remote agent sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Dead on arrival: no ACP update after prompt accepted.
NO_OUTPUT_SECONDS = 60.0
# Soft / heavy history: first TTFB can exceed 60s while agent digests updates.jsonl.
NO_OUTPUT_SOFT_SECONDS = 180.0
NO_OUTPUT_HEAVY_SECONDS = 300.0
NO_OUTPUT_HEAVY_UPDATES_BYTES = 12_000_000
# Second attempt after no-output auto-retry (at least this long).
NO_OUTPUT_RETRY_SECONDS = 300.0

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

# Soft history size for no-output stall scaling (not UI banner).
CONTEXT_SOFT_UPDATES_BYTES = 6_000_000  # ~6MB updates.jsonl


def no_output_seconds_for_session(
    *,
    updates_bytes: int | None = None,
    soft_updates_bytes: int = CONTEXT_SOFT_UPDATES_BYTES,
    base_seconds: float = NO_OUTPUT_SECONDS,
    soft_seconds: float = NO_OUTPUT_SOFT_SECONDS,
    heavy_bytes: int = NO_OUTPUT_HEAVY_UPDATES_BYTES,
    heavy_seconds: float = NO_OUTPUT_HEAVY_SECONDS,
) -> float:
    """Return stall no-output threshold scaled by session history size."""
    if updates_bytes is None:
        return float(base_seconds)
    try:
        size = int(updates_bytes)
    except (TypeError, ValueError):
        return float(base_seconds)
    if size > int(heavy_bytes):
        return float(heavy_seconds)
    if size > int(soft_updates_bytes):
        return float(soft_seconds)
    return float(base_seconds)


def turn_telemetry(
    *,
    started_at: float | None,
    last_activity: float | None,
    saw_update: bool,
    now: float,
    first_update_at: float | None = None,
) -> dict[str, Any]:
    """Return ageSeconds, silenceSeconds, sawUpdate, ttfbSeconds for a live turn.

    - age = now - started_at (None if no start)
    - silence = now - last_activity, or age if no activity stamp
    - ttfb = first_update_at - started_at when first_update_at set;
      else last_activity - started_at when saw_update; else None
    """
    saw = bool(saw_update)
    age: float | None = None
    silence: float | None = None
    ttfb: float | None = None
    if started_at is not None:
        start = float(started_at)
        age = float(now) - start
        if age < 0:
            age = 0.0
        if last_activity is not None:
            silence = float(now) - float(last_activity)
            if silence < 0:
                silence = 0.0
        else:
            silence = age
        if first_update_at is not None:
            ttfb = float(first_update_at) - start
            if ttfb < 0:
                ttfb = 0.0
        elif saw and last_activity is not None:
            ttfb = float(last_activity) - start
            if ttfb < 0:
                ttfb = 0.0
    return {
        "ageSeconds": age,
        "silenceSeconds": silence,
        "sawUpdate": saw,
        "ttfbSeconds": ttfb,
    }


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


def is_no_output_error_message(msg: str | BaseException | None) -> bool:
    """True when an exception/message is the zero-ACP-update hang (force-clear)."""
    text = str(msg or "").lower()
    return "no acp session/update" in text or (
        "force-cleared" in text and "session/update" in text
    )


def should_auto_retry_no_output(
    exc: BaseException | str | None, already_retried: bool
) -> bool:
    """True when no-output should trigger one same-session auto-retry."""
    if already_retried:
        return False
    return is_no_output_error_message(exc)


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


def is_hub_resume_candidate(
    session_id: str | None,
    *,
    created_set: set[str] | frozenset[str],
    remote_map_ids: set[str] | frozenset[str],
    hub_origin: str | None = None,
) -> bool:
    """True when id may be resumed via session/load after restart.

    Process-live (created_set), disk hub_origin user|attach, remote map value,
    or durable hubIds (passed via remote_map_ids by the caller).
    Pure: caller supplies hub_origin from disk I/O.
    """
    if not session_id:
        return False
    sid = str(session_id).strip()
    if not sid:
        return False
    if sid in created_set:
        return True
    origin = str(hub_origin or "").strip()
    if origin in ("user", "attach"):
        return True
    if sid in remote_map_ids:
        return True
    return False


def cwd_key(cwd: str | None) -> str:
    """Normalize cwd for map keys (casefold, backslash, strip trailing sep)."""
    return str(cwd or "").replace("/", "\\").rstrip("\\").casefold()


def sessions_matching_cwd(
    items: list[dict[str, Any]] | None,
    cwd: str | None,
    *,
    exclude_subagents: bool = True,
) -> list[dict[str, Any]]:
    """Filter session dicts (with cwd, sessionId, isSubagent?, updatedAt?) to same cwd_key.

    Sort by updatedAt descending when present. Prefer non-subagents when
    exclude_subagents is True (subagents dropped entirely).
    """
    key = cwd_key(cwd)
    if not key or not items:
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if exclude_subagents and item.get("isSubagent"):
            continue
        item_key = cwd_key(item.get("cwd"))
        if item_key and item_key == key:
            out.append(item)

    def _sort_key(row: dict[str, Any]) -> tuple[int, str]:
        # Prefer rows with updatedAt; empty sorts last via (0, "")
        ts = str(row.get("updatedAt") or "").strip()
        return (1 if ts else 0, ts)

    out.sort(key=_sort_key, reverse=True)
    return out


def entry_requires_resume_choice(prior_count: int) -> bool:
    """True when New flow must offer Resume vs Start new (prior_count > 0)."""
    try:
        n = int(prior_count)
    except (TypeError, ValueError):
        n = 0
    return n > 0


def recovery_keeps_session_id(
    before_id: str, after_id: str, switched_to_new: bool
) -> bool:
    """True when recovery succeeded on same id without treating silent new as ok.

    Same non-empty ids and not switched_to_new.
    """
    before = str(before_id or "").strip()
    after = str(after_id or "").strip()
    if not before or not after:
        return False
    if switched_to_new:
        return False
    return before == after


def resolve_ensure_action(
    view_session_id: str | None,
    cwd: str | None,
    created_set: set[str] | frozenset[str],
    remote_by_cwd: dict[str, str],
    *,
    view_hub_origin: str | None = None,
    remote_hub_origin: str | None = None,
    hub_owned_ids: set[str] | frozenset[str] | None = None,
) -> tuple[str | None, str, str]:
    """Resolve ensure path: (target_id | None, action, reason).

    action is one of: reuse | load | new.

    Preference order (hub-owned view is continuity source of truth after restart):
    1. view process-live → reuse / hub_session (caller records byCwd on use)
    2. view hub resume candidate (origin/hubIds/map) → load / resume_view
    3. byCwd process-live → reuse / reuse_cwd
    4. byCwd resume candidate → load / resume_cwd
    5. else → new / need_session_new

    Rationale: the session the user has open is continuity; byCwd is fallback when
    view is foreign/CLI or empty. One-live-per-cwd is kept by updating byCwd when
    view is used (caller _record_hub_session). hub_owned_ids: durable hub session
    ids from remote-sessions.json hubIds (union into resume set).
    """
    view = str(view_session_id or "").strip() or None
    remote_map_ids = set(remote_by_cwd.values()) if remote_by_cwd else set()
    if hub_owned_ids:
        remote_map_ids |= set(hub_owned_ids)

    if view and view in created_set:
        return view, "reuse", "hub_session"

    if view and is_hub_resume_candidate(
        view,
        created_set=created_set,
        remote_map_ids=remote_map_ids,
        hub_origin=view_hub_origin,
    ):
        return view, "load", "resume_view"

    key = cwd_key(cwd)
    if key:
        existing = remote_by_cwd.get(key)
        if existing:
            existing = str(existing).strip()
            if existing:
                if existing in created_set:
                    return existing, "reuse", "reuse_cwd"
                if is_hub_resume_candidate(
                    existing,
                    created_set=created_set,
                    remote_map_ids=remote_map_ids,
                    hub_origin=remote_hub_origin,
                ):
                    return existing, "load", "resume_cwd"

    return None, "new", "need_session_new"


def resolve_live_session_id(
    view_session_id: str | None,
    cwd: str | None,
    created_set: set[str] | frozenset[str],
    remote_by_cwd: dict[str, str],
) -> tuple[str | None, bool, str]:
    """Process-live only: reusable id without session/load.

    Returns (live_session_id | None, needs_session_new, reason).

    Prefer resolve_ensure_action for ensure/attach (supports post-restart load).
    Kept for backward compatibility and process-live-only checks.
    """
    target, action, reason = resolve_ensure_action(
        view_session_id,
        cwd,
        created_set,
        remote_by_cwd,
    )
    if action == "reuse" and target:
        return target, False, reason
    return None, True, "need_session_new"


def load_remote_sessions(path: Path | str) -> dict[str, str]:
    """Load cwd_key -> session_id map from JSON. Empty dict if missing/invalid.

    Accepts {"byCwd": {...}, "hubIds": [...]} or legacy flat {cwd: id} map.
    Returns only byCwd for backward compatibility; use load_hub_session_ids
    for the durable hub id set.
    """
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
    # Accept either {"byCwd": {...}} or flat map (ignore non-map keys like hubIds)
    if "byCwd" in raw:
        src: Any = raw.get("byCwd")
    else:
        src = {k: v for k, v in raw.items() if k != "hubIds"}
    if not isinstance(src, dict):
        return {}
    for k, v in src.items():
        key = cwd_key(str(k))
        sid = str(v or "").strip()
        if key and sid:
            out[key] = sid
    return out


def load_hub_session_ids(path: Path | str) -> set[str]:
    """Load durable hub session ids from remote-sessions.json hubIds array.

    Also includes byCwd values so older files without hubIds still contribute.
    Empty set if missing/invalid.
    """
    p = Path(path)
    if not p.is_file():
        return set()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return set()
    if not isinstance(raw, dict):
        return set()
    out: set[str] = set()
    hub_list = raw.get("hubIds")
    if isinstance(hub_list, list):
        for item in hub_list:
            sid = str(item or "").strip()
            if sid:
                out.add(sid)
    # Merge byCwd values (or flat map) so map-only files still resume
    mapping = load_remote_sessions(path)
    out.update(mapping.values())
    return out


def save_remote_sessions(
    path: Path | str,
    mapping: dict[str, str],
    hub_ids: set[str] | frozenset[str] | list[str] | None = None,
) -> None:
    """Persist byCwd map and hubIds. Creates parent dirs as needed.

    When hub_ids is None, preserve existing hubIds from disk and merge map values.
    When hub_ids is provided, use that set unioned with map values.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean: dict[str, str] = {}
    for k, v in (mapping or {}).items():
        key = cwd_key(str(k))
        sid = str(v or "").strip()
        if key and sid:
            clean[key] = sid

    ids: set[str] = set()
    if hub_ids is None:
        # Preserve prior hubIds (do not drop historical hub-owned ids)
        if p.is_file():
            try:
                prev = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(prev, dict) and isinstance(prev.get("hubIds"), list):
                    for item in prev["hubIds"]:
                        sid = str(item or "").strip()
                        if sid:
                            ids.add(sid)
            except (OSError, json.JSONDecodeError, UnicodeError):
                pass
    else:
        for item in hub_ids:
            sid = str(item or "").strip()
            if sid:
                ids.add(sid)
    ids.update(clean.values())

    payload = {
        "byCwd": clean,
        "hubIds": sorted(ids),
    }
    p.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
