"use client";

import { useQuery } from "@tanstack/react-query";
import { z } from "zod";

import { apiGet } from "@/lib/api/client";

export type Period = { days: number; includeSynthetic: boolean };

const metricSchema = z.object({
  key: z.string(),
  label: z.string(),
  value: z.number().nullable(),
  unit: z.enum(["count", "ratio", "ms", "usd"]),
  definition: z.string(),
});
export type Metric = z.infer<typeof metricSchema>;

const summarySchema = z.object({
  days: z.number(),
  include_synthetic: z.boolean(),
  scope: z.string(),
  generated_at: z.string(),
  metrics: z.array(metricSchema),
});

const ref = { claim_id: z.string(), claim_reference: z.string() };
const reviewRef = z.object({ ...ref, review_id: z.string(), status: z.string(), due_at: z.string().nullable(), priority_score: z.number() });
const activitySchema = z.object({
  generated_at: z.string(),
  running: z.array(z.object({ ...ref, execution_id: z.string(), status: z.string(), current_node: z.string().nullable(), progress: z.number() })),
  waiting: z.record(z.string(), z.number()),
  recent_failures: z.array(z.object({ ...ref, execution_id: z.string(), error_code: z.string().nullable(), finished_at: z.string().nullable() })),
  pending_approvals: z.array(reviewRef),
  overdue_reviews: z.array(reviewRef),
});
export type Activity = z.infer<typeof activitySchema>;

const msNull = z.number().nullable();
const processingSchema = z.object({
  definition: z.string(),
  products: z.array(z.object({
    product_id: z.string(), product_name: z.string(), runs: z.number(),
    wall_avg_ms: msNull, wall_p50_ms: msNull, wall_p90_ms: msNull, wall_p95_ms: msNull,
    machine_p50_ms: msNull, machine_p90_ms: msNull, machine_p95_ms: msNull,
  })),
});

const claimsSchema = z.object({
  definition: z.string(),
  bucket: z.string(),
  volume: z.array(z.object({ bucket: z.string(), count: z.number() })),
  by_category: z.array(z.object({ category: z.string(), count: z.number() })),
  by_status: z.array(z.object({ status: z.string(), count: z.number() })),
  decisions: z.array(z.object({ decision: z.string(), mode: z.string(), count: z.number() })),
});

const fraudSchema = z.object({
  definition: z.string(),
  by_level: z.record(z.string(), z.number()),
  alerts: z.number(),
  top_indicators: z.array(z.object({ code: z.string(), count: z.number() })),
  labelled_flagged: z.number(),
  false_positives: z.number(),
  false_positive_rate: z.number().nullable(),
});

const agentsSchema = z.object({
  definition: z.string(),
  agents: z.array(z.object({
    agent_key: z.string(), runs: z.number(), failed: z.number(), failure_rate: z.number().nullable(),
    retries: z.number(), p50_ms: msNull, p95_ms: msNull, llm_rate: z.number().nullable(),
    escalation_rate: z.number().nullable(), tokens: z.number(), cost_usd: z.number(),
  })),
  cost_per_claim_usd: z.number().nullable(),
  claims: z.number(),
  models: z.array(z.object({
    model_slug: z.string(), calls: z.number(), errors: z.number(), error_rate: z.number().nullable(),
    p95_latency_ms: msNull, cost_usd: z.number(),
  })),
});

const extractionSchema = z.object({
  definition: z.string(),
  document_types: z.array(z.object({
    document_type: z.string(), fields: z.number(), corrected: z.number(), accuracy: z.number().nullable(), ungrounded: z.number(),
  })),
});

const assessmentSchema = z.object({
  claim_id: z.string(), claim_reference: z.string(), claim_status: z.string(),
  assessment_status: z.string().nullable().optional(), eligible_payout: z.string().nullable().optional(),
  currency: z.string().nullable().optional(), rule_set_version: z.string(), assessed_at: z.string().nullable(),
});
export type Assessment = z.infer<typeof assessmentSchema>;

export function periodQuery(p: Period, extra: Record<string, string> = {}): string {
  return new URLSearchParams({ days: String(p.days), include_synthetic: String(p.includeSynthetic), ...extra }).toString();
}

function useMetricQuery<T>(name: string, path: string, schema: z.ZodType<T>, p: Period, extra?: Record<string, string>) {
  const query = periodQuery(p, extra);
  return useQuery({
    queryKey: ["metrics", name, query],
    queryFn: ({ signal }) => apiGet(`${path}?${query}`, schema, { signal }),
    refetchInterval: 30_000,
  });
}

export const useSummary = (p: Period) => useMetricQuery("summary", "/dashboard/summary", summarySchema, p);
export const useActivity = (p: Period) => useMetricQuery("activity", "/dashboard/activity", activitySchema, p);
export const useProcessingTimes = (p: Period) => useMetricQuery("times", "/analytics/processing-times", processingSchema, p);
export const useClaimsAnalytics = (p: Period, bucket: "day" | "week") =>
  useMetricQuery("claims", "/analytics/claims", claimsSchema, p, { bucket });
export const useFraudAnalytics = (p: Period) => useMetricQuery("fraud", "/analytics/fraud", fraudSchema, p);
export const useAgentAnalytics = (p: Period) => useMetricQuery("agents", "/analytics/agents", agentsSchema, p);
export const useExtractionAccuracy = (p: Period) =>
  useMetricQuery("extraction", "/analytics/extraction-accuracy", extractionSchema, p);

export function useAssessments(includeSynthetic: boolean) {
  return useQuery({
    queryKey: ["assessments", includeSynthetic],
    queryFn: ({ signal }) => apiGet(`/assessments?include_synthetic=${includeSynthetic}`, z.array(assessmentSchema), { signal }),
  });
}

/** Display a metric value by unit; ``null`` means no underlying rows, shown as an em dash. */
export function formatMetric(value: number | null | undefined, unit: Metric["unit"]): string {
  if (value === null || value === undefined) return "—";
  if (unit === "ratio") return `${(value * 100).toFixed(value === 0 || value * 100 >= 10 ? 0 : 1)}%`;
  if (unit === "usd") return `$${value.toFixed(value < 1 ? 4 : 2)}`;
  if (unit === "ms") {
    if (value < 1000) return `${Math.round(value)} ms`;
    if (value < 60_000) return `${(value / 1000).toFixed(1)} s`;
    if (value < 3_600_000) return `${(value / 60_000).toFixed(1)} min`;
    return `${(value / 3_600_000).toFixed(1)} h`;
  }
  return new Intl.NumberFormat().format(value);
}
