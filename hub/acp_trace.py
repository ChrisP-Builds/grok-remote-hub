"""Structured ACP event ring buffer + optional JSONL writer.

Never raises from emit/snapshot/clear. Redacts long strings; no full payloads.
"""

from __future__ import annotations

import json
import logging
import math
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("hub.acp.trace")

_MAX_FIELD_LEN = 200
_MAX_LIST_ITEMS = 20
_MAX_DICT_KEYS = 20

# Secret-ish field names: drop values rather than log them.
_REDACT_KEYS = frozenset(
    {
        "secret",
        "token",
        "password",
        "authorization",
        "server-key",
        "server_key",
        "hub_token",
        "prompt",
        "text",
        "content",
        "delta",
    }
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sanitize_value(value: Any, *, depth: int = 0) -> Any:
    try:
        return _sanitize_value_impl(value, depth=depth)
    except Exception:
        return "<unprintable>"


def _sanitize_value_impl(value: Any, *, depth: int = 0) -> Any:
    if depth > 3:
        return "…"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    if isinstance(value, str):
        if len(value) > _MAX_FIELD_LEN:
            return value[:_MAX_FIELD_LEN] + "…"
        return value
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_value(x, depth=depth + 1) for x in list(value)[:_MAX_LIST_ITEMS]
        ]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _MAX_DICT_KEYS:
                break
            key = str(k)[:64]
            if key.lower() in _REDACT_KEYS or any(
                r in key.lower() for r in ("secret", "token", "password", "authorization")
            ):
                out[key] = "[redacted]"
            else:
                out[key] = _sanitize_value(v, depth=depth + 1)
        return out
    try:
        s = str(value)
    except Exception:
        return "<unprintable>"
    if len(s) > _MAX_FIELD_LEN:
        return s[:_MAX_FIELD_LEN] + "…"
    return s


def session_id_slice(session_id: str | None, n: int = 12) -> str | None:
    """Short session id for logs (never full secret material)."""
    if not session_id:
        return None
    s = str(session_id)
    if len(s) <= n:
        return s
    return s[:n]


class AcpTrace:
    """In-memory ring of ACP lifecycle events; optional daily JSONL under log_dir."""

    def __init__(self, log_dir: Path | None = None, capacity: int = 300):
        cap = max(1, int(capacity))
        self._events: deque[dict[str, Any]] = deque(maxlen=cap)
        self._log_dir = Path(log_dir) if log_dir is not None else None
        self.capacity = cap

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        """Append event with utc iso ts. Never raises."""
        try:
            return self._emit_impl(str(event or "unknown"), **fields)
        except Exception:
            log.debug("acp_trace emit failed", exc_info=True)
            return {"ts": _utc_iso(), "event": str(event or "unknown")}

    def _emit_impl(self, event: str, **fields: Any) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "ts": _utc_iso(),
            "event": event[:_MAX_FIELD_LEN],
        }
        for k, v in fields.items():
            key = str(k)[:64]
            if key.lower() in _REDACT_KEYS or any(
                r in key.lower() for r in ("secret", "token", "password", "authorization")
            ):
                rec[key] = "[redacted]"
            else:
                rec[key] = _sanitize_value(v)
        self._events.append(rec)
        if self._log_dir is not None:
            self._write_jsonl(rec)
        parts = [f"{k}={rec[k]}" for k in rec if k not in ("ts", "event")]
        summary = " ".join(parts)
        if len(summary) > 280:
            summary = summary[:280] + "…"
        log.info("%s %s", rec["event"], summary)
        return rec

    def _write_jsonl(self, rec: dict[str, Any]) -> None:
        try:
            assert self._log_dir is not None
            self._log_dir.mkdir(parents=True, exist_ok=True)
            day = datetime.now(timezone.utc).strftime("%Y%m%d")
            path = self._log_dir / f"acp-trace-{day}.jsonl"
            line = json.dumps(rec, default=str, ensure_ascii=False)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            log.debug("acp_trace jsonl write failed", exc_info=True)

    def snapshot(self, n: int = 100) -> list[dict[str, Any]]:
        """Most recent n events (oldest→newest)."""
        try:
            n = max(0, int(n))
            items = list(self._events)
            if n == 0:
                return []
            if len(items) > n:
                return items[-n:]
            return items
        except Exception:
            log.debug("acp_trace snapshot failed", exc_info=True)
            return []

    def clear(self) -> None:
        try:
            self._events.clear()
        except Exception:
            log.debug("acp_trace clear failed", exc_info=True)
