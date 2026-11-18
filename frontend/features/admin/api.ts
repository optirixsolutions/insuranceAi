"use client";

import { useMutation, useQuery, useQueryClient, useInfiniteQuery } from "@tanstack/react-query";
import { z } from "zod";

import { apiGet, apiRequest } from "@/lib/api/client";

export const organizationSchema = z.object({
  id: z.string(),
  name: z.string(),
  slug: z.string(),
  status: z.string(),
  default_currency: z.string(),
  default_timezone: z.string(),
  jurisdiction_code: z.string().nullable(),
  ai_data_processing_approved: z.boolean(),
  row_version: z.number().int(),
});
export type Organization = z.infer<typeof organizationSchema>;

export const userSchema = z.object({
  id: z.string(),
  email: z.string(),
  display_name: z.string(),
  status: z.enum(["invited", "active", "disabled"]),
  roles: z.array(z.string()),
  last_login_at: z.string().nullable(),
  created_at: z.string(),
  row_version: z.number().int(),
});
export type User = z.infer<typeof userSchema>;

const userPageSchema = z.object({
  items: z.array(userSchema),
  page: z.number(),
  page_size: z.number(),
  total: z.number(),
});

export const accessLinkSchema = z.object({
  url: z.string(),
  purpose: z.enum(["invite", "reset"]),
  expires_at: z.string(),
});
export type AccessLink = z.infer<typeof accessLinkSchema>;

const inviteResponseSchema = z.object({ user: userSchema, access_link: accessLinkSchema });

export const roleSchema = z.object({
  id: z.string(),
  key: z.string(),
  name: z.string(),
  description: z.string(),
  is_system: z.boolean(),
  permissions: z.array(z.string()),
});
export type Role = z.infer<typeof roleSchema>;

export const auditEventSchema = z.object({
  id: z.string(),
  seq: z.number(),
  occurred_at: z.string(),
  actor_type: z.string(),
  actor_id: z.string().nullable(),
  actor_name: z.string().nullable(),
  action: z.string(),
  entity_type: z.string(),
  entity_id: z.string().nullable(),
  request_id: z.string().nullable(),
  ip: z.string().nullable(),
  before: z.record(z.string(), z.unknown()).nullable(),
  after: z.record(z.string(), z.unknown()).nullable(),
});
export type AuditEvent = z.infer<typeof auditEventSchema>;

const auditPageSchema = z.object({ items: z.array(auditEventSchema), next_cursor: z.string().nullable() });
export const auditVerificationSchema = z.object({
  valid: z.boolean(),
  events_checked: z.number(),
  first_invalid_seq: z.number().nullable(),
  reason: z.string().nullable(),
});

const ifMatch = (version: number) => ({ "If-Match": `"${version}"` });

export function useOrganization() {
  return useQuery({ queryKey: ["organization"], queryFn: ({ signal }) => apiGet("/organization", organizationSchema, { signal }) });
}

export function useUpdateOrganization() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ version, changes }: { version: number; changes: Partial<Organization> }) =>
      apiRequest("PATCH", "/organization", organizationSchema, { body: changes, headers: ifMatch(version) }),
    onSuccess: (org) => {
      qc.setQueryData(["organization"], org);
      void qc.invalidateQueries({ queryKey: ["auth", "me"] });
    },
  });
}

export function useUsers(params: { page: number; q: string; status: string }) {
  const search = new URLSearchParams({ page: String(params.page), page_size: "25" });
  if (params.q) search.set("q", params.q);
  if (params.status) search.set("status", params.status);
  return useQuery({
    queryKey: ["users", params],
    queryFn: ({ signal }) => apiGet(`/users?${search.toString()}`, userPageSchema, { signal }),
    placeholderData: (previous) => previous,
  });
}

export function useRoles() {
  return useQuery({ queryKey: ["roles"], queryFn: ({ signal }) => apiGet("/roles", z.array(roleSchema), { signal }), staleTime: 300_000 });
}

function useUserMutation<V, R>(fn: (vars: V) => Promise<R>) {
  const qc = useQueryClient();
  return useMutation({ mutationFn: fn, onSettled: () => qc.invalidateQueries({ queryKey: ["users"] }) });
}

export function useInviteUser() {
  return useUserMutation((body: { email: string; display_name: string; roles: string[] }) =>
    apiRequest("POST", "/users", inviteResponseSchema, { body }),
  );
}

export function useSetUserStatus() {
  return useUserMutation(({ user, status }: { user: User; status: "active" | "disabled" }) =>
    apiRequest("PATCH", `/users/${user.id}`, userSchema, { body: { status }, headers: ifMatch(user.row_version) }),
  );
}

export function useSetUserRoles() {
  return useUserMutation(({ userId, roles }: { userId: string; roles: string[] }) =>
    apiRequest("PUT", `/users/${userId}/roles`, userSchema, { body: { roles } }),
  );
}

export function useResetAccess() {
  return useUserMutation((userId: string) => apiRequest("POST", `/users/${userId}/reset-access`, accessLinkSchema));
}

export function useAuditLog(filters: { action: string }) {
  return useInfiniteQuery({
    queryKey: ["audit", filters],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => {
      const search = new URLSearchParams({ limit: "50" });
      if (pageParam) search.set("cursor", pageParam);
      if (filters.action) search.set("action", filters.action);
      return apiGet(`/audit-logs?${search.toString()}`, auditPageSchema, { signal });
    },
    getNextPageParam: (last) => last.next_cursor,
  });
}

export function useVerifyAuditChain() {
  return useMutation({ mutationFn: () => apiGet("/audit-logs/verify", auditVerificationSchema) });
}
