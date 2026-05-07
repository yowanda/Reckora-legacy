import { expect, test } from "@playwright/test";
import { ADMIN_PASSWORD, ADMIN_USERNAME } from "../playwright.config";

/**
 * Phase 7b: verify the SearchView mounts an EmailEnrichmentPanel
 * after a list-mode email scan finishes, and that the panel renders
 * the WHOIS + breach payload returned by /api/enrich/email.
 *
 * We intercept both /api/search and /api/enrich/email so the test
 * doesn't depend on real upstream RDAP / HIBP responses.
 */

function ssePayload(events: { event: string; data: unknown }[]): string {
  return (
    events
      .map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n`)
      .join("\n") + "\n"
  );
}

test.describe("email enrichment side panel (Phase 7b)", () => {
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

  test("email-mode scan surfaces WHOIS registrar and HIBP breach list", async ({
    page,
  }) => {
    const email = "alice@example.com";

    const sse = ssePayload([
      { event: "start", data: { username: email, total: 1 } },
      {
        event: "result",
        data: {
          site: "Gravatar",
          status: "claimed",
          url: "https://gravatar.com/...",
          url_main: "https://gravatar.com",
          tags: ["images"],
          http_status: 200,
          elapsed_ms: 100,
          error: null,
          confidence: 90,
          signals: [],
          extracted: {},
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

    await page.route("**/api/enrich/email", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          disabled: false,
          rate_limited: false,
          result: {
            email,
            domain: "example.com",
            whois: {
              domain: "example.com",
              registrar: "ExampleReg LLC",
              created: "1995-08-14T04:00:00Z",
              expires: "2030-08-13T04:00:00Z",
              updated: null,
              nameservers: ["a.iana-servers.net"],
              status: ["client transfer prohibited"],
            },
            breaches: [
              {
                name: "Adobe",
                title: "Adobe",
                domain: "adobe.com",
                breach_date: "2013-10-04",
                added_date: "2013-12-04T00:00:00Z",
                pwn_count: 152445165,
                data_classes: ["Email addresses", "Passwords"],
                is_verified: true,
                is_fabricated: false,
                is_sensitive: false,
              },
            ],
            breaches_disabled: false,
          },
        }),
      });
    });

    // SearchBar exposes the identity-type picker as a <select> with an
    // ``aria-label`` of "Identity type"; selectOption swaps it to email
    // and the input's placeholder updates accordingly.
    await page.getByLabel(/identity type/i).selectOption("email");
    await page.getByPlaceholder(/enter an email/i).fill(email);
    await page.getByPlaceholder(/enter an email/i).press("Enter");

    // The enrichment panel renders after the scan finishes and the
    // /api/enrich/email response resolves.
    const panel = page.getByTestId("email-enrichment");
    await expect(panel).toBeVisible({ timeout: 15_000 });

    // WHOIS half: registrar + nameserver line are visible.
    await expect(panel.getByText("ExampleReg LLC")).toBeVisible();
    await expect(panel.getByText("a.iana-servers.net")).toBeVisible();

    // Breach half: Adobe title + breach date are visible. ``Adobe``
    // text appears twice in the panel (the title + the ``·
    // adobe.com`` domain hint), so anchor on the title's exact match.
    await expect(panel.getByText("Adobe", { exact: true })).toBeVisible();
    await expect(panel.getByText("2013-10-04")).toBeVisible();
    // "Email addresses" data class chip.
    await expect(panel.getByText("Email addresses").first()).toBeVisible();
  });
});
