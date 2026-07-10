from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "")
        parts = content.get("content")
        if isinstance(parts, list):
            return "".join(_extract_text(p) for p in parts)
        return ""
    if isinstance(content, list):
        return "".join(_extract_text(p) for p in content)
    return str(content)


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

    if kind == "tool_call":
        tool_id = update.get("toolCallId") or ""
        title = update.get("title") or update.get("tool") or "tool"
        status = "pending"
        meta_status = (update.get("_meta") or {}).get("updateParams") or {}
        if isinstance(meta_status, dict) and meta_status.get("status"):
            status = str(meta_status["status"]).lower()
        raw_input = update.get("rawInput")
        detail = ""
        if raw_input is not None:
            try:
                detail = json.dumps(raw_input, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                detail = str(raw_input)
        return {
            "role": "tool",
            "text": str(title),
            "meta": {
                "toolCallId": tool_id,
                "status": status,
                "detail": detail,
                "kind": "tool_call",
            },
        }

    if kind == "tool_call_update":
        tool_id = update.get("toolCallId") or ""
        title = update.get("title") or "tool"
        status = "updated"
        raw = update.get("status")
        if raw:
            if isinstance(raw, dict):
                status = str(raw.get("status") or status)
            else:
                status = str(raw)
        detail_parts: list[str] = []
        if update.get("locations"):
            try:
                detail_parts.append(json.dumps(update["locations"], ensure_ascii=False))
            except (TypeError, ValueError):
                pass
        if update.get("rawInput") is not None:
            try:
                detail_parts.append(json.dumps(update["rawInput"], ensure_ascii=False, indent=2))
            except (TypeError, ValueError):
                detail_parts.append(str(update["rawInput"]))
        content = update.get("content")
        if content is not None:
            text_c = _extract_text(content)
            if text_c:
                detail_parts.append(text_c)
        return {
            "role": "tool",
            "text": str(title),
            "meta": {
                "toolCallId": tool_id,
                "status": status.lower() if isinstance(status, str) else str(status),
                "detail": "\n".join(detail_parts),
                "kind": "tool_call_update",
            },
        }

    if kind in ("turn_completed", "task_completed", "prompt_complete"):
        return {
            "role": "system",
            "text": kind.replace("_", " "),
            "meta": {"kind": kind},
        }

    return None


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
            merged[-1]["text"] = (merged[-1].get("text") or "") + (msg.get("text") or "")
            continue
        # Collapse consecutive tool_call_update into prior same toolCallId tool card
        if (
            role == "tool"
            and merged
            and merged[-1].get("role") == "tool"
            and (msg.get("meta") or {}).get("toolCallId")
            and (msg.get("meta") or {}).get("toolCallId")
            == (merged[-1].get("meta") or {}).get("toolCallId")
        ):
            prev_meta = dict(merged[-1].get("meta") or {})
            new_meta = msg.get("meta") or {}
            if new_meta.get("status"):
                prev_meta["status"] = new_meta["status"]
            if new_meta.get("detail"):
                prev = prev_meta.get("detail") or ""
                nxt = new_meta["detail"]
                prev_meta["detail"] = (prev + "\n" + nxt).strip() if prev else nxt
            if msg.get("text") and msg["text"] != "tool":
                merged[-1]["text"] = msg["text"]
            merged[-1]["meta"] = prev_meta
            continue
        merged.append(
            {
                "role": msg["role"],
                "text": msg.get("text") or "",
                "meta": dict(msg.get("meta") or {}),
            }
        )
    return merged


def parse_updates_jsonl(path: Path | str, max_messages: int = 200) -> list[dict[str, Any]]:
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
    max_messages: int = 200,
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
