"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { z } from "zod";

import { apiGet, apiRequest } from "@/lib/api/client";

export const NODE_LABELS: Record<string, string> = {
  INTAKE: "Intake",
  DOCUMENT_PROCESSING: "Document processing",
  DOCUMENT_VALIDATION: "Document validation",
  DOCUMENT_REVIEW: "Document review",
  POLICY_VERIFICATION: "Policy verification",
  FRAUD_ANALYSIS: "Fraud analysis",
  CLAIM_ASSESSMENT: "Claim assessment",
  CLAIM_REASSESSMENT: "Reassessment",
  RESULT_AGGREGATION: "Result aggregation",
  CROSS_VALIDATION: "Cross-validation",
  DECISION_RECOMMENDATION: "Decision recommendation",
  COMPLIANCE_CHECK: "Compliance check",
  MORE_INFO: "More information",
  HUMAN_REVIEW: "Human review",
};
export const MAIN_PATH = [
  "INTAKE", "DOCUMENT_PROCESSING", "DOCUMENT_VALIDATION", "POLICY_VERIFICATION", "FRAUD_ANALYSIS",
  "CLAIM_ASSESSMENT", "RESULT_AGGREGATION", "CROSS_VALIDATION", "DECISION_RECOMMENDATION", "COMPLIANCE_CHECK",
];
export const ACTIVE = ["queued", "running", "interrupted"];

export const executionSchema = z.object({
  id: z.string(),
  claim_id: z.string(),
  status: z.string(),
  trigger: z.string(),
  current_node: z.string().nullable(),
  interrupt: z.record(z.string(), z.unknown()).nullable(),
  progress: z.string(),
  iteration_count: z.number(),
  reassessment_count: z.number(),
  more_info_count: z.number(),
  recovery_count: z.number(),
  tokens_used: z.number(),
  created_at: z.string(),
  started_at: z.string().nullable(),
  finished_at: z.string().nullable(),
  duration_ms: z.number().nullable(),
  cancel_requested_at: z.string().nullable(),
  error_code: z.string().nullable(),
  error_detail: z.string().nullable(),
});
export type Execution = z.infer<typeof executionSchema>;
const listItemSchema = executionSchema.extend({ claim_reference: z.string() });
export type ExecutionListItem = z.infer<typeof listItemSchema>;

const eventSchema = z.object({
  seq: z.number(),
  type: z.string(),
  node: z.string().nullable(),
  agent: z.string().nullable().optional(),
  occurred_at: z.string(),
}).passthrough();
export type WorkflowEvent = z.infer<typeof eventSchema> & Record<string, unknown>;
const storedEventSchema = eventSchema.extend({ payload: z.record(z.string(), z.unknown()) });
const detailSchema = executionSchema.extend({ events: z.array(storedEventSchema) });

const EVENT_TYPES = [
  "execution_started", "node_started", "node_completed", "node_failed", "retry", "interrupt", "resumed",
  "status_changed", "execution_completed", "execution_failed", "execution_cancelled", "recovered",
];

export function useClaimExecutions(claimId: string) {
  return useQuery({
    queryKey: ["executions", claimId],
    queryFn: ({ signal }) => apiGet(`/claims/${claimId}/executions`, z.array(executionSchema), { signal }),
    refetchInterval: (q) => (q.state.data?.some((e) => e.status === "queued" || e.status === "running") ? 3000 : false),
  });
}

export function useOrgExecutions(status?: string) {
  return useQuery({
    queryKey: ["executions", "org", status ?? "all"],
    queryFn: ({ signal }) =>
      apiGet(`/workflow-executions${status ? `?status=${status}` : ""}`, z.array(listItemSchema), { signal }),
    refetchInterval: 5000,
  });
}

/** Full history from the API, then live events over SSE (the browser resumes with Last-Event-ID). */
export function useExecutionEvents(executionId: string | null | undefined) {
  const [events, setEvents] = useState<WorkflowEvent[]>([]);
  const [live, setLive] = useState(false);
  const qc = useQueryClient();
  useEffect(() => {
    if (!executionId) return;
    let closed = false;
    const seen = new Set<number>();
    const add = (e: WorkflowEvent) => {
      if (seen.has(e.seq)) return;
      seen.add(e.seq);
      setEvents((prev) => [...prev, e].sort((a, b) => a.seq - b.seq));
    };
    void apiGet(`/workflow-executions/${executionId}`, detailSchema).then((d) => {
      if (!closed) d.events.forEach(({ payload, ...e }) => add({ ...e, ...payload } as WorkflowEvent));
    });
    const source = new EventSource(`/api/v1/workflow-executions/${executionId}/events`, { withCredentials: true });
    source.onopen = () => setLive(true);
    source.onerror = () => setLive(false);
    const onEvent = (msg: MessageEvent<string>) => {
      const parsed = eventSchema.safeParse(JSON.parse(msg.data));
      if (!parsed.success) return;
      add(parsed.data as WorkflowEvent);
      void qc.invalidateQueries({ queryKey: ["executions"] });
      if (parsed.data.type !== "node_started") {
        void qc.invalidateQueries({ queryKey: ["agent-runs"] });
        void qc.invalidateQueries({ queryKey: ["claim"] });
      }
    };
    EVENT_TYPES.forEach((t) => source.addEventListener(t, onEvent as EventListener));
    source.addEventListener("end", () => { source.close(); setLive(false); });
    return () => { closed = true; source.close(); };
  }, [executionId, qc]);
  return { events, live };
}

function useProcessingMutation<V>(fn: (vars: V) => Promise<unknown>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["executions"] });
      void qc.invalidateQueries({ queryKey: ["claim"] });
    },
  });
}

export const useStartProcessing = (claimId: string) =>
  useProcessingMutation(() =>
    apiRequest("POST", `/claims/${claimId}/processing`, executionSchema, { headers: { "Idempotency-Key": crypto.randomUUID() } }));
export const useCancelProcessing = (claimId: string) =>
  useProcessingMutation(() => apiRequest("POST", `/claims/${claimId}/processing/cancel`, executionSchema));
export const useResume = () =>
  useProcessingMutation(({ id, interruptId, action, note }: { id: string; interruptId: string; action: string; note?: string }) =>
    apiRequest("POST", `/workflow-executions/${id}/resume`, executionSchema, { body: { interrupt_id: interruptId, action, note } }));
export const useRetry = () =>
  useProcessingMutation((id: string) => apiRequest("POST", `/workflow-executions/${id}/retry`, executionSchema));
