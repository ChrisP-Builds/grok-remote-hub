"""UX continuity helpers + structural checks for session panes, spellcheck, topbar bubble."""

from __future__ import annotations

import json
from pathlib import Path

from hub.ui_ux import (
    idle_turn_label,
    residual_status_parts,
    session_list_progress_hint,
    should_mark_plan_stale,
    should_scroll_to_bottom,
    topbar_bubble_lines,
    topbar_bubble_text,
    turn_progress_label,
)

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def test_topbar_bubble_lines_sample() -> None:
    lines = topbar_bubble_lines("Grok Remote Hub", "grok-code", r"D:\Projects\Grok Remote Hub")
    assert len(lines) == 3
    assert lines[0].startswith("Project:")
    assert "Grok Remote Hub" in lines[0]
    assert "grok-code" in lines[1]
    assert r"D:\Projects\Grok Remote Hub" in lines[2]
    text = topbar_bubble_text("Grok Remote Hub", "grok-code", r"D:\Projects\Grok Remote Hub")
    assert "\n" in text
    assert text == "\n".join(lines)


def test_topbar_bubble_lines_empty_fallbacks() -> None:
    lines = topbar_bubble_lines("", "", "")
    assert lines == ["Project: —", "Model: —", "Path: —"]


def test_should_scroll_to_bottom() -> None:
    assert should_scroll_to_bottom(True) is True
    assert should_scroll_to_bottom(False) is False
    assert should_scroll_to_bottom(False, force=True) is True
    assert should_scroll_to_bottom(True, force=True) is True


def test_turn_progress_label_running_and_tool() -> None:
    idle = turn_progress_label(running=False, model="m1")
    assert "idle" in idle
    assert "m1" in idle

    residual = turn_progress_label(
        running=False,
        model="m1",
        plan_pending=2,
        plan_failed=1,
        tool_pending=1,
    )
    assert residual.startswith("idle")
    assert "plan 2 open" in residual
    assert "plan 1 failed" in residual
    assert "tool 1 open" in residual
    # model omitted when residual present
    assert residual.count("m1") == 0

    running = turn_progress_label(
        running=True,
        tool="read_file",
        queue=2,
        model="grok",
        elapsed_s=15,
    )
    assert "running" in running
    assert "read_file" in running
    assert "15s" in running
    assert "queue 2" in running
    assert "grok" in running

    quiet = turn_progress_label(running=True, quiet=True, tool="x")
    assert "quiet" in quiet
    assert "x" in quiet


def test_residual_status_and_stale() -> None:
    parts = residual_status_parts(plan_pending=1, plan_failed=2, tool_running=1)
    assert "plan 1 open" in parts
    assert "plan 2 failed" in parts
    assert "tool 1 open" in parts
    label = idle_turn_label(plan_pending=1, tool_failed=1)
    assert label.startswith("idle")
    assert "failed" in label
    assert should_mark_plan_stale(turn_running=False, has_open_or_failed=True) is True
    assert should_mark_plan_stale(turn_running=True, has_open_or_failed=True) is False
    assert should_mark_plan_stale(turn_running=False, has_open_or_failed=False) is False


def test_session_list_progress_hint() -> None:
    assert session_list_progress_hint(is_live_turn=False, tool="x") == ""
    assert session_list_progress_hint(is_live_turn=True, tool="") == "running"
    assert session_list_progress_hint(is_live_turn=True, tool="bash") == "bash"


def test_html_composer_spellcheck() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="composer-input"' in html
    assert 'spellcheck="true"' in html
    assert "autocorrect=" in html
    assert "autocapitalize=" in html
    assert 'id="meta-popover"' in html
    assert "meta-popover" in html
    # Must not nest under overflow:hidden topbar (clipped fixed popovers)
    topbar_i = html.find('class="topbar"')
    meta_i = html.find('id="meta-popover"')
    assert topbar_i >= 0 and meta_i >= 0
    assert meta_i > html.find("</header>", topbar_i), "meta-popover must be outside topbar"


def test_css_meta_popover_not_clipped() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "position: fixed" in css
    assert ".meta-popover" in css
    # High z-index above topbar (10) / rail (40)
    assert "z-index: 240" in css or "z-index:240" in css


def test_js_no_wait_for_turn_session_switch() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "Wait for the current turn to finish before switching sessions." not in js
    assert "sessionViews" in js
    assert "session-pane" in js
    assert "showSessionPane" in js
    assert "withSessionTarget" in js
    assert "liveTurnSessionId" in js
    assert "showMetaPopover" in js or "meta-popover" in js
    assert "buildTopbarBubbleText" in js or "topbarBubbleText" in js
    assert "turnProgressLabel" in js
    assert "scrollTranscriptToBottom" in js
    assert "_scrollRaf" in js
    # Skeptic fixes: composer grow re-sticks; history batch suppress; skip mid-turn attach
    assert "_suppressStickyScroll" in js
    assert "skipAttachMidTurn" in js
    assert "livePromptSessionId" in js
    assert "promptSessionId" in js
    assert "Keep the session the user clicked" in js
    assert "if (state.stickToBottom) scrollIfSticky()" in js
    assert "clampHorizontalScroll" in js
    assert "preventScroll: true" in js
    assert "idleTurnLabel" in js
    assert "countResidualInPane" in js
    assert "markStalePlanItems" in js
    assert "livePromptSessionId" in js
    # autoGrow must re-stick after height change (same turn as resize)
    auto_idx = js.find("function autoGrow")
    assert auto_idx >= 0
    # Next top-level sibling after autoGrow closes
    close_idx = js.find("\n  function setRailTab", auto_idx)
    assert close_idx > auto_idx
    auto_chunk = js[auto_idx:close_idx]
    assert "if (state.stickToBottom) scrollIfSticky()" in auto_chunk


def test_css_meta_popover_and_turn_live() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".meta-popover" in css
    assert ".session-pane" in css
    assert ".session-row.turn-live" in css


def test_js_scroll_ignore_held_across_raf() -> None:
    """scrollTranscriptToBottom must keep _ignoreScroll until after nested rAF."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    idx = js.find("function scrollTranscriptToBottom")
    assert idx >= 0
    chunk = js[idx : idx + 700]
    assert "state._ignoreScroll = true" in chunk
    # Must not clear ignore on the same rAF tick as the first scrollTop set
    # (nested requestAnimationFrame clears it).
    assert chunk.count("requestAnimationFrame") >= 2
    assert "state._ignoreScroll = false" in chunk
