"""Pure UX helpers for topbar bubble, sticky scroll, and turn progress labels.

Mirrored in static/app.js for the browser UI; this module is the pytest source of truth.
"""

from __future__ import annotations


def topbar_bubble_lines(project: str, model: str, path: str) -> list[str]:
    """Return display lines for topbar info bubble."""
    p = (project or "").strip() or "—"
    m = (model or "").strip() or "—"
    path_s = (path or "").strip() or "—"
    return [
        f"Project: {p}",
        f"Model: {m}",
        f"Path: {path_s}",
    ]


def topbar_bubble_text(project: str, model: str, path: str) -> str:
    """Multi-line text for topbar info bubble."""
    return "\n".join(topbar_bubble_lines(project, model, path))


def should_scroll_to_bottom(stick_to_bottom: bool, force: bool = False) -> bool:
    """Whether the transcript should jump/stick to the latest line."""
    return bool(force or stick_to_bottom)


def turn_progress_label(
    *,
    running: bool,
    tool: str = "",
    queue: int = 0,
    model: str = "",
    quiet: bool = False,
    tool_open: bool = False,
    elapsed_s: int | None = None,
    plan_pending: int = 0,
    plan_running: int = 0,
    plan_failed: int = 0,
    tool_pending: int = 0,
    tool_running: int = 0,
    tool_failed: int = 0,
) -> str:
    """Human turn-strip text (idle + residual / running · elapsed · tool · model).

    ``tool`` may be a short tool name or a full activity one-liner (e.g.
    ``read_file · path/to/file`` or ``subagent · thinking · …``).

    When tools/plan are still open mid-turn, prefer ``running`` over ``quiet``
    even if silence crossed the visual-quiet threshold (honest mid-tool wait).
    """
    m = (model or "").strip()
    if not running:
        return idle_turn_label(
            model=m,
            plan_pending=plan_pending,
            plan_running=plan_running,
            plan_failed=plan_failed,
            tool_pending=tool_pending,
            tool_running=tool_running,
            tool_failed=tool_failed,
        )

    # Open tools/plan: stay on "running" (not bare quiet hang).
    parts = ["quiet"] if quiet and not tool_open else ["running"]
    if elapsed_s is not None and elapsed_s >= 0:
        parts.append(f"{int(elapsed_s)}s")
    q = int(queue or 0)
    if q > 0:
        parts.append(f"queue {q}")
    t = (tool or "").strip()
    if t:
        parts.append(t)
    if m:
        parts.append(m)
    return " · ".join(parts)


def session_list_progress_hint(*, is_live_turn: bool, tool: str = "") -> str:
    """Short meta cue for a session list row while its turn is live."""
    if not is_live_turn:
        return ""
    t = (tool or "").strip()
    return t if t else "running"


def residual_status_parts(
    *,
    plan_pending: int = 0,
    plan_running: int = 0,
    plan_failed: int = 0,
    tool_pending: int = 0,
    tool_running: int = 0,
    tool_failed: int = 0,
) -> list[str]:
    """Fragments describing leftover plan/tool work after a turn goes idle."""
    parts: list[str] = []
    pp = max(0, int(plan_pending or 0))
    pr = max(0, int(plan_running or 0))
    pf = max(0, int(plan_failed or 0))
    tp = max(0, int(tool_pending or 0))
    tr = max(0, int(tool_running or 0))
    tf = max(0, int(tool_failed or 0))
    plan_open = pp + pr
    tool_open = tp + tr
    if plan_open:
        parts.append(f"plan {plan_open} open")
    if pf:
        parts.append(f"plan {pf} failed")
    if tool_open:
        parts.append(f"tool {tool_open} open")
    if tf:
        parts.append(f"tool {tf} failed")
    return parts


def idle_turn_label(
    *,
    model: str = "",
    plan_pending: int = 0,
    plan_running: int = 0,
    plan_failed: int = 0,
    tool_pending: int = 0,
    tool_running: int = 0,
    tool_failed: int = 0,
) -> str:
    """Turn-strip text when not running, including residual plan/tool state."""
    parts = ["idle"]
    residual = residual_status_parts(
        plan_pending=plan_pending,
        plan_running=plan_running,
        plan_failed=plan_failed,
        tool_pending=tool_pending,
        tool_running=tool_running,
        tool_failed=tool_failed,
    )
    parts.extend(residual)
    m = (model or "").strip()
    if m and not residual:
        parts.append(m)
    elif m and residual:
        # Keep residual primary; model is optional noise when residual present
        pass
    return " · ".join(parts)


def should_mark_plan_stale(*, turn_running: bool, has_open_or_failed: bool) -> bool:
    """True when pending/open plan rows should show as stale (turn ended)."""
    return (not turn_running) and bool(has_open_or_failed)


def wall_ms_from_age_seconds(now_ms: float, age_seconds: float | None) -> float | None:
    """Client wall-clock epoch ms for event that is age_seconds old. None if age invalid."""
    if age_seconds is None:
        return None
    try:
        age = float(age_seconds)
    except (TypeError, ValueError):
        return None
    if age != age or age < 0 or age == float("inf"):  # NaN / negative / inf
        return None
    return float(now_ms) - age * 1000.0


def elapsed_seconds_from_wall(now_ms: float, started_wall_ms: float | None) -> int:
    """Non-negative whole seconds since started_wall_ms; 0 if missing."""
    if started_wall_ms is None:
        return 0
    try:
        start = float(started_wall_ms)
    except (TypeError, ValueError):
        return 0
    if start != start:  # NaN
        return 0
    s = int((float(now_ms) - start) // 1000)
    return 0 if s < 0 else s


def pick_turn_age_seconds(
    *,
    selected_session_id: str | None,
    live_turns: list[dict],
    primary_age: float | None,
    primary_session_id: str | None,
) -> float | None:
    """Prefer live_turns entry matching selected_session_id; else primary_age if selected is primary or only one turn."""
    turns = list(live_turns or [])

    def _finite_age(raw: object) -> float | None:
        if raw is None:
            return None
        try:
            a = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if a != a or a < 0 or a == float("inf"):
            return None
        return a

    if selected_session_id:
        for t in turns:
            if not isinstance(t, dict):
                continue
            if t.get("sessionId") != selected_session_id:
                continue
            matched = _finite_age(t.get("ageSeconds"))
            if matched is not None:
                return matched
            break

    pa = _finite_age(primary_age)
    if pa is not None:
        if (
            not selected_session_id
            or selected_session_id == primary_session_id
            or len(turns) <= 1
        ):
            return pa

    if len(turns) == 1 and isinstance(turns[0], dict):
        only = _finite_age(turns[0].get("ageSeconds"))
        if only is not None:
            return only
    return None


# ---------------------------------------------------------------------------
# Goal mode helpers (CLI /goal + update_goal tool; mirrored in static/app.js)
# ---------------------------------------------------------------------------


def parse_goal_slash(text: str | None) -> dict | None:
    """Parse a user `/goal …` slash command.

    Returns ``None`` if the text is not a ``/goal`` command.
    Otherwise ``{"action": "start"|"status"|"pause"|"resume"|"clear", "objective"?: str}``.
    Bare ``/goal`` is treated as ``status`` (no lifecycle change).
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    # Whole-message /goal (optional rest). Case-insensitive command name.
    lower = s.lower()
    if lower == "/goal":
        return {"action": "status"}
    if not lower.startswith("/goal"):
        return None
    # Require space or end after /goal so "/goalie" is not a match
    if len(s) > 5 and s[5] not in " \t\n\r":
        return None
    rest = s[5:].strip()
    if not rest:
        return {"action": "status"}
    # Whole-rest match for lifecycle keywords (multi-word = start objective).
    rest_lower = rest.lower()
    if rest_lower == "status":
        return {"action": "status"}
    if rest_lower == "pause":
        return {"action": "pause"}
    if rest_lower == "resume":
        return {"action": "resume"}
    if rest_lower == "clear":
        return {"action": "clear"}
    return {"action": "start", "objective": rest}


def format_goal_elapsed(seconds: float | int | None) -> str:
    """Compact wall-clock elapsed for goal banner: ``45s``, ``3m 12s``, ``1h 05m``."""
    try:
        s = int(seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        s = 0
    if s < 0:
        s = 0
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m {sec:02d}s"
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h}h {m:02d}m"


def goal_banner_text(
    *,
    status: str = "active",
    elapsed_s: float | int | None = 0,
    message: str = "",
    objective: str = "",
) -> str:
    """Slim goal banner label, e.g. ``Goal · 14m 32s · Reading PR0 brief…``."""
    elapsed = format_goal_elapsed(elapsed_s)
    detail = (message or objective or "").strip()
    if len(detail) > 72:
        detail = detail[:71] + "…"
    st = str(status or "").strip().lower()
    parts: list[str] = ["Goal"]
    if st == "paused":
        parts.append("paused")
    parts.append(elapsed)
    if detail:
        parts.append(detail)
    return " · ".join(parts)


def apply_goal_tool_input(
    record: dict | None,
    raw_input: object,
    title: str | None,
    now_ms: float,
) -> dict | None:
    """Fold an ``update_goal`` / ``Goal: …`` tool event into a goal record.

    Returns a new record dict, or ``None`` when the event is not goal-related.
    Record shape: ``{status, objective, message, startedAt, updatedAt}``.
    """
    title_s = str(title or "").strip()
    raw: dict = raw_input if isinstance(raw_input, dict) else {}
    variant = raw.get("variant")
    is_goal = (
        title_s.lower() == "update_goal"
        or title_s.lower().startswith("goal:")
        or str(variant or "") == "UpdateGoal"
    )
    if not is_goal:
        return None

    msg = ""
    if raw.get("message") is not None:
        msg = str(raw.get("message") or "").strip()
    if not msg and title_s.lower().startswith("goal:"):
        msg = title_s[5:].strip()

    completed = raw.get("completed")
    blocked = raw.get("blocked_reason")

    rec = dict(record) if isinstance(record, dict) else {}
    prev_status = str(rec.get("status") or "").strip().lower()
    prev_started = rec.get("startedAt")
    try:
        prev_started_f = float(prev_started) if prev_started is not None else None
    except (TypeError, ValueError):
        prev_started_f = None

    def _base(
        *,
        status: str,
        started: float,
        message: str,
        objective: str,
    ) -> dict:
        return {
            "status": status,
            "objective": objective,
            "message": message,
            "startedAt": started,
            "updatedAt": float(now_ms),
        }

    objective = str(rec.get("objective") or "").strip()
    if completed is True:
        return _base(
            status="done",
            started=prev_started_f if prev_started_f is not None else float(now_ms),
            message=msg or str(rec.get("message") or ""),
            objective=objective,
        )

    # First progress when none/done → new cycle
    if prev_status not in ("active", "paused"):
        seed_msg = msg or objective
        return _base(
            status="active",
            started=float(now_ms),
            message=seed_msg,
            objective=objective or msg,
        )

    # Progress while running: keep startedAt; blocked_reason updates message only
    if blocked:
        blocked_s = str(blocked).strip()
        new_msg = msg or blocked_s or str(rec.get("message") or "")
    else:
        new_msg = msg or str(rec.get("message") or "")

    return _base(
        status="active",
        started=prev_started_f if prev_started_f is not None else float(now_ms),
        message=new_msg,
        objective=objective,
    )
