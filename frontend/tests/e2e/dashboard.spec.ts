import { expect, test } from "@playwright/test";

import { createMotorClaim, registerOrganisation } from "./helpers";

test("the overview and analytics show figures computed from the organisation's real data", async ({ page }) => {
  test.setTimeout(120_000);
  await registerOrganisation(page);
  const claimId = await createMotorClaim(page);
  const csrf = (await page.context().cookies()).find((c) => c.name === "__Host-claimos_csrf")!.value;

  // Real processing by the worker: no documents, so the run waits for more information.
  const started = await page.request.post(`/api/v1/claims/${claimId}/processing`, {
    headers: { "X-CSRF-Token": csrf, "Idempotency-Key": crypto.randomUUID() },
  });
  expect(started.status()).toBe(202);
  const executionId = (await started.json()).id as string;
  await expect.poll(async () => (await (await page.request.get(`/api/v1/workflow-executions/${executionId}`)).json()).status,
    { timeout: 60_000 }).toBe("interrupted");

  await page.goto("/");
  const dashboard = page.getByTestId("dashboard");
  await expect(page.getByTestId("dashboard-scope")).toContainText("organisation");
  await expect(dashboard.getByTestId("kpi-claims_received-value")).toHaveText("1");
  await expect(dashboard.getByTestId("kpi-open_claims-value")).toHaveText("1");
  await expect(dashboard.getByTestId("kpi-stp_rate-value")).toHaveText("—"); // no approvals yet: unknown, not 0%
  await expect(dashboard.getByTestId("activity-waiting")).toContainText("More information: 1");
  await dashboard.getByTestId("kpi-claims_received").getByText("How is this counted?").click();
  await expect(dashboard.getByTestId("kpi-claims_received")).toContainText("Claims reported in the last 30 days.");

  await dashboard.getByLabel("Include synthetic data").uncheck(); // the claim is synthetic
  await expect(dashboard.getByTestId("kpi-claims_received-value")).toHaveText("0");
  await dashboard.getByLabel("Include synthetic data").check();
  await dashboard.getByRole("button", { name: "Last 7 days" }).click();
  await expect(dashboard.getByTestId("kpi-claims_received-value")).toHaveText("1");

  await page.getByRole("link", { name: "Analytics" }).click();
  const agents = page.getByTestId("analytics-agents");
  await expect(agents).toContainText("Intake");
  await expect(agents).toContainText("No model calls in this period.");
  await expect(page.getByTestId("analytics-times")).toContainText("No completed processing runs");
  await expect(page.getByTestId("analytics-fraud")).toBeVisible();

  await page.getByRole("link", { name: "Assessments" }).click();
  await expect(page.getByText("No assessments yet")).toBeVisible();
});
