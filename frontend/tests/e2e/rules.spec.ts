import { expect, test } from "@playwright/test";

import { registerOrganisation } from "./helpers";

test("admin versions a product rule set and enables straight-through processing", async ({ page }) => {
  await registerOrganisation(page);
  const csrf = (await page.context().cookies()).find((c) => c.name === "__Host-claimos_csrf")!.value;
  const created = await page.request.post("/api/v1/products", {
    data: { code: `MOT_${Date.now()}`, name: "Motor rules", line: "motor", currency: "GBP" },
    headers: { "X-CSRF-Token": csrf },
  });
  expect(created.ok()).toBe(true);

  await page.goto("/settings/rules");
  await expect(page.getByTestId("active-rule-set")).toContainText("built-in defaults");
  await page.getByRole("button", { name: "New draft version" }).click();
  const editor = page.getByTestId("rule-set-editor");
  await editor.getByLabel("Straight-through processing enabled").check();
  await editor.getByLabel("STP maximum payout").fill("5000.00");
  await editor.getByRole("button", { name: "Save draft" }).click();
  await expect(editor.getByRole("alert")).toContainText("compliance profile"); // validated server-side

  await editor.getByLabel("Compliance profile allows automated approval").check();
  await editor.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByRole("button", { name: "Publish version 1" })).toBeEnabled();
  await page.getByRole("button", { name: "Publish version 1" }).click();
  await expect(page.getByTestId("active-rule-set")).toContainText("Active: version 1");
  await expect(page.getByTestId("active-rule-set")).toContainText("STP up to 5000");
  await expect(page.getByRole("button", { name: "New draft version" })).toBeVisible();
});
