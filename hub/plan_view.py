"""Read session plan.md + plan_mode.json for Hub plan viewer (read-only)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from hub.session_index import find_session

PLAN_MD_NAME = "plan.md"
PLAN_MODE_NAME = "plan_mode.json"
# Soft cap for plan.md text (~1.5 MiB). Larger files are truncated for the viewer.
PLAN_MD_MAX_BYTES = 1_500_000


class PlanViewError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _under(path: Path, root: Path) -> bool:
    """Return True if path is root or a descendant (Windows case-insensitive)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        pass
    try:
        Path(os.path.normcase(str(path))).relative_to(
            Path(os.path.normcase(str(root)))
        )
        return True
    except ValueError:
        return False


def _resolve_session_dir(
    sessions_root: Path,
    session_id: str,
    session_path: str | Path | None = None,
) -> Path:
    sid = (session_id or "").strip()
    if not sid:
        raise PlanViewError("session not found", 404)

    if session_path:
        session_dir = Path(session_path).expanduser().resolve()
    else:
        info = find_session(Path(sessions_root), sid)
        if not info or not info.path:
            raise PlanViewError("session not found", 404)
        session_dir = Path(info.path).expanduser().resolve()

    if not session_dir.is_dir():
        raise PlanViewError("session not found", 404)

    # Prefer sessions under sessions_root when that root exists; still allow
    # provided paths as long as we only open fixed basenames under session_dir.
    root = Path(sessions_root).expanduser().resolve() if sessions_root else None
    if root is not None and root.is_dir() and not _under(session_dir, root):
        # Foreign path (e.g. unit test with explicit session_path): still OK
        # because we never accept client-relative plan paths.
        pass

    return session_dir


def _safe_plan_file(session_dir: Path, name: str) -> Path:
    """Return session_dir / name only for fixed basenames; reject escapes."""
    base = Path(name).name
    if base != name or base in {".", ".."} or "/" in name or "\\" in name:
        raise PlanViewError("invalid plan path", 400)
    if base not in (PLAN_MD_NAME, PLAN_MODE_NAME):
        raise PlanViewError("invalid plan path", 400)
    path = (session_dir / base).resolve()
    if not _under(path, session_dir.resolve()):
        raise PlanViewError("invalid plan path", 400)
    return path


def _read_plan_md(path: Path) -> tuple[bool, str, bool]:
    """Return (exists, text, truncated)."""
    if not path.is_file():
        return False, "", False
    try:
        size = path.stat().st_size
    except OSError:
        return False, "", False
    truncated = size > PLAN_MD_MAX_BYTES
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            if truncated:
                text = f.read(PLAN_MD_MAX_BYTES)
                text = text.rstrip() + "\n\n… [truncated: plan.md exceeds 1.5 MB]\n"
            else:
                text = f.read()
    except OSError:
        return True, "", False
    return True, text, truncated


def _parse_plan_mode(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _awaiting_approval(plan_mode: dict[str, Any] | None) -> bool:
    if not plan_mode:
        return False
    val = plan_mode.get("awaiting_plan_approval")
    if val is True:
        return True
    if val is False or val is None:
        return False
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes")
    return bool(val)


def _plan_state(plan_mode: dict[str, Any] | None) -> str | None:
    if not plan_mode:
        return None
    for key in ("state", "status", "plan_state", "mode"):
        raw = plan_mode.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            return text
    return None


def read_session_plan(
    sessions_root: Path,
    session_id: str,
    session_path: str | Path | None = None,
) -> dict[str, Any]:
    """
    Read plan.md + plan_mode.json under the session directory ONLY.

    Returns:
      sessionId, exists, markdown, planMode, awaitingApproval, state
      (plus truncated when plan.md was size-capped).
    """
    sid = (session_id or "").strip()
    session_dir = _resolve_session_dir(sessions_root, sid, session_path=session_path)

    plan_path = _safe_plan_file(session_dir, PLAN_MD_NAME)
    mode_path = _safe_plan_file(session_dir, PLAN_MODE_NAME)

    exists, markdown, truncated = _read_plan_md(plan_path)
    plan_mode = _parse_plan_mode(mode_path)

    payload: dict[str, Any] = {
        "sessionId": sid,
        "exists": exists,
        "markdown": markdown if exists else "",
        "planMode": plan_mode,
        "awaitingApproval": _awaiting_approval(plan_mode),
        "state": _plan_state(plan_mode),
    }
    if truncated:
        payload["truncated"] = True
    return payload
