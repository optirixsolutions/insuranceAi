import { expect, test } from "@playwright/test";

import { registerOrganisation } from "./helpers";

test("admin syncs the live OpenRouter catalogue and sees honest model status", async ({ page }) => {
  await registerOrganisation(page);
  await page.getByRole("link", { name: "AI Agents" }).click();
  await expect(page.getByRole("heading", { name: "AI Agents" })).toBeVisible();

  await page.getByRole("button", { name: "Sync catalogue" }).click();
  await expect(page.getByText(/Catalogue synced: \d+ added/)).toBeVisible({ timeout: 30_000 });
  const router = page.getByTestId("model-openrouter/free");
  await expect(router).toBeVisible();
  await expect(router.getByText("never")).toBeVisible(); // nothing is trusted before a probe

  // Every LLM agent is listed, and only verified models can be chosen.
  for (const key of ["document", "policy", "fraud", "assessment", "decision"]) {
    await expect(page.getByTestId(`agent-${key}`)).toBeVisible();
  }
  await expect(page.getByTestId("agent-document")).toContainText("Not verified");

  // The key's real status is shown (configured-but-rejected, missing, or valid), never assumed.
  const status = await (await page.request.get("/api/v1/ai/status")).json();
  if (status.key_status === "rejected") await expect(page.getByText("OpenRouter rejected the configured API key")).toBeVisible();
  if (status.key_status === "not_configured") await expect(page.getByText("No OpenRouter API key is configured")).toBeVisible();
  if (status.key_status !== "valid") await expect(router.getByRole("button", { name: "Test" })).toBeDisabled();
});

test("non-admins cannot reach AI administration", async ({ page }) => {
  await registerOrganisation(page);
  const csrf = (await page.context().cookies()).find((c) => c.name === "__Host-claimos_csrf")!.value;
  const invite = await (await page.request.post("/api/v1/users", {
    data: { email: `mgr-${Date.now()}@example.test`, display_name: "Manager", roles: ["claims_manager"] },
    headers: { "X-CSRF-Token": csrf },
  })).json();
  const token = invite.access_link.url.split("#token=")[1];
  const other = await page.context().browser()!.newContext();
  const p2 = await other.newPage();
  await p2.goto("/login");
  const accepted = await p2.request.post("/api/v1/auth/invitations/accept", { data: { token, password: "manager passphrase 1" } });
  expect(accepted.ok()).toBe(true);
  await p2.goto("/");
  await expect(p2.getByRole("link", { name: "AI Agents" })).toHaveCount(0);
  expect((await p2.request.get("/api/v1/models")).status()).toBe(403);
  await other.close();
});
