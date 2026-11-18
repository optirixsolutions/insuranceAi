"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { apiGet, apiRequest, noContent } from "@/lib/api/client";

export const moneySchema = z.object({ amount: z.string(), currency: z.string() });
export type MoneyValue = z.infer<typeof moneySchema>;

export const claimListItemSchema = z.object({
  id: z.string(),
  claim_reference: z.string(),
  status: z.string(),
  priority: z.enum(["low", "normal", "high", "urgent"]),
  loss_type: z.string(),
  incident_date: z.string(),
  claimed_amount: moneySchema,
  customer_id: z.string(),
  customer_name: z.string(),
  policy_id: z.string(),
  policy_number: z.string(),
  product_name: z.string(),
  product_line: z.string(),
  adjuster_name: z.string().nullable(),
  is_synthetic: z.boolean(),
  created_at: z.string(),
});
export type ClaimListItem = z.infer<typeof claimListItemSchema>;

const findingSchema = z.object({ code: z.string(), severity: z.string(), message: z.string() });
const assignmentSchema = z.object({
  role_on_claim: z.string(),
  user_id: z.string(),
  display_name: z.string(),
  assigned_at: z.string(),
});

export const claimDetailSchema = claimListItemSchema.extend({
  incident_time: z.string().nullable(),
  incident_location: z.record(z.string(), z.unknown()).nullable(),
  description: z.string(),
  channel: z.string(),
  reported_at: z.string(),
  policy_version: z
    .object({
      id: z.string(),
      version_no: z.number(),
      effective_from: z.string(),
      effective_to: z.string(),
      sum_insured: z.string(),
      deductible_amount: z.string(),
      currency: z.string(),
    })
    .nullable(),
  findings: z.array(findingSchema),
  assignments: z.array(assignmentSchema),
  allowed_status_changes: z.array(z.string()),
  required_documents: z.array(z.string()),
  documents_received: z.array(z.string()),
  row_version: z.number(),
  can_edit: z.boolean(),
});
export type ClaimDetail = z.infer<typeof claimDetailSchema>;

export const claimCreatedSchema = claimDetailSchema.extend({
  missing_documents: z.array(z.string()),
  duplicate_suspects: z.array(z.object({ id: z.string(), claim_reference: z.string() })),
});

const pageOf = <T extends z.ZodTypeAny>(item: T) =>
  z.object({ items: z.array(item), page: z.number(), page_size: z.number(), total: z.number() });

const noteSchema = z.object({
  id: z.string(),
  body: z.string(),
  visibility: z.string(),
  author_id: z.string(),
  author_name: z.string(),
  created_at: z.string(),
});
const timelineSchema = z.object({
  at: z.string(),
  kind: z.string(),
  title: z.string(),
  detail: z.string().nullable(),
  actor_name: z.string().nullable(),
});
const relatedSchema = z.object({
  id: z.string(),
  claim_reference: z.string(),
  status: z.string(),
  incident_date: z.string(),
  relation: z.string(),
  source: z.string(),
  detail: z.string(),
});
const assigneeSchema = z.object({ id: z.string(), display_name: z.string(), email: z.string() });

export type ClaimFilters = {
  q: string;
  status: string;
  priority: string;
  assignee: string;
  sort: string;
  page: number;
};

export function claimsSearch(filters: ClaimFilters, includePaging = true) {
  const params = new URLSearchParams();
  if (filters.q) params.set("q", filters.q);
  if (filters.status) params.append("status", filters.status);
  if (filters.priority) params.set("priority", filters.priority);
  if (filters.assignee) params.set("assignee", filters.assignee);
  params.set("sort", filters.sort);
  if (includePaging) {
    params.set("page", String(filters.page));
    params.set("page_size", "20");
  }
  return params.toString();
}

export function useClaims(filters: ClaimFilters) {
  return useQuery({
    queryKey: ["claims", filters],
    queryFn: ({ signal }) => apiGet(`/claims?${claimsSearch(filters)}`, pageOf(claimListItemSchema), { signal }),
    placeholderData: keepPreviousData,
  });
}

export function useClaim(id: string) {
  return useQuery({ queryKey: ["claim", id], queryFn: ({ signal }) => apiGet(`/claims/${id}`, claimDetailSchema, { signal }) });
}

export function useLossTypes() {
  return useQuery({
    queryKey: ["loss-types"],
    queryFn: ({ signal }) => apiGet("/claims/loss-types", z.object({ lines: z.record(z.string(), z.array(z.string())) }), { signal }),
    staleTime: Infinity,
  });
}

export function useCreateClaim() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ body, idempotencyKey }: { body: unknown; idempotencyKey: string }) =>
      apiRequest("POST", "/claims", claimCreatedSchema, { body, headers: { "Idempotency-Key": idempotencyKey } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["claims"] }),
  });
}

function useClaimMutation<V, R>(id: string, fn: (vars: V) => Promise<R>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["claim", id] });
      void qc.invalidateQueries({ queryKey: ["claim-timeline", id] });
      void qc.invalidateQueries({ queryKey: ["claims"] });
    },
  });
}

export function useUpdateClaim(claim: ClaimDetail) {
  return useClaimMutation(claim.id, (changes: Record<string, unknown>) =>
    apiRequest("PATCH", `/claims/${claim.id}`, claimDetailSchema, { body: changes, headers: { "If-Match": `"${claim.row_version}"` } }),
  );
}

export function useChangeStatus(claim: ClaimDetail) {
  return useClaimMutation(claim.id, (body: { status: string; reason: string }) =>
    apiRequest("POST", `/claims/${claim.id}/status`, claimDetailSchema, { body, headers: { "If-Match": `"${claim.row_version}"` } }),
  );
}

export function useAssign(claimId: string) {
  return useClaimMutation(claimId, (body: { role: string; user_id: string | null }) =>
    apiRequest("PUT", `/claims/${claimId}/assignments`, z.array(assignmentSchema), { body }),
  );
}

export function useAssignees(role: string, enabled: boolean) {
  return useQuery({
    queryKey: ["assignees", role],
    queryFn: ({ signal }) => apiGet(`/claims/assignees?role=${role}`, z.array(assigneeSchema), { signal }),
    enabled,
  });
}

export function useNotes(claimId: string) {
  return useQuery({ queryKey: ["claim-notes", claimId], queryFn: ({ signal }) => apiGet(`/claims/${claimId}/notes`, z.array(noteSchema), { signal }) });
}

export function useAddNote(claimId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { body: string; visibility: string }) => apiRequest("POST", `/claims/${claimId}/notes`, noteSchema, { body }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["claim-notes", claimId] });
      void qc.invalidateQueries({ queryKey: ["claim-timeline", claimId] });
    },
  });
}

export function useDeleteNote(claimId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (noteId: string) => apiRequest("DELETE", `/claims/${claimId}/notes/${noteId}`, noContent),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["claim-notes", claimId] });
      void qc.invalidateQueries({ queryKey: ["claim-timeline", claimId] });
    },
  });
}

export function useTimeline(claimId: string) {
  return useQuery({ queryKey: ["claim-timeline", claimId], queryFn: ({ signal }) => apiGet(`/claims/${claimId}/timeline`, z.array(timelineSchema), { signal }) });
}

export function useRelated(claimId: string) {
  return useQuery({ queryKey: ["claim-related", claimId], queryFn: ({ signal }) => apiGet(`/claims/${claimId}/related`, z.array(relatedSchema), { signal }) });
}
