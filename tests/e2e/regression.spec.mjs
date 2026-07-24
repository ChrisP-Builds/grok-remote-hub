/**
 * Playwright regression use-cases against a running hub.
 * Requires: hub up, Chromium via `npx playwright install chromium`
 *
 *   set HUB_URL=http://127.0.0.1:8787
 *   npx playwright test tests/e2e/regression.spec.mjs
 *
 * Note: attach liveSessionId reuse needs hub running code with ensure/byCwd
 * reuse. If hub process is still on old code, restart after deploy.
 */
import { test, expect } from "@playwright/test";

const HUB = process.env.HUB_URL || "http://127.0.0.1:8787";

function isUntitledTitle(title) {
  const t = (title == null ? "" : String(title)).trim();
  return !t || t.toLowerCase() === "untitled session";
}

test.describe("Grok Remote Hub regression use-cases", () => {
  test("health endpoint is ok", async ({ request }) => {
    const res = await request.get(`${HUB}/health`);
    expect(res.ok()).toBeTruthy();
    const body = await res.json();
    expect(body.ok).toBeTruthy();
  });

  test("sessions API lists items; soft-count untitled titles (history may have old untitled)", async ({
    request,
  }) => {
    const res = await request.get(`${HUB}/api/sessions`);
    expect(res.ok()).toBeTruthy();
    const body = await res.json();
    expect(Array.isArray(body.items)).toBeTruthy();
    const untitled = body.items.filter((s) => isUntitledTitle(s.title));
    // Soft assert: log only — do not fail on legacy untitled history.
    console.log(
      `sessions items=${body.items.length} untitled=${untitled.length}`
    );
    if (body.items.length) {
      const s = body.items[0];
      expect(s).toHaveProperty("sessionId");
    }
  });

  test("open first working session without multi-second hang (empty-main hidden ≤20s)", async ({
    page,
  }) => {
    await page.goto(HUB, { waitUntil: "networkidle" });
    await page.locator('.kind-chip[data-kind="working"]').click();
    const rows = page.locator(".session-row");
    const count = await rows.count();
    test.skip(count === 0, "No working sessions on this machine");
    const t0 = Date.now();
    await rows.first().click();
    await expect(page.locator("#empty-main")).toBeHidden({ timeout: 20000 });
    const elapsed = Date.now() - t0;
    console.log(`open working session elapsed_ms=${elapsed}`);
    expect(elapsed).toBeLessThan(20000);
    await expect(page.locator("#chat-title")).not.toHaveText("Select a session");
  });

  test("double attach same open session returns promptly; reuse liveSessionId when process-live", async ({
    page,
    request,
  }) => {
    await page.goto(HUB, { waitUntil: "networkidle" });
    await page.locator('.kind-chip[data-kind="working"]').click();
    const rows = page.locator(".session-row");
    const count = await rows.count();
    test.skip(count === 0, "No working sessions on this machine");
    await rows.first().click();
    await expect(page.locator("#empty-main")).toBeHidden({ timeout: 20000 });

    const viewId = await page.evaluate(() => {
      // Prefer app state if exposed; else read from selected row data attribute.
      const row = document.querySelector(".session-row.selected, .session-row.active");
      if (row && row.dataset && row.dataset.sessionId) return row.dataset.sessionId;
      const any = document.querySelector(".session-row");
      return any && any.dataset ? any.dataset.sessionId || null : null;
    });
    // Fallback: sessions API first working item
    let sessionId = viewId;
    let cwd = "";
    if (!sessionId) {
      const list = await request.get(`${HUB}/api/sessions`);
      const body = await list.json();
      const working = (body.items || []).find((s) => s.isWorking);
      test.skip(!working, "No working session id resolved");
      sessionId = working.sessionId;
      cwd = working.cwd || "";
    } else {
      const list = await request.get(`${HUB}/api/sessions`);
      const body = await list.json();
      const match = (body.items || []).find((s) => s.sessionId === sessionId);
      cwd = (match && match.cwd) || "";
    }
    test.skip(!cwd, "Session has no cwd for attach");

    const attachOnce = async () => {
      const t0 = Date.now();
      const res = await request.post(`${HUB}/api/sessions/${sessionId}/attach`, {
        data: { cwd },
      });
      const elapsed = Date.now() - t0;
      const body = await res.json().catch(() => ({}));
      return { ok: res.ok(), status: res.status(), elapsed, body };
    };

    const first = await attachOnce();
    console.log(
      `attach#1 status=${first.status} elapsed_ms=${first.elapsed} live=${first.body && first.body.liveSessionId}`
    );
    // 503 agent not connected / 500 load fail: soft-skip rather than false red
    test.skip(
      first.status === 503,
      "Agent not connected — cannot prove attach reuse"
    );
    expect(first.ok).toBeTruthy();
    expect(first.elapsed).toBeLessThan(25000);

    const second = await attachOnce();
    console.log(
      `attach#2 status=${second.status} elapsed_ms=${second.elapsed} live=${second.body && second.body.liveSessionId}`
    );
    expect(second.ok).toBeTruthy();
    // Second attach must not hang forever
    expect(second.elapsed).toBeLessThan(25000);

    const live1 = first.body && first.body.liveSessionId;
    const live2 = second.body && second.body.liveSessionId;
    if (live1 && live2) {
      // When view is already process-live, ensure reuses same liveSessionId
      if (first.body.switched === false || first.body.reason === "hub_session" || first.body.reason === "reuse_cwd") {
        expect(live2).toBe(live1);
      } else {
        console.log(
          `attach reuse soft: reason1=${first.body.reason} reason2=${second.body.reason} live1=${live1} live2=${live2}`
        );
        // Best-effort: second attach on healthy hub should still match when both succeed
        if (live1 === sessionId || second.body.switched === false) {
          expect(live2).toBe(live1);
        }
      }
    }
  });

  test("soft WS: reload after open still works without permanent lost connection", async ({
    page,
  }) => {
    await page.goto(HUB, { waitUntil: "networkidle" });
    await page.locator('.kind-chip[data-kind="working"]').click();
    const rows = page.locator(".session-row");
    const count = await rows.count();
    test.skip(count === 0, "No working sessions on this machine");
    await rows.first().click();
    await expect(page.locator("#empty-main")).toBeHidden({ timeout: 20000 });

    await page.reload({ waitUntil: "networkidle" });
    // List / chrome still usable
    await expect(page.locator('.kind-chip[data-kind="working"]')).toBeVisible({
      timeout: 15000,
    });
    // Soft: permanent "lost connection" banner should not stick on healthy hub
    const lost = page.getByText(/lost connection/i);
    const lostVisible = await lost.isVisible().catch(() => false);
    if (lostVisible) {
      // Allow brief reconnect flash; poll until gone or 10s
      await expect
        .poll(async () => !(await lost.isVisible().catch(() => false)), {
          timeout: 10000,
        })
        .toBeTruthy();
    }
    // Session list still present
    await page.locator('.kind-chip[data-kind="working"]').click();
    await expect(page.locator(".session-row").first()).toBeVisible({
      timeout: 15000,
    });
  });

  test("composer enabled after open healthy session (best effort)", async ({
    page,
    request,
  }) => {
    const health = await request.get(`${HUB}/health`);
    test.skip(!health.ok(), "Hub health not ok");
    const hbody = await health.json();
    test.skip(!hbody.ok, "Hub health body not ok");

    await page.goto(HUB, { waitUntil: "networkidle" });
    await page.locator('.kind-chip[data-kind="working"]').click();
    const rows = page.locator(".session-row");
    const count = await rows.count();
    test.skip(count === 0, "No working sessions on this machine");
    await rows.first().click();
    await expect(page.locator("#empty-main")).toBeHidden({ timeout: 20000 });

    const composer = page.locator("#composer-input");
    await expect(composer).toBeVisible({ timeout: 10000 });
    // Best effort: not disabled when health ok and not mid-turn
    await expect
      .poll(
        async () => {
          const disabled = await composer.isDisabled().catch(() => true);
          const ro = await composer.getAttribute("readonly");
          return !disabled && ro == null;
        },
        { timeout: 15000 }
      )
      .toBeTruthy();
  });
});
