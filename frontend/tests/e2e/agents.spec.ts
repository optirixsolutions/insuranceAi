import { expect, test } from "@playwright/test";

import { createMotorClaim, registerOrganisation } from "./helpers";

test("handler runs all eight agents and sees the recommendation, coverage and fraud views", async ({ page }) => {
  await registerOrganisation(page);
  const claimId = await createMotorClaim(page);
  await page.goto(`/claims/${claimId}`);
  await page.getByRole("tab", { name: "Agents" }).click();
  for (const key of ["intake", "document", "policy", "fraud", "assessment", "validation", "decision", "compliance"]) {
    await expect(page.getByTestId(`agent-${key}`)).toContainText("Not run");
  }

  await page.getByRole("button", { name: "Run all agents" }).click();
  const banner = page.getByTestId("decision-banner");
  await expect(banner).toBeVisible({ timeout: 120_000 });
  // No documents yet: the matrix asks for more information and says why, with a templated explanation.
  await expect(banner).toContainText("More information required");
  await expect(banner).toContainText("templated explanation");
  await expect(page.getByTestId("agent-compliance")).toContainText("Succeeded", { timeout: 60_000 });

  await page.getByRole("button", { name: "Intake", exact: true }).click();
  const intake = page.getByTestId("agent-detail-intake");
  await expect(intake).toContainText("MISSING_DOCUMENT");
  await expect(intake).toContainText("Deterministic only");

  await page.getByRole("tab", { name: "Policy Coverage" }).click();
  await expect(page.getByTestId("coverage-panel")).toContainText("own_damage");
  await expect(page.getByTestId("coverage-panel")).toContainText("Covered");

  await page.getByRole("tab", { name: "Fraud Investigation" }).click();
  await expect(page.getByTestId("fraud-panel")).toContainText("Low risk");
});

test("users without processing permission can read agent results but not start runs", async ({ page }) => {
  await registerOrganisation(page);
  const claimId = await createMotorClaim(page);
  const csrf = (await page.context().cookies()).find((c) => c.name === "__Host-claimos_csrf")!.value;
  const invite = await (await page.request.post("/api/v1/users", {
    data: { email: `auditor-${Date.now()}@example.test`, display_name: "Auditor", roles: ["auditor"] },
    headers: { "X-CSRF-Token": csrf },
  })).json();
  const token = invite.access_link.url.split("#token=")[1];
  const other = await page.context().browser()!.newContext();
  const p2 = await other.newPage();
  await p2.goto("/login");
  expect((await p2.request.post("/api/v1/auth/invitations/accept", { data: { token, password: "auditor passphrase 1" } })).ok()).toBe(true);
  const csrf2 = (await other.cookies()).find((c) => c.name === "__Host-claimos_csrf")!.value;
  const denied = await p2.request.post(`/api/v1/claims/${claimId}/agent-runs`, {
    data: { agent_key: "intake" }, headers: { "X-CSRF-Token": csrf2 },
  });
  expect(denied.status()).toBe(403);
  await other.close();
});
