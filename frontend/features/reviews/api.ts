"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { apiGet, apiRequest } from "@/lib/api/client";

export const OPEN_STATUSES = ["pending", "in_progress", "awaiting_second_approval", "escalated"];

const reviewSchema = z.object({
  id: z.string(),
  claim_id: z.string(),
  execution_id: z.string(),
  recommendation: z.string().nullable(),
  payout: z.string().nullable(),
  currency: z.string(),
  fraud_level: z.string().nullable(),
  reasons: z.array(z.string()),
  assigned_to: z.string().nullable(),
  status: z.string(),
  priority_score: z.number(),
  due_at: z.string().nullable(),
  four_eyes_required: z.boolean(),
  outcome: z.string().nullable(),
  created_at: z.string(),
  completed_at: z.string().nullable(),
  row_version: z.number(),
});
export type Review = z.infer<typeof reviewSchema>;

const queueItemSchema = reviewSchema.extend({
  claim_reference: z.string(),
  claim_status: z.string(),
  claimed_amount: z.string(),
  product_id: z.string(),
  assigned_to_name: z.string().nullable(),
  overdue: z.boolean(),
});
export type QueueItem = z.infer<typeof queueItemSchema>;

const decisionSchema = z.object({
  id: z.string(),
  decision: z.string(),
  approved_amount: z.string().nullable(),
  currency: z.string(),
  decided_by: z.string().nullable(),
  decision_mode: z.string(),
  override: z.boolean(),
  override_reason: z.string().nullable(),
  review_id: z.string().nullable(),
  second_approver_id: z.string().nullable(),
  reason: z.string().nullable(),
  reason_code: z.string().nullable(),
  clause_ids: z.array(z.string()),
  created_at: z.string(),
});
export type Decision = z.infer<typeof decisionSchema>;

const authoritySchema = z.object({
  max_approval_amount: z.string(),
  currency: z.string(),
  product_id: z.string().nullable(),
  can_reject: z.boolean(),
  can_override_fraud: z.boolean(),
  valid_from: z.string(),
  valid_to: z.string().nullable(),
});
export type Authority = z.infer<typeof authoritySchema>;

const workspaceSchema = z.object({
  review: queueItemSchema,
  claim_description: z.string(),
  recommendation_explanation: z.string().nullable(),
  matrix_rule: z.string().nullable(),
  contradictions: z.array(z.string()),
  actions: z.array(z.string()),
  waiting: z.boolean(),
  pending_decision: z
    .object({
      decision: z.string(),
      approved_amount: z.string().nullable(),
      reason: z.string(),
      reason_code: z.string().nullable(),
      override: z.boolean(),
      override_reason: z.string().nullable(),
      clause_ids: z.array(z.string()),
      first_decider: z.string(),
      created_at: z.string(),
    })
    .nullable(),
  first_decider_name: z.string().nullable(),
  decisions: z.array(decisionSchema),
  my_authorities: z.array(authoritySchema),
});
export type Workspace = z.infer<typeof workspaceSchema>;

export type QueueFilters = { status?: string; mine?: boolean; risk?: string; overdue?: boolean };

export function queueQuery(f: QueueFilters): string {
  const params = new URLSearchParams();
  if (f.status) params.set("status", f.status);
  if (f.mine) params.set("assignee", "me");
  if (f.risk) params.set("risk_level", f.risk);
  if (f.overdue) params.set("overdue", "true");
  return params.toString();
}

export function useReviewQueue(f: QueueFilters) {
  const qs = queueQuery(f);
  return useQuery({
    queryKey: ["reviews", qs],
    queryFn: ({ signal }) => apiGet(`/reviews${qs ? `?${qs}` : ""}`, z.array(queueItemSchema), { signal }),
    refetchInterval: 15_000,
  });
}

export function useClaimReviews(claimId: string) {
  return useQuery({
    queryKey: ["claim-reviews", claimId],
    queryFn: ({ signal }) => apiGet(`/claims/${claimId}/reviews`, z.array(reviewSchema), { signal }),
  });
}

export function useClaimDecisions(claimId: string) {
  return useQuery({
    queryKey: ["claim-decisions", claimId],
    queryFn: ({ signal }) => apiGet(`/claims/${claimId}/decisions`, z.array(decisionSchema), { signal }),
  });
}

export function useWorkspace(claimId: string, reviewId: string | undefined) {
  return useQuery({
    queryKey: ["review", claimId, reviewId],
    queryFn: ({ signal }) => apiGet(`/claims/${claimId}/reviews/${reviewId}`, workspaceSchema, { signal }),
    enabled: Boolean(reviewId),
  });
}

export function useDeciders(enabled: boolean) {
  return useQuery({
    queryKey: ["review-deciders"],
    queryFn: ({ signal }) =>
      apiGet("/reviews/deciders", z.array(z.object({ id: z.string(), display_name: z.string() })), { signal }),
    enabled,
  });
}

export type ReviewAction =
  | "claim"
  | "approve"
  | "reject"
  | "request-information"
  | "return"
  | "escalate"
  | "second-approval";
const IDEMPOTENT: ReviewAction[] = ["approve", "reject", "request-information", "return", "second-approval"];

/** One mutation for every review action; decisions carry a fresh Idempotency-Key per submit. */
export function useReviewAction(claimId: string, reviewId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ action, body }: { action: ReviewAction; body?: Record<string, unknown> }) =>
      apiRequest("POST", `/claims/${claimId}/reviews/${reviewId}/${action}`, reviewSchema, {
        body: body ?? {},
        headers: IDEMPOTENT.includes(action) ? { "Idempotency-Key": crypto.randomUUID() } : {},
      }),
    onSuccess: () => {
      for (const key of [["review", claimId, reviewId], ["reviews"], ["claim-reviews", claimId], ["claim-decisions", claimId], ["claim", claimId]]) {
        void qc.invalidateQueries({ queryKey: key });
      }
    },
  });
}
