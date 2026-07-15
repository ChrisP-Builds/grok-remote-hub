"""Unit tests for /compact helpers and notification normalization."""

from __future__ import annotations

from pathlib import Path

from hub.compact import (
    COMPACT_TOKEN_ABSURD_MAX,
    extract_compact_update,
    normalize_compact_notification,
    parse_compact_slash,
    sanitize_compact_tokens,
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


def test_ui_has_compact_handlers() -> None:
    """Contract: UI listens for compact/usage and session_notification."""
    root = Path(__file__).resolve().parents[1]
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert "type === \"compact\"" in js or "type === 'compact'" in js
    assert "type === \"usage\"" in js or "type === 'usage'" in js
    assert "handleCompactEvent" in js
    assert "handleUsageEvent" in js
    assert "_x.ai/session_notification" in js
    assert "Context compacted:" in js
    assert "already minimal" in js
    assert "sanitizeCompactToken" in js
    assert "Context compact finished" in js
    assert "COMPACT_TOKEN_ABSURD_MAX" in js


def test_acp_client_has_session_compact() -> None:
    root = Path(__file__).resolve().parents[1]
    src = (root / "hub" / "acp_client.py").read_text(encoding="utf-8")
    assert "async def session_compact" in src
    assert "_x.ai/compact_conversation" in src
