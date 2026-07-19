"""UX continuity helpers + structural checks for session panes, spellcheck, topbar bubble."""

from __future__ import annotations

import json
from pathlib import Path

from hub.ui_ux import (
    apply_goal_tool_input,
    elapsed_seconds_from_wall,
    format_goal_elapsed,
    goal_banner_text,
    idle_turn_label,
    parse_goal_slash,
    pick_turn_age_seconds,
    residual_status_parts,
    session_list_progress_hint,
    should_mark_plan_stale,
    should_scroll_to_bottom,
    topbar_bubble_lines,
    topbar_bubble_text,
    turn_progress_label,
    wall_ms_from_age_seconds,
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


def test_js_session_id_chip_copyable() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert 'id="chat-session-id"' in html
    assert "chatSessionId" in js
    assert "copySessionId" in js
    assert "shortSessionId" in js
    assert "chip-session-id" in css
    assert '["Session", sid]' in js or '["Session",' in js


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

    # Contract: quiet + open tools → still "running" (mid-tool wait, not bare quiet).
    open_tools = turn_progress_label(
        running=True,
        quiet=True,
        tool_open=True,
        tool="read_file",
        elapsed_s=45,
    )
    assert "running" in open_tools
    assert "quiet" not in open_tools
    assert "read_file" in open_tools
    assert "45s" in open_tools

    # Long activity one-liner still surfaces in the strip (tool= may be full line).
    long_tool = "read_file · D:/Projects/Grok Remote Hub/static/app.js · lines 100-200"
    long_line = turn_progress_label(
        running=True,
        tool=long_tool,
        model="grok",
        elapsed_s=8,
    )
    assert "running" in long_line
    assert "read_file" in long_line
    assert "8s" in long_line
    assert "grok" in long_line
    sub_line = turn_progress_label(
        running=True,
        tool="subagent · thinking · planning next step",
        elapsed_s=3,
    )
    assert "subagent" in sub_line
    assert "thinking" in sub_line


def test_turn_strip_inside_composer_shell() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    shell_i = html.find('class="composer-shell"')
    assert shell_i >= 0
    form_i = html.find('id="composer-form"', shell_i)
    strip_i = html.find('id="turn-strip"', shell_i)
    palette_i = html.find('id="slash-palette"', shell_i)
    assert strip_i >= 0, "turn-strip must live inside composer-shell"
    assert form_i > strip_i, "turn-strip must appear before composer-form"
    assert palette_i > strip_i, "slash-palette must follow turn-strip in shell"
    # No turn-strip before composer-shell (old top-of-chat placement)
    assert html.find('id="turn-strip"') == strip_i
    assert "position: absolute" in css
    # Slash palette anchored to top of composer-shell
    assert "bottom: 100%" in css or "bottom:100%" in css
    slash_block = css[css.find(".slash-palette") : css.find(".slash-palette") + 500]
    assert "position: absolute" in slash_block
    assert "bottom: 100%" in slash_block or "bottom:100%" in slash_block
    assert "lastActivityLine" in js
    assert "feedParentActivityFromChild" in js
    assert "activityLineForSession" in js
    assert "formatActivityLine" in js


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


def test_composer_placeholder_responsive() -> None:
    """Short default placeholder; JS swaps full form by width; CSS ellipsizes."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")

    # Default attribute is short (no long parenthetical baked into HTML)
    assert 'placeholder="Message…"' in html
    assert 'placeholder="Message… (/ for commands)"' not in html

    assert "updateComposerPlaceholder" in js
    assert 'PLACEHOLDER_SHORT = "Message…"' in js or 'PLACEHOLDER_SHORT="Message…"' in js
    assert "PLACEHOLDER_FULL" in js
    assert "/ for commands" in js

    assert "composer-input::placeholder" in css or "text-overflow: ellipsis" in css


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
    # Residual strip must ignore history: only count plan/tool rows after last user line
    residual_idx = js.find("function countResidualInPane")
    assert residual_idx >= 0
    residual_end = js.find("\n  function markStalePlanItems", residual_idx)
    assert residual_end > residual_idx
    residual_chunk = js[residual_idx:residual_end]
    assert ".term-line.user" in residual_chunk
    assert "DOCUMENT_POSITION_FOLLOWING" in residual_chunk
    assert "afterLastUser" in residual_chunk
    assert "lastUser" in residual_chunk
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


def test_js_scroll_if_sticky_rate_limit() -> None:
    """scrollIfSticky rate-limits non-forced sticky scrolls (~10/s)."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    idx = js.find("function scrollIfSticky")
    assert idx >= 0
    chunk = js[idx : idx + 900]
    assert "_lastStickyScrollAt" in chunk
    assert "90" in chunk
    assert "force" in chunk



def test_server_boot_id_in_health_and_status() -> None:
    """Hub exposes bootId/startedAt on process start for restart detection."""
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    assert "self.boot_id = secrets.token_hex(8)" in src
    assert "self.started_at = time.time()" in src
    assert '"bootId": self.boot_id' in src
    assert '"startedAt": self._started_at_iso()' in src
    # Both health and status_payload paths
    assert "async def handle_health" in src
    assert "def status_payload" in src
    assert "def _started_at_iso" in src


def test_js_resume_after_reconnect_scroll_freeze() -> None:
    """Reconnect must freeze sticky scroll and resume with one final jump."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function resumeAfterReconnect" in js
    assert "_reconnectScrollFreeze" in js
    assert "state._reconnectScrollFreeze && !force" in js
    assert "Hub reconnected" in js
    # Open handler uses resume path (not naive refreshHistory-only)
    open_idx = js.find('ws.addEventListener("open"')
    assert open_idx >= 0
    open_chunk = js[open_idx : open_idx + 500]
    assert "resumeAfterReconnect" in open_chunk
    assert "wasReconnect" in open_chunk
    # applyHistoryMessages skips mid-freeze jumps
    apply_idx = js.find("function applyHistoryMessages")
    apply_chunk = js[apply_idx : apply_idx + 1400]
    assert "state._reconnectScrollFreeze" in apply_chunk


def test_js_multi_session_resume_after_reconnect() -> None:
    """Reconnect resumes all mid-turn sessions, not only the selected one."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function collectLiveSessionIds" in js
    assert "function hydrateSessionHistory" in js
    assert "function hydrateSessionPane" in js
    assert "function ensureLiveSessionsResumed" in js
    assert "function mergeHealthIntoState" in js
    # collectLiveSessionIds covers liveTurns / flags / pending questions
    collect_idx = js.find("function collectLiveSessionIds")
    assert collect_idx >= 0
    collect_chunk = js[collect_idx : collect_idx + 900]
    assert "liveTurns" in collect_chunk
    assert "sessionFlags" in collect_chunk
    assert "pendingQuestionSessions" in collect_chunk
    # resumeAfterReconnect loops over all resume ids and hydrates offscreen
    resume_idx = js.find("async function resumeAfterReconnect")
    if resume_idx < 0:
        resume_idx = js.find("function resumeAfterReconnect")
    assert resume_idx >= 0
    resume_chunk = js[resume_idx : resume_idx + 7000]
    assert "collectLiveSessionIds" in resume_chunk
    assert "hydrateSessionHistory" in resume_chunk
    assert "mergeHealthIntoState" in resume_chunk
    assert "_reconnectScrollFreeze" in resume_chunk
    assert "live project" in resume_chunk
    # status path can light-resume new live sessions after reconnect
    assert "ensureLiveSessionsResumed" in js


def test_js_stream_parity_thought_and_tools() -> None:
    """Live stream shows thinking panels and tool detail closer to CLI."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")

    # extractText recurses into arrays / nested content objects
    extract_idx = js.find("function extractText")
    assert extract_idx >= 0
    extract_chunk = js[extract_idx : extract_idx + 900]
    assert "Array.isArray(content)" in extract_chunk
    assert "content.content" in extract_chunk
    assert "content.text" in extract_chunk
    assert "map(extractText)" in extract_chunk or "extractText(content.content)" in extract_chunk

    # Thought prefix is muted ·; status text lives only in thought-summary-label
    assert 'if (r === "thought") return "Thinking:"' not in js
    assert 'label.textContent = opts.stream ? "Thinking…" : "Thinking"' in js
    assert 'if (label) label.textContent = "Thinking"' in js
    assert 'if (label) label.textContent = "Thinking…"' in js

    # New thought "screen" after tools / before assistant reply
    tool_call_idx = js.find('if (kind === "tool_call")')
    assert tool_call_idx >= 0
    tool_call_chunk = js[tool_call_idx : tool_call_idx + 500]
    assert "markThoughtComplete" in tool_call_chunk
    assert "thoughtEl = null" in tool_call_chunk

    msg_idx = js.find('if (kind === "agent_message_chunk")')
    assert msg_idx >= 0
    msg_chunk = js[msg_idx : msg_idx + 450]
    assert "markThoughtComplete" in msg_chunk
    assert "thoughtEl = null" in msg_chunk

    # Thought chunks force open + _rawText append
    thought_idx = js.find('if (kind === "agent_thought_chunk")')
    assert thought_idx >= 0
    thought_chunk = js[thought_idx : thought_idx + 900]
    assert "el.open = true" in thought_chunk
    assert "body._rawText" in thought_chunk
    assert "open: true" in thought_chunk

    # Tools collapsed by default (never auto-open for running/pending)
    create_idx = js.find("function createToolLine")
    assert create_idx >= 0
    create_chunk = js[create_idx : create_idx + 900]
    assert "row.open = false" in create_chunk
    assert "row.open = true" not in create_chunk
    assert "setToolDetailBody" in js
    assert "No detail" not in js

    assert "extractToolContentSnippet(update, 8000)" in js
    assert "function extractToolContentSnippet(update, limit = 120)" in js
    # Broader ACP payload shapes for tool detail
    assert "rawOutput" in js
    assert "raw_output" in js
    assert "snippetFromToolValue" in js

    # Subagent activity lines
    assert 'kind === "subagent_spawned"' in js
    assert 'kind === "subagent_finished"' in js

    # Thought panel CSS is prominent
    assert ".term-line.thought" in css
    assert ".thought-summary-label" in css
    assert "font-size: 13px" in css


def test_js_process_restart_clears_stale_live_state() -> None:
    """After hub process restart, client must drop mid-turn/queue/quiet UI."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function clearStaleLiveTurns" in js
    assert "function clearLiveClientStateAfterProcessRestart" in js
    assert "_hubProcessRestarted" in js
    assert "function snapshotLiveClientForRestart" in js
    assert "function snapshotHadLive" in js
    assert "function saveLastPrompt" in js
    assert "function loadLastPrompt" in js
    assert "function offerInterruptedResend" in js
    assert "function resendLastPrompt" in js

    # bootId change snapshots then clears live state and flags process restart
    note_idx = js.find("function noteBootId")
    assert note_idx >= 0
    note_chunk = js[note_idx : note_idx + 1200]
    assert "snapshotLiveClientForRestart" in note_chunk
    assert "snapshotHadLive" in note_chunk
    assert "_pendingRestartInterrupt" in note_chunk
    assert "clearLiveClientStateAfterProcessRestart" in note_chunk
    assert "bootId changed" in note_chunk
    assert "_hubProcessRestarted" in note_chunk
    # One hard reload per new bootId (sessionStorage) — no mixed old JS / new Python
    assert "grh.bootReload." in note_chunk
    assert "sessionStorage" in note_chunk
    assert "location.reload" in note_chunk

    # clearStaleLiveTurns wipes turns, queue, flags, stall; optional questions
    clear_idx = js.find("function clearStaleLiveTurns")
    assert clear_idx >= 0
    clear_chunk = js[clear_idx : clear_idx + 1600]
    assert "liveTurns" in clear_chunk
    assert "promptQueueLength" in clear_chunk
    assert "turnStartedAt" in clear_chunk
    assert "clearStallWatch" in clear_chunk
    assert "sessionFlags" in clear_chunk
    assert "markStalePlanItems" in clear_chunk
    assert "clearQuestions" in clear_chunk
    assert "closeAskUserModal" in clear_chunk
    # Must not touch composer drafts
    assert "composerDraft" not in clear_chunk

    # resumeAfterReconnect handles process restart vs soft stale reconnect
    resume_idx = js.find("async function resumeAfterReconnect")
    if resume_idx < 0:
        resume_idx = js.find("function resumeAfterReconnect")
    assert resume_idx >= 0
    resume_chunk = js[resume_idx : resume_idx + 7000]
    assert "_hubProcessRestarted" in resume_chunk
    assert "clearLiveClientStateAfterProcessRestart" in resume_chunk
    assert "interrupted" in resume_chunk
    assert "reportError" in resume_chunk
    assert "interruptedByRestart" in resume_chunk
    # Snapshot + last prompt BEFORE mergeHealthIntoState (bootId clear race)
    pre_merge = resume_chunk.split("mergeHealthIntoState")[0]
    assert "snapshotLiveClientForRestart" in pre_merge or "preSnap" in pre_merge
    assert "loadLastPrompt" in pre_merge
    assert "hadLiveBeforeClear" in resume_chunk
    assert "offerInterruptedResend" in resume_chunk
    assert "Hub restarted · reconnected" in resume_chunk
    # Auto-attach selected after process restart (session/load without re-open)
    assert "attachSessionLive" in resume_chunk
    # Soft path clears stale live without killing pending questions
    assert "clearStaleLiveTurns" in resume_chunk
    assert "clearQuestions: false" in resume_chunk


def test_js_sticky_user_prompt_collapse_toggle() -> None:
    """Sticky You: one-line collapse by default; expand/collapse on click."""
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
    assert "function setActivePromptCollapsed" in js
    assert "function toggleActivePromptExpand" in js
    assert "is-collapsed" in js
    assert "toggleActivePromptExpand" in js
    assert ".term-line.user.active-prompt.is-collapsed" in css
    assert "white-space: nowrap" in css
    assert "tap to expand" not in css
    assert "tap to collapse" not in css
    assert "display: flex" in css


def test_js_last_prompt_resend_and_optimistic_user() -> None:
    """Persist last prompt, one-tap Resend, optimistic user bubble on submit."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")

    assert "grh.lastPrompt.v1" in js
    assert "function saveLastPrompt" in js
    assert "function loadLastPrompt" in js
    assert "function clearLastPromptPending" in js
    assert "function markLastPromptPending" in js
    assert "function resendLastPrompt" in js
    assert "function offerInterruptedResend" in js
    assert "Hub restarted — turn interrupted" in js or "Hub restarted: live turn interrupted" in js
    assert 'actionLabel: "Resend"' in js or "actionLabel: 'Resend'" in js

    # submitPrompt saves last prompt and optimistic user bubble
    submit_idx = js.find("function submitPrompt")
    assert submit_idx >= 0
    submit_chunk = js[submit_idx : submit_idx + 1800]
    assert "saveLastPrompt" in submit_chunk
    assert "appendMessage" in submit_chunk
    assert 'role: "user"' in submit_chunk or "role: 'user'" in submit_chunk

    # processUserMessageChunk dedupes identical user text
    umc_idx = js.find("function processUserMessageChunk")
    assert umc_idx >= 0
    umc_chunk = js[umc_idx : umc_idx + 900]
    assert "existing === text" in umc_chunk

    # Error strip Resend control
    assert "btn-error-resend" in html
    assert "btnErrorResend" in js
    assert "toast-with-action" in css or "toast-action" in css


def pick_user_prompt_index(tops: list[float], anchor: float) -> int:
    """Mirror of app.js pickUserPromptIndex: last index with tops[i] <= anchor, else -1."""
    idx = -1
    for i, t in enumerate(tops):
        if t <= anchor:
            idx = i
    return idx


def test_pick_user_prompt_index_algorithm() -> None:
    """Pure contract for scroll-linked sticky user prompt selection."""
    assert pick_user_prompt_index([], 100) == -1
    assert pick_user_prompt_index([10, 50, 90], 0) == -1
    assert pick_user_prompt_index([10, 50, 90], 10) == 0
    assert pick_user_prompt_index([10, 50, 90], 49) == 0
    assert pick_user_prompt_index([10, 50, 90], 50) == 1
    assert pick_user_prompt_index([10, 50, 90], 89) == 1
    assert pick_user_prompt_index([10, 50, 90], 90) == 2
    assert pick_user_prompt_index([10, 50, 90], 999) == 2
    # Equal tops still advance to last qualifying index
    assert pick_user_prompt_index([20, 20, 40], 20) == 1


def test_js_active_user_prompt_sticky() -> None:
    """CLI-like active You: line: sticky pin while turn runs, clear when idle."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")

    assert "function activateUserPrompt" in js
    assert "function clearActiveUserPrompt" in js
    assert "function scrollActivePromptToTop" in js
    assert "active-prompt" in js
    assert "activeUserEl" in js

    # CSS sticky pin for active user line (collapsed one-line by default)
    assert ".term-line.user.active-prompt" in css
    assert "position: sticky" in css
    assert ".term-line.user.active-prompt .term-body" in css
    assert "is-collapsed" in css
    assert "white-space: nowrap" in css

    # submitPrompt activates only on !alreadyRunning (not queue-only echoes)
    submit_idx = js.find("function submitPrompt")
    assert submit_idx >= 0
    submit_chunk = js[submit_idx : submit_idx + 2200]
    assert "activateUserPrompt" in submit_chunk
    assert "alreadyRunning" in submit_chunk
    assert "scrollToTop: true" in submit_chunk

    # processUserMessageChunk: new line + exact dedupe while running
    umc_idx = js.find("function processUserMessageChunk")
    assert umc_idx >= 0
    umc_chunk = js[umc_idx : umc_idx + 1600]
    assert "activateUserPrompt" in umc_chunk
    assert "state.turnRunning" in umc_chunk

    # Idle / turn end clears active prompt
    set_idx = js.find("function setTurnRunning")
    assert set_idx >= 0
    set_chunk = js[set_idx : set_idx + 2200]
    assert "clearActiveUserPrompt" in set_chunk

    clear_idx = js.find("function clearStaleLiveTurns")
    assert clear_idx >= 0
    clear_chunk = js[clear_idx : clear_idx + 1600]
    assert "clearActiveUserPrompt" in clear_chunk


def test_js_scroll_linked_sticky_user_prompt() -> None:
    """Scroll-linked sticky: pick by anchor; live stickToBottom pins latest user."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "function pickUserPromptIndex" in js
    assert "function syncStickyUserFromScroll" in js
    assert "function scheduleStickyUserFromScroll" in js
    # Algorithm contract (mirrors pick_user_prompt_index)
    pick_idx = js.find("function pickUserPromptIndex")
    assert pick_idx >= 0
    pick_chunk = js[pick_idx : pick_idx + 400]
    assert "return idx" in pick_chunk
    assert "tops[i] <= anchorY" in pick_chunk

    sync_idx = js.find("function syncStickyUserFromScroll")
    assert sync_idx >= 0
    sync_chunk = js[sync_idx : sync_idx + 1800]
    assert "stickToBottom" in sync_chunk
    assert "turnRunningOnSelected" in sync_chunk
    assert "scrollTop + 56" in sync_chunk
    assert "pickUserPromptIndex" in sync_chunk
    assert 'activateUserPrompt(last, { scrollToTop: false })' in sync_chunk or (
        "scrollToTop: false" in sync_chunk and "activateUserPrompt" in sync_chunk
    )
    assert "idx < 0" in sync_chunk  # pin first when above all

    # Scroll listener schedules sticky sync alongside jump-latest
    assert "scheduleStickyUserFromScroll()" in js
    scroll_listener = 'els.transcript.addEventListener("scroll"'
    sl_idx = js.find(scroll_listener)
    assert sl_idx >= 0
    sl_chunk = js[sl_idx : sl_idx + 200]
    assert "updateJumpLatest" in sl_chunk
    assert "scheduleStickyUserFromScroll" in sl_chunk

    # Wired after history / openSession / jump settle
    assert "applyHistoryMessages" in js
    ah_idx = js.find("function applyHistoryMessages")
    ah_chunk = js[ah_idx : ah_idx + 1200]
    assert "scheduleStickyUserFromScroll" in ah_chunk

    open_idx = js.find("async function openSession")
    open_chunk = js[open_idx : open_idx + 5500]
    assert "scheduleStickyUserFromScroll" in open_chunk

    # Exposed for tests
    hooks_idx = js.find("window.__hubTestHooks")
    assert hooks_idx >= 0
    hooks_chunk = js[hooks_idx : hooks_idx + 2500]
    assert "pickUserPromptIndex" in hooks_chunk
    assert "syncStickyUserFromScroll" in hooks_chunk


def test_js_attach_session_live_helper() -> None:
    """attachSessionLive shared by openSession and resume-after-restart."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function attachSessionLive" in js
    attach_idx = js.find("async function attachSessionLive")
    if attach_idx < 0:
        attach_idx = js.find("function attachSessionLive")
    assert attach_idx >= 0
    attach_chunk = js[attach_idx : attach_idx + 1800]
    assert "/attach" in attach_chunk
    assert "liveSessionId" in attach_chunk
    open_idx = js.find("async function openSession")
    assert open_idx >= 0
    open_chunk = js[open_idx : open_idx + 8000]
    assert "attachSessionLive" in open_chunk

    # Status trusts empty server liveTurns (no forever quiet · queue)
    status_idx = js.find('if (type === "status")')
    assert status_idx >= 0
    status_chunk = js[status_idx : status_idx + 9000]
    assert "msg.liveTurns.length === 0" in status_chunk
    assert "clearStaleLiveTurns" in status_chunk
    assert "all: true" in status_chunk


def test_js_report_error_and_error_strip() -> None:
    """Hub errors are durable: console log, errorLog, toast, persistent strip."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "function reportError" in js
    assert "function reportInfo" in js
    assert "function isRecoverableTurnClear" in js
    assert "errorLog" in js
    assert "updateErrorStrip" in js
    assert "error-strip" in html
    assert 'id="error-strip"' in html
    assert "btn-error-dismiss" in html
    assert "btn-error-copy" in html
    assert ".error-strip" in css
    assert ".error-strip.info" in css
    # Recoverable turn-clear regex covers stall / send again / auto-retry
    rec_idx = js.find("function isRecoverableTurnClear")
    assert rec_idx >= 0
    rec_chunk = js[rec_idx : rec_idx + 450]
    assert "send again" in rec_chunk
    assert "stalled mid-turn" in rec_chunk
    assert "no activity" in rec_chunk
    assert "recovering" in rec_chunk
    assert "retrying" in rec_chunk
    # reportInfo: non-danger toast + info strip + 12s auto-dismiss
    info_idx = js.find("function reportInfo")
    assert info_idx >= 0
    info_chunk = js[info_idx : info_idx + 900]
    assert 'level: "info"' in info_chunk
    assert "6000" in info_chunk
    assert "updateErrorStrip" in info_chunk
    strip_idx = js.find("function updateErrorStrip")
    strip_chunk = js[strip_idx : strip_idx + 2000]
    assert 'classList.toggle("info"' in strip_chunk or 'classList.toggle("info",' in strip_chunk
    assert "12000" in strip_chunk
    # type===error: hard failures reportError; recovering/soft use reportInfo
    err_idx = js.find('if (type === "error")')
    assert err_idx >= 0
    err_chunk = js[err_idx : err_idx + 900]
    assert "reportError" in err_chunk
    assert "reportInfo" in err_chunk
    assert "recovering" in err_chunk
    assert "reportError(msg.error" in js or 'reportError(msg.error' in js
    # danger toasts last longer than info toasts
    toast_idx = js.find("function toast(")
    toast_chunk = js[toast_idx : toast_idx + 1600]
    assert "8000" in toast_chunk
    assert "4200" in toast_chunk


def test_js_turn_idle_clears_selected_strip() -> None:
    """After turn idle (stall clear), selected session strip must go idle."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    turn_idx = js.find('if (type === "turn")')
    assert turn_idx >= 0
    turn_chunk = js[turn_idx : turn_idx + 4500]
    assert 'msg.state === "idle"' in turn_chunk
    assert "isRecoverableTurnClear" in turn_chunk
    assert "reportInfo" in turn_chunk
    assert "forceIdleFlags" in turn_chunk
    assert "turnStartedAt = null" in turn_chunk
    assert "lastTermLineAt = null" in turn_chunk
    assert "clearStallWatch" in turn_chunk
    assert "turnRunningOnSelected" in turn_chunk
    assert 'role: "system"' in turn_chunk
    assert "updateTurnStrip" in turn_chunk
    # Explicit idle flag must beat stale sessions-list liveStatus
    status_fn = js.find("function sessionLiveStatus")
    assert status_fn >= 0
    status_chunk = js[status_fn : status_fn + 1600]
    idle_flag = status_chunk.find('flags[sessionId] === "idle"')
    row_status = status_chunk.find("row.liveStatus")
    assert idle_flag >= 0
    assert row_status >= 0
    assert idle_flag < row_status


def test_merge_stream_text_algorithm() -> None:
    """Cumulative / delta / mixed / overlap matrix (mirror of static/app.js)."""
    from hub.ui_format import merge_stream_text

    # Cumulative: "a" then "ab" then "abc" → "abc" not "aababc"
    body = ""
    for snap in ("a", "ab", "abc"):
        body = merge_stream_text(body, snap)
    assert body == "abc"

    # Pure deltas: "a"+" b" → "a b"; "a"+"b" → "ab"
    assert merge_stream_text("a", " b") == "a b"
    assert merge_stream_text("a", "b") == "ab"
    assert merge_stream_text(merge_stream_text("", "a"), "b") == "ab"

    # Ignore redundant shorter or equal snapshot
    assert merge_stream_text("abc", "ab") == "abc"
    assert merge_stream_text("abc", "abc") == "abc"
    assert merge_stream_text("hello", "") == "hello"
    assert merge_stream_text("", "x") == "x"
    assert merge_stream_text(None, "hi") == "hi"
    assert merge_stream_text("hi", None) == "hi"
    assert merge_stream_text(None, None) == ""

    # Mixed / redundant suffix (double delivery without full cumulative)
    assert merge_stream_text("I will", " will") == "I will"
    assert merge_stream_text("I", "I") == "I"
    assert merge_stream_text("I", " I") == "I"

    # Longest overlap: suffix of prev == prefix of chunk
    assert merge_stream_text("hello", "lo world") == "hello world"
    assert merge_stream_text("hel", "ello") == "hello"
    assert merge_stream_text("hel", "lo") == "helo"  # only "l" overlaps

    # Leading-space delta already present at end
    assert merge_stream_text("word ", " word") == "word "


def test_js_merge_stream_text_contract() -> None:
    """mergeStreamText exists and is used by appendToBody + thought stream path."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    fn_idx = js.find("function mergeStreamText")
    assert fn_idx >= 0
    fn = js[fn_idx : fn_idx + 900]
    assert "prev" in fn and "chunk" in fn
    assert "startsWith" in fn
    assert "endsWith" in fn
    assert "return p + c" in fn or "return p+c" in fn

    app_idx = js.find("function appendToBody")
    assert app_idx >= 0
    app_fn = js[app_idx : app_idx + 900]
    assert "mergeStreamText(prev, text)" in app_fn
    assert "const next = prev + text" not in app_fn
    # Optional 50ms identical-chunk dedupe
    assert "_lastChunk" in app_fn
    assert "50" in app_fn

    thought_idx = js.find('if (kind === "agent_thought_chunk")')
    assert thought_idx >= 0
    thought_chunk = js[thought_idx : thought_idx + 1200]
    assert "mergeStreamText(prev, text)" in thought_chunk
    assert '(body._rawText || body.textContent || "") + text' not in thought_chunk


def test_js_stream_visibility_contract() -> None:
    """ACP stream chunks must paint transcript; optimistic user on submit; e2e hooks."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    # Live stream path: agent message + thought chunks
    msg_idx = js.find('if (kind === "agent_message_chunk")')
    assert msg_idx >= 0
    msg_chunk = js[msg_idx : msg_idx + 500]
    assert "appendMessage" in msg_chunk
    assert 'role: "assistant"' in msg_chunk or "role: 'assistant'" in msg_chunk

    thought_idx = js.find('if (kind === "agent_thought_chunk")')
    assert thought_idx >= 0
    thought_chunk = js[thought_idx : thought_idx + 700]
    assert "appendMessage" in thought_chunk
    assert 'role: "thought"' in thought_chunk or "role: 'thought'" in thought_chunk
    assert "open: true" in thought_chunk

    # handleAcpMessage marks working on stream kinds and dispatches paint
    handle_idx = js.find("function handleAcpMessage")
    assert handle_idx >= 0
    handle_chunk = js[handle_idx : handle_idx + 3500]
    assert "markSessionActivity" in handle_chunk
    assert "agent_message_chunk" in handle_chunk
    assert "agent_thought_chunk" in handle_chunk
    assert '"working"' in handle_chunk
    assert "processAcpSessionUpdate" in handle_chunk

    # Optimistic user bubble on submit (instant feedback, not wait for ACP echo)
    submit_idx = js.find("function submitPrompt")
    assert submit_idx >= 0
    submit_chunk = js[submit_idx : submit_idx + 1800]
    assert "appendMessage" in submit_chunk
    assert 'role: "user"' in submit_chunk or "role: 'user'" in submit_chunk

    # E2E inject hooks: same live path, no auth bypass
    assert "window.__hubTestHooks" in js
    hooks_idx = js.find("window.__hubTestHooks")
    assert hooks_idx >= 0
    hooks_chunk = js[hooks_idx : hooks_idx + 1600]
    assert "injectAcpSessionUpdate" in hooks_chunk
    assert "handleAcpMessage" in hooks_chunk
    assert "transcriptTextIncludes" in hooks_chunk
    assert "transcriptHasRole" in hooks_chunk
    assert "turnStripText" in hooks_chunk
    assert "setSelectedForTest" in hooks_chunk
    assert "showSessionPane" in hooks_chunk


def test_server_emit_error_logs_client_errors() -> None:
    """Every client error path should log via _emit_error (hub daily log)."""
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    assert "async def _emit_error" in src
    assert "client_error session=%s" in src
    assert "turn_error session=%s" in src
    # Prefer helper over bare error broadcasts
    assert 'await self._emit_error' in src
    assert 'await self.broadcast({"type": "error"' not in src


def test_restart_hub_ps1_wait_and_nowait() -> None:
    """restart-hub.ps1 waits for new bootId by default; -NoWait keeps fire-and-forget."""
    ps1 = (ROOT / "restart-hub.ps1").read_text(encoding="utf-8")
    assert "[switch]$NoWait" in ps1 or "NoWait" in ps1
    assert "preBootId" in ps1
    assert "bootId" in ps1
    assert "restart-status.json" in ps1
    assert "Waiting for hub restart" in ps1
    assert "Hub restarted and healthy" in ps1
    assert "Get-HubHealth" in ps1 or "Invoke-RestMethod" in ps1
    assert "if ($NoWait)" in ps1
    # Default bounce keeps agent serve (one-live-per-cwd continuity); KillAgent opt-out
    assert "KeepAgent" in ps1
    assert "KillAgent" in ps1
    assert '" -KeepAgent"' in ps1 or " -KeepAgent" in ps1


def test_js_session_live_status_pending_question_priority() -> None:
    """pendingQuestionSessions must win over sessionFlags idle/working."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    idx = js.find("function sessionLiveStatus")
    assert idx >= 0
    chunk = js[idx : idx + 1200]
    # pending checked before returning flags idle/working
    pending_idx = chunk.find("pendingQuestionSessions")
    flags_working_idx = chunk.find('flags[sessionId] === "working"')
    flags_idle_idx = chunk.find('flags[sessionId] === "idle"')
    assert pending_idx >= 0
    assert flags_working_idx >= 0
    assert pending_idx < flags_working_idx
    # idle flag must not be returned before pending check
    assert flags_idle_idx < 0 or pending_idx < flags_idle_idx
    # early return on pending
    assert 'return "question"' in chunk
    assert "pending.indexOf(sessionId) >= 0" in chunk


def test_js_question_pill_needs_reply() -> None:
    """Session rail shows a clear Needs reply pill for pending questions."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "Needs reply" in js
    # renderSessions path uses status-question pill
    render_idx = js.find("function renderSessions")
    assert render_idx >= 0
    render_chunk = js[render_idx : render_idx + 4500]
    assert "status-question" in render_chunk
    assert "Needs reply" in render_chunk
    # question sessions sort above working
    assert 'st === "question"' in render_chunk or 'liveStatus === "question"' in render_chunk
    assert ".session-pill.status-question" in css
    assert ".session-row.status-question" in css


def test_js_hub_session_pill() -> None:
    """Non-subagent sessions show source pills: Hub (!isCli) or CLI (isCli)."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    render_idx = js.find("function renderSessions")
    assert render_idx >= 0
    render_chunk = js[render_idx : render_idx + 5000]
    assert "!s.isCli" in render_chunk
    assert "s.isCli" in render_chunk
    assert "session-pill hub" in render_chunk
    assert "session-pill cli" in render_chunk
    assert 'textContent = "Hub"' in render_chunk or "textContent = 'Hub'" in render_chunk
    assert 'textContent = "CLI"' in render_chunk or "textContent = 'CLI'" in render_chunk
    assert 'textContent = "live"' not in render_chunk
    assert ".session-pill.hub" in css
    assert ".session-pill.cli" in css


def test_js_status_merge_reapplies_question_for_pending() -> None:
    """Status broadcasts must re-apply question flag for pending sessions."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    status_idx = js.find('if (type === "status")')
    assert status_idx >= 0
    chunk = js[status_idx : status_idx + 3200]
    assert "sessionFlags" in chunk
    assert "pendingQuestionSessions" in chunk
    # force question flag for pending ids after server flags apply
    assert 'state.sessionFlags[pid] = "question"' in chunk
    assert "pendingIds" in chunk


def test_js_on_user_question_flags_rail() -> None:
    """onUserQuestion always flags session + schedule pills for rail notification."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    idx = js.find("function onUserQuestion")
    assert idx >= 0
    chunk = js[idx : idx + 1600]
    assert "pendingQuestionSessions" in chunk
    assert 'markSessionActivity(sessionId, "question")' in chunk
    assert "Waiting for your answer" in chunk
    # sessionId fallbacks when msg omits it
    assert "liveTurnId()" in chunk
    assert "turnSessionId" in chunk
    assert "selectedId" in chunk


def test_js_session_pills_near_streaming() -> None:
    """Session rail pills refresh via markSessionActivity + rAF sync, not only full rebuild."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function markSessionActivity" in js
    assert "function scheduleSessionPills" in js
    assert "function syncVisibleSessionPills" in js
    assert "function flushSessionPills" in js

    mark_idx = js.find("function markSessionActivity")
    assert mark_idx >= 0
    mark_chunk = js[mark_idx : mark_idx + 2200]
    assert '"working"' in mark_chunk
    assert '"question"' in mark_chunk
    assert '"idle"' in mark_chunk
    # Question must not be overwritten by working
    assert '!== "question"' in mark_chunk
    assert "scheduleSessionPills()" in mark_chunk
    assert "liveStatus" in mark_chunk

    sync_idx = js.find("function syncVisibleSessionPills")
    assert sync_idx >= 0
    sync_chunk = js[sync_idx : sync_idx + 2000]
    assert "data-session-id" in sync_chunk
    assert "status-working" in sync_chunk
    assert "status-question" in sync_chunk
    assert "Needs reply" in sync_chunk
    assert "Working" in sync_chunk

    # Rows carry data-session-id for in-place updates
    render_idx = js.find("function renderSessions")
    assert render_idx >= 0
    render_chunk = js[render_idx : render_idx + 2500]
    assert "data-session-id" in render_chunk

    # Stream path marks activity for offscreen sessions
    acp_idx = js.find("function handleAcpMessage")
    assert acp_idx >= 0
    acp_chunk = js[acp_idx : acp_idx + 2500]
    assert 'markSessionActivity(targetId, "working")' in acp_chunk
    assert "user_message_chunk" in acp_chunk
    assert "agent_message_chunk" in acp_chunk


def test_server_status_resync_near_streaming() -> None:
    """While turns run, status resync sleeps 1.0s (not 10s)."""
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    idx = src.find("async def _status_resync_loop")
    assert idx >= 0
    chunk = src[idx : idx + 1600]
    assert "1.0" in chunk
    assert "turn_running" in chunk
    # turn transitions also push status immediately
    bt_idx = src.find("async def _broadcast_turn")
    assert bt_idx >= 0
    bt_chunk = src[bt_idx : bt_idx + 1400]
    assert "status_payload()" in bt_chunk


def test_js_honest_agent_vs_acp_status_pill() -> None:
    """Pill distinguishes process-down from ACP-only disconnect."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    idx = js.find("function updateStatusPill")
    assert idx >= 0
    chunk = js[idx : idx + 2800]
    assert "agentProcess" in chunk
    assert "acpConnected" in chunk
    assert "Agent reconnecting" in chunk
    assert 'stateKey = "acp-down"' in chunk
    assert "Agent down" in chunk
    assert 'stateKey = "agent-down"' in chunk
    # Heal exhausted: process up, ACP down, stop saying reconnecting
    assert "Agent hung — restart" in chunk
    assert 'stateKey = "acp-hung"' in chunk
    assert "acpHealError" in chunk
    assert "acpHealAttempts" in chunk
    # Zombie / stale quality (half-open ACP must not show Connected green)
    assert "acp-zombie" in chunk
    assert "acpQuality" in chunk
    assert "ACP stale" in chunk
    assert "acp-stale" in chunk
    # Status merge carries new fields
    assert "agentProcess: msg.agentProcess" in js
    assert "acpConnected: msg.acpConnected" in js
    assert "agentDetail: msg.agentDetail" in js
    assert "acpQuality: msg.acpQuality" in js
    assert "acpHealAttempts" in js
    assert "acpHealError: msg.acpHealError" in js
    # Distinct warn style for ACP-only path; danger for hung
    assert 'data-state="acp-down"' in css
    assert 'data-state="acp-hung"' in css
    # Server wires helper into payload + health
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    assert "map_agent_status" in src
    assert "agentProcess" in src
    assert "agentDetail" in src
    assert "acpHealAttempts" in src
    assert "acpHealError" in src
    assert "acpQuality" in src
    assert "acp_liveness_snapshot" in src or "acpLastRecvAgeSeconds" in src


def test_restart_agent_admin_api_and_ui() -> None:
    """In-hub KillAgent-style restart: route, supervisor force_restart, hung pill click."""
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    sup = (ROOT / "hub" / "agent_supervisor.py").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")

    # API route + handler (thin wrapper) + shared restart helper
    assert '"/api/admin/restart-agent"' in src or "'/api/admin/restart-agent'" in src
    assert "handle_restart_agent" in src
    assert "async def _restart_agent_process" in src
    assert "force_restart" in src
    assert "_agent_restart_in_progress" in src
    # Handler is a thin HTTP wrapper around the helper
    handler_idx = src.find("async def handle_restart_agent")
    assert handler_idx >= 0
    handler = src[handler_idx : handler_idx + 2500]
    assert "_restart_agent_process" in handler
    assert "sys.exit" not in handler
    assert "os._exit" not in handler
    # Restart logic lives in _restart_agent_process (not hub process exit)
    helper_idx = src.find("async def _restart_agent_process")
    assert helper_idx >= 0
    helper = src[helper_idx : helper_idx + 4500]
    assert "force_clear_turn" in helper
    assert "force_restart" in helper
    assert "reconnect" in helper
    assert "_acp_heal_attempts = 0" in helper
    assert "sys.exit" not in helper
    assert "os._exit" not in helper

    # Supervisor force kill + restart (attached listener, not only _started_by_us)
    assert "async def force_kill_agent" in sup
    assert "async def force_restart" in sup
    assert "_pids_listening_on_port" in sup
    assert "taskkill" in sup
    fk_idx = sup.find("async def force_kill_agent")
    fk = sup[fk_idx : fk_idx + 2200]
    assert "_started_by_us" in fk
    assert "pid" in fk.lower()

    # UI: POSTs restart-agent; pill clickable when hung / agent-down
    assert "/api/admin/restart-agent" in js
    assert "function restartAgentFromPill" in js
    assert "restartingAgent" in js
    assert "Restarting agent…" in js or "Restarting agent" in js
    assert "Agent restarted" in js
    pill_idx = js.find("function updateStatusPill")
    pill = js[pill_idx : pill_idx + 3600]
    assert "status-pill-action" in pill or "status-pill-action" in js
    assert 'stateKey === "acp-hung"' in js or 'st === "acp-hung"' in js
    assert 'stateKey === "agent-down"' in js or 'st === "agent-down"' in js
    assert 'role", "button"' in js or "role=button" in js or 'setAttribute("role", "button")' in js
    assert "confirm" in js
    # Click + keyboard on status pill
    assert "restartAgentFromPill" in js
    bind_idx = js.find("function bindEvents")
    bind = js[bind_idx : bind_idx + 900]
    assert "statusPill" in bind
    assert "restartAgentFromPill" in bind
    # CSS affordance
    assert "status-pill-action" in css
    assert "is-restarting" in css


def test_capacity_banner_structural() -> None:
    """Capacity banner HTML/CSS/JS for multi-turn quiet / other-session busy."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id="capacity-banner"' in html
    assert 'id="capacity-banner-text"' in html
    assert ".capacity-banner" in css
    assert 'data-state="warn"' in css or '[data-state="warn"]' in css
    assert "function updateCapacityBanner" in js
    assert "Working ·" in js
    assert "waiting first token" in js
    assert "Busy on other session" in js
    assert "quiet " in js
    assert "tool open" in js
    # Soft language: no "stuck" in capacity banner path
    cap_idx = js.find("function updateCapacityBanner")
    assert cap_idx >= 0
    cap_chunk = js[cap_idx : cap_idx + 3200]
    assert "stuck" not in cap_chunk.lower()
    assert "silenceSeconds" in cap_chunk or "silence" in cap_chunk
    assert "sawUpdate" in cap_chunk
    assert "tool open" in cap_chunk
    # Status merge stores capacity / turn silence
    assert "msg.capacity" in js
    assert "turnSilenceSeconds" in js
    assert "updateCapacityBanner" in js
    # Called from turn strip + status path
    assert "updateCapacityBanner()" in js


def test_open_tool_wait_visibility_structural() -> None:
    """Mid-tool wait: heartbeat, openedAt, quiet suppress, status-only tool_call_update."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function tickOpenToolHeartbeat" in js
    assert "function ensureOpenToolHeartbeat" in js
    assert "function clearOpenToolHeartbeat" in js
    assert "function syncOpenToolHeartbeat" in js
    assert "OPEN_TOOL_HEARTBEAT_MS" in js
    assert "waiting · " in js
    assert "dataset.openedAt" in js
    assert "tool_open" in js
    assert "hasOpenTools" in js
    # Quiet suppress when tools open (strip path)
    strip_idx = js.find("function updateTurnStrip")
    assert strip_idx >= 0
    strip_chunk = js[strip_idx : strip_idx + 2800]
    assert "hasOpenTools" in strip_chunk
    assert "quietForLabel" in strip_chunk or "tool_open" in strip_chunk
    assert "syncOpenToolHeartbeat" in strip_chunk
    # Heartbeat must not fake server activity
    beat_idx = js.find("function tickOpenToolHeartbeat")
    beat_chunk = js[beat_idx : beat_idx + 1800]
    assert "noteTermLineActivity" not in beat_chunk
    # Status-only tool_call_update always notes activity + scheduleTurnStrip
    upd_idx = js.find('if (kind === "tool_call_update")')
    assert upd_idx >= 0
    upd_chunk = js[upd_idx : upd_idx + 1800]
    assert "noteTermLineActivity()" in upd_chunk
    assert "scheduleTurnStrip()" in upd_chunk
    assert "updateToolLine" in upd_chunk


def test_wall_ms_from_age_seconds() -> None:
    """Server age → client wall epoch ms; invalid age → None."""
    assert wall_ms_from_age_seconds(100000, 12.4) == 100000 - 12400
    assert wall_ms_from_age_seconds(100000, 0) == 100000
    assert wall_ms_from_age_seconds(100000, None) is None
    assert wall_ms_from_age_seconds(100000, -1) is None
    assert wall_ms_from_age_seconds(100000, float("nan")) is None


def test_elapsed_seconds_from_wall_recovers_age() -> None:
    """Round-trip: wall from age, then elapsed recovers whole seconds."""
    now = 1_700_000_000_000.0
    age = 12.4
    wall = wall_ms_from_age_seconds(now, age)
    assert wall is not None
    assert elapsed_seconds_from_wall(now, wall) == 12
    assert elapsed_seconds_from_wall(now, None) == 0
    assert elapsed_seconds_from_wall(now, now + 5000) == 0  # future start clamps


def test_pick_turn_age_seconds_prefers_matching_session() -> None:
    """Selected liveTurns entry wins over primary; primary used when only match."""
    turns = [
        {"sessionId": "a", "ageSeconds": 10.0},
        {"sessionId": "b", "ageSeconds": 42.5},
    ]
    assert (
        pick_turn_age_seconds(
            selected_session_id="b",
            live_turns=turns,
            primary_age=10.0,
            primary_session_id="a",
        )
        == 42.5
    )
    # Selected is primary: use primary_age when no matching entry age
    assert (
        pick_turn_age_seconds(
            selected_session_id="primary",
            live_turns=[],
            primary_age=7.2,
            primary_session_id="primary",
        )
        == 7.2
    )
    # Only one turn: fall back to that entry / primary
    assert (
        pick_turn_age_seconds(
            selected_session_id="other",
            live_turns=[{"sessionId": "x", "ageSeconds": 3.0}],
            primary_age=3.0,
            primary_session_id="x",
        )
        == 3.0
    )
    # Selected elsewhere with multi turns and no match: no primary apply
    assert (
        pick_turn_age_seconds(
            selected_session_id="z",
            live_turns=turns,
            primary_age=10.0,
            primary_session_id="a",
        )
        is None
    )


def test_js_server_turn_timers_structural() -> None:
    """Client seeds turn clocks from server age (survive refresh)."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function wallMsFromAgeSeconds" in js
    assert "function elapsedSecondsFromWall" in js
    assert "function pickTurnAgeSeconds" in js
    assert "function applyServerTurnTimers" in js
    assert "function seedToolOpenedAt" in js
    assert "applyServerTurnTimers(msg)" in js or "applyServerTurnTimers(j)" in js
    # Strip uses wall-clock helper, not raw Date.now()-only elapsed
    strip_idx = js.find("function updateTurnStrip")
    assert strip_idx >= 0
    strip_chunk = js[strip_idx : strip_idx + 1200]
    assert "elapsedSecondsFromWall" in strip_chunk
    # startStallWatch must not always overwrite seeded clocks
    stall_idx = js.find("function startStallWatch")
    assert stall_idx >= 0
    stall_chunk = js[stall_idx : stall_idx + 900]
    assert "if (!state.turnStartedAt)" in stall_chunk


def test_context_budget_banner_removed() -> None:
    """Heavy-session soft banner is gone; context UX is CLI CTX bar only."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "context-budget-banner" not in html
    assert "context-budget-banner" not in css
    assert "updateContextBudgetBanner" not in js
    assert "state.contextBudget" not in js
    assert "j.contextBudget" not in js
    assert "msg.contextBudget" not in js
    assert "Heavy session" not in js


def test_latency_hint_loading_and_large_session_structural() -> None:
    """Honest cold-attach + large-session first-reply hints (do not block Send)."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id="latency-hint-banner"' in html
    assert 'id="latency-hint-banner-text"' in html
    assert ".latency-hint-banner" in css
    assert "Loading session into agent…" in js
    assert "Large session — first reply may take longer" in js
    assert "function attachSessionLive" in js
    attach_idx = js.find("async function attachSessionLive")
    assert attach_idx >= 0
    attach_chunk = js[attach_idx : attach_idx + 2200]
    assert "attachingSessionId" in attach_chunk
    assert "updateLatencyHintBanner" in attach_chunk
    assert "function maybeShowLargeSessionHint" in js
    assert "LARGE_SESSION_TOKENS" in js
    # Soft language: loading hint must not say stuck
    assert "stuck" not in attach_chunk.lower()
    loading_line = [ln for ln in js.splitlines() if "Loading session into agent" in ln]
    assert loading_line
    assert "stuck" not in loading_line[0].lower()


def test_js_history_batch_depth() -> None:
    """History rebuilds batch scroll/turn-strip updates via begin/endHistoryBatch."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function beginHistoryBatch" in js
    assert "function endHistoryBatch" in js
    assert "_historyBatchDepth" in js
    assert "function scheduleTurnStrip" in js
    # renderHistory / applyHistoryMessages / hydrateSessionPane use batch helpers
    render_idx = js.find("function renderHistory")
    assert render_idx >= 0
    render_chunk = js[render_idx : render_idx + 1600]
    assert "beginHistoryBatch" in render_chunk
    assert "endHistoryBatch" in render_chunk
    apply_idx = js.find("function applyHistoryMessages")
    apply_chunk = js[apply_idx : apply_idx + 1600]
    assert "beginHistoryBatch" in apply_chunk
    assert "endHistoryBatch" in apply_chunk
    hydrate_idx = js.find("function hydrateSessionPane")
    hydrate_chunk = js[hydrate_idx : hydrate_idx + 1600]
    assert "beginHistoryBatch" in hydrate_chunk
    assert "endHistoryBatch" in hydrate_chunk
    # Tool paths coalesce turn strip
    assert "scheduleTurnStrip()" in js


def test_js_subscribed_sessions_skip_resubscribe() -> None:
    """Client tracks subscribedSessions and skips redundant subscribe sends."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "subscribedSessions" in js
    sub_idx = js.find("function subscribeSessionIds")
    assert sub_idx >= 0
    sub_chunk = js[sub_idx : sub_idx + 900]
    assert "subscribedSessions" in sub_chunk
    assert "force" in sub_chunk
    # Clear on WS close
    close_idx = js.find('ws.addEventListener("close"')
    assert close_idx >= 0
    close_chunk = js[close_idx : close_idx + 400]
    assert "subscribedSessions" in close_chunk
    assert "clear()" in close_chunk
    # Reconnect clears then re-subscribes
    resume_idx = js.find("async function resumeAfterReconnect")
    if resume_idx < 0:
        resume_idx = js.find("function resumeAfterReconnect")
    assert resume_idx >= 0
    resume_chunk = js[resume_idx : resume_idx + 4500]
    assert "subscribedSessions" in resume_chunk
    assert "clear()" in resume_chunk


def test_js_history_handler_skips_mid_turn() -> None:
    """WS history dump must not rebuild selected transcript while turn is running."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    hist_idx = js.find('if (type === "history")')
    assert hist_idx >= 0
    hist_chunk = js[hist_idx : hist_idx + 600]
    assert "turnRunningOnSelected()" in hist_chunk
    assert "applyHistoryMessages" in hist_chunk
    assert "hydrateSessionPane" in hist_chunk


def test_js_force_history_refresh_on_reconnect_idle_visibility() -> None:
    """Reconnect/visibility/idle force-refresh history so last disk messages appear."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    # refreshHistory accepts force; mid-turn guard respects opts.force
    rh_idx = js.find("async function refreshHistory")
    if rh_idx < 0:
        rh_idx = js.find("function refreshHistory")
    assert rh_idx >= 0
    rh_chunk = js[rh_idx : rh_idx + 1200]
    assert "opts.force" in rh_chunk
    assert "turnRunningOnSelected()" in rh_chunk
    assert "force: !!opts.force" in rh_chunk or "force: opts.force" in rh_chunk

    # applyHistoryMessages: force bypasses turnRunning guard
    apply_idx = js.find("function applyHistoryMessages")
    assert apply_idx >= 0
    apply_chunk = js[apply_idx : apply_idx + 900]
    assert "opts.force" in apply_chunk
    assert "turnRunningOnSelected()" in apply_chunk

    # resumeAfterReconnect finish path force-refreshes selected history
    resume_idx = js.find("async function resumeAfterReconnect")
    if resume_idx < 0:
        resume_idx = js.find("function resumeAfterReconnect")
    assert resume_idx >= 0
    resume_chunk = js[resume_idx : resume_idx + 9000]
    assert "refreshHistory" in resume_chunk
    assert "force: true" in resume_chunk
    assert "reconcileTurnAfterWake" in resume_chunk
    assert "silence >= 30" in resume_chunk or "silence != null && silence >= 30" in resume_chunk

    # setTurnRunning idle path schedules force history refresh
    assert "function scheduleForceHistoryRefresh" in js
    turn_idx = js.find("function setTurnRunning")
    assert turn_idx >= 0
    turn_chunk = js[turn_idx : turn_idx + 3500]
    assert "scheduleForceHistoryRefresh" in turn_chunk

    # visibility: debounced wake → reconcile + gated force history
    vis_idx = js.find('document.addEventListener("visibilitychange"')
    assert vis_idx >= 0
    vis_chunk = js[vis_idx : vis_idx + 500]
    assert "scheduleWakeReconcile" in vis_chunk
    wake_idx = js.find("async function handleWakeReconcile")
    assert wake_idx >= 0
    wake_chunk = js[wake_idx : wake_idx + 1200]
    assert "reconcileTurnAfterWake" in wake_chunk
    assert "force: true" in wake_chunk
    assert "refreshHistory" in wake_chunk
    assert "visibilityOnly" in wake_chunk


def test_js_composer_drafts_per_session() -> None:
    """Composer drafts are per-session with save/restore/clear + localStorage."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "composerDrafts" in js
    assert "function saveComposerDraft" in js
    assert "function restoreComposerDraft" in js
    assert "function clearComposerDraft" in js
    assert "grh.composerDrafts" in js
    assert "function loadComposerDrafts" in js
    # openSession saves prev and restores new
    open_idx = js.find("async function openSession")
    assert open_idx >= 0
    open_chunk = js[open_idx : open_idx + 3500]
    assert "saveComposerDraft" in open_chunk
    assert "restoreComposerDraft" in open_chunk
    # input saves; prompt clears
    assert "saveComposerDraft(state.selectedId)" in js
    assert "clearComposerDraft" in js
    submit_idx = js.find("function submitPrompt")
    assert submit_idx >= 0
    submit_chunk = js[submit_idx : submit_idx + 2200]
    assert "clearComposerDraft" in submit_chunk


def test_js_tool_html_preview_discovery() -> None:
    """Tool rows surface Preview when summary/detail mentions an .html path."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")

    assert "function extractHtmlPreviewCandidate" in js
    assert "function toSitePreviewRelPath" in js
    assert "function htmlPathFromToolRow" in js
    assert "function refreshToolPreviewAction" in js
    assert "tool-preview-btn" in js
    assert "startSitePreview" in js
    assert "async function startSitePreview" in js

    # Wired from create/update tool lines (user click only; no auto-open)
    create_idx = js.find("function createToolLine")
    assert create_idx >= 0
    create_chunk = js[create_idx : create_idx + 2200]
    assert "refreshToolPreviewAction" in create_chunk

    update_idx = js.find("function updateToolLine")
    assert update_idx >= 0
    update_chunk = js[update_idx : update_idx + 2200]
    assert "refreshToolPreviewAction" in update_chunk

    refresh_idx = js.find("function refreshToolPreviewAction")
    assert refresh_idx >= 0
    refresh_chunk = js[refresh_idx : refresh_idx + 900]
    assert "startSitePreview" in refresh_chunk
    assert "tool-preview-btn" in refresh_chunk
    assert "preventDefault" in refresh_chunk
    assert "stopPropagation" in refresh_chunk

    assert ".tool-preview-btn" in css
    assert ".term-line.tool .tool-preview-btn" in css


def test_new_session_folder_browser_contract() -> None:
    """New Session modal: Projects|Browse segments, breadcrumbs, sticky Use this folder."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    server = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    projects = (ROOT / "hub" / "projects.py").read_text(encoding="utf-8")

    assert 'id="project-mode-tabs"' in html
    assert 'id="tab-projects"' in html
    assert 'id="tab-browse"' in html
    assert "Projects" in html and "Browse" in html
    assert 'id="project-browser"' in html
    assert 'id="project-browser-crumbs"' in html
    assert 'id="project-browser-path"' in html
    assert 'id="project-browser-list"' in html
    assert 'id="project-browser-empty"' in html
    assert 'id="project-browser-status"' in html
    assert 'id="btn-browse-start"' in html
    assert "Use this folder" in html
    assert 'id="project-recents"' in html
    assert 'id="project-recents-list"' in html
    assert 'id="project-search"' in html

    assert "/api/projects/browse" in js
    assert "function loadProjectBrowse" in js
    assert "function renderProjectBrowse" in js
    assert "function renderProjectCrumbs" in js
    assert "function openProjectBrowser" in js
    assert "function closeProjectBrowser" in js
    assert "function setProjectModalMode" in js
    assert "function setProjectBrowseError" in js
    # Browse error path must surface status (never blank panel)
    assert "set hub.projects_root in config.toml" in js
    assert "projectBrowserStatus" in js
    assert "Could not load folders" in js
    assert "grh.recentProjects.v1" in js
    assert "rememberRecentProject" in js
    assert "projectEntryReturnMode" in js
    # New-vs-Resume entry: browse/list pick path via onProjectChosen, not bare createSession
    assert "onProjectChosen(abs)" in js or "function onProjectChosen" in js
    assert "function createSession" in js

    assert 'add_get("/api/projects/browse"' in server or 'add_get("/api/projects/browse"' in server
    assert "handle_projects_browse" in server
    assert "list_project_browse" in server
    assert "def list_project_browse" in projects

    assert ".project-mode-tabs" in css
    assert ".project-mode-tab" in css
    assert ".project-browser" in css
    assert ".project-browser-crumbs" in css
    assert ".project-browser-sticky" in css
    assert ".project-browser-list" in css or ".project-browser" in css


def test_cli_reload_and_new_vs_resume_entry_contract() -> None:
    """Reload recovery hook + Resume vs Start new entry (CLI interrupt-then-continue)."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    policy = (ROOT / "hub" / "session_policy.py").read_text(encoding="utf-8")

    # Composer keeps Stop/Send; Reload button is intentionally not in the UI
    assert 'id="btn-stop"' in html
    assert 'id="btn-send"' in html
    assert 'id="btn-reload"' not in html
    assert 'id="project-entry-choice"' in html
    assert 'id="btn-entry-start-new"' in html
    assert 'id="btn-entry-back"' in html
    assert 'id="project-entry-priors"' in html
    assert "Start new session" in html

    assert "function reloadResumeSession" in js
    assert "function onProjectChosen" in js
    assert "function sessionsMatchingCwd" in js
    assert "function stopTurnForSession" in js
    assert "function showEntryChoice" in js
    assert "/api/admin/reset-turn" in js
    assert "openSession" in js
    # Reload re-attaches same id; must not POST create on recovery path
    reload_idx = js.find("async function reloadResumeSession")
    assert reload_idx >= 0
    reload_end = js.find("\n  async function ", reload_idx + 10)
    if reload_end < 0:
        reload_end = js.find("\n  function ", reload_idx + 10)
    reload_chunk = js[reload_idx : reload_end if reload_end > 0 else reload_idx + 4000]
    assert "openSession" in reload_chunk
    assert "stopTurnForSession" in reload_chunk
    assert "const clearOk = await stopTurnForSession" in reload_chunk
    assert "clear_failed" in reload_chunk
    assert "Could not clear turn" in reload_chunk
    assert "opened.ok" in reload_chunk or "!opened.ok" in reload_chunk or "opened && opened.ok" in reload_chunk or "!opened || !opened.ok" in reload_chunk
    assert "Session resumed" in reload_chunk
    assert "/api/sessions" not in reload_chunk
    # openSession returns consistent result objects
    open_idx = js.find("async function openSession")
    assert open_idx >= 0
    open_end = js.find("\n  async function ", open_idx + 10)
    if open_end < 0:
        open_end = js.find("\n  function ", open_idx + 10)
    open_chunk = js[open_idx : open_end if open_end > 0 else open_idx + 8000]
    assert 'reason: "cancelled"' in open_chunk or 'reason:"cancelled"' in open_chunk
    assert "attach_failed" in open_chunk
    assert "historyOnly" in open_chunk
    assert "ok: true" in open_chunk or "ok:true" in open_chunk
    stop_idx = js.find("async function stopTurnForSession")
    assert stop_idx >= 0
    stop_chunk = js[stop_idx : stop_idx + 900]
    assert "reset-turn" in stop_chunk
    assert "sessionId" in stop_chunk
    assert "return res.ok" in stop_chunk
    # New flow offers Resume when priors
    assert "showEntryChoice" in js
    assert "Resume" in js
    assert "entryRequiresResumeChoice" in js
    assert "reloadResumeSession," in js or "reloadResumeSession" in js
    assert "sessionsMatchingCwd" in js
    assert "stopTurnForSession" in js
    assert "setSessionsForTest" in js
    assert "getSessionIdsForTest" in js
    assert "openSession," in js or "openSession" in js
    assert "onProjectChosen," in js or "onProjectChosen" in js

    assert ".project-entry-choice" in css
    assert ".project-entry-row" in css or ".project-entry-priors" in css
    assert "#btn-reload" not in css and "btn-reload" not in css

    assert "def sessions_matching_cwd" in policy
    assert "def entry_requires_resume_choice" in policy
    assert "def recovery_keeps_session_id" in policy


def test_files_media_share_and_video_contract() -> None:
    """Files panel: video preview, Share/Save media, raw download=1."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    server = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    fs_browser = (ROOT / "hub" / "fs_browser.py").read_text(encoding="utf-8")

    assert "function isVideoPath" in js
    assert "function isMediaPath" in js
    assert "function shareFsFile" in js
    assert "function hideVideoPreview" in js
    assert "function rawFsUrl" in js
    assert "download=1" in js
    assert "navigator.canShare" in js or "canShare" in js
    assert "navigator.share" in js
    assert "Open full size, then long-press to save" in js

    assert 'id="btn-file-share"' in html
    assert 'id="file-video-wrap"' in html
    assert 'id="file-video"' in html
    assert "playsinline" in html
    assert "controls" in html

    assert ".file-video-wrap" in css
    assert ".file-video" in css

    assert "RAW_MAX_BYTES" in fs_browser
    assert "VIDEO_EXTS" in fs_browser
    assert "def is_video_path" in fs_browser
    assert "def content_disposition_attachment" in fs_browser
    assert "RAW_MAX_BYTES" in server
    assert "download" in server
    assert "content_disposition_attachment" in server
    assert "handle_fs_raw" in server


def test_fs_upload_and_attach_contract() -> None:
    """PR2: binary upload route + composer attach + Files Upload controls."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    server = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    fs_browser = (ROOT / "hub" / "fs_browser.py").read_text(encoding="utf-8")

    assert 'add_post("/api/fs/upload"' in server or "/api/fs/upload" in server
    assert "handle_fs_upload" in server
    assert "def write_upload_bytes" in fs_browser
    assert "def sanitize_upload_filename" in fs_browser
    assert "UPLOAD_MAX_IMAGE_BYTES" in fs_browser
    assert "UPLOAD_MAX_VIDEO_BYTES" in fs_browser

    assert 'id="btn-attach"' in html
    assert 'id="composer-file-input"' in html
    assert 'id="btn-fs-upload"' in html
    assert "accept=" in html and "image/*" in html

    assert "uploadFsFiles" in js
    assert "/api/fs/upload" in js
    assert "btnAttach" in js or "btn-attach" in js
    assert "Attached file(s):" in js


def test_session_plan_viewer_contract() -> None:
    """Hub plan viewer + durable plan_mode.json handshake (not stock TUI a-key)."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    server = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    plan_view = (ROOT / "hub" / "plan_view.py").read_text(encoding="utf-8")

    assert "/api/sessions/{id}/plan" in server
    assert "/api/sessions/{id}/plan/action" in server
    assert "handle_session_plan" in server
    assert "handle_session_plan_action" in server
    assert "read_session_plan" in server
    assert "apply_plan_action" in server
    assert "PlanViewError" in server

    assert "def read_session_plan" in plan_view
    assert "def apply_plan_action" in plan_view
    assert "def merge_plan_mode_action" in plan_view
    assert "def write_plan_mode" in plan_view
    assert "plan.md" in plan_view
    assert "plan_mode.json" in plan_view
    assert "awaiting_plan_approval" in plan_view
    # plan.md remains read-only; only plan_mode.json is written (atomic via os.replace)
    assert "os.replace" in plan_view
    assert "write_plan_mode" in plan_view

    assert 'id="btn-view-plan"' in html
    assert 'id="modal-plan"' in html
    assert 'id="plan-body"' in html
    assert 'id="btn-plan-approve"' in html
    assert 'id="btn-plan-request-changes"' in html
    assert 'id="btn-plan-quit"' in html
    assert 'id="plan-await-banner"' in html
    assert (
        "Approve clears Hub plan-mode gate (writes plan_mode.json) and continues the session. Not the stock TUI a-key."
        in html
    )

    assert "btnViewPlan" in js
    assert "modalPlan" in js or "modal-plan" in js
    assert "openPlanModal" in js
    assert "hardApprovePlan" in js
    assert "hardRequestPlanChanges" in js
    assert "hardQuitPlan" in js
    assert "postPlanAction" in js
    assert "/plan/action" in js
    assert "refreshSessionPlan" in js
    assert "/api/sessions/" in js and "/plan" in js
    assert "approved — implement the plan in plan.md" in js
    assert "Request changes to the plan:" in js
    # View plan only when awaiting / Active — not leftover plan.md alone
    assert "function planChromeShouldShow" in js
    assert "function applyPlanChrome" in js
    assert "plan-await-banner" in html
    assert 'id="btn-view-plan"' in html
    # Inline strip (not topbar title row)
    assert "topbar-plan" not in html
    assert "plan-inline-btn" in html or "plan-inline-btn" in css
    # No exit_plan_mode RPC call (comments may mention the name)
    assert "exit_plan_mode(" not in js
    assert '"exit_plan_mode"' not in js
    assert "'exit_plan_mode'" not in js
    assert "PLAN_APPROVE_INJECT" in js
    assert "injectPlanComposerText" in js
    assert "updatePlanAwaitBanner" in js

    assert ".modal-card-plan" in css
    assert ".plan-body" in css
    assert ".plan-await-banner" in css


# ---------------------------------------------------------------------------
# Goal mode helpers + session restore contracts
# ---------------------------------------------------------------------------


def test_parse_goal_slash() -> None:
    assert parse_goal_slash(None) is None
    assert parse_goal_slash("") is None
    assert parse_goal_slash("hello") is None
    assert parse_goal_slash("/goalie") is None
    assert parse_goal_slash("/goal") == {"action": "status"}
    assert parse_goal_slash("/goal status") == {"action": "status"}
    assert parse_goal_slash("/goal pause") == {"action": "pause"}
    assert parse_goal_slash("/goal resume") == {"action": "resume"}
    assert parse_goal_slash("/goal clear") == {"action": "clear"}
    assert parse_goal_slash("/goal Fix the login bug") == {
        "action": "start",
        "objective": "Fix the login bug",
    }
    assert parse_goal_slash("  /GOAL  pause  ") == {"action": "pause"}
    # start keeps multi-word objective (not treated as status)
    assert parse_goal_slash("/goal status update the docs") == {
        "action": "start",
        "objective": "status update the docs",
    }


def test_format_goal_elapsed() -> None:
    assert format_goal_elapsed(0) == "0s"
    assert format_goal_elapsed(45) == "45s"
    assert format_goal_elapsed(72) == "1m 12s"
    assert format_goal_elapsed(3 * 60 + 5) == "3m 05s"
    assert format_goal_elapsed(3600 + 5 * 60) == "1h 05m"
    assert format_goal_elapsed(2 * 3600 + 12 * 60 + 3) == "2h 12m"
    assert format_goal_elapsed(-3) == "0s"
    assert format_goal_elapsed(None) == "0s"


def test_goal_banner_text() -> None:
    t = goal_banner_text(
        status="active",
        elapsed_s=14 * 60 + 32,
        message="Reading PR0 brief",
    )
    assert t == "Goal · 14m 32s · Reading PR0 brief"
    paused = goal_banner_text(
        status="paused",
        elapsed_s=45,
        objective="Ship it",
    )
    assert paused.startswith("Goal · paused · 45s")
    assert "Ship it" in paused
    long_msg = "x" * 100
    truncated = goal_banner_text(status="active", elapsed_s=1, message=long_msg)
    assert truncated.endswith("…")
    assert len(truncated) < 100


def test_apply_goal_tool_input() -> None:
    now = 1_700_000_000_000.0
    # Non-goal tool
    assert apply_goal_tool_input(None, {"path": "a.py"}, "read_file", now) is None

    # First update_goal starts cycle
    r1 = apply_goal_tool_input(
        None,
        {"message": "Reading PR0..."},
        "update_goal",
        now,
    )
    assert r1 is not None
    assert r1["status"] == "active"
    assert r1["message"] == "Reading PR0..."
    assert r1["startedAt"] == now

    # Progress keeps startedAt
    later = now + 60_000
    r2 = apply_goal_tool_input(
        r1,
        {
            "variant": "UpdateGoal",
            "completed": None,
            "message": "Still reading…",
            "blocked_reason": None,
        },
        "Goal: Still reading…",
        later,
    )
    assert r2 is not None
    assert r2["status"] == "active"
    assert r2["startedAt"] == now
    assert r2["message"] == "Still reading…"

    # completed:true ends
    r3 = apply_goal_tool_input(
        r2,
        {"variant": "UpdateGoal", "completed": True, "message": "Done"},
        "update_goal",
        later + 1000,
    )
    assert r3 is not None
    assert r3["status"] == "done"
    assert r3["startedAt"] == now

    # Title Goal: without raw is enough to detect
    r4 = apply_goal_tool_input(None, None, "Goal: Hello", now)
    assert r4 is not None
    assert r4["message"] == "Hello"


def test_session_restore_and_goal_banner_contract() -> None:
    """Selected-session restore key + goal banner HTML/CSS/JS wiring."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    py = (ROOT / "hub" / "ui_ux.py").read_text(encoding="utf-8")

    # HTML banner near plan-await
    assert 'id="goal-banner"' in html
    assert 'id="goal-banner-text"' in html
    assert 'id="plan-await-banner"' in html  # not removed

    # CSS states
    assert ".goal-banner" in css
    assert '[data-status="active"]' in css or 'data-status="active"' in css
    assert '[data-status="paused"]' in css or 'data-status="paused"' in css

    # Storage keys (localStorage)
    assert "grh.selectedSession.v1" in js
    assert "grh.sessionGoals.v1" in js
    assert "function saveSelectedSession" in js
    assert "function loadSelectedSessionId" in js
    assert "function loadSelectedSessionMeta" in js
    assert "function updateGoalBanner" in js
    assert "function rehydrateGoalFromHistory" in js
    assert "function noteGoalFromTool" in js
    assert "function noteGoalFromUserText" in js
    assert "function parseGoalSlash" in js
    assert "function formatGoalElapsed" in js
    assert "function goalBannerText" in js
    assert "function applyGoalToolInput" in js
    assert "syncGoalTick" in js

    # saveSelectedSession stores cwd+title for restore-by-id
    save_idx = js.find("function saveSelectedSession")
    assert save_idx >= 0
    save_chunk = js[save_idx : save_idx + 700]
    assert "cwd" in save_chunk
    assert "title" in save_chunk

    # bootstrap restores saved session after /api/sessions; falls back to history-by-id
    boot_idx = js.find("async function bootstrap")
    assert boot_idx >= 0
    boot_chunk = js[boot_idx : boot_idx + 5500]
    assert "loadSelectedSessionMeta" in boot_chunk
    assert "openSession" in boot_chunk
    assert "/api/sessions" in boot_chunk
    assert "/history" in boot_chunk
    assert "clearSelectedSession" in boot_chunk

    # openSession persists selection
    open_idx = js.find("async function openSession")
    assert open_idx >= 0
    open_chunk = js[open_idx : open_idx + 1200]
    assert "saveSelectedSession" in open_chunk

    # Pure helpers exist in Python source of truth
    assert "def parse_goal_slash" in py
    assert "def format_goal_elapsed" in py
    assert "def goal_banner_text" in py
    assert "def apply_goal_tool_input" in py


def test_js_restore_pending_user_prompt_after_history() -> None:
    """Pending lastPrompt reappears after history reload when not on disk."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function restorePendingUserPromptIfMissing" in js
    assert "function isNoOutputKeepPending" in js
    assert "function offerNoOutputResend" in js
    ah_idx = js.find("function applyHistoryMessages")
    assert ah_idx >= 0
    ah_chunk = js[ah_idx : ah_idx + 1600]
    assert "restorePendingUserPromptIfMissing" in ah_chunk
    # keepPending on no-output idle
    assert "keepPending" in js
    assert "opts.keepPending" in js or "opts && opts.keepPending" in js
