import { describe, expect, it } from "vitest";

import { NAV_GROUPS, isActive, isAvailable, isPermitted } from "./nav-items";

const items = NAV_GROUPS.flatMap((g) => g.items);

describe("navigation", () => {
  it("contains all 15 product areas exactly once", () => {
    expect(items).toHaveLength(15);
    expect(new Set(items.map((i) => i.href)).size).toBe(15);
  });

  it("only exposes areas delivered by the current phase", () => {
    const available = items.filter((i) => isAvailable(i)).map((i) => i.label);
    expect(available).toEqual(["Overview", "Claims", "New Claim", "AI Processing", "Human Review", "Document Intelligence", "Fraud Investigation", "Policy Management", "Assessments", "Reports", "Analytics", "Audit Logs", "AI Agents", "Integrations", "Settings"]);
    expect(isAvailable(items.find((i) => i.label === "Claims")!, 3)).toBe(true);
  });

  it("hides permission-gated areas from users without the permission", () => {
    const audit = items.find((i) => i.label === "Audit Logs")!;
    expect(isPermitted(audit, ["claims.read"])).toBe(false);
    expect(isPermitted(audit, ["audit.read"])).toBe(true);
    expect(isPermitted(items.find((i) => i.label === "Settings")!, [])).toBe(true);
  });

  it("matches active routes by prefix except for the root", () => {
    const overview = items.find((i) => i.href === "/")!;
    const claims = items.find((i) => i.href === "/claims")!;
    expect(isActive(overview, "/")).toBe(true);
    expect(isActive(overview, "/claims")).toBe(false);
    expect(isActive(claims, "/claims/123")).toBe(true);
    expect(isActive(claims, "/claimsx")).toBe(false);
    expect(isActive(claims, "/claims/new")).toBe(false);
    expect(isActive(items.find((i) => i.href === "/claims/new")!, "/claims/new")).toBe(true);
  });
});
