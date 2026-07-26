"""Process-local counters + small ops event ring for observability.

Never raises from inc/emit/snapshot. Redacts secret-like fields (same style as acp_trace).
Optional daily JSONL under log_dir when set.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hub.acp_trace import _REDACT_KEYS, _sanitize_value

log = logging.getLogger("hub.ops")

_MAX_FIELD_LEN = 200

DEFAULT_COUNTERS: tuple[str, ...] = (
    "ws_connect",
    "ws_disconnect",
    "ws_client_reset",
    "ensure_reuse",
    "ensure_load",
    "ensure_new",
    "ensure_resume_failed",
    "prompt_ok",
    "prompt_error",
    "prompt_no_output",
    "turn_force_clear",
    "acp_reconnect",
    "acp_heal_attempt",
    "acp_heal_ok",
    "acp_heal_fail",
    "agent_restart",
    "bg_activity_working",
    "bg_activity_stuck",
    "bg_activity_cleared",
    "session_switch",
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class OpsMetrics:
    """Process-local counters + small ops event ring for observability."""

    def __init__(
        self,
        log_dir: Path | None = None,
        capacity: int = 200,
    ):
        cap = max(1, int(capacity))
        self._counters: dict[str, int] = {name: 0 for name in DEFAULT_COUNTERS}
        self._events: deque[dict[str, Any]] = deque(maxlen=cap)
        self._log_dir = Path(log_dir) if log_dir is not None else None
        self.capacity = cap

    def inc(self, name: str, n: int = 1) -> None:
        """Increment counter by n. Never raises. Unknown names are created."""
        try:
            key = str(name or "unknown")[:64]
            add = int(n)
            self._counters[key] = int(self._counters.get(key, 0)) + add
        except Exception:
            log.debug("ops_metrics inc failed", exc_info=True)

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        """Append ops event with utc iso ts. Never raises."""
        try:
            return self._emit_impl(str(event or "unknown"), **fields)
        except Exception:
            log.debug("ops_metrics emit failed", exc_info=True)
            return {"ts": _utc_iso(), "event": str(event or "unknown")}

    def _emit_impl(self, event: str, **fields: Any) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "ts": _utc_iso(),
            "event": event[:_MAX_FIELD_LEN],
        }
        for k, v in fields.items():
            key = str(k)[:64]
            if key.lower() in _REDACT_KEYS or any(
                r in key.lower()
                for r in ("secret", "token", "password", "authorization")
            ):
                rec[key] = "[redacted]"
            else:
                rec[key] = _sanitize_value(v)
        self._events.append(rec)
        if self._log_dir is not None:
            self._write_jsonl(rec)
        return rec

    def _write_jsonl(self, rec: dict[str, Any]) -> None:
        try:
            assert self._log_dir is not None
            self._log_dir.mkdir(parents=True, exist_ok=True)
            day = datetime.now(timezone.utc).strftime("%Y%m%d")
            path = self._log_dir / f"ops-{day}.jsonl"
            line = json.dumps(rec, default=str, ensure_ascii=False)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            log.debug("ops_metrics jsonl write failed", exc_info=True)

    def snapshot_counters(self) -> dict[str, int]:
        """Copy of all counters (defaults always present as 0+)."""
        try:
            return {k: int(v) for k, v in self._counters.items()}
        except Exception:
            log.debug("ops_metrics snapshot_counters failed", exc_info=True)
            return {name: 0 for name in DEFAULT_COUNTERS}

    def snapshot_events(self, n: int = 50) -> list[dict[str, Any]]:
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
            log.debug("ops_metrics snapshot_events failed", exc_info=True)
            return []

    def snapshot(self, n: int = 50) -> dict[str, Any]:
        """Compact {counters, events} for diagnostics."""
        try:
            return {
                "counters": self.snapshot_counters(),
                "events": self.snapshot_events(n),
            }
        except Exception:
            log.debug("ops_metrics snapshot failed", exc_info=True)
            return {
                "counters": {name: 0 for name in DEFAULT_COUNTERS},
                "events": [],
            }

    def clear(self) -> None:
        """Reset counters to 0 and empty event ring. Never raises."""
        try:
            for name in DEFAULT_COUNTERS:
                self._counters[name] = 0
            # Drop any ad-hoc keys
            for key in list(self._counters.keys()):
                if key not in DEFAULT_COUNTERS:
                    self._counters.pop(key, None)
            self._events.clear()
        except Exception:
            log.debug("ops_metrics clear failed", exc_info=True)
