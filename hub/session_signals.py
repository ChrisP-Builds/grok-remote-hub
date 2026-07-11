"""Read normalized context/usage metrics from session signals.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hub.session_index import find_session


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clamp_percent(value: float | None) -> float | None:
    if value is None:
        return None
    if value < 0:
        return 0.0
    if value > 100:
        return 100.0
    return value


def _monthly_percent(raw: dict[str, Any]) -> float | None:
    for key in ("monthlyUsagePercent", "monthly_usage_percent", "usageMonthlyPercent"):
        pct = _as_float(raw.get(key))
        if pct is not None:
            return _clamp_percent(pct)
    usage = raw.get("usage")
    if isinstance(usage, dict):
        pct = _as_float(usage.get("monthlyPercent") or usage.get("monthly_percent"))
        if pct is not None:
            return _clamp_percent(pct)
    return None


def _is_monthly(raw: dict[str, Any], monthly_percent: float | None) -> bool:
    if raw.get("isMonthly") is True:
        return True
    period = str(raw.get("usagePeriod") or raw.get("usage_period") or "").strip().lower()
    if period == "monthly":
        return True
    return monthly_percent is not None


def normalize_signals(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize raw signals.json (or empty) into the API shape."""
    data = raw if isinstance(raw, dict) else {}

    used = _as_int(data.get("contextTokensUsed"))
    window = _as_int(data.get("contextWindowTokens"))

    context_percent = _as_float(data.get("contextWindowUsage"))
    if context_percent is None and used is not None and window and window > 0:
        context_percent = (used / window) * 100.0
    context_percent = _clamp_percent(context_percent)

    monthly_percent = _monthly_percent(data)
    is_monthly = _is_monthly(data, monthly_percent)

    return {
        "contextPercent": context_percent,
        "contextTokensUsed": used,
        "contextWindowTokens": window,
        "monthlyPercent": monthly_percent,
        "isMonthly": is_monthly,
    }


def read_signals_file(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def find_signals_path(sessions_root: Path, session_id: str) -> Path | None:
    """Locate signals.json for a session id via index or rglob fallback."""
    sid = (session_id or "").strip()
    if not sid:
        return None

    info = find_session(sessions_root, sid)
    if info and info.path:
        candidate = Path(info.path) / "signals.json"
        if candidate.is_file():
            return candidate

    root = Path(sessions_root)
    if not root.is_dir():
        return None
    try:
        for path in root.rglob("signals.json"):
            if path.parent.name == sid and path.is_file():
                return path
    except OSError:
        return None
    return None


def read_session_signals(sessions_root: Path, session_id: str) -> dict[str, Any]:
    """Find session signals.json and return normalized usage metrics.

    Returns:
        {
          "contextPercent": float|None,  # 0-100
          "contextTokensUsed": int|None,
          "contextWindowTokens": int|None,
          "monthlyPercent": float|None,
          "isMonthly": bool,
        }
    Prefer contextWindowUsage for contextPercent; else compute used/window*100.
    """
    path = find_signals_path(sessions_root, session_id)
    if path is None:
        return normalize_signals(None)
    raw = read_signals_file(path)
    return normalize_signals(raw)
