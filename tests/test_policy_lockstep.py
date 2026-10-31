"""Lockstep: Python session_policy constants/helpers match static/app.js mirrors."""

from __future__ import annotations

import re
from pathlib import Path

from hub.session_policy import (
    CLIENT_STALL_UNLOCK_SECONDS,
    CLIENT_STALL_WARN_SECONDS,
    should_clear_turn_on_wake,
)

ROOT = Path(__file__).resolve().parents[1]
JS_PATH = ROOT / "static" / "app.js"


def _js_source() -> str:
    return JS_PATH.read_text(encoding="utf-8")


def test_wake_clear_silence_matches_client_stall_warn() -> None:
    js = _js_source()
    m = re.search(
        r"const\s+WAKE_CLEAR_SILENCE_SECONDS\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*;",
        js,
    )
    assert m, "WAKE_CLEAR_SILENCE_SECONDS must be defined in app.js"
    js_val = float(m.group(1))
    assert js_val == float(CLIENT_STALL_WARN_SECONDS)


def test_client_stall_warn_ms_matches_seconds() -> None:
    js = _js_source()
    m = re.search(r"const\s+CLIENT_STALL_WARN_MS\s*=\s*([0-9]+)\s*;", js)
    assert m, "CLIENT_STALL_WARN_MS must be defined in app.js"
    js_ms = int(m.group(1))
    assert js_ms == int(CLIENT_STALL_WARN_SECONDS * 1000)


def test_unlock_disabled_in_python_and_js() -> None:
    assert CLIENT_STALL_UNLOCK_SECONDS == 0
    js = _js_source()
    # JS documents unlock disabled; must not define a positive auto-unlock timer.
    assert "CLIENT_STALL_UNLOCK_SECONDS = 0" in js or "CLIENT_STALL_UNLOCK" in js
    # No positive unlock constant that would auto reset-turn
    bad = re.search(
        r"const\s+CLIENT_STALL_UNLOCK_MS\s*=\s*([1-9][0-9]*)\s*;",
        js,
    )
    assert bad is None, "CLIENT_STALL_UNLOCK_MS must not be a positive auto-unlock"


def test_should_clear_turn_on_wake_default_uses_client_stall_warn() -> None:
    # Default silence_threshold_s must track CLIENT_STALL_WARN_SECONDS.
    assert (
        should_clear_turn_on_wake(
            turn_running=True,
            silence_seconds=CLIENT_STALL_WARN_SECONDS,
            acp_quality="ok",
        )
        is True
    )
    assert (
        should_clear_turn_on_wake(
            turn_running=True,
            silence_seconds=CLIENT_STALL_WARN_SECONDS - 1.0,
            acp_quality="ok",
        )
        is False
    )


def test_should_clear_turn_on_wake_quality_and_open_tools() -> None:
    assert (
        should_clear_turn_on_wake(
            turn_running=True,
            silence_seconds=None,
            acp_quality="stale",
        )
        is True
    )
    assert (
        should_clear_turn_on_wake(
            turn_running=True,
            silence_seconds=None,
            acp_quality="zombie",
        )
        is True
    )
    assert (
        should_clear_turn_on_wake(
            turn_running=True,
            silence_seconds=None,
            acp_quality="down",
        )
        is True
    )
    # Open tools + ok quality: silence alone does not clear
    assert (
        should_clear_turn_on_wake(
            turn_running=True,
            silence_seconds=CLIENT_STALL_WARN_SECONDS + 50,
            acp_quality="ok",
            has_open_tools=True,
        )
        is False
    )
    # Open tools + stale still clears
    assert (
        should_clear_turn_on_wake(
            turn_running=True,
            silence_seconds=CLIENT_STALL_WARN_SECONDS + 50,
            acp_quality="stale",
            has_open_tools=True,
        )
        is True
    )


def test_app_js_mirrors_wake_clear_quality_and_open_tools() -> None:
    js = _js_source()
    assert "shouldClearTurnOnWake" in js
    assert "stale" in js and "zombie" in js and "down" in js
    # Quality check path in the mirror
    assert 'q === "stale"' in js or 'q === "stale" || q === "zombie"' in js
    assert "hasOpenTools" in js
    # Gate: open tools + ok/empty quality keeps turn
    assert "hasOpenTools" in js and ('q === ""' in js or 'q === "ok"' in js)
