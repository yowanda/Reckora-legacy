import { expect, test } from "@playwright/test";
import { ADMIN_PASSWORD, ADMIN_USERNAME } from "../playwright.config";

/**
 * Phase 7c: verify ResultCard mounts a row of reverse-image chips
 * (Yandex / Google Lens / TinEye) under any CLAIMED hit whose
 * extractor pass produced an ``avatar_url``, and that each chip
 * deep-links into the upstream's public reverse-search landing page.
 *
 * We intercept /api/search and /api/reverse-image so the test
 * doesn't depend on real upstream availability or extractor parses.
 */

function ssePayload(events: { event: string; data: unknown }[]): string {
  return (
    events
      .map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n`)
      .join("\n") + "\n"
  );
}

test.describe("reverse-image lookup chips (Phase 7c)", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await page.getByLabel(/username/i).fill(ADMIN_USERNAME);
    await page.getByLabel(/password/i).fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: /sign in/i }).click();
    await expect(
      page.getByRole("button", { name: "Search", exact: true }),
    ).toBeVisible({ timeout: 15_000 });
    const disclaimer = page.getByRole("button", {
      name: /I understand/i,
    });
    if (await disclaimer.isVisible().catch(() => false)) {
      await disclaimer.click();
    }
  });

  test("CLAIMED hit with avatar_url renders Yandex / Google Lens / TinEye chips", async ({
    page,
  }) => {
    const username = "alice";
    const avatarUrl = "https://cdn.example.com/avatar/alice.png";

    const sse = ssePayload([
      { event: "start", data: { username, total: 1 } },
      {
        event: "result",
        data: {
          site: "GitHub",
          status: "claimed",
          url: "https://github.com/alice",
          url_main: "https://github.com",
          tags: ["social"],
          http_status: 200,
          elapsed_ms: 100,
          error: null,
          confidence: 92,
          signals: [],
          extracted: { avatar_url: avatarUrl },
        },
      },
      { event: "done", data: { results: 1 } },
    ]);

    await page.route("**/api/search", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: sse,
      });
    });

    await page.route("**/api/reverse-image", async (route, request) => {
      const body = request.postDataJSON() as { image_url: string };
      const encoded = encodeURIComponent(body.image_url);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          disabled: false,
          providers: [
            {
              slug: "yandex",
              label: "Yandex",
              url: `https://yandex.com/images/search?rpt=imageview&url=${encoded}`,
            },
            {
              slug: "google",
              label: "Google Lens",
              url: `https://lens.google.com/uploadbyurl?url=${encoded}`,
            },
            {
              slug: "tineye",
              label: "TinEye",
              url: `https://tineye.com/search?url=${encoded}`,
            },
          ],
        }),
      });
    });

    await page.getByPlaceholder(/enter a username/i).fill(username);
    await page.getByPlaceholder(/enter a username/i).press("Enter");

    // Expand the extracted-fields panel via the "↗ extracted · 1" chip.
    const extractedToggle = page.getByRole("button", { name: /extracted · 1/i });
    await expect(extractedToggle).toBeVisible({ timeout: 15_000 });
    await extractedToggle.click();

    // The reverse-image chip row renders inside the expanded panel.
    const chipRow = page.getByTestId("reverse-image-chips");
    await expect(chipRow).toBeVisible();

    const yandex = page.getByTestId("reverse-image-yandex");
    const google = page.getByTestId("reverse-image-google");
    const tineye = page.getByTestId("reverse-image-tineye");
    await expect(yandex).toBeVisible();
    await expect(google).toBeVisible();
    await expect(tineye).toBeVisible();

    // The encoded avatar URL is embedded in every provider link, and
    // each opens in a new tab.
    const encoded = encodeURIComponent(avatarUrl);
    for (const link of [yandex, google, tineye]) {
      await expect(link).toHaveAttribute(
        "href",
        new RegExp(encoded.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")),
      );
      await expect(link).toHaveAttribute("target", "_blank");
    }
  });
});
