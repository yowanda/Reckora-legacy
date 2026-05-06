import { expect, test } from "@playwright/test";
import { ADMIN_PASSWORD, ADMIN_USERNAME } from "../playwright.config";

test.describe("login", () => {
  test("anonymous load shows the login screen and gates /api/sites", async ({
    page,
    request,
  }) => {
    await page.goto("/");
    // The login page renders a "Sign in" submit button on the unauthenticated
    // path; the search view never does.
    await expect(
      page.getByRole("button", { name: /sign in/i }),
    ).toBeVisible();
    await expect(page.getByText(/members-only osint/i)).toBeVisible();

    const sitesResp = await request.get("/api/sites");
    expect(sitesResp.status()).toBe(401);
  });

  test("wrong password shows the static error message and does not leak existence", async ({
    page,
  }) => {
    await page.goto("/");
    await page.getByLabel(/username/i).fill(ADMIN_USERNAME);
    await page.getByLabel(/password/i).fill("definitely-not-the-password");
    await page.getByRole("button", { name: /sign in/i }).click();
    await expect(
      page.getByText("Invalid username or password.", { exact: true }),
    ).toBeVisible();
  });

  test("admin login lands on the search view with the Admin tab visible", async ({
    page,
  }) => {
    await page.goto("/");
    await page.getByLabel(/username/i).fill(ADMIN_USERNAME);
    await page.getByLabel(/password/i).fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: /sign in/i }).click();

    // Header navigation tabs only render after a successful login.
    await expect(
      page.getByRole("button", { name: "Search", exact: true }),
    ).toBeVisible({ timeout: 15_000 });
    await expect(
      page.getByRole("button", { name: "Admin", exact: true }),
    ).toBeVisible();
    await expect(page.getByText(/signed in as/i)).toBeVisible();
  });
});
