import { describe, expect, it } from "vitest";

import { formatMetric, periodQuery } from "./api";

describe("dashboard formatting", () => {
  it("shows an unknown value as a dash, never as zero", () => {
    expect(formatMetric(null, "ratio")).toBe("—");
    expect(formatMetric(undefined, "ms")).toBe("—");
    expect(formatMetric(0, "ratio")).toBe("0%");
  });

  it("formats each unit", () => {
    expect(formatMetric(0.5, "ratio")).toBe("50%");
    expect(formatMetric(0.0333, "ratio")).toBe("3.3%");
    expect(formatMetric(0.06, "usd")).toBe("$0.0600");
    expect(formatMetric(12.5, "usd")).toBe("$12.50");
    expect(formatMetric(250, "ms")).toBe("250 ms");
    expect(formatMetric(6_000, "ms")).toBe("6.0 s");
    expect(formatMetric(90_000, "ms")).toBe("1.5 min");
    expect(formatMetric(7_200_000, "ms")).toBe("2.0 h");
    expect(formatMetric(1234, "count")).toMatch(/^1.234$/);
  });

  it("sends the period and synthetic switch to the API", () => {
    expect(periodQuery({ days: 7, includeSynthetic: false })).toBe("days=7&include_synthetic=false");
    expect(periodQuery({ days: 30, includeSynthetic: true }, { bucket: "week" })).toBe("days=30&include_synthetic=true&bucket=week");
  });
});
