/**
 * Playwright smoke against a running hub (default http://127.0.0.1:8787).
 * Requires: hub up, Chromium via `npx playwright install chromium`
 *
 *   set HUB_URL=http://127.0.0.1:8787
 *   npx playwright test tests/e2e/smoke.spec.mjs
 */
import { test, expect } from "@playwright/test";

const HUB = process.env.HUB_URL || "http://127.0.0.1:8787";

test.describe("Grok Remote Hub smoke", () => {
  test("health endpoint is ok", async ({ request }) => {
    const res = await request.get(`${HUB}/health`);
    expect(res.ok()).toBeTruthy();
    const body = await res.json();
    expect(body.ok).toBeTruthy();
  });

  test("sessions API returns items with working classification fields", async ({
    request,
  }) => {
    const res = await request.get(`${HUB}/api/sessions`);
    expect(res.ok()).toBeTruthy();
    const body = await res.json();
    expect(Array.isArray(body.items)).toBeTruthy();
    if (body.items.length) {
      const s = body.items[0];
      expect(s).toHaveProperty("sessionId");
      expect(s).toHaveProperty("isSubagent");
      expect(s).toHaveProperty("isWorking");
    }
  });

  test("empty state and session filters render", async ({ page }) => {
    await page.goto(HUB, { waitUntil: "networkidle" });
    await expect(page.locator("#empty-main")).toBeVisible();
    await expect(page.getByRole("heading", { name: "No session selected" })).toBeVisible();
    await expect(page.locator('.kind-chip[data-kind="working"]')).toBeVisible();
    await expect(page.locator('.kind-chip[data-kind="subagent"]')).toBeVisible();
    await expect(page.locator('.kind-chip[data-kind="all"]')).toBeVisible();
    await expect(page.locator("#composer-input")).toHaveAttribute("spellcheck", "true");
    await expect(page.locator("#meta-popover")).toHaveCount(1);
  });

  test("open first working session and show topbar chips", async ({ page }) => {
    await page.goto(HUB, { waitUntil: "networkidle" });
    // Ensure Working filter
    await page.locator('.kind-chip[data-kind="working"]').click();
    const rows = page.locator(".session-row");
    const count = await rows.count();
    test.skip(count === 0, "No working sessions on this machine");
    await rows.first().click();
    // Empty main should hide after open
    await expect(page.locator("#empty-main")).toBeHidden({ timeout: 15000 });
    await expect(page.locator("#chat-title")).not.toHaveText("Select a session");
    // Project chip or cwd should appear when meta loads
    const project = page.locator("#chat-project");
    const cwd = page.locator("#chat-cwd");
    await expect
      .poll(async () => {
        const p = await project.isVisible().catch(() => false);
        const c = (await cwd.textContent()) || "";
        return p || c.trim().length > 0;
      }, { timeout: 15000 })
      .toBeTruthy();
  });

  test("meta bubble opens on project chip hover when session selected", async ({
    page,
  }) => {
    await page.goto(HUB, { waitUntil: "networkidle" });
    await page.locator('.kind-chip[data-kind="working"]').click();
    const rows = page.locator(".session-row");
    test.skip((await rows.count()) === 0, "No working sessions");
    await rows.first().click();
    await expect(page.locator("#empty-main")).toBeHidden({ timeout: 15000 });
    const project = page.locator("#chat-project");
    await expect(project).toBeVisible({ timeout: 15000 });
    await project.hover();
    await expect(page.locator("#meta-popover")).toBeVisible({ timeout: 5000 });
    await expect(page.locator("#meta-popover")).toContainText(/Project/i);
    await expect(page.locator("#meta-popover")).toContainText(/Path/i);
  });
});
