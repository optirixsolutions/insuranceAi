"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { apiGet, apiRequest } from "@/lib/api/client";

const pageOf = <T extends z.ZodTypeAny>(item: T) =>
  z.object({ items: z.array(item), page: z.number(), page_size: z.number(), total: z.number() });

export const customerSchema = z.object({
  id: z.string(),
  full_name: z.string(),
  customer_type: z.string(),
  external_ref: z.string().nullable(),
  date_of_birth: z.string().nullable(),
  email: z.string().nullable(),
  phone: z.string().nullable(),
  address: z.record(z.string(), z.unknown()).nullable(),
  is_synthetic: z.boolean(),
  row_version: z.number(),
});
export type Customer = z.infer<typeof customerSchema>;

export const productSchema = z.object({
  id: z.string(),
  code: z.string(),
  name: z.string(),
  line: z.string(),
  currency: z.string(),
  description: z.string(),
  status: z.string(),
  row_version: z.number(),
  document_requirements: z.array(z.object({ document_type: z.string(), mandatory: z.boolean(), description: z.string() })),
});
export type Product = z.infer<typeof productSchema>;

export const policySummarySchema = z.object({
  id: z.string(),
  policy_number: z.string(),
  status: z.string(),
  customer_id: z.string(),
  customer_name: z.string(),
  product_id: z.string(),
  product_name: z.string(),
  product_line: z.string(),
  currency: z.string(),
  is_synthetic: z.boolean(),
});
export type PolicySummary = z.infer<typeof policySummarySchema>;

export const versionSchema = z.object({
  id: z.string(),
  version_no: z.number(),
  status: z.string(),
  effective_from: z.string(),
  effective_to: z.string(),
  currency: z.string(),
  sum_insured: z.string(),
  deductible_amount: z.string(),
  annual_aggregate_limit: z.string().nullable(),
  waiting_period_days: z.number(),
  endorsements: z.array(z.record(z.string(), z.unknown())),
  published_at: z.string().nullable(),
  clauses: z.array(z.object({ clause_ref: z.string(), section: z.string(), title: z.string(), text: z.string() })),
  coverages: z.array(
    z.object({
      coverage_code: z.string(),
      perils: z.array(z.string()),
      limit_amount: z.string(),
      clause_ref: z.string(),
      deductible_override: z.string().nullable(),
      co_pay_pct: z.string().nullable(),
    }),
  ),
  exclusions: z.array(z.object({ exclusion_code: z.string(), applies_to: z.array(z.string()), description: z.string(), clause_ref: z.string() })),
});
export type PolicyVersion = z.infer<typeof versionSchema>;

export const policySchema = policySummarySchema.extend({
  source: z.string(),
  external_ref: z.string().nullable(),
  versions: z.array(versionSchema),
});

export const importReportSchema = z.object({
  dry_run: z.boolean(),
  policies_created: z.number(),
  customers_created: z.number(),
  versions_created: z.number(),
  versions_published: z.number(),
  policy_numbers: z.array(z.string()),
});

export function useCustomers(q: string, page = 1) {
  const search = new URLSearchParams({ page: String(page), page_size: "20" });
  if (q) search.set("q", q);
  return useQuery({
    queryKey: ["customers", q, page],
    queryFn: ({ signal }) => apiGet(`/customers?${search.toString()}`, pageOf(customerSchema), { signal }),
    placeholderData: keepPreviousData,
  });
}

export function usePolicies(params: { q?: string; customerId?: string; page?: number }, enabled = true) {
  const search = new URLSearchParams({ page: String(params.page ?? 1), page_size: "20" });
  if (params.q) search.set("q", params.q);
  if (params.customerId) search.set("customer_id", params.customerId);
  return useQuery({
    queryKey: ["policies", params],
    queryFn: ({ signal }) => apiGet(`/policies?${search.toString()}`, pageOf(policySummarySchema), { signal }),
    placeholderData: keepPreviousData,
    enabled,
  });
}

export function usePolicy(id: string) {
  return useQuery({ queryKey: ["policy", id], queryFn: ({ signal }) => apiGet(`/policies/${id}`, policySchema, { signal }) });
}

export function useProducts() {
  return useQuery({ queryKey: ["products"], queryFn: ({ signal }) => apiGet("/products", z.array(productSchema), { signal }) });
}

function useCatalogMutation<V, R>(fn: (vars: V) => Promise<R>, keys: string[][]) {
  const qc = useQueryClient();
  return useMutation({ mutationFn: fn, onSuccess: () => keys.forEach((queryKey) => void qc.invalidateQueries({ queryKey })) });
}

export function useCreateProduct() {
  return useCatalogMutation((body: Record<string, unknown>) => apiRequest("POST", "/products", productSchema, { body }), [["products"]]);
}

export function useCreateCustomer() {
  return useCatalogMutation((body: Record<string, unknown>) => apiRequest("POST", "/customers", customerSchema, { body }), [["customers"]]);
}

export function useCreatePolicy() {
  return useCatalogMutation((body: Record<string, unknown>) => apiRequest("POST", "/policies", policySchema, { body }), [["policies"]]);
}

export function usePublishVersion(policyId: string) {
  return useCatalogMutation(
    ({ versionId, supersede }: { versionId: string; supersede: string[] }) =>
      apiRequest("POST", `/policies/${policyId}/versions/${versionId}/publish`, versionSchema, { body: { supersede_version_ids: supersede } }),
    [["policy", policyId]],
  );
}

export function useImportPolicies() {
  return useCatalogMutation(
    (body: unknown) => apiRequest("POST", "/policies/import", importReportSchema, { body }),
    [["policies"], ["customers"]],
  );
}
