"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { apiGet, apiRequest } from "@/lib/api/client";

const probeSchema = z.object({ ok: z.boolean(), latency_ms: z.number(), detail: z.string(), tested_at: z.string() });

export const modelSchema = z.object({
  id: z.string(),
  slug: z.string(),
  display_name: z.string(),
  is_free: z.boolean(),
  context_length: z.number().nullable(),
  input_modalities: z.array(z.string()),
  declared_capabilities: z.record(z.string(), z.boolean()),
  verified_capabilities: z.record(z.string(), probeSchema),
  probe_status: z.string(),
  probed_at: z.string().nullable(),
  prompt_price_per_mtok: z.string(),
  completion_price_per_mtok: z.string(),
  data_policy: z.string(),
  data_policy_note: z.string().nullable(),
  status: z.string(),
  in_catalog: z.boolean(),
  requests_per_minute: z.number(),
  row_version: z.number(),
});
export type AiModel = z.infer<typeof modelSchema>;

export const agentSchema = z.object({
  agent_key: z.string(),
  description: z.string(),
  enabled: z.boolean(),
  primary_model_id: z.string().nullable(),
  fallback_model_ids: z.array(z.string()),
  required_capabilities: z.array(z.string()),
  confidence_threshold: z.number(),
  max_retries: z.number(),
  timeout_seconds: z.number(),
  max_output_tokens: z.number(),
  temperature: z.number(),
  row_version: z.number(),
});
export type AgentConfig = z.infer<typeof agentSchema>;

const statusSchema = z.object({
  provider: z.string(),
  api_key_configured: z.boolean(),
  key_status: z.string(),
  key_detail: z.string().nullable(),
  default_model: z.string(),
  models_registered: z.number(),
  models_verified: z.number(),
});
const jobSchema = z.object({ job_id: z.string(), status: z.string(), result: z.record(z.string(), z.unknown()).nullable().optional() });
export type Job = z.infer<typeof jobSchema>;
const usageSchema = z.array(
  z.object({
    agent_key: z.string(),
    model_slug: z.string(),
    calls: z.number(),
    succeeded: z.number(),
    prompt_tokens: z.number(),
    completion_tokens: z.number(),
    cost_usd: z.string(),
    p50_latency_ms: z.number(),
  }),
);

export const useAiStatus = () =>
  useQuery({ queryKey: ["ai-status"], queryFn: ({ signal }) => apiGet("/ai/status", statusSchema, { signal }) });

export function useModels() {
  return useQuery({
    queryKey: ["ai-models"],
    queryFn: ({ signal }) => apiGet("/models", z.array(modelSchema), { signal }),
    refetchInterval: (q) => (q.state.data?.some((m) => m.probe_status === "running") ? 3000 : false),
  });
}

export const useAgents = () =>
  useQuery({ queryKey: ["ai-agents"], queryFn: ({ signal }) => apiGet("/agents", z.array(agentSchema), { signal }) });

export const useUsage = () =>
  useQuery({ queryKey: ["ai-usage"], queryFn: ({ signal }) => apiGet("/ai/usage?days=7", usageSchema, { signal }) });

function useAiMutation<V, R>(fn: (vars: V) => Promise<R>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSettled: () => ["ai-models", "ai-agents", "ai-status", "ai-usage"].forEach((k) => void qc.invalidateQueries({ queryKey: [k] })),
  });
}

export const useSyncCatalog = () =>
  useAiMutation(() => apiRequest("POST", "/models/sync", z.record(z.string(), z.number())));

export const useUpdateModel = () =>
  useAiMutation(({ model, changes }: { model: AiModel; changes: Record<string, unknown> }) =>
    apiRequest("PATCH", `/models/${model.id}`, modelSchema, { body: changes, headers: { "If-Match": `"${model.row_version}"` } }),
  );

export const useProbeModel = () =>
  useAiMutation((model: AiModel) => apiRequest("POST", `/models/${model.id}/capability-test`, jobSchema));

export const useUpdateAgent = () =>
  useAiMutation(({ agent, changes }: { agent: AgentConfig; changes: Record<string, unknown> }) =>
    apiRequest("PATCH", `/agents/${agent.agent_key}`, agentSchema, { body: changes, headers: { "If-Match": `"${agent.row_version}"` } }),
  );

export const useStartTestRun = () =>
  useAiMutation((body: { agent_key: string; text: string }) =>
    apiRequest("POST", "/ai/test-runs", jobSchema, { body: { ...body, confirm_synthetic: true } }),
  );

export function useJob(jobId: string | null) {
  return useQuery({
    queryKey: ["ai-job", jobId],
    queryFn: ({ signal }) => apiGet(`/ai/jobs/${jobId}`, jobSchema, { signal }),
    enabled: Boolean(jobId),
    refetchInterval: (q) => (q.state.data && ["done", "failed"].includes(q.state.data.status) ? false : 2000),
  });
}

export function verifiedCaps(model: AiModel): string[] {
  return Object.entries(model.verified_capabilities).filter(([, v]) => v.ok).map(([k]) => k);
}
