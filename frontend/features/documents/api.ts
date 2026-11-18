"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { apiGet, apiRequest, apiUpload } from "@/lib/api/client";

export const documentSchema = z.object({
  id: z.string(),
  claim_id: z.string(),
  version_no: z.number(),
  supersedes_id: z.string().nullable(),
  original_filename: z.string(),
  detected_mime: z.string(),
  size_bytes: z.number(),
  sha256: z.string(),
  scan_status: z.string(),
  processing_status: z.string(),
  document_type: z.string().nullable(),
  classification_confidence: z.number().nullable(),
  classification_source: z.string().nullable(),
  page_count: z.number().nullable(),
  quality_score: z.number().nullable(),
  review_reasons: z.array(z.string()),
  duplicates: z.array(
    z.object({ document_id: z.string(), claim_id: z.string(), claim_reference: z.string(), method: z.string(), distance: z.number(), same_claim: z.boolean() }),
  ),
  error_detail: z.string().nullable(),
  processed_at: z.string().nullable(),
  created_at: z.string(),
  row_version: z.number(),
});
export type ClaimDocument = z.infer<typeof documentSchema>;

const uploadResultSchema = z.object({
  filename: z.string(),
  accepted: z.boolean(),
  document: documentSchema.nullable().optional(),
  error_code: z.string().nullable().optional(),
  error: z.string().nullable().optional(),
});
export type UploadResult = z.infer<typeof uploadResultSchema>;

export const pageSchema = z.object({
  page_number: z.number(),
  text_source: z.string(),
  text: z.string(),
  ocr_confidence: z.number().nullable(),
  quality_score: z.number(),
  quality_flags: z.array(z.string()),
  width: z.number().nullable(),
  height: z.number().nullable(),
  rotation_applied: z.number(),
  has_image: z.boolean(),
  words: z.array(z.object({ t: z.string(), b: z.array(z.number()), c: z.number() })),
});
export type DocumentPage = z.infer<typeof pageSchema>;

export const fieldSchema = z.object({
  id: z.string(),
  field_name: z.string(),
  value_text: z.string().nullable(),
  value_normalized: z.unknown(),
  value_type: z.string(),
  confidence: z.number(),
  page_number: z.number().nullable(),
  evidence_excerpt: z.string().nullable(),
  bbox: z.array(z.number()).nullable(),
  grounding_status: z.string(),
  validation_status: z.string(),
  source: z.string(),
  created_at: z.string(),
});
export type ExtractedField = z.infer<typeof fieldSchema>;

const ACTIVE = new Set(["queued", "processing"]);

export function useClaimDocuments(claimId: string) {
  return useQuery({
    queryKey: ["claim-documents", claimId],
    queryFn: ({ signal }) => apiGet(`/claims/${claimId}/documents`, z.array(documentSchema), { signal }),
    // Poll only while the worker is still busy; stops automatically when everything has finished.
    refetchInterval: (query) => (query.state.data?.some((d) => ACTIVE.has(d.processing_status)) ? 2000 : false),
  });
}

export function useUploadDocuments(claimId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (files: File[]) => {
      const form = new FormData();
      files.forEach((file) => form.append("files", file));
      return apiUpload(`/claims/${claimId}/documents`, z.array(uploadResultSchema), form);
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["claim-documents", claimId] });
      void qc.invalidateQueries({ queryKey: ["claim", claimId] });
    },
  });
}

export function usePages(documentId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["document-pages", documentId],
    queryFn: ({ signal }) => apiGet(`/documents/${documentId}/pages`, z.array(pageSchema), { signal }),
    enabled,
  });
}

export function useFields(documentId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["document-fields", documentId],
    queryFn: ({ signal }) => apiGet(`/documents/${documentId}/fields`, z.array(fieldSchema), { signal }),
    enabled,
  });
}

export function useDocumentTypes() {
  return useQuery({ queryKey: ["document-types"], queryFn: ({ signal }) => apiGet("/documents/types", z.array(z.string()), { signal }), staleTime: Infinity });
}

function useDocMutation<V, R>(doc: ClaimDocument, fn: (vars: V) => Promise<R>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["document-fields", doc.id] });
      void qc.invalidateQueries({ queryKey: ["claim-documents", doc.claim_id] });
      void qc.invalidateQueries({ queryKey: ["claim", doc.claim_id] });
    },
  });
}

export function useCorrectField(doc: ClaimDocument) {
  return useDocMutation(doc, ({ fieldId, value, reason }: { fieldId: string; value: string; reason: string }) =>
    apiRequest("PATCH", `/documents/${doc.id}/fields/${fieldId}`, fieldSchema, { body: { value, reason } }),
  );
}

export function useVerifyField(doc: ClaimDocument) {
  return useDocMutation(doc, (fieldId: string) => apiRequest("POST", `/documents/${doc.id}/fields/${fieldId}/verify`, fieldSchema));
}

export function useSetType(doc: ClaimDocument) {
  return useDocMutation(doc, (body: { document_type: string; reason: string }) =>
    apiRequest("PATCH", `/documents/${doc.id}/type`, documentSchema, { body }),
  );
}

export function useReprocess(doc: ClaimDocument) {
  return useDocMutation(doc, () => apiRequest("POST", `/documents/${doc.id}/reprocess`, documentSchema));
}

export function useReviewQueue() {
  return useQuery({
    queryKey: ["document-review-queue"],
    queryFn: ({ signal }) =>
      apiGet("/documents/review-queue", z.array(z.object({ document: documentSchema, claim_reference: z.string() })), { signal }),
  });
}
