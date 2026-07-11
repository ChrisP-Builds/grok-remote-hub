from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("hub.tailer")

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]


def extract_event_id(msg: dict[str, Any]) -> str | None:
    """Extract _meta.eventId from ACP message or nested params/update."""
    if not isinstance(msg, dict):
        return None
    meta = msg.get("_meta")
    if isinstance(meta, dict) and meta.get("eventId") is not None:
        return str(meta["eventId"])
    params = msg.get("params")
    if isinstance(params, dict):
        meta = params.get("_meta")
        if isinstance(meta, dict) and meta.get("eventId") is not None:
            return str(meta["eventId"])
        update = params.get("update")
        if isinstance(update, dict):
            meta = update.get("_meta")
            if isinstance(meta, dict) and meta.get("eventId") is not None:
                return str(meta["eventId"])
    return None


def stable_event_key(msg: dict[str, Any]) -> str:
    """Stable dedupe key: prefer eventId, else hash of stable fields (no timestamp)."""
    eid = extract_event_id(msg)
    if eid:
        return f"id:{eid}"
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    update = params.get("update") if isinstance(params.get("update"), dict) else {}
    payload = {
        "method": msg.get("method"),
        "sessionId": params.get("sessionId") or params.get("session_id"),
        "sessionUpdate": update.get("sessionUpdate"),
        "toolCallId": update.get("toolCallId") or update.get("tool_call_id"),
        "status": update.get("status"),
        "content": update.get("content"),
        "title": update.get("title"),
        "rawInput": update.get("rawInput"),
        "entries": update.get("entries"),
        "text": update.get("text"),
        "availableCommands": update.get("availableCommands"),
    }
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"h:{digest}"


def parse_updates_line(line: str) -> dict[str, Any] | None:
    """Parse one updates.jsonl line into a message dict, or None if incomplete/invalid."""
    text = line.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


class EventDedupe:
    """Per-session sliding window of seen event keys (deque + set)."""

    def __init__(self, maxlen: int = 2000):
        self.maxlen = maxlen
        self._order: dict[str, deque[str]] = {}
        self._sets: dict[str, set[str]] = {}

    def should_emit(self, session_id: str, msg: dict[str, Any]) -> bool:
        if not session_id:
            return True
        key = stable_event_key(msg)
        order = self._order.get(session_id)
        seen = self._sets.get(session_id)
        if order is None or seen is None:
            order = deque(maxlen=self.maxlen)
            seen = set()
            self._order[session_id] = order
            self._sets[session_id] = seen
        if key in seen:
            return False
        if len(order) == order.maxlen:
            old = order[0]
            order.append(key)
            if old not in order:
                seen.discard(old)
            seen.add(key)
        else:
            order.append(key)
            seen.add(key)
        return True

    def clear_session(self, session_id: str) -> None:
        self._order.pop(session_id, None)
        self._sets.pop(session_id, None)


class _FileWatch:
    __slots__ = ("path", "offset", "partial", "exists")

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.partial = ""
        self.exists = False

    def open_at_end(self) -> None:
        """Seek to EOF so we only stream new lines (no history replay)."""
        self.partial = ""
        try:
            if self.path.is_file():
                self.offset = self.path.stat().st_size
                self.exists = True
            else:
                self.offset = 0
                self.exists = False
        except OSError as exc:
            log.debug("tail open_at_end failed %s: %s", self.path, exc)
            self.offset = 0
            self.exists = False

    def open_at_offset(self, offset: int) -> None:
        """Resume mid-file from a previously stored byte offset (clamped to size)."""
        self.partial = ""
        try:
            if self.path.is_file():
                size = self.path.stat().st_size
                self.offset = max(0, min(int(offset), size))
                self.exists = True
            else:
                self.offset = 0
                self.exists = False
        except OSError as exc:
            log.debug("tail open_at_offset failed %s: %s", self.path, exc)
            self.offset = 0
            self.exists = False

    def read_new_lines(self) -> list[str]:
        """Return complete new lines; keep trailing partial in buffer."""
        try:
            if not self.path.is_file():
                if self.exists:
                    # File removed
                    self.exists = False
                    self.offset = 0
                    self.partial = ""
                return []
            size = self.path.stat().st_size
        except OSError:
            return []

        if not self.exists:
            # Appeared after watch started: resume from stored offset if any, else EOF
            self.exists = True
            if self.offset > size:
                self.offset = size
            # If offset is 0 and file already has content, start at end (history is HTTP/WS)
            # only when we never had a deliberate resume offset stored by SessionTailer.
            # SessionTailer sets exists+offset before first poll; this branch is for new files.
            if self.offset == 0 and size > 0:
                self.offset = size
            self.partial = ""
            return []

        if size < self.offset:
            # Truncated or replaced: seek end to avoid replaying full history
            log.debug("updates.jsonl shrunk for %s; reseek end", self.path)
            self.offset = size
            self.partial = ""
            return []

        if size == self.offset:
            return []

        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(self.offset)
                chunk = f.read()
                self.offset = f.tell()
        except OSError as exc:
            log.debug("tail read failed %s: %s", self.path, exc)
            return []

        if not chunk:
            return []

        data = self.partial + chunk
        parts = data.split("\n")
        self.partial = parts.pop()  # incomplete last segment
        return [p for p in parts if p.strip()]


class SessionTailer:
    """Poll subscribed sessions' updates.jsonl and emit new ACP-shaped lines.

    Offsets persist for process lifetime even after stop_watching, so a later
    ensure_watching resumes mid-file instead of open_at_end (iOS reconnect fix).
    """

    def __init__(
        self,
        sessions_root: Path,
        on_event: EventCallback,
        poll_interval: float = 0.25,
    ):
        self.sessions_root = Path(sessions_root)
        self.on_event = on_event
        self.poll_interval = poll_interval
        self._watched: dict[str, _FileWatch] = {}
        # Persistent byte offsets: survive stop_watching so reconnects catch up.
        self._offsets: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._stop.clear()
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self.run_loop(), name="session-tailer")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            self._watched.clear()
            self._offsets.clear()

    async def ensure_watching(self, session_id: str, session_path: Path | None = None) -> None:
        if not session_id:
            return
        path = self._resolve_updates_path(session_id, session_path)
        async with self._lock:
            existing = self._watched.get(session_id)
            if existing and existing.path == path:
                return

            watch = _FileWatch(path)
            stored = self._offsets.get(session_id)
            if stored is not None:
                watch.open_at_offset(stored)
                try:
                    size = path.stat().st_size if path.is_file() else 0
                except OSError:
                    size = 0
                log.info(
                    "tail resume session %s from offset %s (file size %s)",
                    session_id,
                    watch.offset,
                    size,
                )
            else:
                # First ever watch this process: start at EOF; client gets history separately.
                watch.open_at_end()
                log.info(
                    "tail watching session %s at %s (offset=%s)",
                    session_id,
                    path,
                    watch.offset,
                )
            self._offsets[session_id] = watch.offset
            self._watched[session_id] = watch

    async def stop_watching(self, session_id: str) -> None:
        """Stop polling this session but keep the stored offset for resume."""
        async with self._lock:
            watch = self._watched.pop(session_id, None)
            if watch is not None:
                self._offsets[session_id] = watch.offset
                log.info("tail stopped session %s (kept offset=%s)", session_id, watch.offset)

    def is_watching(self, session_id: str) -> bool:
        return session_id in self._watched

    def get_offset(self, session_id: str) -> int | None:
        """Return stored/current offset for tests and diagnostics."""
        watch = self._watched.get(session_id)
        if watch is not None:
            return watch.offset
        return self._offsets.get(session_id)

    def _resolve_updates_path(self, session_id: str, session_path: Path | None) -> Path:
        if session_path is not None:
            p = Path(session_path)
            if p.is_file() and p.name == "updates.jsonl":
                return p
            return p / "updates.jsonl"
        # Fallback: session_id dir under sessions_root (may not exist yet)
        return self.sessions_root / session_id / "updates.jsonl"

    async def run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("session tailer poll error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

    async def _poll_once(self) -> None:
        async with self._lock:
            items = list(self._watched.items())
        for session_id, watch in items:
            try:
                lines = watch.read_new_lines()
            except Exception:
                log.exception("read failed for session %s", session_id)
                continue
            # Always persist offset after read (even if no complete lines yet)
            async with self._lock:
                self._offsets[session_id] = watch.offset
            for line in lines:
                msg = parse_updates_line(line)
                if not msg:
                    continue
                try:
                    result = self.on_event(session_id, msg)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    log.exception("tail on_event failed session=%s", session_id)
