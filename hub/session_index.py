from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

# UUID and UUID-like (UUIDv7 etc.) session folder names
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass
class SessionInfo:
    sessionId: str
    title: str
    cwd: str
    updatedAt: str
    modelId: str
    path: str
    isSubagent: bool = False
    parentSessionId: str = ""
    agentName: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_uuid_like(name: str) -> bool:
    return bool(UUID_RE.match(name))


def _normalize_cwd(raw: str) -> str:
    if not raw:
        return ""
    cwd = raw
    if cwd.startswith("\\\\?\\"):
        cwd = cwd[4:]
    if cwd.startswith("//?/"):
        cwd = cwd[4:]
    return cwd


def _decode_cwd_from_parent(encoded: str) -> str:
    try:
        return _normalize_cwd(unquote(encoded))
    except Exception:
        return encoded


def _parse_updated(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _has_useful_content(summary: dict[str, Any], session_dir: Path) -> bool:
    title = (summary.get("generated_title") or summary.get("session_summary") or "").strip()
    if title:
        return True
    if int(summary.get("num_chat_messages") or 0) > 0:
        return True
    if int(summary.get("num_messages") or 0) > 0:
        return True
    updates = session_dir / "updates.jsonl"
    if updates.is_file() and updates.stat().st_size > 0:
        return True
    return False


def _title_from_summary(summary: dict[str, Any]) -> str:
    for key in ("generated_title", "session_summary"):
        val = (summary.get(key) or "").strip()
        if val:
            return val
    return "Untitled session"


def scan_sessions(sessions_root: Path, limit: int = 80) -> list[SessionInfo]:
    root = Path(sessions_root)
    if not root.is_dir():
        return []

    found: list[tuple[datetime, SessionInfo]] = []

    for summary_path in root.rglob("summary.json"):
        try:
            session_dir = summary_path.parent
            session_id = session_dir.name
            if not _is_uuid_like(session_id):
                continue

            path_str = str(summary_path)
            if "oracle-grok" in path_str.replace("\\", "/").lower():
                continue

            with summary_path.open("r", encoding="utf-8") as f:
                summary = json.load(f)

            if not _has_useful_content(summary, session_dir):
                continue

            info = summary.get("info") or {}
            cwd = _normalize_cwd(str(info.get("cwd") or ""))
            if not cwd:
                cwd = _decode_cwd_from_parent(session_dir.parent.name)

            if "oracle-grok" in cwd.replace("\\", "/").lower():
                continue

            updated_raw = (
                summary.get("updated_at")
                or summary.get("last_active_at")
                or summary.get("created_at")
                or ""
            )
            updated_dt = _parse_updated(str(updated_raw) if updated_raw else None)

            parts = session_dir.parts
            is_sub = False
            parent_id = ""
            if "subagents" in parts:
                idx = parts.index("subagents")
                is_sub = True
                if idx > 0:
                    parent_id = parts[idx - 1]  # parent session uuid folder
            agent_name = str(summary.get("agent_name") or "").strip()

            found.append(
                (
                    updated_dt,
                    SessionInfo(
                        sessionId=str(info.get("id") or session_id),
                        title=_title_from_summary(summary),
                        cwd=cwd,
                        updatedAt=updated_dt.isoformat().replace("+00:00", "Z"),
                        modelId=str(summary.get("current_model_id") or ""),
                        path=str(session_dir),
                        isSubagent=is_sub,
                        parentSessionId=parent_id,
                        agentName=agent_name,
                    ),
                )
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue

    found.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in found[:limit]]


def list_projects(projects_root: Path, sessions: list[SessionInfo] | None = None) -> list[dict[str, str]]:
    """Project roots for New Session: D:\\Projects\\* dirs + distinct session cwds."""
    items: dict[str, str] = {}

    root = Path(projects_root)
    if root.is_dir():
        try:
            for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_dir() and not entry.name.startswith("."):
                    items[str(entry)] = entry.name
        except OSError:
            pass

    if sessions:
        for s in sessions:
            cwd = (s.cwd or "").strip()
            if not cwd:
                continue
            p = Path(cwd)
            key = str(p)
            if key not in items:
                items[key] = p.name or key

    result = [{"path": path, "name": name} for path, name in items.items()]
    result.sort(key=lambda x: x["name"].lower())
    return result


def find_session(sessions_root: Path, session_id: str) -> SessionInfo | None:
    for s in scan_sessions(sessions_root, limit=10_000):
        if s.sessionId == session_id:
            return s
    return None
