"""Structural UI tests for version badge, session banner, product copy."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def test_html_version_badge_and_banner() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="version-badge"' in html
    assert 'id="version-label"' in html
    assert 'id="compat-dot"' in html
    assert 'id="session-banner"' in html
    assert 'id="session-banner-text"' in html
    assert "No session selected" in html
    assert "Pick a chat from the sidebar" in html
    # Product positioning: not full CLI clone
    assert "full Grok CLI" not in html


def test_js_session_mode_and_version_state() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "hubVersion" in js
    assert "cliVersion" in js
    assert "compatOk" in js
    assert "hubSessionIds" in js
    assert "sessionMode" in js
    assert "updateVersionBadge" in js
    assert "updateSessionBanner" in js
    assert "setSessionMode" in js
    assert "live-remote" in js
    assert "Viewing saved history" in js
    assert "Live remote session" in js
    assert "Desktop TUI history is separate" in js or "TUI stays separate" in js
    assert "/attach" in js
    assert "No session selected" in js
    assert "isHubCreatedSession" in js
    # TUI-aligned client: soft warn at 120s; never auto reset-turn / unlock
    assert "CLIENT_STALL_WARN_MS" in js
    assert "120000" in js
    assert "Still working (like desktop TUI)" in js
    assert "Turn unlocked after 90s" not in js
    assert "requestResetTurn" not in js


def test_css_version_and_banner() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".version-badge" in css
    assert ".compat-dot" in css
    assert ".session-banner" in css
    assert 'data-state="ok"' in css or '[data-state="ok"]' in css
    assert 'data-state="warn"' in css or '[data-state="warn"]' in css
    assert ".empty-sub" in css


def test_readme_product_scope() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    # Product scope is stated in plain language (heading optional).
    assert "remote control plane for the agent stream" in readme
    assert "not full tui parity" in readme.lower()
    assert "/api/compat" in readme
