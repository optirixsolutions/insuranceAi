import { expect, test, type Page } from "@playwright/test";

import { registerOrganisation } from "./helpers";

/** A real one-page PDF with a selectable text layer (Helvetica), built byte by byte. */
function textPdf(lines: string[]): Buffer {
  const stream = ["BT", "/F1 11 Tf", "14 TL", "56 780 Td", ...lines.map((l) => `(${l.replace(/[\\()]/g, "\\$&")}) Tj T*`), "ET"].join("\n");
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
    `<< /Length ${Buffer.byteLength(stream, "latin1")} >>\nstream\n${stream}\nendstream`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
  ];
  let body = "%PDF-1.4\n";
  const offsets: number[] = [];
  objects.forEach((obj, i) => {
    offsets.push(Buffer.byteLength(body, "latin1"));
    body += `${i + 1} 0 obj\n${obj}\nendobj\n`;
  });
  const xref = Buffer.byteLength(body, "latin1");
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets.map((o) => `${String(o).padStart(10, "0")} 00000 n \n`).join("")}`;
  body += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(body, "latin1");
}

const ESTIMATE = [
  "BRIDGEWATER AUTO BODY - REPAIR ESTIMATE",
  "Estimate No: EST-90311",
  "Policyholder: Grace Okafor",
  "Policy Number: POL-E2E-DOC",
  "Registration: LK21 ABC",
  "Date of loss: 08 Oct 2026",
  "Rear bumper replacement - parts 640.00",
  "Labour 6.5 hours - 455.00",
  "Total due: USD 1,095.00",
];

async function claimWithPolicy(page: Page) {
  const r = (path: string, data: unknown, headers: Record<string, string> = {}) =>
    page.request.post(`/api/v1${path}`, { data, headers: { ...headers, "X-CSRF-Token": csrf } });
  const csrf = (await page.context().cookies()).find((c) => c.name === "__Host-claimos_csrf")!.value;
  const product = await (await r("/products", { code: `MOTOR_${Date.now()}`, name: "Motor", line: "motor", currency: "USD" })).json();
  const customer = await (await r("/customers", { full_name: "Grace Okafor", is_synthetic: true })).json();
  const start = new Date(Date.now() - 90 * 86_400_000).toISOString().slice(0, 10);
  const end = new Date(Date.now() + 275 * 86_400_000).toISOString().slice(0, 10);
  const policy = await (await r("/policies", {
    policy_number: `POL-${Date.now()}`, customer_id: customer.id, product_id: product.id, is_synthetic: true, publish_initial_version: true,
    initial_version: { effective_from: start, effective_to: end, sum_insured: "20000.00", deductible_amount: "250.00",
      clauses: [{ clause_ref: "1", section: "coverage", title: "Cover", text: "Accidental damage." }],
      coverages: [{ coverage_code: "own_damage", perils: ["collision"], limit_amount: "20000.00", clause_ref: "1" }] },
  })).json();
  const claim = await (await r("/claims", {
    customer_id: customer.id, policy_id: policy.id, loss_type: "collision", is_synthetic: true,
    incident_date: new Date(Date.now() - 4 * 86_400_000).toISOString().slice(0, 10),
    description: "Rear-ended at a junction; bumper damaged.", claimed_amount: { amount: "1095.00", currency: "USD" },
  }, { "Idempotency-Key": crypto.randomUUID() })).json();
  return claim.id as string;
}

test("upload, OCR-free text layer, evidence highlight and audited correction", async ({ page }) => {
  await registerOrganisation(page);
  const claimId = await claimWithPolicy(page);
  await page.goto(`/claims/${claimId}?tab=documents`);
  await expect(page.getByTestId("required-repair_estimate")).toHaveCount(0); // overview card not on this tab

  await page.getByLabel("Upload documents").setInputFiles({ name: "estimate.pdf", mimeType: "application/pdf", buffer: textPdf(ESTIMATE) });
  const list = page.getByTestId("document-list");
  await expect(list).toContainText("estimate.pdf");
  await expect(list).toContainText(/Processed|Needs review/, { timeout: 60_000 });
  await expect(list).toContainText("Repair estimate");

  const fields = page.getByTestId("extracted-fields");
  await expect(fields).toContainText("POL-E2E-DOC");
  await fields.getByRole("button", { name: /Policy number/ }).click();
  await expect(page.getByTestId("evidence-highlight")).toBeVisible();

  const holder = fields.getByRole("listitem").filter({ hasText: "Policyholder name" });
  await holder.getByRole("button", { name: /Policyholder name/ }).click();
  await holder.getByRole("button", { name: "Correct" }).click();
  await page.getByLabel("Corrected Policyholder name").fill("Grace A. Okafor");
  await page.getByLabel("Reason for correction").fill("Middle initial from the policy schedule");
  await page.getByRole("button", { name: "Save correction" }).click();
  await expect(fields).toContainText("Grace A. Okafor");
  await expect(fields).toContainText("Corrected");

  // The overview now shows the repair estimate as received.
  await page.getByRole("tab", { name: "Overview" }).click();
  await expect(page.getByTestId("required-repair_estimate")).toContainText("Received");
});

test("unsafe files are rejected with a reason", async ({ page }) => {
  await registerOrganisation(page);
  const claimId = await claimWithPolicy(page);
  await page.goto(`/claims/${claimId}?tab=documents`);
  await page.getByLabel("Upload documents").setInputFiles({ name: "photo.png", mimeType: "image/png", buffer: textPdf(["not really a png"]) });
  await expect(page.getByRole("alert").filter({ hasText: "photo.png" })).toContainText("content is application/pdf");
});
