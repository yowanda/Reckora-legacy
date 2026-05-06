import { defineConfig, devices } from "@playwright/test";

const PORT = Number(process.env.PLAYWRIGHT_PORT ?? 8123);
const BASE_URL = process.env.PLAYWRIGHT_BASE_URL ?? `http://127.0.0.1:${PORT}`;
const ADMIN_USERNAME = process.env.RECKORA_E2E_ADMIN ?? "e2eadmin";
const ADMIN_PASSWORD = process.env.RECKORA_E2E_PASSWORD ?? "e2epass1234";

// Single-origin Playwright config: builds the SPA, copies it into the
// backend's static dir, and runs uvicorn so a real production-shaped
// stack is exercised end-to-end (no vite dev server, no proxies).
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [["github"], ["list"]] : "list",
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: process.env.PLAYWRIGHT_NO_WEBSERVER
    ? undefined
    : {
        command: [
          // 1) build frontend; 2) sync to backend/app/static;
          // 3) launch uvicorn from backend dir with seed admin envs.
          "bash -c '",
          "set -euo pipefail;",
          "pnpm build > /dev/null;",
          "rm -rf ../backend/app/static && mkdir -p ../backend/app/static;",
          "cp -r dist/* ../backend/app/static/;",
          `cd ../backend && uv run uvicorn app.main:app --host 127.0.0.1 --port ${PORT}`,
          "'",
        ].join(" "),
        cwd: ".",
        url: `${BASE_URL}/api/health`,
        timeout: 180_000,
        reuseExistingServer: !process.env.CI,
        env: {
          RECKORA_ADMIN_USERNAME: ADMIN_USERNAME,
          RECKORA_ADMIN_PASSWORD: ADMIN_PASSWORD,
          RECKORA_DB_PATH: "/tmp/reckora-e2e.db",
          PYTHONUNBUFFERED: "1",
        },
      },
});

export { ADMIN_USERNAME, ADMIN_PASSWORD };
