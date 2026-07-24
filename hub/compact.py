"""Pure helpers for /compact slash handling and ACP compact notifications."""

from __future__ import annotations

import math
import re
from typing import Any

_COMPACT_SLASH_RE = re.compile(r"^/compact(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)

_COMPACT_STATES = {
    "auto_compact_started": "started",
    "auto_compact_completed": "completed",
    "auto_compact_failed": "failed",
    "auto_compact_cancelled": "cancelled",
}

# Hub-owned /compact gate: suppress agent auto_compact_* type:compact while
# execute owns started+terminal (in-flight), then brief grace for late notifs.
HUB_COMPACT_GATE_INFLIGHT_S = 3600.0
HUB_COMPACT_GATE_GRACE_S = 15.0

_COMPACT_NOTIFICATION_BLOCKED_STATES = frozenset(
    {"started", "completed", "failed", "cancelled"}
)


def hub_compact_gate_set_inflight(
    gate: dict[str, float], session_id: str, *, now: float
) -> None:
    """Mark hub-owned /compact in flight (suppress notification terminal paint)."""
    gate[session_id] = now + HUB_COMPACT_GATE_INFLIGHT_S


def hub_compact_gate_set_grace(
    gate: dict[str, float], session_id: str, *, now: float
) -> None:
    """After execute ends: brief window so late auto_compact_* cannot re-broadcast."""
    gate[session_id] = now + HUB_COMPACT_GATE_GRACE_S


def hub_compact_gate_suppresses_notification(
    gate: dict[str, float],
    session_id: str,
    body_state: str,
    *,
    now: float,
) -> bool:
    """True if notification path must NOT broadcast type:compact for this state.

    Expire/pop gate entry when now >= deadline. Only suppress blocked states
    (started/completed/failed/cancelled). Caller still may refresh usage on
    completed when suppressed.
    """
    deadline = gate.get(session_id)
    if deadline is None:
        return False
    if now >= deadline:
        gate.pop(session_id, None)
        return False
    return body_state in _COMPACT_NOTIFICATION_BLOCKED_STATES


def parse_compact_slash(text: str) -> dict[str, str] | None:
    """Return ``{"context": str}`` if text is a /compact command, else None.

    Context is the optional trailing argument (stripped); empty when bare /compact.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    m = _COMPACT_SLASH_RE.match(raw)
    if not m:
        return None
    ctx = (m.group(1) or "").strip()
    return {"context": ctx}


# Reject absurd compact token counts (UI painted ~375k garbage / scroll thrash).
COMPACT_TOKEN_ABSURD_MAX = 5_000_000

# Compact after vs session signals: if signals still show much more than compact_after,
# the toast must not imply the CTX window is now compact_after.
COMPACT_VS_SIGNALS_SHRINK_SLACK = 50_000

# Without a known window, treat this many used tokens as "still full".
_FULL_ABS_FALLBACK = 50_000


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s if s else None
    return str(value)


def sanitize_compact_tokens(
    before: Any, after: Any
) -> tuple[int | None, int | None]:
    """Reject non-finite / absurd token counts (e.g. > 5_000_000 or negative)."""
    return _sanitize_one_token(before), _sanitize_one_token(after)


def _sanitize_one_token(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n < 0 or n > COMPACT_TOKEN_ABSURD_MAX:
        return None
    return n


def format_token_short(n: int | None) -> str:
    """Format token count roughly like UI formatTokenCompact (e.g. 421K)."""
    if n is None:
        return "—"
    try:
        n_int = int(n)
    except (TypeError, ValueError):
        return "—"
    if n_int < 0:
        return "—"
    if n_int < 1000:
        return str(n_int)
    if n_int < 1_000_000:
        k = n_int / 1000.0
        if k >= 100:
            return f"{round(k)}K"
        val = round(k * 10) / 10
        if val == int(val):
            return f"{int(val)}K"
        return f"{val}K"
    m = n_int / 1_000_000.0
    if m >= 10:
        return f"{round(m)}M"
    val = round(m * 10) / 10
    if val == int(val):
        return f"{int(val)}M"
    return f"{val}M"


def compact_claims_reduction(before: int | None, after: int | None) -> bool:
    """True only when both set and after < before (real shrink)."""
    if before is None or after is None:
        return False
    return after < before


def compact_toast_should_claim_window_shrink(
    *,
    compact_before: int | None,
    compact_after: int | None,
    signals_used: int | None,
) -> bool:
    """True only when compact reduced AND signals (if known) agree roughly.

    If signals_used is None, True when compact claims reduction.
    If signals_used is set and much larger than compact_after
    (signals_used > compact_after + 50_000), return False — toast must not
    claim the window shrunk to after (CTX bar uses session signals).
    """
    if not compact_claims_reduction(compact_before, compact_after):
        return False
    if signals_used is None:
        return True
    if compact_after is None:
        return False
    if signals_used > compact_after + COMPACT_VS_SIGNALS_SHRINK_SLACK:
        return False
    return True


def should_emit_compact_completed_feedback(
    before: int | None, after: int | None
) -> str:
    """Return feedback kind for completed compact: reduced | no_change | unknown."""
    if before is not None and after is not None:
        if after < before:
            return "reduced"
        return "no_change"
    return "unknown"


def _usage_is_still_full(
    used: int,
    window: int | None,
    *,
    full_threshold_pct: float,
) -> bool:
    """True when session usage is still high enough to not call 'low'."""
    if window is not None and window > 0:
        return (used / window) * 100.0 >= full_threshold_pct
    return used >= _FULL_ABS_FALLBACK


def _msg_reduced(
    before: int, after: int, window: int | None
) -> str:
    s = f"Context compacted: {format_token_short(before)} → {format_token_short(after)}"
    if window is not None and window > 0:
        pb = (before / window) * 100.0
        pa = (after / window) * 100.0
        s += f" ({pb:.0f}% → {pa:.0f}%)"
    return s


def _msg_still_full(after: int, window: int | None) -> str:
    if window is not None and window > 0:
        pct = (after / window) * 100.0
        return (
            "Compact did not free context — session still "
            f"{format_token_short(after)} / {format_token_short(window)} ({pct:.0f}%)."
        )
    return (
        "Compact did not free context — session still "
        f"~{format_token_short(after)} tokens."
    )


def _msg_low(after: int, window: int | None) -> str:
    if window is not None and window > 0:
        return (
            "Compact finished; session usage already low "
            f"(~{format_token_short(after)} / {format_token_short(window)})."
        )
    return (
        "Compact finished; session usage already low "
        f"(~{format_token_short(after)} tokens)."
    )


def resolve_compact_outcome(
    *,
    signals_before_used: int | None,
    signals_after_used: int | None,
    signals_window: int | None = None,
    agent_before: int | None = None,
    agent_after: int | None = None,
    full_threshold_pct: float = 25.0,
) -> dict[str, Any]:
    """Ground compact feedback in session signals (CTX bar source of truth).

    Returns:
      {
        "reduced": bool,  # True when signals (or agent+signals agree) show shrink
        "feedback": "reduced" | "no_change_still_full" | "no_change_low" | "unknown",
        "tokensBefore": int|None,  # what UI should display
        "tokensAfter": int|None,
        "windowTokens": int|None,
        "message": str,  # human one-liner; never "already minimal" when still full
      }
    """
    before = _sanitize_one_token(signals_before_used)
    after = _sanitize_one_token(signals_after_used)
    window = _sanitize_one_token(signals_window)
    agent_b = _sanitize_one_token(agent_before)
    agent_a = _sanitize_one_token(agent_after)

    unknown_msg = "Compact finished. Check context bar for current usage."

    # Both signals known: SoT for reduced / still full / low.
    if before is not None and after is not None:
        if after < before:
            return {
                "reduced": True,
                "feedback": "reduced",
                "tokensBefore": before,
                "tokensAfter": after,
                "windowTokens": window,
                "message": _msg_reduced(before, after, window),
            }
        if _usage_is_still_full(
            after, window, full_threshold_pct=full_threshold_pct
        ):
            return {
                "reduced": False,
                "feedback": "no_change_still_full",
                "tokensBefore": before,
                "tokensAfter": after,
                "windowTokens": window,
                "message": _msg_still_full(after, window),
            }
        return {
            "reduced": False,
            "feedback": "no_change_low",
            "tokensBefore": before,
            "tokensAfter": after,
            "windowTokens": window,
            "message": _msg_low(after, window),
        }

    # After only (typical notification path): use agent claim only if signals agree.
    if after is not None:
        if compact_toast_should_claim_window_shrink(
            compact_before=agent_b,
            compact_after=agent_a,
            signals_used=after,
        ):
            # Agent reduced and signals roughly match agent after.
            display_before = agent_b if agent_b is not None else after
            return {
                "reduced": True,
                "feedback": "reduced",
                "tokensBefore": display_before,
                "tokensAfter": after,
                "windowTokens": window,
                "message": _msg_reduced(display_before, after, window),
            }
        if _usage_is_still_full(
            after, window, full_threshold_pct=full_threshold_pct
        ):
            return {
                "reduced": False,
                "feedback": "no_change_still_full",
                "tokensBefore": before,
                "tokensAfter": after,
                "windowTokens": window,
                "message": _msg_still_full(after, window),
            }
        return {
            "reduced": False,
            "feedback": "no_change_low",
            "tokensBefore": before,
            "tokensAfter": after,
            "windowTokens": window,
            "message": _msg_low(after, window),
        }

    # No usable signals after — never invent shrink from agent alone for the bar.
    return {
        "reduced": False,
        "feedback": "unknown",
        "tokensBefore": before,
        "tokensAfter": after,
        "windowTokens": window,
        "message": unknown_msg,
    }



def compact_user_outcome_state(
    *,
    reduced: bool,
    feedback: str,
    signals_after_used: int | None,
    signals_window: int | None = None,
    full_threshold_pct: float = 25.0,
) -> str:
    """Return hub compact event state for UI: completed | failed.

    - reduced True → completed
    - feedback no_change_low / unknown with low or missing → completed
    - no_change_still_full (high usage, no shrink) → failed
      (agent ran but did not free context — not CLI-success-equivalent)
    """
    if reduced:
        return "completed"
    fb = (feedback or "").strip()
    if fb == "no_change_still_full":
        return "failed"
    if fb == "no_change_low":
        return "completed"
    if fb == "unknown":
        # missing usage → completed (cannot prove still full)
        if signals_after_used is None:
            return "completed"
        used = int(signals_after_used)
        if _usage_is_still_full(
            used, signals_window, full_threshold_pct=full_threshold_pct
        ):
            return "failed"
        return "completed"
    # Other no-shrink feedback: fail only when usage is clearly still full.
    if signals_after_used is not None and _usage_is_still_full(
        int(signals_after_used),
        signals_window,
        full_threshold_pct=full_threshold_pct,
    ):
        return "failed"
    return "completed"



def normalize_compact_notification(
    update: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Map an ACP auto_compact_* update into a hub compact event body.

    Returns None when update is not a compact lifecycle notification.
    """
    if not isinstance(update, dict):
        return None
    kind = str(update.get("sessionUpdate") or update.get("session_update") or "")
    state = _COMPACT_STATES.get(kind)
    if state is None:
        return None

    tokens_before = _as_int(
        update.get("tokensBefore")
        if "tokensBefore" in update
        else update.get("tokens_before")
    )
    tokens_after = _as_int(
        update.get("tokensAfter")
        if "tokensAfter" in update
        else update.get("tokens_after")
    )
    tokens_before, tokens_after = sanitize_compact_tokens(tokens_before, tokens_after)
    summary = _as_str(
        update.get("summaryPreview")
        if "summaryPreview" in update
        else update.get("summary_preview")
    )
    error = _as_str(update.get("error") or update.get("message") or update.get("reason"))

    body: dict[str, Any] = {
        "state": state,
        "tokensBefore": tokens_before,
        "tokensAfter": tokens_after,
        "summaryPreview": summary,
        "error": error,
    }
    # Mark no-op completed clearly so UI never invents success from equal/null tokens.
    if state == "completed":
        body["reduced"] = compact_claims_reduction(tokens_before, tokens_after)
        body["feedback"] = should_emit_compact_completed_feedback(
            tokens_before, tokens_after
        )
    return body


def usage_from_compact_tokens(
    tokens_after: int | None,
    window_tokens: int | None = None,
) -> dict[str, Any]:
    """Build usage patch fields from compact tokens_after (+ optional window)."""
    out: dict[str, Any] = {}
    used = _as_int(tokens_after)
    window = _as_int(window_tokens)
    if used is not None:
        out["contextTokensUsed"] = used
    if window is not None and window > 0:
        out["contextWindowTokens"] = window
    if used is not None and window is not None and window > 0:
        pct = (used / window) * 100.0
        if pct < 0:
            pct = 0.0
        elif pct > 100:
            pct = 100.0
        out["contextPercent"] = pct
    return out


def extract_compact_update(msg: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pull params.update from auto_compact ACP messages (notification or update)."""
    if not isinstance(msg, dict):
        return None
    method = str(msg.get("method") or "")
    if method not in (
        "_x.ai/session_notification",
        "x.ai/session_notification",
        "session/update",
        "_x.ai/session/update",
    ):
        return None
    params = msg.get("params")
    if not isinstance(params, dict):
        return None
    update = params.get("update")
    if isinstance(update, dict):
        kind = str(update.get("sessionUpdate") or update.get("session_update") or "")
        if kind.startswith("auto_compact_") or method.endswith("session_notification"):
            return update
        return None
    # Some shapes nest sessionUpdate at params root
    kind_p = str(params.get("sessionUpdate") or params.get("session_update") or "")
    if kind_p.startswith("auto_compact_"):
        return params
    return None
