import { describe, expect, it } from "vitest";

import { queueQuery } from "./api";
import { money } from "./review-queue";

describe("review queue", () => {
  it("builds the queue filter query the API expects", () => {
    expect(queueQuery({})).toBe("");
    expect(queueQuery({ status: "escalated", mine: true, risk: "high", overdue: true })).toBe(
      "status=escalated&assignee=me&risk_level=high&overdue=true",
    );
    expect(queueQuery({ mine: false, overdue: false })).toBe("");
  });

  it("shows NUMERIC(19,4) amounts with two decimals", () => {
    expect(money("2000.0000", "GBP")).toMatch(/^GBP 2.000\.00$/);
    expect(money("815.5", "USD")).toBe("USD 815.5");
    expect(money("1475", "GBP")).toMatch(/^GBP 1.475$/);
  });
});
