"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { apiGet, apiRequest } from "@/lib/api/client";

const similarSchema = z.object({
  model: z.string(),
  definition: z.string(),
  items: z.array(z.object({
    claim_id: z.string(), claim_reference: z.string(), status: z.string(), category: z.string(),
    same_customer: z.boolean(), incident_date: z.string(), claimed_amount: z.string(), currency: z.string(),
    similarity: z.number(), shared_terms: z.array(z.string()),
  })),
});

const anomalySchema = z.object({
  definition: z.string(),
  notes: z.array(z.string()),
  anomalies: z.array(z.object({
    type: z.string(), severity: z.string(), score: z.number(), explanation: z.string(),
    claims: z.array(z.object({ claim_id: z.string(), claim_reference: z.string() })),
  })),
});

const feedbackSummarySchema = z.object({
  definition: z.string(),
  agents: z.array(z.object({
    agent_key: z.string(), counts: z.record(z.string(), z.number()),
    agreement_rate: z.number().nullable(), precision: z.number().nullable(), helpful_rate: z.number().nullable(),
  })),
});

const evalSchema = z.object({
  id: z.string(), agent_key: z.string(), kind: z.string(), prompt_version: z.string(), prompt_sha256: z.string(),
  model_id: z.string().nullable(), model_slug: z.string().nullable(), status: z.string(), cases: z.number(),
  metrics: z.array(z.object({
    name: z.string(), value: z.number(), threshold: z.number().nullable(), passed: z.boolean(), part: z.string(),
  }).passthrough()),
  regressions: z.array(z.string()), model_calls: z.number(), error: z.string().nullable(),
  created_at: z.string(), finished_at: z.string().nullable(),
});
export type EvalRun = z.infer<typeof evalSchema>;

const promptSchema = z.object({
  id: z.string(), agent_key: z.string(), version: z.string(), content_sha256: z.string(), content: z.string(),
  active_for_org: z.boolean(), latest_eval: evalSchema.nullable(),
});
export type PromptVersion = z.infer<typeof promptSchema>;

export function useSimilar(claimId: string) {
  return useQuery({
    queryKey: ["similar", claimId],
    queryFn: ({ signal }) => apiGet(`/claims/${claimId}/similar`, similarSchema, { signal }),
  });
}

export function useAnomalies(days = 90) {
  return useQuery({
    queryKey: ["anomalies", days],
    queryFn: ({ signal }) => apiGet(`/analytics/anomalies?days=${days}`, anomalySchema, { signal }),
  });
}

export function useFeedbackSummary(days = 90) {
  return useQuery({
    queryKey: ["feedback-summary", days],
    queryFn: ({ signal }) => apiGet(`/analytics/feedback?days=${days}`, feedbackSummarySchema, { signal }),
  });
}

export function useGiveFeedback(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (label: "helpful" | "not_helpful") =>
      apiRequest("POST", `/agent-runs/${runId}/feedback`, z.object({ label: z.string() }), { body: { label } }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["feedback-summary"] }),
  });
}

export function usePrompts() {
  return useQuery({ queryKey: ["prompts"], queryFn: ({ signal }) => apiGet("/prompts", z.array(promptSchema), { signal }) });
}

export function useEvals() {
  return useQuery({
    queryKey: ["evals"],
    queryFn: ({ signal }) => apiGet("/evals", z.array(evalSchema), { signal }),
    refetchInterval: (q) => (q.state.data?.some((r) => r.status === "queued" || r.status === "running") ? 3_000 : false),
  });
}

export function useEvalActions() {
  const qc = useQueryClient();
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["evals"] });
    void qc.invalidateQueries({ queryKey: ["prompts"] });
    void qc.invalidateQueries({ queryKey: ["ai-agents"] });
  };
  return {
    run: useMutation({
      mutationFn: (body: { agent_key: string; prompt_version?: string; model_id?: string }) =>
        apiRequest("POST", "/evals", evalSchema, { body }),
      onSuccess: refresh,
    }),
    compare: useMutation({
      mutationFn: (body: { agent_key: string; model_ids: string[] }) =>
        apiRequest("POST", "/evals/compare", z.array(evalSchema), { body }),
      onSuccess: refresh,
    }),
    activate: useMutation({
      mutationFn: (promptId: string) =>
        apiRequest("POST", `/prompts/${promptId}/activate`, z.object({ agent_key: z.string(), prompt_version: z.string() })),
      onSuccess: refresh,
    }),
  };
}
