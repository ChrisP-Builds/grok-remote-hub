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

    # Thought prefix + labels
    assert 'if (r === "thought") return "Thinking:"' in js
    assert 'label.textContent = opts.stream ? "thinking…" : "Thinking"' in js
    assert 'if (label) label.textContent = "Thinking"' in js

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

    # Tools auto-open when running/pending; detail limit 8000
    create_idx = js.find("function createToolLine")
    assert create_idx >= 0
    create_chunk = js[create_idx : create_idx + 700]
    assert 'st === "running" || st === "pending"' in create_chunk
    assert "row.open = true" in create_chunk

    assert "extractToolContentSnippet(update, 8000)" in js
    assert "function extractToolContentSnippet(update, limit = 120)" in js

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
    note_chunk = js[note_idx : note_idx + 900]
    assert "snapshotLiveClientForRestart" in note_chunk
    assert "snapshotHadLive" in note_chunk
    assert "_pendingRestartInterrupt" in note_chunk
    assert "clearLiveClientStateAfterProcessRestart" in note_chunk
    assert "bootId changed" in note_chunk
    assert "_hubProcessRestarted" in note_chunk

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
    status_chunk = js[status_idx : status_idx + 7000]
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
    chunk = js[status_idx : status_idx + 2200]
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
