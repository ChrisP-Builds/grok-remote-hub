"""Structural checks for the client side of ADR-029: seq gate, resume subscribe, replay handling."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
EM_DASH = chr(0x2014)


def _js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


def _body(src: str, name: str) -> str:
    match = re.search(rf"^( *)(async )?function {name}\(", src, re.M)
    assert match, f"missing function {name}"
    close = src.find("\n" + match.group(1) + "}\n", match.start())
    assert close > 0
    return src[match.start() : close]


def _seq_block(js: str) -> str:
    start = js.find("// Sequenced events and resume (ADR-029)")
    end = js.find("// End sequenced events")
    assert 0 <= start < end, "sequenced events section markers missing"
    return js[start:end]


def test_cache_bust_bumped_for_app_js() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "/app.js?v=20261101d" in html


def test_pane_tracks_last_seq_and_replay_mark() -> None:
    pane = _body(_js(), "getSessionPane")
    assert "lastSeq: 0" in pane
    assert "replayed: false" in pane


def test_wiping_a_pane_clears_its_last_seq() -> None:
    js = _js()
    clear = _body(js, "clearTranscript")
    assert clear.find("v.historyLoaded = false") < clear.find("v.lastSeq = 0")
    hydrate = _body(js, "hydrateSessionPane")
    wipe = hydrate.find('v.pane.innerHTML = ""')
    assert 0 < wipe < hydrate.find("v.lastSeq = 0")


def test_gate_drops_already_seen_events_before_handling() -> None:
    block = _seq_block(_js())
    types = re.search(r"const SEQUENCED_TYPES = new Set\(\[([^\]]*)\]", block)
    assert types
    assert {t.strip().strip('"') for t in types.group(1).split(",")} == {
        "acp", "turn", "system", "terminal_out", "compact", "usage", "commands",
    }
    admit = _body(block, "admitSequenced")
    assert "state.sessionViews.get(msg.sessionId)" in admit
    assert "seq <= (v.lastSeq || 0)" in admit
    assert "getSessionPane" not in admit
    note = _body(block, "noteSequenced")
    assert "seq > (v.lastSeq || 0)" in note and "v.lastSeq = seq" in note
    receive = _body(block, "receiveHubMessage")
    assert receive.find("admitSequenced(msg)") < receive.find("onHubMessage(msg)")
    assert "finally" in receive and receive.find("onHubMessage(msg)") < receive.find("noteSequenced(msg)")
    js = _js()
    listener = js.find('ws.addEventListener("message"')
    assert "receiveHubMessage(msg)" in js[listener : listener + 300]
    assert "onHubMessage(msg)" not in js[listener : listener + 300]


def test_history_snapshot_raises_last_seq_only_when_applied() -> None:
    js = _js()
    start = js.find('if (type === "history")')
    chunk = js[start : start + 700]
    settle = chunk.find('settleResumeWaiter(msg.sessionId, "history")')
    guard = chunk.find("turnRunningOnSelected()")
    assert 0 < settle < guard
    assert chunk.count("noteSnapshotSeq(msg.sessionId, msg.seq)") == 2
    assert chunk.find("applyHistoryMessages") < chunk.find("noteSnapshotSeq")
    assert chunk.find("hydrateSessionPane") < chunk.rfind("noteSnapshotSeq")
    note = _body(_seq_block(js), "noteSnapshotSeq")
    assert "if (v && n > (v.lastSeq || 0)) v.lastSeq = n" in note


def test_subscribe_sends_since_seq_only_for_a_valid_cached_pane() -> None:
    js = _js()
    sub = _body(js, "subscribeSessionIds")
    assert "resumeParamsFor(id)" in sub
    assert "msg.sinceSeq = resume.sinceSeq" in sub and "msg.bootId = resume.bootId" in sub
    assert "registerResumeWaiter(id)" in sub
    params = _body(_seq_block(js), "resumeParamsFor")
    assert "state.sessionViews.get(sessionId)" in params
    assert "getSessionPane" not in params
    for cond in ("!v", "!v.historyLoaded", "!(v.lastSeq > 0)", "!state.bootId"):
        assert cond in params
    assert "sinceSeq: v.lastSeq" in params and "bootId: state.bootId" in params


def test_replay_feeds_events_through_the_normal_path_and_marks_the_pane() -> None:
    js = _js()
    assert re.search(r'if \(type === "replay"\) \{\s+onReplay\(msg\);\s+return;', js)
    replay = _body(_seq_block(js), "onReplay")
    assert "msg.events" in replay
    assert "receiveHubMessage(events[i])" in replay
    assert replay.find("receiveHubMessage(events[i])") < replay.find("v.replayed = true")
    assert 'settleResumeWaiter(sid, "replay")' in replay
    assert replay.find("v.replayed = true") < replay.find('settleResumeWaiter(sid, "replay")')


def test_reconnect_skips_disk_reload_for_replayed_sessions_only() -> None:
    js = _js()
    resume = _body(js, "resumeAfterReconnect")
    wait = resume.find("await awaitReplayedSessions(resumeIds")
    assert resume.find("subscribeSessionIds(state.selectedId)") < wait
    assert wait < resume.find("hydrates = []")
    assert "if (replayed.has(sid)) continue;" in resume
    assert "if (state.selectedId && !replayed.has(state.selectedId))" in resume
    assert "refreshHistory(state.selectedId, { force: true })" in resume
    waiter = _body(_seq_block(js), "awaitReplayedSessions")
    assert "timeoutMs" in waiter and "Promise.race" in waiter
    assert "v.replayed" in waiter


def test_wake_reconcile_skips_forced_reload_after_replay() -> None:
    js = _js()
    wake = _body(js, "handleWakeReconcile")
    assert "if (state.selectedId && !paneReplayed(state.selectedId))" in wake
    assert "force: true" in wake and "visibilityOnly: true" in wake
    assert "reconcileTurnAfterWake(reason)" in wake


def test_turn_idle_reload_and_idle_poll_unchanged() -> None:
    js = _js()
    sched = _body(js, "scheduleForceHistoryRefresh")
    assert "refreshHistory(state.selectedId, { force: true })" in sched
    assert "replayed" not in sched
    poll = _body(js, "startHistoryPoll")
    assert "refreshHistory(state.selectedId);" in poll and "4000" in poll
    assert "replayed" not in poll


def test_socket_and_boot_changes_reset_resume_state() -> None:
    js = _js()
    open_idx = js.find('ws.addEventListener("open"')
    open_chunk = js[open_idx : open_idx + 600]
    assert open_chunk.find("resetSocketResumeState()") < open_chunk.find("resumeAfterReconnect(")
    close_idx = js.find('ws.addEventListener("close"')
    assert "resetSocketResumeState()" in js[close_idx : close_idx + 500]
    reset = _body(_seq_block(js), "resetSocketResumeState")
    assert "v.replayed = false" in reset and 'settleAllResumeWaiters("reset")' in reset
    boot = _body(js, "noteBootId")
    assert boot.find("state.bootId !== bootId") < boot.find("resetSequenceTracking()") < boot.find("location.reload()")
    tracking = _body(_seq_block(js), "resetSequenceTracking")
    assert "v.lastSeq = 0" in tracking and "v.replayed = false" in tracking


def test_new_code_has_no_em_dashes_or_html_sinks() -> None:
    block = _seq_block(_js())
    assert EM_DASH not in block
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
        assert sink not in block


def test_connect_ws_keeps_a_single_live_socket() -> None:
    """Deep-link startup opened a second socket while the first was connecting, so every event arrived twice."""
    body = _body(_js(), "connectWs")
    assert "WebSocket.CONNECTING" in body and "WebSocket.OPEN" in body, "must not open a second socket"
    assert "force" in body, "dead-hub recovery still needs a forced reconnect"
    assert body.count("if (state.ws !== ws) return;") >= 3, "stale socket events must be ignored"
    assert re.search(r"prev\.close\(\)", body), "a forced reconnect closes the previous socket"


def test_dead_hub_recovery_forces_reconnect() -> None:
    assert "connectWs({ force: true })" in _body(_js(), "maybeHardRecoverFromDeadHub")

def test_history_tool_rows_show_full_detail_not_the_one_liner() -> None:
    """History sends a short summary and the fuller output as detail; the expanded body must use detail."""
    js = _js()
    assert "detail: detailText" in _body(js, "appendToolLine")
    assert re.search(r"setToolDetailBody\(row, \(detail \|\| \"\"\)\.trim\(\) \|\| snip\)", _body(js, "createToolLine"))
    assert 'String(detail || "").trim() || full' in _body(js, "updateToolLine")