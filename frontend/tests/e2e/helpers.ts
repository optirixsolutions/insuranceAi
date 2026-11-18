import { expect, type Page } from "@playwright/test";

export const PASSWORD = "e2e synthetic passphrase";

export function uniqueEmail(prefix: string) {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.test`;
}

/** Register a fresh organisation through the UI; the session cookie stays on the page's context. */
export async function registerOrganisation(page: Page, email = uniqueEmail("admin")) {
  await page.goto("/register");
  await page.getByLabel("Organisation name").fill("E2E Synthetic Insurance");
  await page.getByLabel("Your name").fill("E2E Admin");
  await page.getByLabel("Work email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Create organisation" }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  return email;
}

/** Create a synthetic motor claim (no documents) through the real API. */
export async function createMotorClaim(page: Page): Promise<string> {
  const csrf = (await page.context().cookies()).find((c) => c.name === "__Host-claimos_csrf")!.value;
  const post = async (path: string, data: unknown, headers: Record<string, string> = {}) => {
    const r = await page.request.post(`/api/v1${path}`, { data, headers: { ...headers, "X-CSRF-Token": csrf } });
    expect(r.ok(), `${path}: ${await r.text()}`).toBe(true);
    return r.json();
  };
  const product = await post("/products", { code: `MOT_${Date.now()}`, name: "Motor", line: "motor", currency: "GBP" });
  const customer = await post("/customers", { full_name: "Rohan Tanaka", is_synthetic: true });
  const start = new Date(Date.now() - 120 * 86_400_000).toISOString().slice(0, 10);
  const end = new Date(Date.now() + 240 * 86_400_000).toISOString().slice(0, 10);
  const policy = await post("/policies", {
    policy_number: `POL-AG-${Date.now()}`, customer_id: customer.id, product_id: product.id, is_synthetic: true, publish_initial_version: true,
    initial_version: { effective_from: start, effective_to: end, sum_insured: "18000.00", deductible_amount: "500.00",
      clauses: [{ clause_ref: "2.1", section: "coverage", title: "Own damage", text: "Accidental damage by collision." }],
      coverages: [{ coverage_code: "own_damage", perils: ["collision"], limit_amount: "18000.00", clause_ref: "2.1" }] },
  });
  const claim = await post("/claims", {
    customer_id: customer.id, policy_id: policy.id, loss_type: "collision", is_synthetic: true,
    incident_date: new Date(Date.now() - 5 * 86_400_000).toISOString().slice(0, 10),
    description: "Rear-ended at a junction while stationary; bumper damaged.", claimed_amount: { amount: "1475.00", currency: "GBP" },
  }, { "Idempotency-Key": crypto.randomUUID() });
  return claim.id as string;
}
