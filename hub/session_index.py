from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

HUB_TITLE_MAX = 200

# UUID and UUID-like (UUIDv7 etc.) session folder names
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_NOISE_TITLES = (
    "e2e-turn",
    "e2e_",
    "safari_e2e",
    "dual_ok",
    "solo_ok",
    "mt_one",
    "pong1",
    "stream_5",
    "life_a",
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
    hubOrigin: str = ""  # user | attach | "" from summary hub_origin
    isNoise: bool = False  # temp/e2e junk
    isHubRemote: bool = False  # filled by scan when hub_remote_ids passed
    isWorking: bool = True  # computed: not isSubagent and not isNoise

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
    title = (
        summary.get("hub_title")
        or summary.get("generated_title")
        or summary.get("session_summary")
        or ""
    ).strip()
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
    for key in ("hub_title", "generated_title", "session_summary"):
        val = (summary.get(key) or "").strip()
        if val:
            return val
    return "Untitled session"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_suffix(path.suffix + ".hubtmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        path.write_text(text, encoding="utf-8")


def is_noise_session(cwd: str, title: str = "") -> bool:
    """True for ephemeral e2e/temp sessions that should not clutter Working."""
    c = (cwd or "").replace("/", "\\").casefold()
    # Windows temp / AppData Local Temp / grok-hub-e2e
    if "\\temp\\" in c or c.endswith("\\temp") or "\\tmp\\" in c:
        if "grok-hub-e2e" in c or "oracle-grok" in c or "\\pytest" in c:
            return True
        # generic temp with e2e markers in path
        if "e2e" in c or "grok-hub" in c:
            return True
    # Temp path always noise if under Local\\Temp or AppData\\Local\\Temp
    if "\\appdata\\local\\temp\\" in c:
        return True
    t = (title or "").casefold()
    # Title-only noise: strong e2e markers when title is clearly test
    if any(x in t for x in _NOISE_TITLES):
        return True
    return False


def _find_summary_path(sessions_root: Path, session_id: str) -> Path | None:
    """Locate summary.json for session_id (including not-yet-useful sessions)."""
    session = find_session(sessions_root, session_id)
    if session:
        p = Path(session.path) / "summary.json"
        if p.is_file():
            return p
    root = Path(sessions_root)
    if not root.is_dir() or not _is_uuid_like(session_id):
        return None
    try:
        for summary_path in root.rglob("summary.json"):
            if summary_path.parent.name == session_id and summary_path.is_file():
                return summary_path
    except OSError:
        return None
    return None


def stamp_hub_origin(sessions_root: Path, session_id: str, origin: str) -> bool:
    """Write hub_origin on summary.json (user|attach). Returns True if wrote."""
    if origin not in ("user", "attach"):
        return False
    summary_path = _find_summary_path(sessions_root, session_id)
    if not summary_path:
        return False
    try:
        with summary_path.open("r", encoding="utf-8") as f:
            summary = json.load(f)
        if not isinstance(summary, dict):
            return False
        summary["hub_origin"] = origin
        _atomic_write_json(summary_path, summary)
        return True
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False


def scan_sessions(
    sessions_root: Path,
    limit: int = 80,
    hub_remote_ids: set[str] | None = None,
) -> list[SessionInfo]:
    root = Path(sessions_root)
    if not root.is_dir():
        return []

    remote_ids = hub_remote_ids or set()
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

            session_kind = str(summary.get("session_kind") or "").strip().lower()
            is_sub = session_kind in ("subagent", "subagent_fork")
            parent_id = ""
            parent_raw = summary.get("parent_session_id")
            if isinstance(parent_raw, str) and parent_raw.strip():
                parent_id = parent_raw.strip()

            parts = session_dir.parts
            if "subagents" in parts:
                # Legacy layout: nested under parent/subagents/<uuid>
                is_sub = True
                if not parent_id:
                    idx = parts.index("subagents")
                    if idx > 0:
                        parent_id = parts[idx - 1]
            agent_name = str(summary.get("agent_name") or "").strip()
            hub_origin = str(summary.get("hub_origin") or "").strip()
            if hub_origin not in ("user", "attach"):
                hub_origin = ""

            title = _title_from_summary(summary)
            sid = str(info.get("id") or session_id)
            # Subagents never classified as noise; they have their own filter
            is_noise = is_noise_session(cwd, title) and not is_sub
            is_hub_remote = sid in remote_ids
            is_working = (not is_sub) and (not is_noise)

            found.append(
                (
                    updated_dt,
                    SessionInfo(
                        sessionId=sid,
                        title=title,
                        cwd=cwd,
                        updatedAt=updated_dt.isoformat().replace("+00:00", "Z"),
                        modelId=str(summary.get("current_model_id") or ""),
                        path=str(session_dir),
                        isSubagent=is_sub,
                        parentSessionId=parent_id,
                        agentName=agent_name,
                        hubOrigin=hub_origin,
                        isNoise=is_noise,
                        isHubRemote=is_hub_remote,
                        isWorking=is_working,
                    ),
                )
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue

    found.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in found[:limit]]


def list_projects(projects_root: Path, sessions: list[SessionInfo] | None = None) -> list[dict[str, str]]:
    """Project roots for New Session: projects_root/* dirs + distinct session cwds."""
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


def rename_session(sessions_root: Path, session_id: str, title: str) -> SessionInfo | None:
    """Set hub_title (and generated_title) on summary.json. Returns updated info or None."""
    cleaned = (title or "").strip()
    if not cleaned:
        return None
    if len(cleaned) > HUB_TITLE_MAX:
        cleaned = cleaned[:HUB_TITLE_MAX]

    session = find_session(sessions_root, session_id)
    if not session:
        return None

    summary_path = Path(session.path) / "summary.json"
    if not summary_path.is_file():
        return None

    try:
        with summary_path.open("r", encoding="utf-8") as f:
            summary = json.load(f)
        if not isinstance(summary, dict):
            return None
        summary["hub_title"] = cleaned
        summary["generated_title"] = cleaned
        _atomic_write_json(summary_path, summary)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None

    return SessionInfo(
        sessionId=session.sessionId,
        title=cleaned,
        cwd=session.cwd,
        updatedAt=session.updatedAt,
        modelId=session.modelId,
        path=session.path,
        isSubagent=session.isSubagent,
        parentSessionId=session.parentSessionId,
        agentName=session.agentName,
        hubOrigin=session.hubOrigin,
        isNoise=session.isNoise,
        isHubRemote=session.isHubRemote,
        isWorking=session.isWorking,
    )


def delete_session(sessions_root: Path, session_id: str) -> bool:
    """Delete session folder (summary.json parent UUID dir). Returns True if removed."""
    if not session_id or not _is_uuid_like(session_id):
        return False

    session = find_session(sessions_root, session_id)
    if not session:
        return False

    root = Path(sessions_root).resolve()
    try:
        session_dir = Path(session.path).resolve()
        session_dir.relative_to(root)
    except (ValueError, OSError):
        return False

    if not session_dir.is_dir():
        return False

    try:
        shutil.rmtree(session_dir)
        return True
    except OSError:
        return False
