"""Structural checks for the phone approval card and mode switch (ADR-027)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
EM_DASH = "\u2014"


def _js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


def _html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _css() -> str:
    return (STATIC / "app.css").read_text(encoding="utf-8")


def _fn(js: str, name: str, span: int = 1600) -> str:
    idx = js.find(f"function {name}(")
    assert idx >= 0, f"missing function {name}"
    return js[idx : idx + span]


def _approvals_block(js: str) -> str:
    start = js.find("// Approvals (ADR-027)")
    end = js.find("// End approvals")
    assert 0 <= start < end, "approvals section markers missing"
    return js[start:end]


def _status_handler(js: str) -> str:
    start = js.find('if (type === "status") {')
    end = js.find('if (type === "queued") {', start)
    assert 0 <= start < end
    return js[start:end]


def test_ws_handlers_dispatch_both_message_types() -> None:
    js = _js()
    assert 'type === "approval_request"' in js
    assert 'type === "approval_resolved"' in js
    assert "onApprovalRequest(msg)" in js
    assert "onApprovalResolved(msg)" in js


def test_status_merges_approval_fields() -> None:
    js = _js()
    assert "applyStatusApprovals(msg)" in _status_handler(js)
    merge = _fn(js, "applyStatusApprovals")
    assert "msg.pendingApprovals" in merge
    assert "msg.approvalModes" in merge
    assert "msg.approvalDefaultMode" in merge


def test_answer_and_mode_sends_match_contract() -> None:
    block = _approvals_block(_js())
    assert 'type: "approval_answer"' in block
    assert re.search(r"answerApproval\(\s*req\.id,\s*\"deny\",\s*\"once\"\s*\)", block)
    assert re.search(r"answerApproval\(\s*req\.id,\s*\"allow\",\s*\"once\"\s*\)", block)
    assert re.search(r"answerApproval\(\s*req\.id,\s*\"allow\",\s*\"project\"\s*\)", block)
    assert 'type: "set_approval_mode"' in block
    assert "sessionId: approvalSessionId()" in _fn(block, "setApprovalMode")


def test_approvals_follow_live_prompt_session() -> None:
    js = _js()
    block = _approvals_block(js)
    assert "return promptSessionId()" in _fn(block, "approvalSessionId", 200)
    assert "approvalSessionId()" in _fn(block, "currentApprovalMode", 300)
    on_screen = _fn(block, "isApprovalOnScreen", 400)
    assert "state.selectedId" in on_screen
    assert "state.livePromptSessionId" in on_screen
    assert "isApprovalOnScreen" in _fn(block, "visibleApprovals", 400)
    assert "isApprovalOnScreen(req)" in _fn(block, "notifyApproval", 500)
    assert js.count("state.livePromptSessionId = ") == 1
    assert "renderApprovals()" in _fn(js, "setLivePromptSessionId", 200)


def test_answer_watchdog_reenables_buttons() -> None:
    block = _approvals_block(_js())
    watchdog = _fn(block, "startAnswerWatchdog", 700)
    assert "setTimeout" in watchdog
    assert "No confirmation from the hub; tap again" in watchdog
    assert "startAnswerWatchdog(id)" in _fn(block, "answerApproval", 700)
    assert "clearTimeout" in _fn(block, "stopAnswering", 400)


def test_trusted_mode_requires_confirmation() -> None:
    set_mode = _fn(_approvals_block(_js()), "setApprovalMode", 1200)
    confirm = set_mode.find("window.confirm(")
    send = set_mode.find("sendWs(")
    assert 0 <= confirm < send


def test_countdown_corrects_server_clock_skew() -> None:
    block = _approvals_block(_js())
    normalize = _fn(block, "normalizeApproval", 1000)
    assert "raw.serverNow" in normalize
    assert "Date.now() / 1000" in normalize
    assert "req.deadline" in _fn(block, "updateApprovalCountdowns", 700)


def test_cache_bust_token_bumped() -> None:
    html = _html()
    assert "/app.css?v=20261031a" in html
    assert "/app.js?v=20261031a" in html


def test_approvals_block_never_parses_html() -> None:
    block = _approvals_block(_js())
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
        assert sink not in block


def test_answer_offline_toasts_and_keeps_buttons_enabled() -> None:
    js = _js()
    assert 'const APPROVAL_OFFLINE_TEXT = "Not connected; answer not sent"' in js
    answer = _fn(js, "answerApproval", 700)
    offline = answer.find("toast(APPROVAL_OFFLINE_TEXT")
    busy = answer.find("approvalAnswering.add")
    assert 0 <= offline < busy, "offline check must run before buttons are disabled"


def test_ws_close_releases_answering_buttons() -> None:
    js = _js()
    idx = js.find('ws.addEventListener("close"')
    assert idx >= 0
    assert "releaseApprovalAnswers()" in js[idx : idx + 500]


def test_card_buttons_countdown_and_collapse() -> None:
    block = _approvals_block(_js())
    assert '"Deny"' in block
    assert '"Allow"' in block
    assert '"Always allow " + req.similarRule + " in this project"' in block
    assert "if (req.similarRule)" in block
    assert '"Auto-deny in "' in block
    assert "Math.max(0," in _fn(block, "formatApprovalCountdown", 400)
    assert '" more lines"' in block
    assert "requestAnimationFrame" not in _fn(block, "buildApprovalSummary", 1200)
    assert "cards.forEach(syncApprovalSummary)" in _fn(block, "renderApprovalStack", 1400)
    assert "syncApprovalSummary" in _fn(block, "tickApprovals", 300)
    for label in ("Shell command", "File change", '"Tool"'):
        assert label in block


def test_resolved_notes_and_cross_session_toast() -> None:
    block = _approvals_block(_js())
    for text in (
        '"Allowed by you: "',
        '"Allowed for this project: "',
        '"Denied: "',
        '"Timed out, denied: "',
    ):
        assert text in block
    assert '"Approval needed: "' in block
    assert "onTap: () => focusApprovalSession(sid)" in _fn(block, "notifyApproval", 700)
    js = _js()
    assert 'if (typeof opts.onTap === "function") makeToastTappable(el, opts.onTap);' in js
    assert "onTap();" in _fn(js, "makeToastTappable", 500)


def test_rail_badge_synced_on_render_paths() -> None:
    js = _js()
    assert "status-approval" in js
    assert "syncApprovalBadges()" in _fn(js, "syncVisibleSessionPills", 4000)
    render = js[js.find("function renderSessions(") : js.find("function emptyStreamBuffers(")]
    assert "syncApprovalBadges()" in render


def test_html_card_host_above_composer_and_mode_control() -> None:
    html = _html()
    stack = html.find('id="approval-stack"')
    form = html.find('id="composer-form"')
    shell = html.find('class="composer-shell"')
    assert 0 <= shell < stack < form
    stack_tag = html[stack : html.find(">", stack)]
    assert "aria-live" in stack_tag

    mode = html.find('id="approval-mode"')
    title = html.find('id="chat-title"')
    assert 0 <= title < mode
    mode_block = html[mode : html.find("</div>", mode)]
    for value in ("balanced", "strict", "trusted"):
        assert f'data-mode="{value}"' in mode_block
    assert "Never asks (commands, file changes, tools)" in mode_block
    assert 'class="approval-mode hidden"' in html


def test_css_touch_targets_and_tokens() -> None:
    css = _css()
    assert ".approval-card" in css
    actions = css[css.find(".approval-actions .btn {") :]
    assert "min-height: 44px" in actions[: actions.find("}")]
    trusted = css[css.find('.approval-mode-opt[data-mode="trusted"][aria-pressed="true"]') :]
    assert "var(--warn)" in trusted[: trusted.find("}")]
    assert ".session-pill.status-approval" in css


def test_no_em_dashes_in_new_code() -> None:
    assert EM_DASH not in _approvals_block(_js())
    css = _css()
    start = css.find("/* Approvals")
    assert start >= 0
    assert EM_DASH not in css[start : css.find("/* End approvals", start)]
    html = _html()
    assert EM_DASH not in html[html.find('id="approval-mode"') : html.find("</div>", html.find('id="approval-mode"'))]
