import { createHmac } from "node:crypto";

import { expect, test, type Page } from "@playwright/test";

import { PASSWORD, registerOrganisation } from "./helpers";

/** RFC 6238 TOTP (SHA-1, 6 digits, 30 s), as an authenticator app computes it. */
function totp(secret: string, at = Date.now()): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const ch of secret.replace(/=+$/, "")) bits += alphabet.indexOf(ch).toString(2).padStart(5, "0");
  const key = Buffer.from(bits.match(/.{8}/g)!.map((b) => parseInt(b, 2)));
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(at / 1000 / 30)));
  const mac = createHmac("sha1", key).update(counter).digest();
  const offset = mac.readUInt8(mac.length - 1) & 0x0f;
  return ((mac.readUInt32BE(offset) & 0x7fffffff) % 1_000_000).toString().padStart(6, "0");
}

async function signIn(page: Page, email: string) {
  await page.goto("/login");
  await page.getByLabel("Work email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByLabel("Authentication code")).toBeVisible();
}

test("user turns on two-factor authentication and signs in with a code and a recovery code", async ({ page, browser }) => {
  const email = await registerOrganisation(page);

  await page.getByRole("link", { name: "Settings" }).click();
  await page.getByRole("link", { name: "Security" }).click();
  await page.getByRole("button", { name: "Set up two-factor authentication" }).click();
  const secret = (await page.getByTestId("mfa-secret").textContent())!.trim();
  expect(secret).toMatch(/^[A-Z2-7]{32}$/);
  await page.getByLabel("Code from the app").fill(totp(secret));
  await page.getByRole("button", { name: "Turn on" }).click();
  const items = page.getByRole("list", { name: "Recovery codes" }).getByRole("listitem");
  await expect(items).toHaveCount(10);
  const codes = await items.allTextContents();
  await page.getByRole("button", { name: "I have saved them" }).click();
  await expect(page.getByText("Recovery codes left: 10")).toBeVisible();

  // A new browser: password, then a wrong code, then the next code from the app.
  const second = await browser.newContext();
  const other = await second.newPage();
  await signIn(other, email);
  await other.getByLabel("Authentication code").fill(totp(secret) === "000000" ? "111111" : "000000");
  await other.getByRole("button", { name: "Verify" }).click();
  await expect(other.getByRole("alert").filter({ hasText: "not valid" })).toBeVisible();
  await other.getByLabel("Authentication code").fill(totp(secret, Date.now() + 30_000));
  await other.getByRole("button", { name: "Verify" }).click();
  await expect(other.getByRole("heading", { name: "Overview" })).toBeVisible();
  await second.close();

  // A lost phone: a recovery code works once.
  const third = await browser.newContext();
  const recovery = await third.newPage();
  await signIn(recovery, email);
  await recovery.getByLabel("Authentication code").fill(codes[0]!);
  await recovery.getByRole("button", { name: "Verify" }).click();
  await expect(recovery.getByRole("heading", { name: "Overview" })).toBeVisible();
  await third.close();

  await page.reload();
  await expect(page.getByText("Recovery codes left: 9")).toBeVisible();
});
