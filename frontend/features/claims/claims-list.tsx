"use client";

import { Download, FilePlus2 } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { hasPermission, useMe } from "@/features/auth/use-me";
import { API_BASE } from "@/lib/api/client";

import { claimsSearch, useClaims, type ClaimFilters } from "./api";
import { formatDate, formatMoney, humanize, PRIORITY_TONE, STATUS_LABELS, statusTone } from "./format";

const SORTS: Record<string, string> = {
  "-created_at": "Newest first",
  created_at: "Oldest first",
  "-priority": "Most urgent first",
  "-claimed_amount": "Largest amount",
  "-incident_date": "Latest incident",
};

function readFilters(params: URLSearchParams): ClaimFilters {
  return {
    q: params.get("q") ?? "",
    status: params.get("status") ?? "",
    priority: params.get("priority") ?? "",
    assignee: params.get("assignee") ?? "",
    sort: params.get("sort") ?? "-created_at",
    page: Number(params.get("page") ?? "1") || 1,
  };
}

export function ClaimsList() {
  const router = useRouter();
  const params = useSearchParams();
  const filters = readFilters(params);
  const [search, setSearch] = useState(filters.q);
  const claims = useClaims(filters);
  const canCreate = hasPermission(useMe().data, "claims.write");

  // Filters live in the URL so views can be bookmarked and shared.
  function update(changes: Partial<ClaimFilters>) {
    const next = { ...filters, page: 1, ...changes };
    router.replace(`/claims?${claimsSearch(next)}`);
  }

  const totalPages = claims.data ? Math.max(1, Math.ceil(claims.data.total / claims.data.page_size)) : 1;

  return (
    <div className="space-y-4">
      <form
        className="grid gap-3 sm:grid-cols-2 lg:grid-cols-[minmax(0,2fr)_repeat(4,minmax(0,1fr))] lg:items-end"
        onSubmit={(e) => {
          e.preventDefault();
          update({ q: search.trim() });
        }}
      >
        <Field id="claim-search" label="Search">
          <Input id="claim-search" type="search" placeholder="Reference, customer or policy number" value={search}
            onChange={(e) => setSearch(e.target.value)} onBlur={() => search.trim() !== filters.q && update({ q: search.trim() })} />
        </Field>
        <Field id="claim-status" label="Status">
          <Select id="claim-status" value={filters.status} onChange={(e) => update({ status: e.target.value })}>
            <option value="">All statuses</option>
            {Object.entries(STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </Select>
        </Field>
        <Field id="claim-priority" label="Priority">
          <Select id="claim-priority" value={filters.priority} onChange={(e) => update({ priority: e.target.value })}>
            <option value="">All priorities</option>
            {["urgent", "high", "normal", "low"].map((p) => <option key={p} value={p}>{humanize(p)}</option>)}
          </Select>
        </Field>
        <Field id="claim-assignee" label="Adjuster">
          <Select id="claim-assignee" value={filters.assignee} onChange={(e) => update({ assignee: e.target.value })}>
            <option value="">Anyone</option>
            <option value="me">Assigned to me</option>
            <option value="none">Unassigned</option>
          </Select>
        </Field>
        <Field id="claim-sort" label="Sort">
          <Select id="claim-sort" value={filters.sort} onChange={(e) => update({ sort: e.target.value })}>
            {Object.entries(SORTS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </Select>
        </Field>
      </form>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground" aria-live="polite">
          {claims.data ? `${claims.data.total} claim${claims.data.total === 1 ? "" : "s"}` : " "}
        </p>
        <div className="flex gap-2">
          <Button variant="secondary" size="sm" asChild>
            <a href={`${API_BASE}/claims/export?${claimsSearch(filters, false)}`} download>
              <Download aria-hidden />Export CSV
            </a>
          </Button>
          {canCreate ? (
            <Button size="sm" asChild>
              <Link href="/claims/new"><FilePlus2 aria-hidden />New claim</Link>
            </Button>
          ) : null}
        </div>
      </div>

      <Card className="overflow-x-auto">
        <table className="w-full min-w-[880px] text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th scope="col" className="px-4 py-2.5 font-medium">Claim</th>
              <th scope="col" className="px-4 py-2.5 font-medium">Customer / policy</th>
              <th scope="col" className="px-4 py-2.5 font-medium">Incident</th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">Claimed</th>
              <th scope="col" className="px-4 py-2.5 font-medium">Status</th>
              <th scope="col" className="px-4 py-2.5 font-medium">Priority</th>
              <th scope="col" className="px-4 py-2.5 font-medium">Adjuster</th>
            </tr>
          </thead>
          <tbody>
            {claims.isPending ? (
              <tr><td colSpan={7} className="p-4"><Skeleton className="h-32" /></td></tr>
            ) : claims.error ? (
              <tr><td colSpan={7} className="p-4 text-danger" role="alert">{claims.error.message}</td></tr>
            ) : claims.data.items.length === 0 ? (
              <tr><td colSpan={7} className="p-8 text-center text-muted-foreground">No claims match these filters.</td></tr>
            ) : (
              claims.data.items.map((c) => (
                <tr key={c.id} className="border-b border-border last:border-0 hover:bg-surface-muted/50" data-testid={`claim-row-${c.claim_reference}`}>
                  <td className="px-4 py-3">
                    <Link href={`/claims/${c.id}`} className="font-mono text-[13px] font-medium text-primary hover:underline">
                      {c.claim_reference}
                    </Link>
                    <p className="text-xs text-muted-foreground">{humanize(c.loss_type)} · {c.product_name}</p>
                  </td>
                  <td className="px-4 py-3">
                    <p>{c.customer_name}</p>
                    <p className="font-mono text-xs text-muted-foreground">{c.policy_number}</p>
                  </td>
                  <td className="px-4 py-3 whitespace-nowrap">{formatDate(c.incident_date)}</td>
                  <td className="px-4 py-3 text-right font-mono text-[13px] tabular-nums whitespace-nowrap">{formatMoney(c.claimed_amount)}</td>
                  <td className="px-4 py-3"><Badge tone={statusTone(c.status)}>{STATUS_LABELS[c.status] ?? c.status}</Badge></td>
                  <td className="px-4 py-3"><Badge tone={PRIORITY_TONE[c.priority]}>{humanize(c.priority)}</Badge></td>
                  <td className="px-4 py-3 text-muted-foreground">{c.adjuster_name ?? "Unassigned"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </Card>

      <div className="flex items-center justify-end gap-2 text-sm text-muted-foreground">
        <Button variant="secondary" size="sm" disabled={filters.page <= 1} onClick={() => update({ page: filters.page - 1 })}>Previous</Button>
        <span>Page {filters.page} of {totalPages}</span>
        <Button variant="secondary" size="sm" disabled={filters.page >= totalPages} onClick={() => update({ page: filters.page + 1 })}>Next</Button>
      </div>
    </div>
  );
}
