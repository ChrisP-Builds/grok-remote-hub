"""Playwright smoke against a running hub (optional).

Skipped automatically when the hub is down or Playwright is not installed.

  python -m pip install playwright
  python -m playwright install chromium
  python -m pytest tests/test_e2e_smoke.py -q

Env:
  HUB_URL  default http://127.0.0.1:8787
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

HUB = os.environ.get("HUB_URL", "http://127.0.0.1:8787").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]


def _hub_ok() -> bool:
    try:
        with urllib.request.urlopen(f"{HUB}/health", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        return bool(data.get("ok"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return False


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark = [
    pytest.mark.skipif(not _hub_ok(), reason=f"hub not reachable at {HUB}"),
    pytest.mark.skipif(
        not _playwright_available(),
        reason="playwright package not installed (pip install playwright)",
    ),
]


def test_health_and_sessions_api_fields() -> None:
    with urllib.request.urlopen(f"{HUB}/health", timeout=5) as r:
        health = json.loads(r.read().decode("utf-8"))
    assert health.get("ok") is True

    with urllib.request.urlopen(f"{HUB}/api/sessions", timeout=15) as r:
        body = json.loads(r.read().decode("utf-8"))
    items = body.get("items") or []
    assert isinstance(items, list)
    if items:
        s0 = items[0]
        assert "sessionId" in s0
        assert "isSubagent" in s0
        assert "isWorking" in s0


def test_ui_empty_state_and_filters() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(HUB, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_selector("#empty-main", timeout=15_000)
            assert page.locator("h2", has_text="No session selected").count() >= 1
            assert page.locator('.kind-chip[data-kind="working"]').count() == 1
            assert page.locator('.kind-chip[data-kind="subagent"]').count() == 1
            assert page.locator('.kind-chip[data-kind="all"]').count() == 1
            assert page.locator("#composer-input").get_attribute("spellcheck") == "true"
            assert page.locator("#meta-popover").count() == 1
        finally:
            browser.close()


def test_open_working_session_and_meta_bubble() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(HUB, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_selector(".kind-chip", timeout=15_000)
            page.locator('.kind-chip[data-kind="working"]').click()
            # Wait for session list paint
            page.wait_for_timeout(500)
            rows = page.locator(".session-row")
            if rows.count() == 0:
                pytest.skip("No working sessions on this machine")
            rows.first.click()
            # empty-main uses .hidden class or hidden attribute
            page.wait_for_function(
                """() => {
                  const el = document.getElementById('empty-main');
                  if (!el) return true;
                  return el.hidden || el.classList.contains('hidden')
                    || getComputedStyle(el).display === 'none';
                }""",
                timeout=20_000,
            )
            title = page.locator("#chat-title").inner_text(timeout=10_000)
            assert title.strip() and title.strip() != "Select a session"

            project = page.locator("#chat-project")
            # Wait until project chip visible or cwd has text
            page.wait_for_function(
                """() => {
                  const p = document.getElementById('chat-project');
                  const c = document.getElementById('chat-cwd');
                  const pVis = p && !p.classList.contains('hidden') && (p.textContent||'').trim();
                  const cTxt = c && (c.textContent||'').trim();
                  return !!(pVis || cTxt);
                }""",
                timeout=20_000,
            )
            if project.is_visible():
                project.hover()
                pop = page.locator("#meta-popover")
                pop.wait_for(state="visible", timeout=8_000)
                text = pop.inner_text()
                assert "Project" in text
                assert "Path" in text
        finally:
            browser.close()
