import { expect, test } from "@playwright/test";

import { createMotorClaim, registerOrganisation } from "./helpers";

test("handler starts processing, watches live steps, sees the information request and cancels", async ({ page }) => {
  await registerOrganisation(page);
  const claimId = await createMotorClaim(page);
  await page.goto(`/claims/${claimId}`);
  await page.getByRole("tab", { name: "Processing" }).click();
  const panel = page.getByTestId("processing-panel");
  await expect(panel).toContainText("Not processed yet");

  await panel.getByRole("button", { name: "Start processing" }).click();
  // No documents uploaded: intake stops the run and asks for them; the step list updates live over SSE.
  await expect(page.getByTestId("node-INTAKE")).toHaveAttribute("data-state", "done", { timeout: 60_000 });
  const card = page.getByTestId("interrupt-card");
  await expect(card).toContainText("Waiting for more information", { timeout: 60_000 });
  await expect(card).toContainText("document: claim_form");
  await expect(page.getByTestId("execution-status")).toHaveText("Interrupted");
  await expect(page.getByTestId("claim-status")).toContainText(/awaiting information/i);

  await panel.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByTestId("execution-status")).toHaveText("Cancelled", { timeout: 30_000 });
  await expect(page.getByTestId("claim-status")).toContainText(/submitted/i);
  await expect(panel.getByRole("button", { name: "Start processing" })).toBeVisible();

  await page.getByRole("link", { name: "AI Processing" }).click();
  await expect(page.getByTestId("processing-runs")).toContainText("Cancelled");
});
