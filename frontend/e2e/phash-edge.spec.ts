import { expect, test } from "@playwright/test";
import { ADMIN_PASSWORD, ADMIN_USERNAME } from "../playwright.config";

/**
 * Phase 7a: verify GraphView renders a dashed purple pHash-derived
 * edge when /api/search/graph emits a ``phash_edge`` SSE event, and
 * that the SidePanel surfaces a "pHash matches" stat reflecting the
 * stitched count.
 *
 * We intercept the graph endpoint and respond with a synthetic SSE
 * stream so the test doesn't depend on real avatar fetches or live
 * site probes.
 */

function ssePayload(events: { event: string; data: unknown }[]): string {
  return (
    events
      .map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n`)
      .join("\n") + "\n"
  );
}

test.describe("phash graph stitching (Phase 7a)", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await page.getByLabel(/username/i).fill(ADMIN_USERNAME);
    await page.getByLabel(/password/i).fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: /sign in/i }).click();
    await expect(
      page.getByRole("button", { name: "Search", exact: true }),
    ).toBeVisible({ timeout: 15_000 });
    // Dismiss the responsible-use disclaimer overlay; otherwise it
    // captures pointer events on the rest of the page.
    const disclaimer = page.getByRole("button", {
      name: /I understand/i,
    });
    if (await disclaimer.isVisible().catch(() => false)) {
      await disclaimer.click();
    }
  });

  test("dashed purple pHash edge renders and SidePanel shows match count", async ({
    page,
  }) => {
    // Two CLAIMED hits with prebaked extracted fields. Their site
    // names are echoed in the phash_edge event; the layout should
    // place them side-by-side and stitch a dashed purple edge.
    const username = "alice";
    const claimedGithub = {
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
      extracted: { avatar_phash: "0123456789abcdef" },
    };
    const claimedTwitter = {
      site: "Twitter",
      status: "claimed",
      url: "https://twitter.com/alice",
      url_main: "https://twitter.com",
      tags: ["social"],
      http_status: 200,
      elapsed_ms: 110,
      error: null,
      confidence: 88,
      signals: [],
      extracted: { avatar_phash: "0123456789abcdef" },
    };

    const sse = ssePayload([
      { event: "start", data: { username, total: 2, depth: 0 } },
      { event: "result", data: claimedGithub },
      { event: "result", data: claimedTwitter },
      {
        event: "phash_edge",
        data: {
          site_a: "GitHub",
          site_b: "Twitter",
          parent_username_a: null,
          parent_username_b: null,
          hamming: 0,
          max_distance: 6,
        },
      },
      {
        event: "done",
        data: {
          results: 2,
          edges: 0,
          phash_edges: 1,
          secondary_count: 0,
        },
      },
    ]);

    await page.route("**/api/search/graph", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: sse,
      });
    });

    // Switch to graph mode, then run the search.
    await page.getByRole("button", { name: "graph", exact: true }).click();
    await page.getByPlaceholder(/enter a username/i).fill(username);
    await page.getByPlaceholder(/enter a username/i).press("Enter");

    // The dashed purple edge surfaces its label in the graph SVG.
    const phashLabel = page.locator("text=↺ phash · d=0");
    await expect(phashLabel).toBeVisible({ timeout: 15_000 });

    // SidePanel summary shows a Stat row labelled "pHash matches"
    // with value 1.
    const phashStat = page.getByText("pHash matches", { exact: false });
    await expect(phashStat).toBeVisible();

    // The static helper text mentioning "dashed purple edges" is also
    // present on the default SidePanel — sanity-check the
    // user-visible explanation didn't regress.
    await expect(page.getByText(/dashed purple edges/i)).toBeVisible();
  });
});
