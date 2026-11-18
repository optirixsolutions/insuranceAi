import { readFileSync } from "node:fs";

import { expect, test, type Page } from "@playwright/test";

import { registerOrganisation } from "./helpers";

const isoDaysAgo = (days: number) => new Date(Date.now() - days * 86_400_000).toLocaleDateString("en-CA");

async function setUpBook(page: Page, suffix: string) {
  await page.getByRole("link", { name: "Policy Management" }).click();

  await page.getByRole("button", { name: "Products" }).click();
  await page.getByRole("button", { name: "New product" }).click();
  let dialog = page.getByRole("dialog");
  await dialog.getByLabel("Code").fill(`MOTOR_${suffix}`);
  await dialog.getByLabel("Name").fill("Private Motor E2E");
  await dialog.getByRole("button", { name: "Create product" }).click();
  await expect(page.getByText("Private Motor E2E")).toBeVisible();

  await page.getByRole("button", { name: "Customers" }).click();
  await page.getByRole("button", { name: "New customer" }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByLabel("Full name").fill(`Grace Okafor ${suffix}`);
  await dialog.getByLabel("External reference").fill(`E2E-${suffix}`);
  await dialog.getByRole("button", { name: "Create customer" }).click();
  await expect(page.getByText(`Grace Okafor ${suffix}`)).toBeVisible();

  await page.getByRole("button", { name: "Policies" }).click();
  await page.getByRole("button", { name: "New policy" }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByLabel("Policy number").fill(`POL-E2E-${suffix}`);
  await dialog.getByLabel("Find customer").fill(`Okafor ${suffix}`);
  await expect(dialog.getByLabel("Customer", { exact: true }).locator("option")).toHaveCount(1);
  await dialog.getByLabel("Effective from").fill(isoDaysAgo(120));
  await dialog.getByLabel("Effective to (exclusive)").fill(isoDaysAgo(-245));
  await dialog.getByLabel("Sum insured").fill("20000.00");
  await dialog.getByLabel("Deductible").fill("500.00");
  await dialog.getByLabel("Perils covered").fill("collision, fire, theft, weather");
  await dialog.getByLabel("Clause wording").fill("We cover accidental damage to the insured vehicle up to the sum insured.");
  await dialog.getByRole("button", { name: "Create policy" }).click();
  await expect(page.getByRole("heading", { name: `POL-E2E-${suffix}` })).toBeVisible();
  await expect(page.getByTestId("version-1")).toContainText("published");
}

test("claims handler journey: policy set-up, intake, triage, notes and status", async ({ page }) => {
  const suffix = Date.now().toString(36).toUpperCase();
  await registerOrganisation(page);
  await setUpBook(page, suffix);

  // Submit a claim through the New Claim form.
  await page.getByRole("link", { name: "New Claim" }).click();
  await page.getByLabel("Find customer").fill(`Okafor ${suffix}`);
  await page.getByLabel("Customer", { exact: true }).selectOption({ label: `Grace Okafor ${suffix} (E2E-${suffix})` });
  await page.getByLabel("Policy", { exact: true }).selectOption({ index: 1 });
  await page.getByLabel("Type of loss").selectOption("collision");
  await page.getByLabel(/Claimed amount/).fill("4800.50");
  await page.getByLabel("Incident date").fill(isoDaysAgo(4));
  await page.getByLabel("City (optional)").fill("Bristol");
  await page.getByLabel("What happened").fill("Side-swiped by a delivery van while parked; driver door and mirror damaged.");
  await page.getByRole("button", { name: "Submit claim" }).click();

  await expect(page.getByText(/Claim CLM-\d{4}-\d{6} submitted/)).toBeVisible();
  const reference = (await page.getByRole("heading", { level: 1 }).textContent())!.trim();
  await expect(page.getByTestId("claim-status")).toHaveText("Submitted");
  await expect(page.getByText("USD 4,800.50")).toBeVisible();
  await expect(page.getByText("Repair estimate")).toBeVisible();

  // Move to awaiting information with a reason, then check the timeline and notes.
  await page.getByLabel("Reason").fill("Waiting for repair estimate and photos");
  await page.getByRole("button", { name: "Apply" }).click();
  await expect(page.getByTestId("claim-status")).toHaveText("Awaiting information");
  await page.getByRole("tab", { name: "Activity Timeline" }).click();
  await page.getByLabel("New note").fill("Customer emailed: estimate expected by Friday.");
  await page.getByRole("button", { name: "Add note" }).click();
  const timeline = page.getByTestId("claim-timeline");
  await expect(timeline).toContainText("Status: SUBMITTED → AWAITING_INFORMATION");
  await expect(timeline).toContainText("Note added");

  // Find it again from the claims list with search and a status filter.
  await page.getByRole("link", { name: "Claims", exact: true }).click();
  await page.getByLabel("Status").selectOption("AWAITING_INFORMATION");
  await page.getByLabel("Search").fill(reference);
  await page.getByLabel("Search").press("Enter");
  await expect(page.getByTestId(`claim-row-${reference}`)).toBeVisible();
  await expect(page.getByText("1 claim")).toBeVisible();

  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "Export CSV" }).click();
  const csv = readFileSync(await (await download).path(), "utf8");
  expect(csv).toContain(reference);
});

test("intake flags a claim dated before cover started", async ({ page }) => {
  const suffix = Date.now().toString(36).toUpperCase();
  await registerOrganisation(page);
  await setUpBook(page, suffix);
  await page.getByRole("link", { name: "New Claim" }).click();
  await page.getByLabel("Find customer").fill(`Okafor ${suffix}`);
  await page.getByLabel("Customer", { exact: true }).selectOption({ index: 1 });
  await page.getByLabel("Policy", { exact: true }).selectOption({ index: 1 });
  await page.getByLabel("Type of loss").selectOption("theft");
  await page.getByLabel(/Claimed amount/).fill("9000");
  await page.getByLabel("Incident date").fill(isoDaysAgo(200));
  await page.getByLabel("What happened").fill("Vehicle stolen from a supermarket car park during the day.");
  await page.getByRole("button", { name: "Submit claim" }).click();
  await expect(page.getByTestId("intake-findings")).toContainText("No published policy version covers the incident date");
  await expect(page.getByText("None in force")).toBeVisible();
});
