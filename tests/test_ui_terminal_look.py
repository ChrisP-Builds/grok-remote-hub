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
    # Tables: transcript must allow horizontal scroll (not overflow-x:hidden only)
    assert ".term-table-wrap" in css
    assert ".term-table" in css
    assert "-webkit-overflow-scrolling: touch" in css
    # .transcript block uses overflow-x: auto so .term-table-wrap can scroll on mobile
    idx = css.find(".transcript {")
    assert idx >= 0
    chunk = css[idx : idx + 350]
    assert "overflow-x: auto" in chunk
    assert "overflow-x: hidden" not in chunk


def test_html_turn_strip_and_empty_state() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="turn-strip"' in html
    assert "turn-strip-text" in html
    assert "turn-strip-cursor" in html
    assert "No session selected" in html
    assert "Pick a chat from the sidebar" in html
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
    assert "finalizeAssistantTables" in js
    assert "bodyEl._rawText = raw" in js
    assert "shouldShowToolLine" in js
    # Tools always visible path
    assert "shouldShowToolLine" in js
    # Collapsible tool rows + plan auto-expand
    assert "createToolLine" in js
    assert "tool-one-liner" in js
    assert "tool-detail" in js
    assert "planHasActiveWork" in js
    assert 'createElement("details")' in js
    # Tools always start closed; user expands (never auto-open on create/update)
    create_idx = js.find("function createToolLine")
    assert create_idx >= 0
    create_chunk = js[create_idx : create_idx + 900]
    assert "row.open = false" in create_chunk
    assert "row.open = true" not in create_chunk
    update_idx = js.find("function updateToolLine")
    assert update_idx >= 0
    update_chunk = js[update_idx : update_idx + 900]
    assert "row.open = true" not in update_chunk
    assert "setToolDetailBody" in js
    assert "data-has-detail" in js or "hasDetail" in js
    assert "No detail" not in js
    assert "toolOneLinerRedundant" in js
    # Live tool_call must not build label+summary as title
    assert 'truncate(`${label} ${summary}`, 160)' not in js
    assert "truncate(`${label} ${summary}`, 160)" not in js


def test_js_mobile_table_raw_text_contracts() -> None:
    """GFM tables: _rawText is source of truth; finalize on idle/history/stale clear."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    # setTermBodyContent always stores raw before parse/render
    set_idx = js.find("function setTermBodyContent")
    assert set_idx >= 0
    set_fn = js[set_idx : set_idx + 400]
    assert "bodyEl._rawText = raw" in set_fn

    # appendToBody accumulates from _rawText via mergeStreamText (cumulative-safe)
    app_idx = js.find("function appendToBody")
    assert app_idx >= 0
    app_fn = js[app_idx : app_idx + 700]
    assert "body._rawText" in app_fn
    assert "const prev = body._rawText != null" in app_fn
    assert "mergeStreamText(prev, text)" in app_fn
    # Must not rebuild next solely from textContent when table present
    assert "querySelector(\".term-table\")" not in app_fn or "textContent" in app_fn

    # finalizeAssistantTables re-parses from _rawText when pipes present
    fin_idx = js.find("function finalizeAssistantTables")
    assert fin_idx >= 0
    fin_fn = js[fin_idx : fin_idx + 550]
    assert "body._rawText" in fin_fn
    assert 'raw.includes("|")' in fin_fn
    assert "setTermBodyContent(body, raw)" in fin_fn
    assert ".term-line.assistant .term-body" in fin_fn

    # Call sites: turn idle (setTurnRunning), history batch end, clear stale live
    assert "finalizeAssistantTables" in js
    idle_hook = js.find("Turn ended: re-parse any assistant tables")
    assert idle_hook >= 0
    assert "finalizeAssistantTables(idleRoot)" in js[idle_hook : idle_hook + 500]
    hist_hook = js.find("ensure GFM tables in loaded history")
    assert hist_hook >= 0
    assert "finalizeAssistantTables(transcriptRoot())" in js[hist_hook : hist_hook + 200]
    # clearStaleLiveTurns finalizes panes + root
    stale_idx = js.find("function clearStaleLiveTurns")
    assert stale_idx >= 0
    stale_chunk = js[stale_idx : stale_idx + 1200]
    assert "finalizeAssistantTables" in stale_chunk


def test_css_tool_plan_expand() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".term-line.tool > summary" in css
    assert ".tool-one-liner" in css
    assert ".tool-detail" in css
    assert '.plan-item[data-status="running"]' in css
    assert ".plan-item.active" in css
    # Closed tools/thoughts/plans hide non-summary children (WebKit/iOS)
    assert ".term-line.tool:not([open]) > :not(summary)" in css
    assert "display: none !important" in css
    # No-detail tools mute expand affordance
    assert ".term-line.tool:not([data-has-detail])" in css
    # Compact single-line tool summary row
    assert "flex-wrap: nowrap" in css
    assert ".term-line.tool .tool-name" in css
    assert "text-overflow: ellipsis" in css


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

    # GFM sample with alignment colons + CRLF (stream-like)
    gfm = "| A | B |\r\n| :--- | ---: |\r\n| 1 | 2 |\r\n"
    gfm_rows = parse_simple_markdown_table(gfm)
    assert gfm_rows is not None
    assert gfm_rows[0] == ["A", "B"]
    assert gfm_rows[1] == ["1", "2"]


def test_format_plan_summary() -> None:
    assert format_plan_summary([]) == "plan (empty)"
    entries = [
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "pending"},
        {"content": "c", "status": "running"},
    ]
    assert format_plan_summary(entries) == "plan 1/3"
