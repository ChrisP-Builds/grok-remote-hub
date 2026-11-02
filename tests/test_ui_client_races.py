"""Behavioural checks for client race fixes: Stop fallback identity, seq dedupe window, late replay, wake mark.

The functions under test are sliced out of static/app.js and run in node against small stubs.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


def _body(src: str, name: str) -> str:
    match = re.search(rf"^( *)(async )?function {name}\(", src, re.M)
    assert match, f"missing function {name}"
    close = src.find("\n" + match.group(1) + "}\n", match.start())
    assert close > 0
    return src[match.start() : close + len(match.group(1)) + 2]


def _seq_block(js: str) -> str:
    start = js.find("// Sequenced events and resume (ADR-029)")
    end = js.find("// End sequenced events")
    assert 0 <= start < end, "sequenced events section markers missing"
    return js[start:end]


def _run(script: str) -> Any:
    assert NODE
    done = subprocess.run(
        [NODE, "-"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=30
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


SEQ_PRELUDE = """
const handled = [];
const state = { sessionViews: new Map(), bootId: "b1" };
function onHubMessage(m) { handled.push(m.seq); }
function acp(seq) { return { type: "acp", sessionId: "s", seq, message: {} }; }
"""


def _seq_script(scenario: str) -> str:
    return SEQ_PRELUDE + _seq_block(_js()) + "\n(async () => {\n" + scenario + "\n})();\n"


@needs_node
def test_out_of_order_seq_from_a_second_viewer_is_not_dropped() -> None:
    out = _run(
        _seq_script(
            """
            state.sessionViews.set("s", { lastSeq: 9, historyLoaded: true, replayed: false });
            for (const n of [11, 10, 10, 11, 12]) receiveHubMessage(acp(n));
            console.log(JSON.stringify({ handled, lastSeq: state.sessionViews.get("s").lastSeq }));
            """
        )
    )
    assert out == {"handled": [11, 10, 12], "lastSeq": 12}


@needs_node
def test_snapshot_seq_still_drops_events_it_already_contains() -> None:
    out = _run(
        _seq_script(
            """
            state.sessionViews.set("s", { lastSeq: 0, historyLoaded: true, replayed: false });
            noteSnapshotSeq("s", 20);
            for (const n of [15, 20, 21]) receiveHubMessage(acp(n));
            console.log(JSON.stringify({ handled, lastSeq: state.sessionViews.get("s").lastSeq }));
            """
        )
    )
    assert out == {"handled": [21], "lastSeq": 21}


@needs_node
def test_dedupe_window_stays_bounded() -> None:
    out = _run(
        _seq_script(
            """
            state.sessionViews.set("s", { lastSeq: 0, historyLoaded: true, replayed: false });
            for (let n = 1; n <= 5000; n++) receiveHubMessage(acp(n));
            const v = state.sessionViews.get("s");
            receiveHubMessage(acp(3));
            console.log(JSON.stringify({ count: handled.length, size: v.seenSeqs.size, lastSeq: v.lastSeq }));
            """
        )
    )
    assert out["count"] == 5000
    assert out["size"] <= 1024
    assert out["lastSeq"] == 5000


@needs_node
def test_late_replay_after_http_rebuild_is_ignored() -> None:
    out = _run(
        _seq_script(
            """
            const v = { lastSeq: 5, historyLoaded: true, replayed: false };
            state.sessionViews.set("s", v);
            registerResumeWaiter("s");
            const replayed = await awaitReplayedSessions(["s"], 20);
            receiveHubMessage(acp(7));
            notePaneRebuilt("s", v);
            onReplay({ type: "replay", sessionId: "s", events: [acp(6), acp(7)] });
            receiveHubMessage(acp(7));
            receiveHubMessage(acp(8));
            console.log(JSON.stringify({
              replayed: [...replayed], handled, mark: v.replayed, waiting: state.resumeWaiters.size,
            }));
            """
        )
    )
    assert out == {"replayed": [], "handled": [7, 8], "mark": False, "waiting": 0}


@needs_node
def test_late_replay_without_a_rebuild_still_applies() -> None:
    """A disk reload that found nothing new leaves the pane intact, so the replay's events are still needed."""
    out = _run(
        _seq_script(
            """
            const v = { lastSeq: 5, historyLoaded: true, replayed: false };
            state.sessionViews.set("s", v);
            registerResumeWaiter("s");
            await awaitReplayedSessions(["s"], 20);
            receiveHubMessage(acp(7));
            onReplay({ type: "replay", sessionId: "s", events: [acp(6), acp(7)] });
            console.log(JSON.stringify({ handled, lastSeq: v.lastSeq }));
            """
        )
    )
    assert out == {"handled": [7, 6], "lastSeq": 7}


@needs_node
def test_replay_racing_a_rebuild_inside_the_wait_is_reported_stale() -> None:
    out = _run(
        _seq_script(
            """
            const v = { lastSeq: 5, historyLoaded: true, replayed: false };
            state.sessionViews.set("s", v);
            registerResumeWaiter("s");
            const wait = awaitReplayedSessions(["s"], 500);
            notePaneRebuilt("s", v);
            onReplay({ type: "replay", sessionId: "s", events: [acp(6)] });
            const replayed = await wait;
            console.log(JSON.stringify({ replayed: [...replayed], handled }));
            """
        )
    )
    assert out == {"replayed": [], "handled": []}


@needs_node
def test_timely_replay_is_applied_and_reported() -> None:
    out = _run(
        _seq_script(
            """
            const v = { lastSeq: 5, historyLoaded: true, replayed: false };
            state.sessionViews.set("s", v);
            registerResumeWaiter("s");
            const wait = awaitReplayedSessions(["s"], 500);
            onReplay({ type: "replay", sessionId: "s", events: [acp(6), acp(7)] });
            const replayed = await wait;
            console.log(JSON.stringify({ replayed: [...replayed], handled, lastSeq: v.lastSeq }));
            """
        )
    )
    assert out == {"replayed": ["s"], "handled": [6, 7], "lastSeq": 7}


@needs_node
def test_replay_mark_is_consumed_by_one_wake() -> None:
    out = _run(
        _seq_script(
            """
            state.sessionViews.set("s", { lastSeq: 5, historyLoaded: true, replayed: true });
            console.log(JSON.stringify([takeReplayMark("s"), takeReplayMark("s"), takeReplayMark("nope")]));
            """
        )
    )
    assert out == [True, False, False]


def test_wake_consumes_the_replay_mark() -> None:
    wake = _body(_js(), "handleWakeReconcile")
    assert "!takeReplayMark(state.selectedId)" in wake
    assert "paneReplayed" not in _js()


def test_pane_wipe_keeps_the_dedupe_window() -> None:
    js = _js()
    for name in ("clearTranscript", "hydrateSessionPane"):
        body = _body(js, name)
        assert "notePaneRebuilt(" in body
        assert "seenSeqs" not in body and "seqFloor" not in body
    rebuilt = _body(_seq_block(js), "notePaneRebuilt")
    assert "seenSeqs" not in rebuilt and "seqFloor" not in rebuilt
    pane = _body(js, "getSessionPane")
    assert "seenSeqs: new Set()" in pane and "seqFloor: 0" in pane


STOP_PRELUDE = """
const calls = [];
const toasts = [];
const live = {};
let resetReply = { ok: true, body: { cleared: true } };
const state = { turnMarks: new Map() };
function sessionLiveStatus(sid) { return live[sid] || "idle"; }
function apiUrl(p) { return p; }
function toast(text, kind) { toasts.push([text, kind || ""]); }
async function fetch(url, init) {
  calls.push([url, JSON.parse(init.body)]);
  return { ok: resetReply.ok, json: async () => resetReply.body };
}
"""


def _stop_script(scenario: str) -> str:
    js = _js()
    fns = "\n".join(_body(js, n) for n in ("noteTurnState", "turnEpoch", "runStopFallback"))
    return STOP_PRELUDE + fns + "\n(async () => {\n" + scenario + "\nconsole.log(JSON.stringify({ calls, toasts }));\n})();\n"


@needs_node
def test_stop_fallback_skips_a_follow_up_turn() -> None:
    out = _run(
        _stop_script(
            """
            noteTurnState("s", true); live.s = "working";
            const epoch = turnEpoch("s");
            noteTurnState("s", false); live.s = "idle";
            noteTurnState("s", true); live.s = "working";
            await runStopFallback("s", epoch);
            """
        )
    )
    assert out == {"calls": [], "toasts": []}


@needs_node
def test_stop_fallback_ignores_another_projects_turn() -> None:
    out = _run(
        _stop_script(
            """
            noteTurnState("s", true); live.s = "working";
            noteTurnState("other", true); live.other = "working";
            const epoch = turnEpoch("s");
            noteTurnState("s", false); live.s = "idle";
            await runStopFallback("s", epoch);
            """
        )
    )
    assert out == {"calls": [], "toasts": []}


@needs_node
def test_stop_fallback_resets_the_same_stuck_turn() -> None:
    out = _run(
        _stop_script(
            """
            noteTurnState("s", true); live.s = "working";
            const epoch = turnEpoch("s");
            noteTurnState("s", true);
            await runStopFallback("s", epoch);
            """
        )
    )
    assert out["calls"] == [["/api/admin/reset-turn", {"sessionId": "s"}]]
    assert out["toasts"] == [["Turn force-cleared (Stop fallback)", ""]]


@needs_node
def test_stop_fallback_toasts_only_when_the_hub_cleared_something() -> None:
    out = _run(
        _stop_script(
            """
            live.s = "working";
            resetReply = { ok: true, body: { ok: true, cleared: false } };
            await runStopFallback("s", turnEpoch("s"));
            """
        )
    )
    assert len(out["calls"]) == 1
    assert out["toasts"] == []


@needs_node
def test_stop_fallback_reports_a_failed_reset() -> None:
    out = _run(
        _stop_script(
            """
            live.s = "working";
            resetReply = { ok: false, body: {} };
            await runStopFallback("s", turnEpoch("s"));
            """
        )
    )
    assert out["toasts"] == [["Stop did not clear the turn. Try Stop again.", "danger"]]


def test_stop_click_captures_the_turn_before_waiting() -> None:
    js = _js()
    click = js[js.find("els.btnStop.addEventListener"):]
    click = click[: click.find("\n    });\n")]
    assert click.find("turnEpoch(sid)") < click.find("setTimeout(")
    assert "runStopFallback(sid, epoch)" in click
    assert "state.turnRunning" not in click
    turn = js[js.find('if (type === "turn") {'):]
    assert 'noteTurnState(sid, msg.state === "running")' in turn[:2500]


def test_windows_opened_on_agent_content_send_no_referrer() -> None:
    opens = re.findall(r"window\.open\(([^;]*)\);", _js())
    assert opens
    for call in opens:
        assert "noreferrer" in call, call
