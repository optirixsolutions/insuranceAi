"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { apiGet, apiRequest } from "@/lib/api/client";

export const AGENT_ORDER = ["intake", "document", "policy", "fraud", "assessment", "validation", "decision", "compliance"] as const;
export type AgentKey = (typeof AGENT_ORDER)[number];

export const AGENT_LABELS: Record<AgentKey, string> = {
  intake: "Intake",
  document: "Document intelligence",
  policy: "Policy verification",
  fraud: "Fraud investigation",
  assessment: "Claim assessment",
  validation: "Cross-document validation",
  decision: "Decision recommendation",
  compliance: "Compliance",
};

const findingSchema = z.object({
  code: z.string(),
  severity: z.string(),
  message: z.string(),
  evidence: z.array(z.record(z.string(), z.unknown())).optional(),
});
export type AgentFinding = z.infer<typeof findingSchema>;

export const runSchema = z.object({
  id: z.string(),
  claim_id: z.string(),
  agent_key: z.string(),
  attempt: z.number(),
  status: z.string(),
  started_at: z.string(),
  finished_at: z.string().nullable(),
  duration_ms: z.number().nullable(),
  model_slug: z.string().nullable(),
  prompt_version_id: z.string().nullable(),
  rule_set_version: z.string(),
  input_hash: z.string(),
  used_llm: z.boolean(),
  llm_note: z.string().nullable(),
  escalate: z.boolean(),
  findings: z.array(findingSchema),
  missing: z.array(z.object({ item: z.string(), reason: z.string() })),
  prompt_tokens: z.number(),
  completion_tokens: z.number(),
  cost_usd: z.string(),
  error_code: z.string().nullable(),
  error_detail: z.string().nullable(),
});
export type AgentRun = z.infer<typeof runSchema>;

export const runDetailSchema = runSchema.extend({
  output: z.record(z.string(), z.unknown()).nullable(),
  output_schema: z.string().nullable(),
  output_hash: z.string().nullable(),
});
export type AgentRunDetail = z.infer<typeof runDetailSchema>;

const pipelineSchema = z.object({ job_id: z.string(), agents: z.array(z.string()) });

export function useAgentRuns(claimId: string) {
  return useQuery({
    queryKey: ["agent-runs", claimId],
    queryFn: ({ signal }) => apiGet(`/claims/${claimId}/agent-runs`, z.array(runSchema), { signal }),
    refetchInterval: (q) => (q.state.data?.some((r) => r.status === "running") ? 2500 : false),
  });
}

/** Latest run per agent (runs arrive newest first). */
export function latestRuns(runs: AgentRun[] | undefined): Partial<Record<AgentKey, AgentRun>> {
  const out: Partial<Record<AgentKey, AgentRun>> = {};
  for (const run of runs ?? []) {
    const key = run.agent_key as AgentKey;
    if (!out[key]) out[key] = run;
  }
  return out;
}

export function useAgentRun(runId: string | null | undefined) {
  return useQuery({
    queryKey: ["agent-run", runId],
    queryFn: ({ signal }) => apiGet(`/agent-runs/${runId}`, runDetailSchema, { signal }),
    enabled: Boolean(runId),
  });
}

export function useStartAgent(claimId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (agentKey: AgentKey) =>
      apiRequest("POST", `/claims/${claimId}/agent-runs`, runSchema, { body: { agent_key: agentKey } }),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["agent-runs", claimId] }),
  });
}

export function useStartPipeline(claimId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiRequest("POST", `/claims/${claimId}/agent-runs/pipeline`, pipelineSchema),
    onSuccess: () => {
      // The worker creates the runs one by one; poll until they appear (then the list polls itself).
      const poll = (n: number) => {
        void qc.invalidateQueries({ queryKey: ["agent-runs", claimId] });
        if (n > 0) setTimeout(() => poll(n - 1), 2500);
      };
      poll(12);
    },
  });
}
