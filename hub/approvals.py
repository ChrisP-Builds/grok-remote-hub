"""Approvals broker: hold risky agent tool calls until the phone answers. ADR-027."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web

from hub.approval_policy import (
    ALLOW,
    Verdict,
    classify,
    command_verdict,
    file_verdict,
    normalize_mode,
    path_key,
)

log = logging.getLogger("hub.approvals")

DECISION_TIMEOUT_S = 300.0
HOOK_TIMEOUT_S = 330
APPROVAL_MEMORY_S = 600.0

_NO_RETRY = "Do not retry it; continue without it or ask the user."
_REASONS = {
    "user": "The user denied this action in Grok Remote Hub ({category}). " + _NO_RETRY,
    "timeout": "No answer from the user in Grok Remote Hub in time, so this action was denied. " + _NO_RETRY,
    "cancelled": "The user stopped the turn in Grok Remote Hub. " + _NO_RETRY,
}

Publish = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class _Pending:
    id: str
    session_id: str
    verdict: Verdict
    root: str | None
    tool: str
    created_at: float
    expires_at: float
    future: asyncio.Future = field(repr=False)

    def payload(self, now: float) -> dict[str, Any]:
        return {
            "id": self.id,
            "sessionId": self.session_id or None,
            "tool": self.tool,
            "kind": self.verdict.kind,
            "summary": _visible(self.verdict.summary),
            "cwd": _visible(self.root),
            "category": self.verdict.category,
            "similarRule": _visible(self.verdict.similar_rule),
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
            "serverNow": now,
        }


_INVISIBLE = re.compile("[​-‏‪-‮⁦-⁩﻿]")


def _visible(text: str | None) -> str | None:
    """Show bidi and zero-width characters as markers so a command cannot be disguised."""
    return _INVISIBLE.sub(lambda m: f"<U+{ord(m.group()):04X}>", text) if text else text


class ApprovalBroker:
    def __init__(
        self,
        rules_path: Path,
        publish: Publish,
        *,
        session_root_for: Callable[[str], str | None] = lambda _sid: None,
        default_mode: str = "balanced",
        timeout: float = DECISION_TIMEOUT_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._rules_path = Path(rules_path)
        self._publish = publish
        self._known_root = session_root_for
        self.default_mode = normalize_mode(default_mode)
        self._timeout = timeout
        self._clock = clock
        self._pending: dict[str, _Pending] = {}
        self._modes: dict[str, str] = {}
        self._event_roots: dict[str, str] = {}
        self._approved: dict[tuple[str, str, str], float] = {}

    def mode_for(self, session_id: str) -> str:
        return self._modes.get(session_id, self.default_mode)

    def set_mode(self, session_id: str, mode: str) -> str:
        self._modes[session_id] = normalize_mode(mode)
        return self._modes[session_id]

    def modes_payload(self) -> dict[str, str]:
        return dict(self._modes)

    def pending_payload(self) -> list[dict[str, Any]]:
        now = self._clock()
        return [p.payload(now) for p in sorted(self._pending.values(), key=lambda p: p.created_at)]

    def _root(self, session_id: str) -> str | None:
        return self._known_root(session_id) or self._event_roots.get(session_id)

    def _read_rules(self) -> dict[str, list[str]]:
        try:
            data = json.loads(self._rules_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        projects = data.get("projects") if isinstance(data, dict) else None
        return {str(k): [str(r) for r in v] for k, v in (projects or {}).items() if isinstance(v, list)}

    def grants_for(self, root: str | None) -> list[str]:
        if not root:
            return []
        want = path_key(root)
        return [r for k, rules in self._read_rules().items() if path_key(k) == want for r in rules]

    def _add_grant(self, root: str, rule: str) -> None:
        projects = self._read_rules()
        key = next((k for k in projects if path_key(k) == path_key(root)), root)
        if rule not in projects.setdefault(key, []):
            projects[key].append(rule)
        self._rules_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._rules_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"projects": projects}, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self._rules_path)

    async def _emit(self, payload: dict[str, Any]) -> None:
        try:
            await self._publish(payload)
        except Exception:
            log.exception("approval publish failed")

    async def decide(self, event: dict[str, Any]) -> dict[str, str]:
        """Answer one Grok PreToolUse event: defer, allow, or deny with a reason."""
        sid = str(event.get("sessionId") or event.get("session_id") or "")
        if sid and event.get("cwd"):
            self._event_roots.setdefault(sid, str(event["cwd"]))
        root = self._root(sid)
        tool = str(event.get("toolName") or event.get("tool_name") or "")
        verdict = classify(
            tool,
            event.get("toolInput") or event.get("tool_input"),
            mode=self.mode_for(sid),
            session_root=root,
            grants=self.grants_for(root),
            truncated=bool(event.get("toolInputTruncated")),
        )
        if verdict.action == ALLOW:
            return {"decision": "defer"}
        now = self._clock()
        pending = _Pending(
            secrets.token_hex(6), sid, verdict, root, tool, now, now + self._timeout,
            asyncio.get_running_loop().create_future(),
        )
        self._pending[pending.id] = pending
        await self._emit({"type": "approval_request", **pending.payload(self._clock())})
        try:
            decision, scope, by = await asyncio.wait_for(pending.future, self._timeout)
        except asyncio.TimeoutError:
            decision, scope, by = "deny", None, "timeout"
        finally:
            self._pending.pop(pending.id, None)
        await self._emit({
            "type": "approval_resolved", "id": pending.id, "sessionId": sid or None,
            "decision": decision, "by": by, "scope": scope,
        })
        if decision != "allow":
            return {"decision": "deny", "reason": _REASONS[by].format(category=verdict.category.lower())}
        self._remember_approval(sid, verdict)
        if scope == "project" and root and verdict.similar_rule:
            self._add_grant(root, verdict.similar_rule)
        return {"decision": "allow"}

    def answer(self, approval_id: str, decision: str, scope: str | None = "once") -> bool:
        pending = self._pending.get(str(approval_id))
        if pending is None or pending.future.done():
            return False
        allow = str(decision).lower() == "allow"
        pending.future.set_result(
            ("allow" if allow else "deny", ("project" if scope == "project" else "once") if allow else None, "user")
        )
        return True

    def cancel_session(self, session_id: str) -> int:
        cancelled = 0
        for pending in list(self._pending.values()):
            if pending.session_id == session_id and not pending.future.done():
                pending.future.set_result(("deny", None, "cancelled"))
                cancelled += 1
        return cancelled

    @staticmethod
    def _approval_key(kind: str, value: str) -> str:
        return " ".join(value.split()) if kind == "shell" else path_key(value)

    def _remember_approval(self, session_id: str, verdict: Verdict) -> None:
        if verdict.kind in ("shell", "file"):
            key = (session_id, verdict.kind, self._approval_key(verdict.kind, verdict.summary))
            self._approved[key] = self._clock() + APPROVAL_MEMORY_S

    def exec_refusal(self, kind: str, session_id: str, value: str) -> str | None:
        """Executor backstop for shell and file writes; None means run it."""
        root = self._root(session_id)
        mode, grants = self.mode_for(session_id), self.grants_for(root)
        if kind == "shell":
            verdict = command_verdict(value, mode, grants)
        else:
            verdict = file_verdict(value, mode, root, grants)
        if verdict.action == ALLOW:
            return None
        expires = self._approved.get((session_id, kind, self._approval_key(kind, value)), 0.0)
        if expires > self._clock():
            return None
        return (
            "Blocked by Grok Remote Hub: this action needs the user's approval and none was given. "
            "If no approval card appeared, restart the agent so the approval hook loads."
        )


def is_loopback_peer(remote: str | None) -> bool:
    try:
        return ipaddress.ip_address(str(remote or "")).is_loopback
    except ValueError:
        return False


def make_pre_tool_use_handler(broker: ApprovalBroker, key: str):
    async def handler(request: web.Request) -> web.Response:
        if not is_loopback_peer(request.remote) or not secrets.compare_digest(
            (request.query.get("key") or "").encode(), key.encode()
        ):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            event = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid json"}, status=400)
        try:
            return web.json_response(await broker.decide(event if isinstance(event, dict) else {}))
        except Exception:
            log.exception("approval decision failed")
            return web.json_response({"decision": "deny", "reason": "Grok Remote Hub approval error. " + _NO_RETRY})

    return handler


def write_gate_plugin(plugin_dir: Path, url: str, timeout_s: int = HOOK_TIMEOUT_S) -> Path:
    hooks_file = Path(plugin_dir) / "hooks" / "hooks.json"
    body = json.dumps(
        {"hooks": {"PreToolUse": [{"hooks": [{"type": "http", "url": url, "timeout": timeout_s}]}]}},
        indent=2,
    ) + "\n"
    if not hooks_file.is_file() or hooks_file.read_text(encoding="utf-8") != body:
        hooks_file.parent.mkdir(parents=True, exist_ok=True)
        hooks_file.write_text(body, encoding="utf-8")
    return hooks_file
