import { expect, test } from "@playwright/test";

import { registerOrganisation } from "./helpers";

test("overview shows live readiness from the backend", async ({ page }) => {
  await registerOrganisation(page);
  const readiness = page.waitForResponse((r) => r.url().endsWith("/api/v1/health/ready"));
  await page.reload();
  const body = (await (await readiness).json()) as { components: { name: string; status: string }[] };

  await expect(page.getByRole("heading", { name: "System health" })).toBeVisible();
  // The UI must reflect what the API actually reported, not a hard-coded state.
  for (const component of body.components) {
    const row = page.getByTestId(`component-${component.name}`);
    const label = { ok: "Operational", degraded: "Degraded", down: "Unavailable" }[component.status]!;
    await expect(row.getByText(label, { exact: true })).toBeVisible();
  }
  for (const name of ["database", "migrations", "redis", "object_storage"]) {
    expect(body.components.find((c) => c.name === name)?.status).toBe("ok");
  }
});

test("pages are served with a nonce-based content security policy", async ({ page }) => {
  const response = await page.goto("/login");
  const csp = response?.headers()["content-security-policy"] ?? "";
  expect(csp).toContain("script-src 'self' 'nonce-");
  expect(csp).toContain("frame-ancestors 'none'");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});

test("API requests carry a request id through the proxy", async ({ request }) => {
  const response = await request.get("/api/v1/health/live");
  expect(response.ok()).toBe(true);
  expect(response.headers()["x-request-id"]).toMatch(/^[a-f0-9]{32}$/);
});
