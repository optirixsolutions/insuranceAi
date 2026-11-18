"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { hasPermission, useMe } from "@/features/auth/use-me";
import { formatDate, formatMoney, humanize } from "@/features/claims/format";
import { ApiError } from "@/lib/api/client";

import { usePolicy, usePublishVersion, type PolicyVersion } from "./api";

const VERSION_TONE: Record<string, BadgeTone> = { published: "success", draft: "info", superseded: "neutral" };

function VersionCard({ policyId, version, canPublish }: { policyId: string; version: PolicyVersion; canPublish: boolean }) {
  const publish = usePublishVersion(policyId);
  const money = (amount: string) => formatMoney({ amount, currency: version.currency });
  return (
    <Card data-testid={`version-${version.version_no}`}>
      <CardHeader>
        <div>
          <CardTitle>Version {version.version_no}</CardTitle>
          <p className="mt-1 text-sm text-muted-foreground">{formatDate(version.effective_from)} – {formatDate(version.effective_to)} (end exclusive)</p>
        </div>
        <div className="flex items-center gap-2">
          <Badge tone={VERSION_TONE[version.status] ?? "neutral"}>{version.status}</Badge>
          {version.status === "draft" && canPublish ? (
            <Button size="sm" disabled={publish.isPending} onClick={() => publish.mutate({ versionId: version.id, supersede: [] })}>Publish</Button>
          ) : null}
        </div>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {publish.error ? <Alert>{publish.error instanceof ApiError ? publish.error.userMessage : "Publishing failed."}</Alert> : null}
        <dl className="grid gap-3 sm:grid-cols-4">
          <div><dt className="text-xs text-muted-foreground">Sum insured</dt><dd className="font-mono">{money(version.sum_insured)}</dd></div>
          <div><dt className="text-xs text-muted-foreground">Deductible</dt><dd className="font-mono">{money(version.deductible_amount)}</dd></div>
          <div><dt className="text-xs text-muted-foreground">Annual limit</dt><dd className="font-mono">{version.annual_aggregate_limit ? money(version.annual_aggregate_limit) : "—"}</dd></div>
          <div><dt className="text-xs text-muted-foreground">Waiting period</dt><dd>{version.waiting_period_days} days</dd></div>
        </dl>
        <div>
          <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Coverages</h3>
          <ul className="space-y-1">
            {version.coverages.map((c) => (
              <li key={c.coverage_code}>
                <span className="font-medium">{humanize(c.coverage_code)}</span> up to <span className="font-mono">{money(c.limit_amount)}</span>
                <span className="text-muted-foreground"> · perils: {c.perils.map(humanize).join(", ")} · clause {c.clause_ref}</span>
              </li>
            ))}
          </ul>
        </div>
        {version.exclusions.length ? (
          <div>
            <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Exclusions</h3>
            <ul className="space-y-1">{version.exclusions.map((e) => <li key={e.exclusion_code}>{e.description} <span className="text-muted-foreground">· clause {e.clause_ref}</span></li>)}</ul>
          </div>
        ) : null}
        <details>
          <summary className="cursor-pointer text-xs font-semibold uppercase tracking-wide text-muted-foreground">Clauses ({version.clauses.length})</summary>
          <ul className="mt-2 space-y-2">
            {version.clauses.map((c) => (
              <li key={c.clause_ref}><p className="font-medium">{c.clause_ref} {c.title} <span className="text-xs font-normal text-muted-foreground">({c.section})</span></p><p className="whitespace-pre-wrap text-muted-foreground">{c.text}</p></li>
            ))}
          </ul>
        </details>
      </CardContent>
    </Card>
  );
}

export function PolicyDetailView({ id }: { id: string }) {
  const policy = usePolicy(id);
  const canPublish = hasPermission(useMe().data, "policies.manage");
  if (policy.isPending) return <Skeleton className="h-64" />;
  if (policy.error) return <Alert>{policy.error.message}</Alert>;
  const p = policy.data;
  return (
    <div className="space-y-6">
      <Link href="/policies" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"><ArrowLeft className="size-4" aria-hidden />All policies</Link>
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-mono text-2xl font-semibold tracking-tight">{p.policy_number}</h1>
        <Badge tone={p.status === "active" ? "success" : "neutral"}>{p.status}</Badge>
        {p.is_synthetic ? <Badge>synthetic</Badge> : null}
      </div>
      <p className="text-sm text-muted-foreground">{p.customer_name} · {p.product_name} ({p.currency}) · source: {p.source}</p>
      <p className="text-xs text-muted-foreground">Published versions are immutable. Claims use the version in force on the incident date.</p>
      {p.versions.length === 0 ? <Alert>This policy has no versions yet, so no claim date is covered.</Alert> : null}
      {[...p.versions].reverse().map((v) => <VersionCard key={v.id} policyId={p.id} version={v} canPublish={canPublish} />)}
    </div>
  );
}
