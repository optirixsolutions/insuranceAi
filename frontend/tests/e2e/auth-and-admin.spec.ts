import { expect, test } from "@playwright/test";

import { PASSWORD, registerOrganisation, uniqueEmail } from "./helpers";

test("unauthenticated visitors are sent to sign-in with a safe return path", async ({ page }) => {
  await page.goto("/settings/users");
  await expect(page).toHaveURL(/\/login\?next=%2Fsettings%2Fusers$/);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});

test("wrong credentials show a generic error", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Work email").fill(uniqueEmail("nobody"));
  await page.getByLabel("Password").fill("definitely wrong password");
  await page.getByRole("button", { name: "Sign in" }).click();
  // Scoped by text: Next.js also renders an (empty) role="alert" route announcer.
  await expect(page.getByRole("alert").filter({ hasText: "Invalid email or password" })).toBeVisible();
});

test("admin invites an adjuster who signs up with limited access", async ({ page, browser }) => {
  const adminEmail = await registerOrganisation(page);
  await expect(page.getByText("E2E Admin")).toBeVisible();

  // Invite through Settings > Users.
  await page.getByRole("link", { name: "Settings" }).click();
  await page.getByRole("link", { name: "Users" }).click();
  await expect(page.getByTestId(`user-row-${adminEmail}`)).toContainText("(you)");
  const adjusterEmail = uniqueEmail("adjuster");
  await page.getByRole("button", { name: "Invite user" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Name").fill("E2E Adjuster");
  await dialog.getByLabel("Work email").fill(adjusterEmail);
  await dialog.getByRole("button", { name: "Create invitation" }).click();
  const link = await dialog.getByLabel("One-time access link").inputValue();
  expect(link).toContain("/accept-invite#token=");
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(page.getByTestId(`user-row-${adjusterEmail}`)).toContainText("invited");

  // The invited user completes sign-up in a separate browser context.
  const context = await browser.newContext();
  const invitee = await context.newPage();
  await invitee.goto(link);
  await expect(invitee).toHaveURL(/\/accept-invite$/); // token removed from the address bar
  await invitee.getByLabel("New password").fill(PASSWORD);
  await invitee.getByLabel("Confirm password").fill(PASSWORD);
  await invitee.getByRole("button", { name: "Set password and sign in" }).click();
  await expect(invitee.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(invitee.getByText("Claims Adjuster")).toBeVisible();

  // Least privilege: no audit log in navigation, no user management tab, API refuses.
  await expect(invitee.getByRole("link", { name: "Audit Logs" })).toHaveCount(0);
  await invitee.getByRole("link", { name: "Settings" }).click();
  await expect(invitee.getByRole("link", { name: "Users" })).toHaveCount(0);
  await expect(invitee.getByLabel("Organisation name")).toBeDisabled();
  expect((await invitee.request.get("/api/v1/users")).status()).toBe(403);

  // Sign out ends the session server-side.
  await invitee.getByRole("button", { name: "Sign out" }).click();
  await expect(invitee.getByRole("heading", { name: "Sign in" })).toBeVisible();
  expect((await invitee.request.get("/api/v1/auth/me")).status()).toBe(401);
  await context.close();

  // The admin sees the trail and a valid hash chain.
  await page.getByRole("link", { name: "Audit Logs" }).click();
  await expect(page.getByText("user.invited")).toBeVisible();
  await expect(page.getByText("auth.invite_accepted")).toBeVisible();
  await page.getByRole("button", { name: "Verify integrity" }).click();
  await expect(page.getByText(/Hash chain intact: \d+ events verified/)).toBeVisible();
});

test("state-changing API calls without the CSRF token are refused", async ({ page }) => {
  await registerOrganisation(page);
  const response = await page.request.post("/api/v1/auth/logout");
  expect(response.status()).toBe(403);
  expect((await response.json()).code).toBe("CSRF_FAILED");
});
