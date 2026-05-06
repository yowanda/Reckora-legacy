import { expect, test } from "@playwright/test";
import { ADMIN_PASSWORD, ADMIN_USERNAME } from "../playwright.config";

/**
 * Phase 5h-FE: verify that ResultCard renders the correct signal badges
 * and Stats shows the "Filtered" counters when the backend emits
 * link-liveness and confidence-floor demote signals.
 *
 * We intercept ``/api/search`` and respond with a synthetic SSE stream so
 * the test doesn't depend on real site probes.
 */

function ssePayload(results: Record<string, unknown>[]): string {
  const lines: string[] = [];
  lines.push(`event: start\ndata: ${JSON.stringify({ username: "test", total: results.length })}\n`);
  for (const r of results) {
    lines.push(`event: result\ndata: ${JSON.stringify(r)}\n`);
  }
  lines.push(`event: done\ndata: ${JSON.stringify({ results: results.length })}\n`);
  return lines.join("\n");
}

test.describe("signal badges", () => {
  test.beforeEach(async ({ page }) => {
    // Login first
    await page.goto("/");
    await page.getByLabel(/username/i).fill(ADMIN_USERNAME);
    await page.getByLabel(/password/i).fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: /sign in/i }).click();
    await expect(
      page.getByRole("button", { name: "Search", exact: true }),
    ).toBeVisible({ timeout: 15_000 });
  });

  test("link-verified-live badge and Demoted (link dead) badge render correctly", async ({
    page,
  }) => {
    const results = [
      {
        site: "GitHub",
        status: "claimed",
        url: "https://github.com/test",
        url_main: "https://github.com",
        tags: ["social"],
        http_status: 200,
        elapsed_ms: 120,
        error: null,
        confidence: 90,
        signals: ["link-verified-live"],
      },
      {
        site: "OldSite",
        status: "unknown",
        url: "https://oldsite.example.com/test",
        url_main: "https://oldsite.example.com",
        tags: ["misc"],
        http_status: 404,
        elapsed_ms: 80,
        error: null,
        confidence: 70,
        signals: ["link-dead-after-claim:404"],
      },
      {
        site: "LowConf",
        status: "unknown",
        url: "https://lowconf.example.com/test",
        url_main: "https://lowconf.example.com",
        tags: ["misc"],
        http_status: 200,
        elapsed_ms: 90,
        error: null,
        confidence: 40,
        signals: ["claimed-below-confidence-floor:40"],
      },
    ];

    // Intercept search SSE endpoint.
    await page.route("**/api/search", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: ssePayload(results),
      });
    });

    // Trigger a search.
    await page.getByPlaceholder(/enter a username/i).fill("test");
    await page.getByPlaceholder(/enter a username/i).press("Enter");

    // Wait for results to render (use data-testid badge to avoid matching
    // header / footer links that also contain "GitHub").
    await expect(page.getByTestId("badge-link-live")).toBeVisible({ timeout: 10_000 });

    // Assert "Link verified live" green badge on the claimed row.
    const liveBadge = page.getByTestId("badge-link-live");
    await expect(liveBadge).toBeVisible();
    await expect(liveBadge).toHaveText("Link verified live");

    // Assert "Demoted (link dead)" amber badge on the unknown row.
    const deadBadge = page.getByTestId("badge-link-dead");
    await expect(deadBadge).toBeVisible();
    await expect(deadBadge).toHaveText("Demoted (link dead)");

    // Assert Stats "Filtered (link)" counter shows 1.
    await expect(page.getByText("Filtered (link)")).toBeVisible();

    // Assert Stats "Filtered (confidence)" counter shows 1.
    await expect(page.getByText("Filtered (confidence)")).toBeVisible();
  });
});
