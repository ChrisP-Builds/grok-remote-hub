"""Working-mark allowlist: only stream-relevant kinds mark sessions Working."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "static" / "app.js"

STREAM_KINDS = (
    "user_message_chunk",
    "agent_message_chunk",
    "agent_thought_chunk",
    "plan",
    "tool_call",
    "tool_call_update",
)

NON_STREAM_KINDS = (
    "available_commands_update",
    "current_mode_update",
    "session_info_update",
)


def _read_app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _extract_stream_working_kinds(js: str) -> set[str]:
    m = re.search(
        r"const STREAM_WORKING_KINDS\s*=\s*new Set\(\[([\s\S]*?)\]\)",
        js,
    )
    assert m, "STREAM_WORKING_KINDS Set not found in static/app.js"
    return set(re.findall(r'["\']([^"\']+)["\']', m.group(1)))


def _working_mark_block(js: str) -> str:
    """Slice around markSessionActivity(..., 'working') near handleAcpMessage."""
    handle = js.find("function handleAcpMessage")
    assert handle >= 0, "handleAcpMessage not found"
    mark = js.find('markSessionActivity(targetId, "working")', handle)
    if mark < 0:
        mark = js.find("markSessionActivity(targetId, 'working')", handle)
    assert mark > handle, "Working markSessionActivity not found in handleAcpMessage"
    start = max(handle, mark - 400)
    return js[start : mark + 80]


def test_stream_working_helpers_exist() -> None:
    js = _read_app_js()
    assert "STREAM_WORKING_KINDS" in js
    assert "function isStreamWorkingKind" in js
    assert "isStreamWorkingKind(kind)" in js


def test_working_mark_no_bare_trailing_kind() -> None:
    js = _read_app_js()
    block = _working_mark_block(js)
    # Must use the helper, not an OR-chain that ends with bare `|| kind`.
    assert "isStreamWorkingKind(kind)" in block
    assert re.search(r"\|\|\s*kind\s*\)", block) is None
    assert re.search(r"\|\|\s*kind\s*\n", block) is None


def test_stream_kinds_in_allowlist() -> None:
    kinds = _extract_stream_working_kinds(_read_app_js())
    for k in STREAM_KINDS:
        assert k in kinds, f"expected stream kind {k!r} in STREAM_WORKING_KINDS"


def test_non_stream_kinds_not_in_allowlist() -> None:
    kinds = _extract_stream_working_kinds(_read_app_js())
    for k in NON_STREAM_KINDS:
        assert k not in kinds, f"control kind {k!r} must not mark Working"
