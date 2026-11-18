import { expect, test } from "@playwright/test";

import { createMotorClaim, PASSWORD, registerOrganisation, uniqueEmail } from "./helpers";

test("a reviewer works the queue: authority is enforced, then the approval completes the claim", async ({ page, browser }) => {
  test.setTimeout(180_000);
  await registerOrganisation(page);
  const claimId = await createMotorClaim(page);
  const csrf = (await page.context().cookies()).find((c) => c.name === "__Host-claimos_csrf")!.value;

  // Processing stops for missing documents; the handler escalates it to human review.
  await page.goto(`/claims/${claimId}`);
  await page.getByRole("tab", { name: "Processing" }).click();
  await page.getByTestId("processing-panel").getByRole("button", { name: "Start processing" }).click();
  const card = page.getByTestId("interrupt-card");
  await expect(card).toContainText("Waiting for more information", { timeout: 60_000 });
  await card.getByRole("button", { name: "Escalate to review" }).click();
  await expect(card).toContainText("Waiting for a reviewer", { timeout: 60_000 });
  await expect(card.getByRole("link", { name: "Open the review workspace" })).toBeVisible();
  await expect(page.getByTestId("claim-status")).toContainText(/human review/i);

  // The admin invites a reviewer and grants a GBP 2,000 approval limit.
  const invited = await page.request.post("/api/v1/users", {
    data: { email: uniqueEmail("reviewer"), display_name: "Riya Reviewer", roles: ["reviewer"] },
    headers: { "X-CSRF-Token": csrf },
  });
  expect(invited.ok()).toBe(true);
  const invitation = await invited.json();
  const today = new Date().toISOString().slice(0, 10);
  const granted = await page.request.put(`/api/v1/users/${invitation.user.id}/authorities`, {
    data: [{ max_approval_amount: "2000.00", currency: "GBP", valid_from: today, can_reject: true }],
    headers: { "X-CSRF-Token": csrf },
  });
  expect(granted.ok(), await granted.text()).toBe(true);

  const context = await browser.newContext();
  const reviewer = await context.newPage();
  await reviewer.goto(invitation.access_link.url);
  await reviewer.getByLabel("New password").fill(PASSWORD);
  await reviewer.getByLabel("Confirm password").fill(PASSWORD);
  await reviewer.getByRole("button", { name: "Set password and sign in" }).click();
  await reviewer.getByRole("link", { name: "Human Review" }).click();
  const queue = reviewer.getByTestId("review-queue");
  await expect(queue).toContainText("Pending");
  await queue.getByRole("link").first().click();

  const ws = reviewer.getByTestId("review-workspace");
  await expect(ws.getByTestId("review-recommendation")).toContainText("None");
  await expect(ws).toContainText("Up to GBP 2,000.00");
  await ws.getByRole("button", { name: "Take ownership" }).click();
  await expect(ws.getByTestId("review-status")).toHaveText("In progress");

  const form = ws.getByTestId("form-approve");
  await form.getByLabel("Approved amount (GBP)").fill("2500.00");
  await form.getByLabel("Reason", { exact: true }).fill("Bumper repair estimate verified against photos");
  await form.getByRole("button", { name: "Approve" }).click();
  await expect(form.getByRole("alert")).toContainText("exceeds your approval authority");
  await expect(ws.getByTestId("review-status")).toHaveText("In progress");

  await form.getByLabel("Approved amount (GBP)").fill("1475.00");
  await form.getByRole("button", { name: "Approve" }).click();
  await expect(ws.getByTestId("review-status")).toHaveText("Completed");
  await expect(ws.getByTestId("decision-history")).toContainText("Approved");
  await expect(ws.getByTestId("decision-history")).toContainText("GBP 1,475.00");

  // Back on the handler's side the claim is approved and the run finishes.
  await page.reload();
  await expect(page.getByTestId("claim-status")).toContainText(/approved/i, { timeout: 30_000 });
  await page.getByRole("tab", { name: "Decision" }).click();
  await expect(page.getByTestId("decision-history")).toContainText("Bumper repair estimate verified");
  await page.getByRole("tab", { name: "Processing" }).click();
  await expect(page.getByTestId("execution-status")).toHaveText("Completed", { timeout: 60_000 });
  await context.close();
});
