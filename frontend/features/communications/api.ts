"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { apiGet, apiRequest, noContent } from "@/lib/api/client";

const reportSchema = z.object({
  id: z.string(),
  claim_id: z.string(),
  version_no: z.number(),
  format: z.enum(["pdf", "json"]),
  status: z.enum(["queued", "ready", "failed"]),
  template_version: z.string(),
  sha256: z.string().nullable(),
  size_bytes: z.number().nullable(),
  evidence_count: z.number().nullable(),
  error: z.string().nullable(),
  created_at: z.string(),
  generated_at: z.string().nullable(),
});
export type Report = z.infer<typeof reportSchema>;
const recentSchema = reportSchema.extend({ claim_reference: z.string() });

const notificationSchema = z.object({
  id: z.string(),
  template_key: z.string(),
  title: z.string(),
  body: z.string(),
  link: z.string().nullable(),
  claim_id: z.string().nullable(),
  created_at: z.string(),
  read_at: z.string().nullable(),
});
export type AppNotification = z.infer<typeof notificationSchema>;
const inboxSchema = z.object({ unread: z.number(), items: z.array(notificationSchema) });

const webhookSchema = z.object({
  id: z.string(),
  url: z.string(),
  description: z.string(),
  secret_hint: z.string(),
  event_types: z.array(z.string()),
  active: z.boolean(),
  created_at: z.string(),
  disabled_at: z.string().nullable(),
});
export type Webhook = z.infer<typeof webhookSchema>;
const createdSchema = webhookSchema.extend({ secret: z.string() });
const deliverySchema = z.object({
  id: z.string(),
  event_type: z.string(),
  attempt: z.number(),
  status: z.string(),
  response_code: z.number().nullable(),
  last_error: z.string().nullable(),
  next_attempt_at: z.string().nullable(),
  created_at: z.string(),
  delivered_at: z.string().nullable(),
});
const statusSchema = z.object({
  email: z.object({ provider: z.string(), from: z.string(), host: z.string() }),
  sms: z.object({ provider: z.string(), sends_real_messages: z.boolean() }),
  webhooks: z.object({ active: z.number(), total: z.number() }),
  encryption_key_configured: z.boolean(),
});
const testResultSchema = z.object({
  status: z.string(), provider: z.string().nullable(), message_id: z.string().nullable(), error: z.string().nullable(),
});

export const downloadUrl = (reportId: string) => `/api/v1/reports/${reportId}/download`;

// --- reports -------------------------------------------------------------------------------------

export function useClaimReports(claimId: string) {
  return useQuery({
    queryKey: ["reports", claimId],
    queryFn: ({ signal }) => apiGet(`/claims/${claimId}/reports`, z.array(reportSchema), { signal }),
    refetchInterval: (q) => (q.state.data?.some((r) => r.status === "queued") ? 2_000 : false),
  });
}

export function useRecentReports() {
  return useQuery({ queryKey: ["reports", "recent"], queryFn: ({ signal }) => apiGet("/reports", z.array(recentSchema), { signal }) });
}

export function useRequestReport(claimId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (format: "pdf" | "json") =>
      apiRequest("POST", `/claims/${claimId}/reports`, reportSchema, {
        body: { format },
        headers: { "Idempotency-Key": crypto.randomUUID() },
      }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["reports"] }),
  });
}

// --- notifications -------------------------------------------------------------------------------

export function useInbox(unreadOnly = false) {
  return useQuery({
    queryKey: ["notifications", unreadOnly],
    queryFn: ({ signal }) => apiGet(`/notifications${unreadOnly ? "?unread=true" : ""}`, inboxSchema, { signal }),
    refetchInterval: 20_000,
  });
}

export function useMarkRead() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id?: string) => {
      if (id) await apiRequest("POST", `/notifications/${id}/read`, notificationSchema);
      else await apiRequest("POST", "/notifications/read-all", z.object({ marked_read: z.number() }));
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["notifications"] }),
  });
}

// --- webhooks and messaging ----------------------------------------------------------------------

export function useWebhooks() {
  return useQuery({ queryKey: ["webhooks"], queryFn: ({ signal }) => apiGet("/webhooks", z.array(webhookSchema), { signal }) });
}

export function useEventTypes() {
  return useQuery({
    queryKey: ["webhook-events"],
    queryFn: ({ signal }) => apiGet("/webhooks/event-types", z.array(z.string()), { signal }),
  });
}

export function useDeliveries(webhookId: string | null) {
  return useQuery({
    queryKey: ["webhook-deliveries", webhookId],
    queryFn: ({ signal }) => apiGet(`/webhooks/${webhookId}/deliveries`, z.array(deliverySchema), { signal }),
    enabled: Boolean(webhookId),
    refetchInterval: 5_000,
  });
}

export function useWebhookActions() {
  const qc = useQueryClient();
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["webhooks"] });
    void qc.invalidateQueries({ queryKey: ["webhook-deliveries"] });
  };
  return {
    create: useMutation({
      mutationFn: (body: { url: string; event_types: string[]; description: string }) =>
        apiRequest("POST", "/webhooks", createdSchema, { body }),
      onSuccess: refresh,
    }),
    disable: useMutation({ mutationFn: (id: string) => apiRequest("DELETE", `/webhooks/${id}`, noContent), onSuccess: refresh }),
    ping: useMutation({ mutationFn: (id: string) => apiRequest("POST", `/webhooks/${id}/ping`, deliverySchema), onSuccess: refresh }),
  };
}

export function useIntegrationStatus() {
  return useQuery({ queryKey: ["integrations"], queryFn: ({ signal }) => apiGet("/integrations/status", statusSchema, { signal }) });
}

export function useTestMessage() {
  return useMutation({
    mutationFn: (body: { channel: "email" | "sms"; to?: string }) =>
      apiRequest("POST", "/integrations/test-message", testResultSchema, { body }),
  });
}
