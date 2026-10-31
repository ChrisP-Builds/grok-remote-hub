"""Build a short catch-up recap from session history messages."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from hub.history import load_session_history
from hub.session_index import find_session

DEFAULT_RECAP_MAX_CHARS = 800
_MAX_SCAN_MESSAGES = 40


def _clip(text: str, max_chars: int) -> str:
    s = (text or "").strip()
    if max_chars <= 0:
        return ""
    if len(s) <= max_chars:
        return s
    if max_chars <= 1:
        return "…"
    return s[: max_chars - 1].rstrip() + "…"


def _meaningful_text(msg: dict[str, Any]) -> str:
    if not isinstance(msg, dict):
        return ""
    role = str(msg.get("role") or "").strip().lower()
    if role not in ("user", "assistant"):
        return ""
    text = str(msg.get("text") or "").strip()
    if not text:
        return ""
    # Skip pure slash noise and empty-ish system echoes
    if role == "user" and text.startswith("/") and len(text) < 40:
        # Still useful for /compact etc.; keep short slash lines
        pass
    return text


def build_session_recap(
    messages: Iterable[dict[str, Any]] | None = None,
    *,
    session_dir: Path | str | None = None,
    sessions_root: Path | str | None = None,
    session_id: str | None = None,
    max_chars: int = DEFAULT_RECAP_MAX_CHARS,
    max_messages: int = 800,
) -> dict[str, Any]:
    """Return a bounded catch-up summary from history.

    Prefer last user + last assistant snippets. Accepts either an in-memory
    message list or disk location (session_dir / sessions_root+session_id).
    """
    cap = max(80, int(max_chars or DEFAULT_RECAP_MAX_CHARS))
    msgs: list[dict[str, Any]]
    if messages is not None:
        msgs = [m for m in messages if isinstance(m, dict)]
    else:
        root = Path(sessions_root) if sessions_root else None
        sid = str(session_id or "").strip()
        path: Path | str | None = session_dir
        if path is None and root is not None and sid:
            info = find_session(root, sid)
            path = info.path if info else None
        if root is None or not sid:
            msgs = []
        else:
            msgs = load_session_history(
                root,
                sid,
                session_path=path,
                max_messages=max_messages,
            )

    last_user: str | None = None
    last_assistant: str | None = None
    scanned = 0
    for msg in reversed(msgs):
        if scanned >= _MAX_SCAN_MESSAGES and last_user and last_assistant:
            break
        scanned += 1
        role = str(msg.get("role") or "").strip().lower()
        text = _meaningful_text(msg)
        if not text:
            continue
        if role == "assistant" and last_assistant is None:
            last_assistant = text
        elif role == "user" and last_user is None:
            last_user = text
        if last_user is not None and last_assistant is not None:
            break

    parts: list[str] = []
    if last_user:
        parts.append(f"You: {_clip(last_user, max(40, cap // 3))}")
    if last_assistant:
        remaining = cap - (len(parts[0]) + 2 if parts else 0)
        parts.append(f"Assistant: {_clip(last_assistant, max(60, remaining))}")
    summary = "\n".join(parts).strip()
    if len(summary) > cap:
        summary = _clip(summary, cap)

    return {
        "sessionId": str(session_id or "").strip() or None,
        "summary": summary,
        "lastUser": _clip(last_user or "", min(cap, 400)) if last_user else None,
        "lastAssistant": (
            _clip(last_assistant or "", min(cap, 600)) if last_assistant else None
        ),
        "empty": not bool(summary),
        "maxChars": cap,
    }


def build_session_recap_from_disk(
    sessions_root: Path | str,
    session_id: str,
    *,
    session_path: Path | str | None = None,
    max_chars: int = DEFAULT_RECAP_MAX_CHARS,
    max_messages: int = 800,
) -> dict[str, Any]:
    """Load history from disk and build recap (API helper)."""
    sid = str(session_id or "").strip()
    root = Path(sessions_root)
    path = session_path
    if path is None and sid:
        info = find_session(root, sid)
        path = info.path if info else None
    return build_session_recap(
        None,
        sessions_root=root,
        session_id=sid,
        session_dir=path,
        max_chars=max_chars,
        max_messages=max_messages,
    )
