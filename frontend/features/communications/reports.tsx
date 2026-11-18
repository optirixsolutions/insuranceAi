"use client";

import { Download, FileJson, FileText } from "lucide-react";
import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { hasPermission, useMe } from "@/features/auth/use-me";
import { formatDateTime } from "@/features/claims/format";
import { ApiError } from "@/lib/api/client";

import { downloadUrl, useClaimReports, useRecentReports, useRequestReport, type Report } from "./api";

const TONE = { queued: "warning", ready: "success", failed: "danger" } as const;
const errorText = (e: unknown) => (e instanceof ApiError ? e.userMessage : "Something went wrong.");
const size = (n: number | null) => (n === null ? "—" : n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`);

function ReportRow({ r, claimRef }: { r: Report; claimRef?: { id: string; reference: string } }) {
  return (
    <tr className="border-t border-border" data-testid={`report-${r.format}-v${r.version_no}`}>
      {claimRef ? (
        <td className="p-3 font-mono"><Link className="underline-offset-2 hover:underline" href={`/claims/${claimRef.id}?tab=reports`}>{claimRef.reference}</Link></td>
      ) : null}
      <td className={claimRef ? "" : "p-3"}>v{r.version_no} · {r.format.toUpperCase()}</td>
      <td><Badge tone={TONE[r.status]}>{r.status === "queued" ? "Generating" : r.status === "ready" ? "Ready" : "Failed"}</Badge></td>
      <td className="tabular-nums">{r.evidence_count ?? "—"}</td>
      <td className="tabular-nums">{size(r.size_bytes)}</td>
      <td className="font-mono text-xs" title={r.sha256 ?? undefined}>{r.sha256 ? `${r.sha256.slice(0, 12)}…` : r.error ?? "—"}</td>
      <td>{formatDateTime(r.generated_at ?? r.created_at)}</td>
      <td>
        {r.status === "ready" ? (
          <a className="inline-flex items-center gap-1 text-primary underline-offset-2 hover:underline" href={downloadUrl(r.id)}>
            <Download className="size-4" aria-hidden />Download
          </a>
        ) : null}
      </td>
    </tr>
  );
}

export function ReportsPanel({ claimId }: { claimId: string }) {
  const reports = useClaimReports(claimId);
  const request = useRequestReport(claimId);
  const me = useMe().data;
  const canGenerate = hasPermission(me, "reports.generate");
  return (
    <Card data-testid="reports-panel">
      <CardHeader>
        <div>
          <CardTitle>Reports</CardTitle>
          <CardDescription>
            Versioned claim reports built from the stored evidence. Every statement cites an evidence entry (E1, E2 …)
            that points at a document page, field, policy clause, fraud indicator or line item. Versions never change.
          </CardDescription>
        </div>
        {canGenerate ? (
          <div className="flex gap-2">
            <Button size="sm" onClick={() => request.mutate("pdf")} disabled={request.isPending}><FileText aria-hidden />Generate PDF</Button>
            <Button size="sm" variant="secondary" onClick={() => request.mutate("json")} disabled={request.isPending}><FileJson aria-hidden />Generate JSON</Button>
          </div>
        ) : null}
      </CardHeader>
      <CardContent className="overflow-x-auto">
        {request.error ? <Alert>{errorText(request.error)}</Alert> : null}
        {reports.isPending ? <Skeleton className="h-24" /> : reports.error ? <Alert>Could not load reports.</Alert> : reports.data.length === 0 ? (
          <p className="text-sm text-muted-foreground">No reports yet.</p>
        ) : (
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-muted-foreground">
              <tr><th className="p-3">Version</th><th>Status</th><th>Evidence cited</th><th>Size</th><th>SHA-256</th><th>Generated</th><th /></tr>
            </thead>
            <tbody>{reports.data.map((r) => <ReportRow key={r.id} r={r} />)}</tbody>
          </table>
        )}
      </CardContent>
    </Card>
  );
}

export function RecentReports() {
  const reports = useRecentReports();
  if (reports.isPending) return <Skeleton className="h-48" />;
  if (reports.error) return <Alert>Could not load reports.</Alert>;
  return (
    <Card>
      <CardContent className="overflow-x-auto p-0">
        {reports.data.length === 0 ? <p className="p-4 text-sm text-muted-foreground">No reports yet. Generate one from a claim&apos;s Reports tab.</p> : (
          <table className="w-full text-left text-sm" data-testid="recent-reports">
            <thead className="text-xs text-muted-foreground">
              <tr><th className="p-3">Claim</th><th>Version</th><th>Status</th><th>Evidence cited</th><th>Size</th><th>SHA-256</th><th>Generated</th><th /></tr>
            </thead>
            <tbody>{reports.data.map((r) => <ReportRow key={r.id} r={r} claimRef={{ id: r.claim_id, reference: r.claim_reference }} />)}</tbody>
          </table>
        )}
      </CardContent>
    </Card>
  );
}
