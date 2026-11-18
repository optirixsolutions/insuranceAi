import { expect, test } from "@playwright/test";

import { createMotorClaim, registerOrganisation } from "./helpers";

test("similar claims, portfolio anomalies and the prompt activation gate", async ({ page }) => {
  test.setTimeout(120_000);
  await registerOrganisation(page);
  // The helper creates a new customer and policy each time with the same synthetic story.
  const first = await createMotorClaim(page);
  const second = await createMotorClaim(page);

  await page.goto(`/claims/${first}?tab=fraud`);
  const similar = page.getByTestId("similar-claims");
  await expect(similar).toContainText("Different customer");
  await expect(similar).toContainText("100%");
  await expect(similar.getByRole("link").first()).toHaveAttribute("href", `/claims/${second}`);

  await page.getByRole("link", { name: "Fraud Investigation" }).click();
  const anomalies = page.getByTestId("anomalies");
  await expect(anomalies.getByTestId("anomaly").first()).toContainText("Similar narrative");
  await expect(anomalies).toContainText("below the 30 needed");

  await page.getByRole("link", { name: "AI Agents" }).click();
  const evals = page.getByTestId("evaluations");
  await evals.getByLabel("Agent").selectOption("fraud");
  const v2 = evals.getByTestId("prompt-v2");
  await expect(evals.getByTestId("prompt-v1")).toContainText("Active");
  await expect(v2).toContainText("Not evaluated");
  await v2.getByRole("button", { name: "Activate" }).click();
  await expect(evals.getByRole("alert")).toContainText("no passed regression run");
  await expect(evals.getByTestId("prompt-v1")).toContainText("Active"); // nothing changed
});
