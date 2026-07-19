"""Unit tests for /compact helpers and notification normalization."""

from __future__ import annotations

from pathlib import Path

from hub.compact import (
    COMPACT_TERMINAL_MSG_DEDUPE_MS,
    COMPACT_TOKEN_ABSURD_MAX,
    COMPACT_VS_SIGNALS_SHRINK_SLACK,
    HUB_COMPACT_GATE_GRACE_S,
    HUB_COMPACT_GATE_INFLIGHT_S,
    compact_claims_reduction,
    compact_terminal_message_should_emit,
    compact_toast_should_claim_window_shrink,
    compact_user_outcome_state,
    extract_compact_update,
    format_token_short,
    hub_compact_gate_set_grace,
    hub_compact_gate_set_inflight,
    hub_compact_gate_suppresses_notification,
    normalize_compact_notification,
    parse_compact_slash,
    resolve_compact_outcome,
    sanitize_compact_tokens,
    should_emit_compact_completed_feedback,
    usage_from_compact_tokens,
)


def test_parse_compact_bare() -> None:
    assert parse_compact_slash("/compact") == {"context": ""}
    assert parse_compact_slash("  /compact  ") == {"context": ""}
    assert parse_compact_slash("/COMPACT") == {"context": ""}


def test_parse_compact_with_context() -> None:
    assert parse_compact_slash("/compact keep auth") == {"context": "keep auth"}
    assert parse_compact_slash("/Compact  keep   auth  ") == {
        "context": "keep   auth"
    }


def test_parse_compact_non_match() -> None:
    assert parse_compact_slash("") is None
    assert parse_compact_slash("compact") is None
    assert parse_compact_slash("/compress") is None
    assert parse_compact_slash("please /compact") is None
    assert parse_compact_slash("/compacted") is None


def test_normalize_auto_compact_completed() -> None:
    body = normalize_compact_notification(
        {
            "sessionUpdate": "auto_compact_completed",
            "tokens_before": 120000,
            "tokens_after": 45000,
            "summary_preview": "Kept recent turns",
        }
    )
    assert body is not None
    assert body["state"] == "completed"
    assert body["tokensBefore"] == 120000
    assert body["tokensAfter"] == 45000
    assert body["summaryPreview"] == "Kept recent turns"
    assert body["error"] is None
    assert body["reduced"] is True
    assert body["feedback"] == "reduced"


def test_normalize_auto_compact_camel_case() -> None:
    body = normalize_compact_notification(
        {
            "sessionUpdate": "auto_compact_completed",
            "tokensBefore": 10,
            "tokensAfter": 10,
        }
    )
    assert body is not None
    assert body["tokensBefore"] == 10
    assert body["tokensAfter"] == 10
    assert body["reduced"] is False
    assert body["feedback"] == "no_change"


def test_normalize_auto_compact_started_failed() -> None:
    started = normalize_compact_notification(
        {"sessionUpdate": "auto_compact_started"}
    )
    assert started is not None
    assert started["state"] == "started"

    failed = normalize_compact_notification(
        {
            "sessionUpdate": "auto_compact_failed",
            "error": "boom",
        }
    )
    assert failed is not None
    assert failed["state"] == "failed"
    assert failed["error"] == "boom"

    cancelled = normalize_compact_notification(
        {"sessionUpdate": "auto_compact_cancelled"}
    )
    assert cancelled is not None
    assert cancelled["state"] == "cancelled"


def test_normalize_non_compact() -> None:
    assert normalize_compact_notification(None) is None
    assert normalize_compact_notification({}) is None
    assert (
        normalize_compact_notification(
            {"sessionUpdate": "agent_message_chunk"}
        )
        is None
    )


def test_sanitize_compact_tokens_rejects_absurd() -> None:
    assert sanitize_compact_tokens(120000, 45000) == (120000, 45000)
    assert sanitize_compact_tokens(-1, 10) == (None, 10)
    assert sanitize_compact_tokens(10, COMPACT_TOKEN_ABSURD_MAX + 1) == (10, None)
    assert sanitize_compact_tokens(float("nan"), float("inf")) == (None, None)
    assert sanitize_compact_tokens("nope", None) == (None, None)
    # normalize drops absurd raw values so UI never paints garbage counts
    body = normalize_compact_notification(
        {
            "sessionUpdate": "auto_compact_completed",
            "tokens_before": 375_000_000,
            "tokens_after": 12,
        }
    )
    assert body is not None
    assert body["tokensBefore"] is None
    assert body["tokensAfter"] == 12


def test_usage_from_compact_tokens() -> None:
    assert usage_from_compact_tokens(None, None) == {}
    only_window = usage_from_compact_tokens(None, 200000)
    assert only_window == {"contextWindowTokens": 200000}
    only_used = usage_from_compact_tokens(45000, None)
    assert only_used == {"contextTokensUsed": 45000}

    full = usage_from_compact_tokens(50000, 200000)
    assert full["contextTokensUsed"] == 50000
    assert full["contextWindowTokens"] == 200000
    assert full["contextPercent"] == 25.0


def test_usage_percent_clamped() -> None:
    over = usage_from_compact_tokens(300000, 200000)
    assert over["contextPercent"] == 100.0
    under = usage_from_compact_tokens(-10, 200000)
    assert under["contextPercent"] == 0.0


def test_extract_compact_update() -> None:
    msg = {
        "method": "_x.ai/session_notification",
        "params": {
            "sessionId": "abc",
            "update": {
                "sessionUpdate": "auto_compact_completed",
                "tokens_before": 1,
                "tokens_after": 1,
            },
        },
    }
    update = extract_compact_update(msg)
    assert update is not None
    assert update["sessionUpdate"] == "auto_compact_completed"
    assert extract_compact_update({"method": "session/update"}) is None
    assert (
        extract_compact_update(
            {
                "method": "x.ai/session_notification",
                "params": {"sessionUpdate": "auto_compact_started"},
            }
        )
        == {"sessionUpdate": "auto_compact_started"}
    )


def test_compact_claims_reduction() -> None:
    assert compact_claims_reduction(100, 50) is True
    assert compact_claims_reduction(50, 50) is False
    assert compact_claims_reduction(50, 60) is False
    assert compact_claims_reduction(None, 50) is False
    assert compact_claims_reduction(100, None) is False
    assert compact_claims_reduction(None, None) is False


def test_compact_toast_should_claim_window_shrink() -> None:
    """Matrix: reduction + signals agreement gate for window-shrink claims."""
    # No reduction → never claim window shrink
    assert (
        compact_toast_should_claim_window_shrink(
            compact_before=100, compact_after=100, signals_used=None
        )
        is False
    )
    assert (
        compact_toast_should_claim_window_shrink(
            compact_before=50, compact_after=80, signals_used=None
        )
        is False
    )
    assert (
        compact_toast_should_claim_window_shrink(
            compact_before=None, compact_after=10, signals_used=None
        )
        is False
    )
    assert (
        compact_toast_should_claim_window_shrink(
            compact_before=100, compact_after=None, signals_used=None
        )
        is False
    )

    # Reduction, signals unknown → trust compact reduction
    assert (
        compact_toast_should_claim_window_shrink(
            compact_before=120_000, compact_after=45_000, signals_used=None
        )
        is True
    )

    # Reduction, signals roughly agree (within slack)
    assert (
        compact_toast_should_claim_window_shrink(
            compact_before=120_000,
            compact_after=45_000,
            signals_used=50_000,
        )
        is True
    )
    assert (
        compact_toast_should_claim_window_shrink(
            compact_before=120_000,
            compact_after=45_000,
            signals_used=45_000 + COMPACT_VS_SIGNALS_SHRINK_SLACK,
        )
        is True
    )

    # Reduction, signals still much larger than after → do not claim bar is after
    assert (
        compact_toast_should_claim_window_shrink(
            compact_before=312_000,
            compact_after=19_500,
            signals_used=421_000,
        )
        is False
    )
    assert (
        compact_toast_should_claim_window_shrink(
            compact_before=120_000,
            compact_after=45_000,
            signals_used=45_000 + COMPACT_VS_SIGNALS_SHRINK_SLACK + 1,
        )
        is False
    )


def test_should_emit_compact_completed_feedback() -> None:
    assert should_emit_compact_completed_feedback(100, 40) == "reduced"
    assert should_emit_compact_completed_feedback(10, 10) == "no_change"
    assert should_emit_compact_completed_feedback(10, 20) == "no_change"
    assert should_emit_compact_completed_feedback(None, 5) == "unknown"
    assert should_emit_compact_completed_feedback(5, None) == "unknown"
    assert should_emit_compact_completed_feedback(None, None) == "unknown"


def test_format_token_short() -> None:
    assert format_token_short(421) == "421"
    assert format_token_short(421_000) in ("421K", "421.0K")
    assert format_token_short(None) == "—"


def test_resolve_compact_outcome_reduced() -> None:
    out = resolve_compact_outcome(
        signals_before_used=400_000,
        signals_after_used=120_000,
        signals_window=500_000,
    )
    assert out["reduced"] is True
    assert out["feedback"] == "reduced"
    assert out["tokensBefore"] == 400_000
    assert out["tokensAfter"] == 120_000
    assert out["windowTokens"] == 500_000
    assert "Context compacted:" in out["message"]
    assert "→" in out["message"]
    assert "already minimal" not in out["message"]


def test_resolve_compact_outcome_still_full() -> None:
    out = resolve_compact_outcome(
        signals_before_used=420_879,
        signals_after_used=420_879,
        signals_window=500_000,
    )
    assert out["reduced"] is False
    assert out["feedback"] == "no_change_still_full"
    assert out["tokensAfter"] == 420_879
    assert "420.9K" in out["message"] or "421K" in out["message"]
    assert "500K" in out["message"] or "500.0K" in out["message"]
    assert "did not free" in out["message"]
    assert "already minimal" not in out["message"]
    # Design: still full message includes the after count for 420879/500000
    assert "84%" in out["message"]


def test_resolve_compact_outcome_low() -> None:
    out = resolve_compact_outcome(
        signals_before_used=40_000,
        signals_after_used=40_000,
        signals_window=500_000,
    )
    assert out["reduced"] is False
    assert out["feedback"] == "no_change_low"
    assert "already low" in out["message"]
    assert "already minimal" not in out["message"]


def test_resolve_compact_outcome_unknown() -> None:
    out = resolve_compact_outcome(
        signals_before_used=None,
        signals_after_used=None,
    )
    assert out["reduced"] is False
    assert out["feedback"] == "unknown"
    assert "Check context bar" in out["message"]
    assert "already minimal" not in out["message"]


def test_resolve_compact_outcome_agent_claim_rejected_when_signals_high() -> None:
    """Agent claims shrink to 19.5k but signals still ~421k → still full."""
    out = resolve_compact_outcome(
        signals_before_used=None,
        signals_after_used=421_000,
        signals_window=500_000,
        agent_before=312_000,
        agent_after=19_500,
    )
    assert out["reduced"] is False
    assert out["feedback"] == "no_change_still_full"
    assert out["tokensAfter"] == 421_000
    assert "did not free" in out["message"]
    assert "already minimal" not in out["message"]
    assert "84%" in out["message"] or "421K" in out["message"]


def test_resolve_compact_outcome_agent_claim_accepted_when_signals_agree() -> None:
    out = resolve_compact_outcome(
        signals_before_used=None,
        signals_after_used=48_000,
        signals_window=500_000,
        agent_before=120_000,
        agent_after=45_000,
    )
    assert out["reduced"] is True
    assert out["feedback"] == "reduced"
    assert out["tokensAfter"] == 48_000
    assert "Context compacted:" in out["message"]


def test_resolve_compact_outcome_never_says_already_minimal() -> None:
    cases = [
        dict(signals_before_used=100, signals_after_used=100, signals_window=200),
        dict(signals_before_used=420_879, signals_after_used=420_879, signals_window=500_000),
        dict(signals_before_used=10_000, signals_after_used=10_000, signals_window=500_000),
        dict(signals_before_used=None, signals_after_used=None),
        dict(
            signals_before_used=None,
            signals_after_used=421_000,
            signals_window=500_000,
            agent_before=300_000,
            agent_after=20_000,
        ),
        dict(
            signals_before_used=200_000,
            signals_after_used=50_000,
            signals_window=500_000,
        ),
    ]
    for kwargs in cases:
        msg = resolve_compact_outcome(**kwargs)["message"]
        assert "already minimal" not in msg.lower()
        assert "nothing to free" not in msg.lower()


def test_ui_has_compact_handlers() -> None:
    """Contract: UI listens for compact/usage and session_notification."""
    root = Path(__file__).resolve().parents[1]
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert "type === \"compact\"" in js or "type === 'compact'" in js
    assert "type === \"usage\"" in js or "type === 'usage'" in js
    assert "handleCompactEvent" in js
    assert "handleUsageEvent" in js
    assert "_x.ai/session_notification" in js
    assert "already minimal" not in js
    assert "sanitizeCompactToken" in js
    assert "COMPACT_TOKEN_ABSURD_MAX" in js
    assert "compactClaimsReduction" in js
    assert "allowCompactSystemLine" in js
    assert "COMPACT_FEEDBACK_COOLDOWN_MS" in js
    # Hub message is authority for completed compact copy.
    assert "msg.message" in js
    # Scope to handleCompactEvent (other st===completed exist for plan counts).
    hce = js.find("function handleCompactEvent")
    assert hce > 0
    completed_idx = js.find('if (st === "completed")', hce)
    assert completed_idx > hce
    next_branch = js.find('if (st === "failed"', completed_idx)
    completed_block = js[completed_idx : next_branch if next_branch > 0 else None]
    assert "refreshUsage()" in completed_block
    assert (
        "applyUsagePatch(sid, { contextTokensUsed: Number(after) })"
        not in completed_block
    )
    assert "already minimal" not in completed_block
    assert "msg.message" in completed_block
    # Always delayed refresh for lag (not only when reduced).
    assert "1000" in completed_block or "1_000" in completed_block
    assert "did not free context" in completed_block
    assert "reportError" in completed_block


def test_server_broadcast_usage_prefers_signals() -> None:
    """Structural: after compact, usage broadcast prefers signals over tokens_after."""
    root = Path(__file__).resolve().parents[1]
    src = (root / "hub" / "server.py").read_text(encoding="utf-8")
    idx = src.find("async def _broadcast_usage_after_compact")
    assert idx > 0
    end = src.find("\n    def _any_subscribed", idx)
    block = src[idx : end if end > 0 else None]
    assert "read_session_signals" in block
    assert 'used = signals.get("contextTokensUsed")' in block
    # tokens_after only as fallback when signals lack used
    assert "if used is None and tokens_after is not None" in block
    # Emit path must not inject compact after into usage when signals exist
    emit_idx = src.find("async def _emit_compact_from_notification")
    assert emit_idx > 0
    emit_end = src.find("async def _broadcast_usage_after_compact", emit_idx)
    emit_block = src[emit_idx:emit_end]
    assert "tokens_after=tokens_for_usage" not in emit_block
    assert "await self._broadcast_usage_after_compact(sid)" in emit_block
    assert "resolve_compact_outcome" in emit_block


def test_server_execute_compact_reads_signals_and_resolves() -> None:
    """Structural: /compact path reads signals, maps still-full to failed."""
    root = Path(__file__).resolve().parents[1]
    src = (root / "hub" / "server.py").read_text(encoding="utf-8")
    idx = src.find("async def _execute_compact")
    assert idx > 0
    end = src.find("async def _execute_prompt", idx)
    block = src[idx : end if end > 0 else None]
    assert "read_session_signals" in block
    assert "resolve_compact_outcome" in block
    assert "compact_user_outcome_state" in block
    assert "signals_before_used" in block
    assert "signals_after_used" in block
    assert '"message"' in block or "'message'" in block
    assert "state" in block and "completed" in block
    assert '"failed"' in block or "'failed'" in block
    assert "range(10)" in block
    assert "0.5" in block
    assert "method=_x.ai/compact_conversation" in block
    assert "compact outcome reduced=" in block


def test_acp_client_has_session_compact() -> None:
    root = Path(__file__).resolve().parents[1]
    src = (root / "hub" / "acp_client.py").read_text(encoding="utf-8")
    assert "async def session_compact" in src
    assert "_x.ai/compact_conversation" in src
    # Sole method string used by request()
    idx = src.find("async def session_compact")
    end = src.find("async def session_prompt", idx)
    block = src[idx : end if end > 0 else None]
    assert '"_x.ai/compact_conversation"' in block
    assert "session/prompt" not in block or "does not" in block


def test_compact_user_outcome_state_matrix() -> None:
    """reduced → completed; still_full → failed; low/unknown-missing → completed."""
    assert (
        compact_user_outcome_state(
            reduced=True,
            feedback="reduced",
            signals_after_used=50_000,
            signals_window=500_000,
        )
        == "completed"
    )
    assert (
        compact_user_outcome_state(
            reduced=False,
            feedback="no_change_still_full",
            signals_after_used=420_879,
            signals_window=500_000,
        )
        == "failed"
    )
    assert (
        compact_user_outcome_state(
            reduced=False,
            feedback="no_change_low",
            signals_after_used=20_000,
            signals_window=500_000,
        )
        == "completed"
    )
    assert (
        compact_user_outcome_state(
            reduced=False,
            feedback="unknown",
            signals_after_used=None,
            signals_window=None,
        )
        == "completed"
    )
    assert (
        compact_user_outcome_state(
            reduced=False,
            feedback="unknown",
            signals_after_used=10_000,
            signals_window=500_000,
        )
        == "completed"
    )
    assert (
        compact_user_outcome_state(
            reduced=False,
            feedback="unknown",
            signals_after_used=420_000,
            signals_window=500_000,
        )
        == "failed"
    )


def test_extract_compact_from_session_update() -> None:
    """auto_compact on session/update is extractable and signals-grounded to failed."""
    msg = {
        "method": "_x.ai/session/update",
        "params": {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "auto_compact_completed",
                "tokensBefore": 312_000,
                "tokensAfter": 19_500,
            },
        },
    }
    u = extract_compact_update(msg)
    assert u is not None
    assert u.get("sessionUpdate") == "auto_compact_completed"
    out = resolve_compact_outcome(
        signals_before_used=None,
        signals_after_used=421_000,
        signals_window=500_000,
        agent_before=312_000,
        agent_after=19_500,
    )
    assert out["reduced"] is False
    assert out["feedback"] == "no_change_still_full"
    assert "did not free" in out["message"].lower()
    st = compact_user_outcome_state(
        reduced=out["reduced"],
        feedback=out["feedback"],
        signals_after_used=out["tokensAfter"],
        signals_window=out["windowTokens"],
    )
    assert st == "failed"


def test_raw_agent_tokens_must_not_claim_bar_shrink_when_signals_high() -> None:
    assert compact_claims_reduction(312_000, 19_500) is True
    assert (
        compact_toast_should_claim_window_shrink(
            compact_before=312_000,
            compact_after=19_500,
            signals_used=420_879,
        )
        is False
    )


def test_server_grounds_session_update_auto_compact() -> None:
    src = (Path(__file__).resolve().parents[1] / "hub" / "server.py").read_text(
        encoding="utf-8"
    )
    emit = src[src.find("async def _emit_acp") : src.find("async def _emit_acp") + 2500]
    assert "auto_compact_" in emit
    assert "_emit_compact_from_notification" in emit
    assert "startswith" in emit and "auto_compact_" in emit


def test_ui_ungrounded_notification_no_green_shrink() -> None:
    js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    idx = js.find("function handleCompactNotification")
    chunk = js[idx : idx + 2200]
    assert "never claim shrink from raw agent tokens" in chunk
    assert "refreshUsage()" in chunk
    assert "msg.reduced === true" in js
    assert "force: true" in js or "opts.force" in js


def test_hub_compact_gate_inflight_suppresses_terminal_states() -> None:
    """In-flight gate suppresses started/completed/failed/cancelled; expires after TTL."""
    gate: dict[str, float] = {}
    sid = "sess-a"
    t0 = 100.0
    hub_compact_gate_set_inflight(gate, sid, now=t0)
    assert gate[sid] == t0 + HUB_COMPACT_GATE_INFLIGHT_S
    for st in ("started", "completed", "failed", "cancelled"):
        assert (
            hub_compact_gate_suppresses_notification(gate, sid, st, now=t0) is True
        )
    # Unknown / empty states are not blocked while gate is live.
    assert (
        hub_compact_gate_suppresses_notification(gate, sid, "unknown", now=t0)
        is False
    )
    assert (
        hub_compact_gate_suppresses_notification(gate, sid, "", now=t0) is False
    )
    # Expired: no suppress; entry popped.
    t_exp = t0 + HUB_COMPACT_GATE_INFLIGHT_S + 1.0
    assert (
        hub_compact_gate_suppresses_notification(
            gate, sid, "completed", now=t_exp
        )
        is False
    )
    assert sid not in gate


def test_hub_compact_gate_grace_window() -> None:
    """Post-execute grace suppresses briefly, then allows notification path again."""
    gate: dict[str, float] = {}
    sid = "sess-b"
    hub_compact_gate_set_grace(gate, sid, now=0.0)
    assert gate[sid] == HUB_COMPACT_GATE_GRACE_S
    assert (
        hub_compact_gate_suppresses_notification(
            gate, sid, "completed", now=10.0
        )
        is True
    )
    assert (
        hub_compact_gate_suppresses_notification(
            gate, sid, "completed", now=16.0
        )
        is False
    )
    assert sid not in gate


def test_compact_terminal_message_should_emit_dedupes_identical() -> None:
    last: dict[str, dict] = {}
    sid = "s1"
    msg = (
        "Compact did not free context — session still 421K / 500K (84%)."
    )
    assert (
        compact_terminal_message_should_emit(
            last, sid, msg, now_ms=1_000
        )
        is True
    )
    assert (
        compact_terminal_message_should_emit(
            last, sid, msg, now_ms=1_000 + 5_000
        )
        is False
    )
    assert (
        compact_terminal_message_should_emit(
            last,
            sid,
            msg,
            now_ms=1_000 + COMPACT_TERMINAL_MSG_DEDUPE_MS,
        )
        is True
    )


def test_compact_terminal_message_should_emit_dedupes_both_did_not_free() -> None:
    last: dict[str, dict] = {}
    sid = "s2"
    a = "Compact did not free context — session still 421K / 500K (84%)."
    b = "Compact did not free context — session still ~400K tokens."
    assert compact_terminal_message_should_emit(last, sid, a, now_ms=0) is True
    assert (
        compact_terminal_message_should_emit(last, sid, b, now_ms=3_000)
        is False
    )


def test_server_hub_compact_gate_suppresses_notification_terminal() -> None:
    """Hub /compact owns terminal outcome; gate covers in-flight + post grace."""
    root = Path(__file__).resolve().parents[1]
    src = (root / "hub" / "server.py").read_text(encoding="utf-8")
    assert "_hub_compact_gate" in src
    assert "_hub_owned_compact" not in src

    exec_idx = src.find("async def _execute_compact")
    assert exec_idx > 0
    body_idx = src.find("async def _execute_compact_body", exec_idx)
    assert body_idx > exec_idx
    exec_block = src[exec_idx:body_idx]
    # Wired pure helpers (not raw deadline arithmetic).
    assert "hub_compact_gate_set_inflight" in exec_block
    assert "hub_compact_gate_set_grace" in exec_block
    assert "finally:" in exec_block

    emit_idx = src.find("async def _emit_compact_from_notification")
    assert emit_idx > 0
    emit_end = src.find("async def _broadcast_usage_after_compact", emit_idx)
    emit_block = src[emit_idx:emit_end]
    assert "hub_compact_gate_suppresses_notification" in emit_block
    assert "_hub_compact_gate" in emit_block
    assert "time.monotonic()" in emit_block
    # While gate active: still refresh usage on completed, do not broadcast compact.
    assert 'body["state"] == "completed"' in emit_block
    assert "await self._broadcast_usage_after_compact(sid)" in emit_block
    assert "return" in emit_block
    assert 'await self.broadcast(payload, session_id=sid)' in emit_block


def test_ui_compact_terminal_message_dedupe() -> None:
    """UI suppresses duplicate terminal compact lines; window matches Python."""
    root = Path(__file__).resolve().parents[1]
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert "COMPACT_TERMINAL_MSG_DEDUPE_MS" in js
    assert "COMPACT_TERMINAL_MSG_DEDUPE_MS = 12000" in js
    assert COMPACT_TERMINAL_MSG_DEDUPE_MS == 12000
    assert "allowCompactSystemLine" in js
    allow_idx = js.find("function allowCompactSystemLine")
    assert allow_idx > 0
    # Through handleCompactEvent start is enough context for the gate helpers.
    hce = js.find("function handleCompactEvent", allow_idx)
    allow_block = js[allow_idx : hce if hce > allow_idx else allow_idx + 2500]
    assert "_lastCompactTerminal" in allow_block
    assert "did not free context" in allow_block
    assert "COMPACT_TERMINAL_MSG_DEDUPE_MS" in allow_block
    # Message-level match returns false before painting a second identical line.
    assert "return false" in allow_block
    # Terminal slot normalization so failed vs completed-still-full share a key.
    hce_block = js[hce : hce + 900]
    assert ":terminal:" in hce_block
    assert "isTerminal" in hce_block
