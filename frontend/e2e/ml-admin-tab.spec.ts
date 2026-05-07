import { expect, test } from "@playwright/test";
import { ADMIN_PASSWORD, ADMIN_USERNAME } from "../playwright.config";

/**
 * Phase 7d: verify the new admin "ML" tab renders the model status,
 * the drift report, and that "Retrain now" round-trips the
 * /api/admin/ml/retrain endpoint and refreshes the status.
 *
 * Both /api/admin/ml/status and /api/admin/ml/retrain are intercepted
 * so the test doesn't depend on actually subprocess-running the
 * trainer or having a real production drift signal.
 */

test.describe("admin ML tab (Phase 7d)", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await page.getByLabel(/username/i).fill(ADMIN_USERNAME);
    await page.getByLabel(/password/i).fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: /sign in/i }).click();
    await expect(
      page.getByRole("button", { name: "Admin", exact: true }),
    ).toBeVisible({ timeout: 15_000 });
    const disclaimer = page.getByRole("button", {
      name: /I understand/i,
    });
    if (await disclaimer.isVisible().catch(() => false)) {
      await disclaimer.click();
    }
  });

  test("renders model status + drift report; Retrain now refreshes status", async ({
    page,
  }) => {
    let retrainCount = 0;

    await page.route("**/api/admin/ml/status", async (route) => {
      const lastRetrain =
        retrainCount === 0
          ? null
          : {
              ok: true,
              started_at: 1700000000,
              finished_at: 1700000001,
              duration_s: 1.234,
              log: "stubbed retrain log line\n",
              error: null,
              triggered_by: "rootadmin",
              extras: {},
            };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          model_age_days: 12.5,
          last_retrain: lastRetrain,
          drift: {
            threshold: 0.1,
            any_alarm: true,
            alarm_features: ["log_body_length"],
            features: [
              {
                feature: "log_body_length",
                ks_statistic: 0.42,
                p_value: 0.001,
                alarm: true,
                skipped: false,
                reference: {
                  count: 100,
                  mean: 3.0,
                  std: 1.0,
                  min: 0,
                  p50: 3,
                  p90: 4.5,
                  max: 5,
                },
                current: {
                  count: 100,
                  mean: 4.5,
                  std: 1.0,
                  min: 1,
                  p50: 4.5,
                  p90: 6,
                  max: 7,
                },
              },
              {
                feature: "http_status_2xx",
                ks_statistic: 0.05,
                p_value: 0.85,
                alarm: false,
                skipped: false,
                reference: {
                  count: 100,
                  mean: 0.5,
                  std: 0.5,
                  min: 0,
                  p50: 0.5,
                  p90: 1,
                  max: 1,
                },
                current: {
                  count: 100,
                  mean: 0.5,
                  std: 0.5,
                  min: 0,
                  p50: 0.5,
                  p90: 1,
                  max: 1,
                },
              },
            ],
          },
        }),
      });
    });

    await page.route("**/api/admin/ml/retrain", async (route) => {
      retrainCount += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: {
            ok: true,
            started_at: 1700000000,
            finished_at: 1700000001,
            duration_s: 1.234,
            log: "stubbed retrain log line\n",
            error: null,
            triggered_by: "rootadmin",
            extras: {},
          },
        }),
      });
    });

    await page.getByRole("button", { name: "Admin", exact: true }).click();
    await page.getByRole("button", { name: "ML", exact: true }).click();

    await expect(page.getByTestId("ml-status-card")).toBeVisible();
    await expect(page.getByTestId("ml-model-age")).toContainText("12.50 days");
    // No retrain has happened yet on the initial load.
    await expect(page.getByTestId("ml-last-retrain")).toHaveCount(0);

    // Drift summary chip + alarmed row + ok row.
    await expect(page.getByTestId("ml-drift-summary")).toContainText(
      "1 feature(s) drifted",
    );
    await expect(
      page.getByTestId("ml-drift-row-log_body_length"),
    ).toHaveAttribute("data-alarm", "1");
    await expect(
      page.getByTestId("ml-drift-row-http_status_2xx"),
    ).toHaveAttribute("data-alarm", "0");

    // Click Retrain now -> /api/admin/ml/retrain hit -> status refreshed
    // -> ml-last-retrain row appears.
    await page.getByTestId("ml-retrain-button").click();
    await expect(page.getByTestId("ml-last-retrain")).toBeVisible();
    await expect(page.getByTestId("ml-last-retrain")).toContainText("rootadmin");

    // Toggle the log block.
    await page.getByTestId("ml-toggle-log").click();
    await expect(page.getByTestId("ml-retrain-log")).toContainText(
      "stubbed retrain log",
    );
  });
});
