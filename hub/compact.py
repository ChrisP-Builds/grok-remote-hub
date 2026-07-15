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

    return {
        "state": state,
        "tokensBefore": tokens_before,
        "tokensAfter": tokens_after,
        "summaryPreview": summary,
        "error": error,
    }


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
    """Pull params.update from a session_notification ACP message, if any."""
    if not isinstance(msg, dict):
        return None
    method = str(msg.get("method") or "")
    if method not in (
        "_x.ai/session_notification",
        "x.ai/session_notification",
    ):
        return None
    params = msg.get("params")
    if not isinstance(params, dict):
        return None
    update = params.get("update")
    if isinstance(update, dict):
        return update
    # Some shapes nest sessionUpdate at params root
    if params.get("sessionUpdate") or params.get("session_update"):
        return params
    return None
