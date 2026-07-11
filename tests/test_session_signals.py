"""Unit tests for session signals / context usage normalization."""

from __future__ import annotations

import json
from pathlib import Path

from hub.session_signals import (
    find_signals_path,
    normalize_signals,
    read_session_signals,
    read_signals_file,
)


def _write_session(
    root: Path,
    session_id: str,
    signals: dict | None = None,
    *,
    title: str = "Test session",
) -> Path:
    """Create a minimal session dir with summary.json (+ optional signals.json)."""
    session_dir = root / "project" / session_id
    session_dir.mkdir(parents=True)
    summary = {
        "generated_title": title,
        "num_chat_messages": 1,
        "updated_at": "2026-07-10T12:00:00Z",
        "current_model_id": "grok-test",
        "info": {"id": session_id, "cwd": str(root / "cwd")},
    }
    (session_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    if signals is not None:
        (session_dir / "signals.json").write_text(json.dumps(signals), encoding="utf-8")
    return session_dir


def test_normalize_context_window_usage() -> None:
    result = normalize_signals(
        {
            "contextWindowUsage": 36,
            "contextTokensUsed": 72000,
            "contextWindowTokens": 200000,
        }
    )
    assert result["contextPercent"] == 36.0
    assert result["contextTokensUsed"] == 72000
    assert result["contextWindowTokens"] == 200000
    assert result["monthlyPercent"] is None
    assert result["isMonthly"] is False


def test_normalize_computes_percent_from_tokens() -> None:
    result = normalize_signals(
        {
            "contextTokensUsed": 50,
            "contextWindowTokens": 200,
        }
    )
    assert result["contextPercent"] == 25.0


def test_normalize_prefers_context_window_usage() -> None:
    result = normalize_signals(
        {
            "contextWindowUsage": 10,
            "contextTokensUsed": 50,
            "contextWindowTokens": 200,
        }
    )
    assert result["contextPercent"] == 10.0


def test_normalize_clamps_percent() -> None:
    assert normalize_signals({"contextWindowUsage": 150})["contextPercent"] == 100.0
    assert normalize_signals({"contextWindowUsage": -5})["contextPercent"] == 0.0


def test_normalize_monthly_keys() -> None:
    for key in ("monthlyUsagePercent", "monthly_usage_percent", "usageMonthlyPercent"):
        result = normalize_signals({key: 12.5, "contextWindowUsage": 40})
        assert result["monthlyPercent"] == 12.5
        assert result["isMonthly"] is True
        assert result["contextPercent"] == 40.0


def test_normalize_nested_usage_monthly() -> None:
    result = normalize_signals(
        {
            "contextWindowUsage": 20,
            "usage": {"monthlyPercent": 8},
        }
    )
    assert result["monthlyPercent"] == 8.0
    assert result["isMonthly"] is True


def test_normalize_is_monthly_flag() -> None:
    result = normalize_signals({"contextWindowUsage": 5, "isMonthly": True})
    assert result["isMonthly"] is True
    assert result["monthlyPercent"] is None


def test_normalize_usage_period_monthly() -> None:
    result = normalize_signals({"contextWindowUsage": 5, "usagePeriod": "monthly"})
    assert result["isMonthly"] is True


def test_normalize_empty() -> None:
    result = normalize_signals(None)
    assert result["contextPercent"] is None
    assert result["contextTokensUsed"] is None
    assert result["contextWindowTokens"] is None
    assert result["monthlyPercent"] is None
    assert result["isMonthly"] is False


def test_read_session_signals_from_disk(tmp_path: Path) -> None:
    sid = "019f0b7b-dfb9-7382-8721-67d0c24caba7"
    _write_session(
        tmp_path,
        sid,
        {
            "contextWindowUsage": 36,
            "contextTokensUsed": 72000,
            "contextWindowTokens": 200000,
            "monthlyUsagePercent": 12,
        },
    )
    result = read_session_signals(tmp_path, sid)
    assert result["contextPercent"] == 36.0
    assert result["contextTokensUsed"] == 72000
    assert result["contextWindowTokens"] == 200000
    assert result["monthlyPercent"] == 12.0
    assert result["isMonthly"] is True


def test_read_session_signals_missing(tmp_path: Path) -> None:
    sid = "019f0b7b-dfb9-7382-8721-67d0c24caba8"
    _write_session(tmp_path, sid, signals=None)
    result = read_session_signals(tmp_path, sid)
    assert result["contextPercent"] is None
    assert result["isMonthly"] is False


def test_find_signals_path(tmp_path: Path) -> None:
    sid = "019f0b7b-dfb9-7382-8721-67d0c24caba9"
    session_dir = _write_session(
        tmp_path,
        sid,
        {"contextWindowUsage": 1},
    )
    path = find_signals_path(tmp_path, sid)
    assert path is not None
    assert path == session_dir / "signals.json"
    raw = read_signals_file(path)
    assert raw is not None
    assert raw["contextWindowUsage"] == 1


def test_read_session_signals_unknown_id(tmp_path: Path) -> None:
    result = read_session_signals(tmp_path, "00000000-0000-0000-0000-000000000000")
    assert result["contextPercent"] is None
