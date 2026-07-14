"""Playwright checks for CLI-style Reload recovery + New-vs-Resume entry.

Drives real shipped client paths via window.__hubTestHooks (no theater filters).

Skipped when hub is down or Playwright is not installed (same pattern as smoke).

  python -m pytest tests/test_e2e_cli_resume.py -q
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


def _fetch_sessions() -> list[dict]:
    with urllib.request.urlopen(f"{HUB}/api/sessions", timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    return list(data.get("items") or [])


def _working_sessions(items: list[dict]) -> list[dict]:
    return [i for i in items if i and not i.get("isSubagent")]


def _hub_remote_sessions(items: list[dict]) -> list[dict]:
    """Prefer hub-owned sessions so attach/resume can succeed."""
    remote = [i for i in _working_sessions(items) if i.get("isHubRemote")]
    if remote:
        return remote
    return _working_sessions(items)


pytestmark = [
    pytest.mark.skipif(not _hub_ok(), reason=f"hub not reachable at {HUB}"),
    pytest.mark.skipif(
        not _playwright_available(),
        reason="playwright package not installed (pip install playwright)",
    ),
]


def test_reload_and_entry_choice_structure() -> None:
    """Structural smoke: Reload + entry-choice DOM exist."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(HUB, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_selector("#empty-main", timeout=15_000)

            assert page.locator("#btn-reload").count() == 1
            assert page.locator("#btn-stop").count() == 1

            page.locator("#btn-new").click()
            page.wait_for_selector("#modal-new:not(.hidden)", timeout=10_000)
            assert page.locator("#project-entry-choice").count() == 1
            assert page.locator("#btn-entry-start-new").count() == 1
            assert page.locator("#btn-entry-back").count() == 1
            assert page.locator("#project-list-view").count() == 1
            page.locator('#modal-new button[data-close="modal-new"]').click()
            page.wait_for_function(
                "() => document.getElementById('modal-new')?.classList.contains('hidden')",
                timeout=10_000,
            )
        finally:
            browser.close()


def test_sessions_matching_cwd_via_shipped_hook() -> None:
    """Seed state.sessions and call shipped sessionsMatchingCwd (not reimplemented)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(HUB, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_function(
                "() => !!(window.__hubTestHooks && "
                "window.__hubTestHooks.setSessionsForTest && "
                "window.__hubTestHooks.sessionsMatchingCwd)",
                timeout=15_000,
            )
            result = page.evaluate(
                """() => {
                  const h = window.__hubTestHooks;
                  if (!h || typeof h.setSessionsForTest !== "function") {
                    return { ok: false, reason: "setSessionsForTest missing" };
                  }
                  if (typeof h.sessionsMatchingCwd !== "function") {
                    return { ok: false, reason: "sessionsMatchingCwd missing" };
                  }
                  if (typeof h.entryRequiresResumeChoice !== "function") {
                    return { ok: false, reason: "entryRequiresResumeChoice missing" };
                  }
                  h.setSessionsForTest([
                    {
                      sessionId: "sess-old",
                      cwd: "D:\\\\Projects\\\\Demo",
                      updatedAt: "2026-01-01T00:00:00Z",
                      isSubagent: false,
                      title: "Old",
                    },
                    {
                      sessionId: "sess-new",
                      cwd: "d:/Projects/Demo/",
                      updatedAt: "2026-07-01T00:00:00Z",
                      isSubagent: false,
                      title: "New",
                    },
                    {
                      sessionId: "sess-sub",
                      cwd: "D:\\\\Projects\\\\Demo",
                      updatedAt: "2026-07-02T00:00:00Z",
                      isSubagent: true,
                      title: "Sub",
                    },
                    {
                      sessionId: "sess-other",
                      cwd: "D:\\\\Projects\\\\Other",
                      updatedAt: "2026-07-03T00:00:00Z",
                      isSubagent: false,
                      title: "Other",
                    },
                  ]);
                  const matched = h.sessionsMatchingCwd("D:\\\\Projects\\\\Demo");
                  const matchedIds = (matched || []).map((s) => s.sessionId);
                  return {
                    ok: true,
                    matchedIds,
                    entry2: h.entryRequiresResumeChoice(2),
                    entry0: h.entryRequiresResumeChoice(0),
                  };
                }"""
            )
            assert result.get("ok") is True, result
            assert result.get("matchedIds") == ["sess-new", "sess-old"], result
            assert result.get("entry2") is True
            assert result.get("entry0") is False
        finally:
            browser.close()


def test_reload_resume_same_id_via_hook() -> None:
    """Drive real reloadResumeSession on a hub session; same selectedId recovery."""
    from playwright.sync_api import sync_playwright

    items = _fetch_sessions()
    candidates = _hub_remote_sessions(items)
    if not candidates:
        pytest.skip("No sessions to resume")

    # Prefer isHubRemote for attach success; fall back to first working.
    session = candidates[0]
    sid = str(session.get("sessionId") or "").strip()
    if not sid:
        pytest.skip("No sessions to resume")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(HUB, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_function(
                "() => !!(window.__hubTestHooks && "
                "window.__hubTestHooks.reloadResumeSession && "
                "window.__hubTestHooks.openSession && "
                "window.__hubTestHooks.getSessionIdsForTest)",
                timeout=15_000,
            )

            # Open via shipped openSession (real attach path).
            opened = page.evaluate(
                """async (row) => {
                  const h = window.__hubTestHooks;
                  const out = await h.openSession(row);
                  const ids = h.getSessionIdsForTest();
                  return { out, ids };
                }""",
                session,
            )
            assert opened.get("ids", {}).get("selectedId") == sid, opened

            # Reload button visible when session selected.
            page.wait_for_function(
                "() => { const b = document.getElementById('btn-reload'); "
                "return b && !b.classList.contains('hidden'); }",
                timeout=10_000,
            )

            # Optional reset-turn so clear succeeds even if mid-turn.
            page.evaluate(
                """async (sid) => {
                  try {
                    await fetch('/api/admin/reset-turn', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ sessionId: sid }),
                    });
                  } catch (_) {}
                }""",
                sid,
            )

            result = page.evaluate(
                """async () => {
                  const h = window.__hubTestHooks;
                  const before = h.getSessionIdsForTest();
                  const out = await h.reloadResumeSession();
                  const after = h.getSessionIdsForTest();
                  return { out, before, after };
                }"""
            )
            out = result.get("out") or {}
            before = result.get("before") or {}
            after = result.get("after") or {}

            # clear_failed is a real hub bug for admin; fail hard.
            assert out.get("reason") != "clear_failed", result

            if out.get("reason") == "attach_failed":
                # Foreign CLI-only id: cannot fully resume live attach.
                if not session.get("isHubRemote"):
                    pytest.skip(
                        f"attach failed for non-hub session {sid}; need isHubRemote"
                    )
                pytest.fail(f"hub-remote session attach failed on reload: {result}")

            assert out.get("ok") is True, result
            assert out.get("expectedView") == sid or after.get("selectedId") == sid, result
            assert before.get("selectedId") == sid, result
            assert after.get("selectedId") == sid, result
            assert after.get("selectedId") == before.get("selectedId") == sid, result

            # Click path when possible: reload again via button.
            if page.locator("#btn-reload:not(.hidden)").count() == 1:
                page.locator("#btn-reload").click()
                page.wait_for_function(
                    """(sid) => {
                      const b = document.getElementById('btn-reload');
                      const h = window.__hubTestHooks;
                      if (!h || !h.getSessionIdsForTest) return false;
                      const ids = h.getSessionIdsForTest();
                      // Reload finished: button visible again, same selected id.
                      return (
                        !!b &&
                        !b.classList.contains('hidden') &&
                        ids.selectedId === sid
                      );
                    }""",
                    arg=sid,
                    timeout=20_000,
                )
                after_click = page.evaluate(
                    "() => window.__hubTestHooks.getSessionIdsForTest()"
                )
                assert after_click.get("selectedId") == sid, after_click
        finally:
            browser.close()


def test_new_modal_resume_vs_start_new_with_priors() -> None:
    """Entry choice: priors → Resume opens prior; Back returns to project list."""
    from playwright.sync_api import sync_playwright

    items = _working_sessions(_fetch_sessions())
    cwd_with_priors: str | None = None
    priors_for_cwd: list[dict] = []
    # Group by normalized cwd string as client sees it.
    by_cwd: dict[str, list[dict]] = {}
    for it in items:
        cwd = str(it.get("cwd") or "").strip()
        if not cwd:
            continue
        by_cwd.setdefault(cwd, []).append(it)
    for cwd, rows in by_cwd.items():
        if len(rows) >= 1:
            cwd_with_priors = cwd
            priors_for_cwd = rows
            break

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(HUB, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_function(
                "() => !!(window.__hubTestHooks && "
                "window.__hubTestHooks.onProjectChosen && "
                "window.__hubTestHooks.setSessionsForTest)",
                timeout=15_000,
            )

            if not cwd_with_priors:
                # Inject synthetic priors for a known path.
                cwd_with_priors = r"D:\Projects\Demo"
                priors_for_cwd = [
                    {
                        "sessionId": "sess-prior-a",
                        "cwd": cwd_with_priors,
                        "updatedAt": "2026-07-01T00:00:00Z",
                        "isSubagent": False,
                        "title": "Prior A",
                    },
                    {
                        "sessionId": "sess-prior-b",
                        "cwd": cwd_with_priors,
                        "updatedAt": "2026-06-01T00:00:00Z",
                        "isSubagent": False,
                        "title": "Prior B",
                    },
                ]
                page.evaluate(
                    """(items) => {
                      window.__hubTestHooks.setSessionsForTest(items);
                    }""",
                    priors_for_cwd,
                )
            else:
                # Ensure client state has the same sessions for matching.
                page.evaluate(
                    """(items) => {
                      window.__hubTestHooks.setSessionsForTest(items);
                    }""",
                    items,
                )

            # Open New modal then drive entry choice via onProjectChosen.
            page.locator("#btn-new").click()
            page.wait_for_selector("#modal-new:not(.hidden)", timeout=10_000)

            page.evaluate(
                """async (cwd) => {
                  await window.__hubTestHooks.onProjectChosen(cwd);
                }""",
                cwd_with_priors,
            )

            # Entry choice visible (not hidden).
            page.wait_for_function(
                """() => {
                  const el = document.getElementById('project-entry-choice');
                  return el && !el.classList.contains('hidden');
                }""",
                timeout=10_000,
            )
            entry = page.locator("#project-entry-choice")
            assert "hidden" not in (entry.get_attribute("class") or "").split()

            # At least one Resume button in priors list.
            resume_btns = page.locator("#project-entry-priors button")
            assert resume_btns.count() >= 1

            # Start new present.
            assert page.locator("#btn-entry-start-new").is_visible()

            # Back returns to project list.
            page.locator("#btn-entry-back").click()
            page.wait_for_function(
                """() => {
                  const list = document.getElementById('project-list-view');
                  const entry = document.getElementById('project-entry-choice');
                  return list && !list.classList.contains('hidden')
                    && entry && entry.classList.contains('hidden');
                }""",
                timeout=10_000,
            )

            # Re-open entry choice and click first Resume.
            page.evaluate(
                """async (cwd) => {
                  await window.__hubTestHooks.onProjectChosen(cwd);
                }""",
                cwd_with_priors,
            )
            page.wait_for_function(
                """() => {
                  const el = document.getElementById('project-entry-choice');
                  return el && !el.classList.contains('hidden');
                }""",
                timeout=10_000,
            )

            first_prior_id = page.evaluate(
                """() => {
                  const h = window.__hubTestHooks;
                  const priors = h.sessionsMatchingCwd(
                    document.getElementById('project-entry-cwd')?.textContent || ''
                  );
                  return (priors[0] && priors[0].sessionId) || null;
                }"""
            )
            assert first_prior_id, "expected at least one prior sessionId"

            page.locator("#project-entry-priors button").first.click()

            # Modal closes; selectedId equals chosen prior (not a brand-new create).
            page.wait_for_function(
                """(sid) => {
                  const modal = document.getElementById('modal-new');
                  const h = window.__hubTestHooks;
                  if (!h || !h.getSessionIdsForTest) return false;
                  const ids = h.getSessionIdsForTest();
                  return modal?.classList.contains('hidden') && ids.selectedId === sid;
                }""",
                arg=first_prior_id,
                timeout=20_000,
            )
            after = page.evaluate(
                "() => window.__hubTestHooks.getSessionIdsForTest()"
            )
            assert after.get("selectedId") == first_prior_id, after

            # Start new: only assert presence + policy helper (avoid creating sessions).
            policy = page.evaluate(
                """() => {
                  const h = window.__hubTestHooks;
                  return {
                    entry2: h.entryRequiresResumeChoice(2),
                    entry0: h.entryRequiresResumeChoice(0),
                    hasStartNew: !!document.getElementById('btn-entry-start-new'),
                  };
                }"""
            )
            assert policy.get("entry2") is True
            assert policy.get("entry0") is False
            assert policy.get("hasStartNew") is True
        finally:
            browser.close()
