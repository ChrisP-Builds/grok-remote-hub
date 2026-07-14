"""Hub-reachable e2e: stream visibility without multi-minute model waits.

Injects ACP session/update chunks via window.__hubTestHooks (same paint path
as live stream). Skipped when hub is down.

  python -m pytest tests/test_e2e_stream_visibility.py -q

Env:
  HUB_URL  default http://127.0.0.1:8787
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

HUB = os.environ.get("HUB_URL", "http://127.0.0.1:8787").rstrip("/")


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
]


def test_health_turn_state_fields_present() -> None:
    """GET /health exposes fields used to observe stuck / live turns."""
    with urllib.request.urlopen(f"{HUB}/health", timeout=5) as r:
        health = json.loads(r.read().decode("utf-8"))
    assert health.get("ok") is True
    assert "turnRunning" in health
    assert "acpConnected" in health
    assert "liveTurns" in health
    assert isinstance(health["liveTurns"], list)


@pytest.mark.skipif(
    not _playwright_available(),
    reason="playwright package not installed (pip install playwright)",
)
def test_injected_agent_chunk_appears_in_transcript_quickly() -> None:
    """Agent message chunk must paint within 5s (not multi-minute empty wait)."""
    from playwright.sync_api import sync_playwright

    marker = "E2E_STREAM_MARKER_xyz"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(HUB, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_selector("#app", timeout=15_000)
            page.wait_for_function(
                "() => !!(window.__hubTestHooks && window.__hubTestHooks.injectAcpSessionUpdate)",
                timeout=10_000,
            )
            page.evaluate(
                """([kind, text, sid]) => {
                  window.__hubTestHooks.injectAcpSessionUpdate(kind, text, sid);
                }""",
                ["agent_message_chunk", marker, "e2e-stream-sid"],
            )
            page.wait_for_function(
                """(m) => {
                  const h = window.__hubTestHooks;
                  return !!(h && h.transcriptTextIncludes(m));
                }""",
                arg=marker,
                timeout=5_000,
            )
            assert page.evaluate(
                "(m) => window.__hubTestHooks.transcriptTextIncludes(m)",
                marker,
            )
        finally:
            browser.close()


@pytest.mark.skipif(
    not _playwright_available(),
    reason="playwright package not installed (pip install playwright)",
)
def test_injected_thought_appears_open() -> None:
    """Thought chunk must create an open .term-line.thought within 5s."""
    from playwright.sync_api import sync_playwright

    marker = "E2E_THOUGHT_MARKER_abc"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(HUB, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_selector("#app", timeout=15_000)
            page.wait_for_function(
                "() => !!(window.__hubTestHooks && window.__hubTestHooks.injectAcpSessionUpdate)",
                timeout=10_000,
            )
            page.evaluate(
                """([kind, text, sid]) => {
                  window.__hubTestHooks.injectAcpSessionUpdate(kind, text, sid);
                }""",
                ["agent_thought_chunk", marker, "e2e-thought-sid"],
            )
            page.wait_for_function(
                """(m) => {
                  const h = window.__hubTestHooks;
                  if (!h || !h.transcriptHasRole("thought")) return false;
                  if (!h.transcriptTextIncludes(m)) return false;
                  const root = document.querySelector(".session-pane:not([hidden])")
                    || document.getElementById("transcript");
                  const el = root && root.querySelector(".term-line.thought");
                  if (!el) return false;
                  // details open attribute or visible body text
                  if (el.tagName === "DETAILS") return !!el.open;
                  return true;
                }""",
                arg=marker,
                timeout=5_000,
            )
        finally:
            browser.close()
