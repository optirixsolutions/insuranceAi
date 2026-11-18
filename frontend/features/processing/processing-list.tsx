"use client";

import Link from "next/link";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Alert } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateTime, humanize } from "@/features/claims/format";

import { NODE_LABELS, useOrgExecutions } from "./api";
import { EXEC_TONE } from "./processing-panel";

const FILTERS = ["", "running", "interrupted", "failed", "completed", "cancelled"];

export function ProcessingList() {
  const [status, setStatus] = useState("");
  const runs = useOrgExecutions(status || undefined);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2" role="group" aria-label="Filter by status">
        {FILTERS.map((f) => (
          <button key={f || "all"} type="button" aria-pressed={status === f} onClick={() => setStatus(f)}
            className={`rounded-full border px-3 py-1 text-sm ${status === f ? "border-primary bg-primary/10" : "border-border"}`}>
            {f ? humanize(f) : "All"}
          </button>
        ))}
      </div>
      {runs.isPending ? <Skeleton className="h-48" /> : runs.error ? <Alert>Could not load processing runs.</Alert> : (
        <Card>
          <CardContent className="p-0">
            {runs.data.length === 0 ? <p className="p-4 text-sm text-muted-foreground">No processing runs.</p> : (
              <table className="w-full text-left text-sm" data-testid="processing-runs">
                <thead className="text-xs text-muted-foreground">
                  <tr><th className="p-3">Claim</th><th>Status</th><th>Step</th><th>Progress</th><th>Started</th><th>Recoveries</th></tr>
                </thead>
                <tbody>
                  {runs.data.map((r) => (
                    <tr key={r.id} className="border-t border-border">
                      <td className="p-3 font-mono"><Link className="underline-offset-2 hover:underline" href={`/claims/${r.claim_id}?tab=analysis`}>{r.claim_reference}</Link></td>
                      <td><Badge tone={EXEC_TONE[r.status] ?? "neutral"}>{humanize(r.status)}</Badge></td>
                      <td>{r.current_node ? NODE_LABELS[r.current_node] ?? r.current_node : "—"}</td>
                      <td className="tabular-nums">{Math.round(Number(r.progress) * 100)}%</td>
                      <td>{formatDateTime(r.created_at)}</td>
                      <td className="tabular-nums">{r.recovery_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
