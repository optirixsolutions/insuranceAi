"use client";

import Link from "next/link";
import { useState } from "react";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Alert } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateTime, formatMoney, humanize } from "@/features/claims/format";

import { useReviewQueue } from "./api";

export const REVIEW_TONE: Record<string, BadgeTone> = {
  pending: "warning",
  in_progress: "info",
  awaiting_second_approval: "warning",
  escalated: "danger",
  completed: "success",
  cancelled: "neutral",
};
export const RISK_TONE: Record<string, BadgeTone> = { low: "success", medium: "warning", high: "danger", critical: "danger" };
const STATUSES = ["", "pending", "in_progress", "awaiting_second_approval", "escalated", "completed", "cancelled"];

/** Amounts arrive with 4 decimals (NUMERIC(19,4)); show 2. */
export function money(amount: string, currency: string) {
  return formatMoney({ amount: amount.replace(/(\.\d{2})\d*$/, "$1"), currency });
}

export function reviewHref(claimId: string, reviewId: string) {
  return `/reviews/${claimId}/${reviewId}`;
}

function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" aria-pressed={on} onClick={onClick}
      className={`rounded-full border px-3 py-1 text-sm ${on ? "border-primary bg-primary/10" : "border-border"}`}>
      {children}
    </button>
  );
}

/** The review queue: highest priority first. ``risk`` pins a fraud-risk filter (fraud investigation view). */
export function ReviewQueue({ risk: fixedRisk }: { risk?: string }) {
  const [status, setStatus] = useState("");
  const [mine, setMine] = useState(false);
  const [overdue, setOverdue] = useState(false);
  const [risk, setRisk] = useState(fixedRisk ?? "");
  const queue = useReviewQueue({ status: status || undefined, mine, overdue, risk: risk || undefined });
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filter reviews">
        {STATUSES.map((s) => (
          <Chip key={s || "open"} on={status === s} onClick={() => setStatus(s)}>{s ? humanize(s) : "Open"}</Chip>
        ))}
        <span className="mx-1 h-5 w-px bg-border" aria-hidden />
        <Chip on={mine} onClick={() => setMine(!mine)}>Assigned to me</Chip>
        <Chip on={overdue} onClick={() => setOverdue(!overdue)}>Overdue</Chip>
        {fixedRisk === undefined ? (
          <select aria-label="Fraud risk" className="rounded-md border border-border bg-background px-2 py-1 text-sm"
            value={risk} onChange={(e) => setRisk(e.target.value)}>
            <option value="">Any risk</option>
            {["low", "medium", "high", "critical"].map((r) => <option key={r} value={r}>{humanize(r)}</option>)}
          </select>
        ) : (
          <select aria-label="Fraud risk" className="rounded-md border border-border bg-background px-2 py-1 text-sm"
            value={risk} onChange={(e) => setRisk(e.target.value)}>
            {["high", "critical", "medium"].map((r) => <option key={r} value={r}>{humanize(r)} risk</option>)}
          </select>
        )}
      </div>
      {queue.isPending ? <Skeleton className="h-48" /> : queue.error ? (
        <Alert>Could not load the review queue. You need the reviews.decide or fraud.investigate permission.</Alert>
      ) : (
        <Card>
          <CardContent className="overflow-x-auto p-0">
            {queue.data.length === 0 ? <p className="p-4 text-sm text-muted-foreground">No reviews match these filters.</p> : (
              <table className="w-full text-left text-sm" data-testid="review-queue">
                <thead className="text-xs text-muted-foreground">
                  <tr>
                    <th className="p-3">Claim</th><th>Status</th><th>Recommendation</th><th>Payout</th><th>Fraud risk</th>
                    <th>Priority</th><th>Due</th><th>Assigned to</th>
                  </tr>
                </thead>
                <tbody>
                  {queue.data.map((r) => (
                    <tr key={r.id} className="border-t border-border">
                      <td className="p-3 font-mono">
                        <Link className="underline-offset-2 hover:underline" href={reviewHref(r.claim_id, r.id)}>{r.claim_reference}</Link>
                      </td>
                      <td>
                        <Badge tone={REVIEW_TONE[r.status] ?? "neutral"}>{humanize(r.status)}</Badge>
                        {r.four_eyes_required ? <span className="ml-1 text-xs text-muted-foreground" title="Needs a second approver">4-eyes</span> : null}
                      </td>
                      <td>{r.recommendation ? humanize(r.recommendation) : "—"}</td>
                      <td className="tabular-nums">{r.payout ? money(r.payout, r.currency) : "—"}</td>
                      <td>{r.fraud_level ? <Badge tone={RISK_TONE[r.fraud_level] ?? "neutral"}>{humanize(r.fraud_level)}</Badge> : "—"}</td>
                      <td className="tabular-nums">{r.priority_score}</td>
                      <td className={r.overdue ? "font-medium text-danger" : undefined}>
                        {r.due_at ? formatDateTime(r.due_at) : "—"}{r.overdue ? " (overdue)" : ""}
                      </td>
                      <td>{r.assigned_to_name ?? <span className="text-muted-foreground">Unassigned</span>}</td>
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
