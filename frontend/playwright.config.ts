import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests run against the full docker compose stack (Nginx on port 8080 by default).
 * Start it first: `docker compose up -d --build`.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 30_000,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:8080",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop-chromium", use: { ...devices["Desktop Chrome"] } },
    // Mobile checks layout-independent flows; the admin journey relies on the desktop sidebar.
    { name: "mobile-chromium", use: { ...devices["Pixel 7"] }, testMatch: /system-health\.spec\.ts/ },
  ],
});
