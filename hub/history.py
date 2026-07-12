from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (int, float, bool)):
        return str(content)
    if isinstance(content, dict):
        if "text" in content and content.get("text") not in (None, ""):
            return str(content.get("text") or "")
        parts = content.get("content")
        if isinstance(parts, list):
            return "".join(_extract_text(p) for p in parts)
        if isinstance(parts, dict):
            return _extract_text(parts)
        if isinstance(parts, str):
            return parts
        if "text" in content:
            return str(content.get("text") or "")
        return ""
    if isinstance(content, list):
        return "".join(_extract_text(p) for p in content)
    return str(content)


def _truncate(s: str, limit: int = 120) -> str:
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


def normalize_status(status: Any) -> str:
    """Map ACP status values to pending|running|completed|failed|cancelled."""
    if status is None:
        return "pending"
    if isinstance(status, dict):
        status = status.get("status") or status.get("state") or "pending"
    s = str(status).strip().lower()
    if not s:
        return "pending"
    if s in ("pending", "queued", "waiting"):
        return "pending"
    if s in ("running", "in_progress", "in-progress", "active", "started"):
        return "running"
    if s in ("completed", "complete", "ok", "success", "succeeded", "done"):
        return "completed"
    if s in ("failed", "error", "errored"):
        return "failed"
    if s in ("cancelled", "canceled", "aborted"):
        return "cancelled"
    if "complete" in s or s in ("ok", "success"):
        return "completed"
    if "fail" in s or "error" in s:
        return "failed"
    if "cancel" in s or "abort" in s:
        return "cancelled"
    if "run" in s or "progress" in s:
        return "running"
    return s


def tool_label(update: dict[str, Any]) -> str:
    """Prefer _meta x.ai/tool label, then friendly title, then tool name."""
    meta = update.get("_meta") or {}
    xai = meta.get("x.ai/tool") if isinstance(meta, dict) else None
    if isinstance(xai, dict):
        label = xai.get("label") or xai.get("name")
        if label:
            return str(label)
    title = update.get("title")
    if title:
        return str(title)
    tool = update.get("tool")
    if tool:
        return str(tool)
    return "tool"


def tool_summary(update: dict[str, Any], limit: int = 120) -> str:
    """Short one-line summary from rawInput paths/commands (not full JSON)."""
    raw = update.get("rawInput")
    if raw is None:
        # Fall back to content snippet from updates
        content = update.get("content")
        if content is not None:
            text = _extract_text(content).strip()
            if text:
                return _truncate(text, limit)
        return ""

    if isinstance(raw, str):
        return _truncate(raw, limit)

    if not isinstance(raw, dict):
        return _truncate(str(raw), limit)

    # Prefer common path / command keys
    path_keys = (
        "target_file",
        "path",
        "file",
        "file_path",
        "filepath",
        "filename",
        "cwd",
        "directory",
        "dir",
        "url",
        "uri",
    )
    for key in path_keys:
        val = raw.get(key)
        if val is not None and str(val).strip():
            return _truncate(str(val).strip(), limit)

    cmd_keys = ("command", "cmd", "shell", "script")
    for key in cmd_keys:
        val = raw.get(key)
        if val is not None and str(val).strip():
            return _truncate(str(val).strip(), limit)

    pattern_keys = ("pattern", "query", "search", "q", "grep")
    for key in pattern_keys:
        val = raw.get(key)
        if val is not None and str(val).strip():
            return _truncate(str(val).strip(), limit)

    # Compact single-line JSON of a few short fields
    parts: list[str] = []
    for k, v in list(raw.items())[:4]:
        if v is None:
            continue
        if isinstance(v, (dict, list)):
            continue
        sv = str(v).strip()
        if not sv:
            continue
        parts.append(f"{k}={_truncate(sv, 40)}")
        if len(", ".join(parts)) >= limit:
            break
    if parts:
        return _truncate(", ".join(parts), limit)

    try:
        return _truncate(json.dumps(raw, ensure_ascii=False, separators=(",", ":")), limit)
    except (TypeError, ValueError):
        return _truncate(str(raw), limit)


def _content_snippet(update: dict[str, Any], limit: int = 120) -> str:
    content = update.get("content")
    if content is None:
        return ""
    # ACP content is often list of {type, content: {type, text}}
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                inner = item.get("content")
                text = _extract_text(inner if inner is not None else item)
            else:
                text = _extract_text(item)
            if text.strip():
                parts.append(text.strip())
        if parts:
            return _truncate(" ".join(parts), limit)
        return ""
    return _truncate(_extract_text(content).strip(), limit)


def _update_from_line(obj: dict[str, Any]) -> dict[str, Any] | None:
    method = obj.get("method") or ""
    if method not in ("session/update", "_x.ai/session/update"):
        # Some lines may be raw update payloads
        if "sessionUpdate" in obj:
            return obj
        if "update" in obj and isinstance(obj["update"], dict):
            return obj["update"]
        return None
    params = obj.get("params") or {}
    update = params.get("update")
    if isinstance(update, dict):
        return update
    return None


def _message_from_update(update: dict[str, Any]) -> dict[str, Any] | None:
    kind = update.get("sessionUpdate") or ""

    if kind == "user_message_chunk":
        text = _extract_text(update.get("content"))
        if not text:
            return None
        return {"role": "user", "text": text, "meta": {}}

    if kind == "agent_message_chunk":
        text = _extract_text(update.get("content"))
        if not text:
            return None
        return {"role": "assistant", "text": text, "meta": {}}

    if kind == "agent_thought_chunk":
        text = _extract_text(update.get("content"))
        if not text:
            return None
        return {"role": "thought", "text": text, "meta": {}}

    if kind == "plan":
        entries_in = update.get("entries") or []
        entries: list[dict[str, Any]] = []
        if isinstance(entries_in, list):
            for e in entries_in:
                if not isinstance(e, dict):
                    continue
                entries.append(
                    {
                        "content": str(e.get("content") or ""),
                        "status": normalize_status(e.get("status")),
                        "priority": str(e.get("priority") or ""),
                    }
                )
        return {
            "role": "plan",
            "text": "",
            "meta": {"entries": entries, "kind": "plan"},
        }

    if kind == "tool_call":
        tool_id = update.get("toolCallId") or ""
        label = tool_label(update)
        summary = tool_summary(update)
        status = normalize_status(update.get("status")) if update.get("status") is not None else "pending"
        meta_block = update.get("_meta") or {}
        xai = meta_block.get("x.ai/tool") if isinstance(meta_block, dict) else None
        tool_kind = ""
        if isinstance(xai, dict):
            tool_kind = str(xai.get("kind") or "")
        # Display title: "Read path…" style when we have both label and summary
        if summary and label and label.lower() not in summary.lower():
            text = _truncate(f"{label} {summary}", 160)
        elif summary:
            text = summary
        else:
            text = label
        return {
            "role": "tool",
            "text": text,
            "meta": {
                "toolCallId": tool_id,
                "status": status,
                "summary": summary,
                "detail": summary,  # short one-liner only, not JSON dump
                "label": label,
                "kind": tool_kind or "tool_call",
            },
        }

    if kind == "tool_call_update":
        tool_id = update.get("toolCallId") or ""
        title = update.get("title") or tool_label(update)
        status = normalize_status(update.get("status"))
        snippet = _content_snippet(update)
        if not snippet:
            snippet = tool_summary(update)
        return {
            "role": "tool",
            "text": str(title),
            "meta": {
                "toolCallId": tool_id,
                "status": status,
                "summary": snippet,
                "detail": snippet,
                "label": str(title),
                "kind": "tool_call_update",
            },
        }

    if kind in ("hook_execution", "hook_execution_start", "hook_execution_end"):
        # Skip hook noise in transcript
        return None

    if kind in ("subagent_spawned", "subagent_finished"):
        sid = (
            update.get("sessionId")
            or update.get("agentId")
            or update.get("subagentId")
            or update.get("agentSessionId")
            or ""
        )
        label = str(kind).replace("_", " ")
        text = f"{label} ({sid})" if sid else label
        return {"role": "system", "text": text, "meta": {"kind": kind}}

    if kind in ("turn_completed", "task_completed", "prompt_complete"):
        return {
            "role": "system",
            "text": kind.replace("_", " "),
            "meta": {"kind": kind},
        }

    return None


def _find_tool_index(merged: list[dict[str, Any]], tool_call_id: str) -> int:
    """Search backward for a tool message with the same toolCallId."""
    if not tool_call_id:
        return -1
    for i in range(len(merged) - 1, -1, -1):
        m = merged[i]
        if m.get("role") != "tool":
            continue
        if (m.get("meta") or {}).get("toolCallId") == tool_call_id:
            return i
    return -1


def _merge_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not messages:
        return []
    merged: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if (
            merged
            and role in ("user", "assistant", "thought")
            and merged[-1].get("role") == role
        ):
            prev = merged[-1].get("text") or ""
            new = msg.get("text") or ""
            if not new:
                continue
            if prev == new or prev.startswith(new):
                continue
            if new.startswith(prev):
                merged[-1]["text"] = new
                continue
            merged[-1]["text"] = prev + new
            continue

        # Plan: keep only latest entries (replace previous plan message)
        if role == "plan":
            entries = list((msg.get("meta") or {}).get("entries") or [])
            # Prefer replace last plan if consecutive or any prior plan in this turn
            replaced = False
            for i in range(len(merged) - 1, -1, -1):
                if merged[i].get("role") == "plan":
                    merged[i] = {
                        "role": "plan",
                        "text": "",
                        "meta": {"entries": entries, "kind": "plan"},
                    }
                    replaced = True
                    break
                # Stop at user message so multi-turn plans stay per-turn-ish
                if merged[i].get("role") == "user":
                    break
            if not replaced:
                merged.append(
                    {
                        "role": "plan",
                        "text": "",
                        "meta": {"entries": entries, "kind": "plan"},
                    }
                )
            continue

        # Tools: search backward for same toolCallId (not only adjacent)
        if role == "tool":
            tool_id = (msg.get("meta") or {}).get("toolCallId") or ""
            idx = _find_tool_index(merged, tool_id) if tool_id else -1
            if idx >= 0:
                prev = merged[idx]
                prev_meta = dict(prev.get("meta") or {})
                new_meta = msg.get("meta") or {}
                if new_meta.get("status"):
                    prev_meta["status"] = normalize_status(new_meta["status"])
                # Prefer short summary/detail from update; do not append JSON dumps
                for key in ("summary", "detail"):
                    val = new_meta.get(key)
                    if val:
                        prev_meta[key] = val
                if new_meta.get("label"):
                    prev_meta["label"] = new_meta["label"]
                if new_meta.get("kind") and new_meta["kind"] != "tool_call_update":
                    prev_meta["kind"] = new_meta["kind"]
                if msg.get("text") and msg["text"] not in ("tool", "tool_call"):
                    prev["text"] = msg["text"]
                prev["meta"] = prev_meta
                continue

            meta = dict(msg.get("meta") or {})
            if meta.get("status"):
                meta["status"] = normalize_status(meta["status"])
            merged.append(
                {
                    "role": "tool",
                    "text": msg.get("text") or "",
                    "meta": meta,
                }
            )
            continue

        merged.append(
            {
                "role": msg["role"],
                "text": msg.get("text") or "",
                "meta": dict(msg.get("meta") or {}),
            }
        )
    return merged


def parse_updates_jsonl(path: Path | str, max_messages: int = 800) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return []

    raw_messages: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                update = _update_from_line(obj)
                if not update:
                    continue
                msg = _message_from_update(update)
                if msg:
                    raw_messages.append(msg)
    except OSError:
        return []

    merged = _merge_messages(raw_messages)
    if max_messages > 0 and len(merged) > max_messages:
        return merged[-max_messages:]
    return merged


def load_session_history(
    sessions_root: Path,
    session_id: str,
    session_path: str | Path | None = None,
    max_messages: int = 800,
) -> list[dict[str, Any]]:
    if session_path:
        updates = Path(session_path) / "updates.jsonl"
        if updates.is_file():
            return parse_updates_jsonl(updates, max_messages=max_messages)

    root = Path(sessions_root)
    if not root.is_dir():
        return []
    for summary in root.rglob("summary.json"):
        if summary.parent.name == session_id:
            return parse_updates_jsonl(summary.parent / "updates.jsonl", max_messages=max_messages)
    return []
