"""Structural + unit tests for terminal-style web UI."""

from __future__ import annotations

from pathlib import Path

from hub.ui_format import (
    format_plan_summary,
    format_term_prefix,
    format_tool_line,
    parse_simple_markdown_table,
    should_show_tool_line,
)

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def test_css_terminal_tokens() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "--bg: #0a0c10" in css
    assert "--user: #5ec8e8" in css
    assert "--assistant: #6dca8d" in css
    assert "--accent: #e6b84d" in css
    assert "IBM Plex Mono" in css
    assert ".term-line" in css
    assert ".term-prefix" in css
    assert ".turn-strip" in css
    assert "overflow-x: hidden" in css
    assert ".composer-prompt" in css
    assert ".term-cursor" in css
    # Primary transcript is linear term lines, not chat-bubble layout
    assert "margin-left: auto" not in css or css.count(".term-line") > 0
    assert ".term-line" in css
    assert "@media (max-width: 899px)" in css


def test_html_turn_strip_and_empty_state() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="turn-strip"' in html
    assert "turn-strip-text" in html
    assert "turn-strip-cursor" in html
    assert "Open a session to attach the stream" in html
    assert "Message… (/ for commands)" in html or "Message" in html
    assert "IBM+Plex+Mono" in html or "IBM Plex Mono" in html
    assert "composer-prompt" in html
    assert "&gt;" in html or ">" in html


def test_js_term_line_structure() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "term-line" in js
    assert "term-prefix" in js
    assert "term-body" in js
    assert "turn-strip" in js or "turnStrip" in js
    assert "beginNewUserTurn" in js
    assert "formatTermPrefix" in js
    assert "formatToolLine" in js
    assert "parseSimpleMarkdownTable" in js
    assert "shouldShowToolLine" in js
    # Tools always visible path
    assert "shouldShowToolLine" in js
    # Collapsible tool rows + plan auto-expand
    assert "createToolLine" in js
    assert "tool-one-liner" in js
    assert "tool-detail" in js
    assert "planHasActiveWork" in js
    assert 'createElement("details")' in js


def test_css_tool_plan_expand() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".term-line.tool > summary" in css
    assert ".tool-one-liner" in css
    assert ".tool-detail" in css
    assert '.plan-item[data-status="running"]' in css
    assert ".plan-item.active" in css


def test_format_term_prefix() -> None:
    assert format_term_prefix("user") == "You:"
    assert format_term_prefix("assistant") == "Grok:"
    assert format_term_prefix("tool") == "·"
    assert format_term_prefix("thought") == "·"
    assert format_term_prefix("plan") == "·"
    assert format_term_prefix("system") == "·"


def test_format_tool_line() -> None:
    line = format_tool_line("Read path.ext", "completed", "path.ext")
    assert "Read path.ext" in line
    assert "[completed]" in line
    # summary omitted when already in title
    line2 = format_tool_line("Read path.ext", "completed", "Read path.ext")
    assert line2.count("Read path.ext") == 1
    line3 = format_tool_line("Read", "running", "/tmp/a.py")
    assert "[running]" in line3
    assert "/tmp/a.py" in line3


def test_should_show_tool_line_always() -> None:
    assert should_show_tool_line() is True


def test_parse_simple_markdown_table() -> None:
    text = """| Name | Status |
| --- | --- |
| alpha | ok |
| beta | fail |
"""
    rows = parse_simple_markdown_table(text)
    assert rows is not None
    assert rows[0] == ["Name", "Status"]
    assert rows[1] == ["alpha", "ok"]
    assert rows[2] == ["beta", "fail"]

    assert parse_simple_markdown_table("no table here") is None
    assert parse_simple_markdown_table("") is None


def test_format_plan_summary() -> None:
    assert format_plan_summary([]) == "plan (empty)"
    entries = [
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "pending"},
        {"content": "c", "status": "running"},
    ]
    assert format_plan_summary(entries) == "plan 1/3"
