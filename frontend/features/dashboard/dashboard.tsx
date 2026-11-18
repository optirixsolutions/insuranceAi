"use client";

import Link from "next/link";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateTime, humanize } from "@/features/claims/format";
import { NODE_LABELS } from "@/features/processing/api";

import { formatMetric, useActivity, useClaimsAnalytics, useSummary, type Metric, type Period } from "./api";
import { BarList, Columns } from "./charts";

const PERIODS = [7, 30, 90];
const DECISION_TONE = { approved: "success", rejected: "danger" } as const;

export function PeriodControls({ period, onChange }: { period: Period; onChange: (p: Period) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Period">
      {PERIODS.map((d) => (
        <button key={d} type="button" aria-pressed={period.days === d} onClick={() => onChange({ ...period, days: d })}
          className={`rounded-full border px-3 py-1 text-sm ${period.days === d ? "border-primary bg-primary/10" : "border-border"}`}>
          Last {d} days
        </button>
      ))}
      <label className="ml-2 flex items-center gap-2 text-sm">
        <input type="checkbox" checked={period.includeSynthetic} onChange={(e) => onChange({ ...period, includeSynthetic: e.target.checked })} />
        Include synthetic data
      </label>
    </div>
  );
}

function Kpi({ m }: { m: Metric }) {
  return (
    <div className="rounded-lg border border-border p-3" data-testid={`kpi-${m.key}`}>
      <div className="text-xs text-muted-foreground">{m.label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums" data-testid={`kpi-${m.key}-value`}>{formatMetric(m.value, m.unit)}</div>
      <details className="mt-1 text-xs text-muted-foreground">
        <summary className="cursor-pointer select-none">How is this counted?</summary>
        <p className="mt-1">{m.definition}</p>
      </details>
    </div>
  );
}

function Kpis({ period }: { period: Period }) {
  const summary = useSummary(period);
  if (summary.isPending) return <Skeleton className="h-40" />;
  if (summary.error) return <Alert>Could not load the dashboard metrics.</Alert>;
  return (
    <section aria-label="Key figures" className="space-y-2">
      <p className="text-xs text-muted-foreground" data-testid="dashboard-scope">
        Showing {summary.data.scope} · updated {formatDateTime(summary.data.generated_at)}
      </p>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-7">
        {summary.data.metrics.map((m) => <Kpi key={m.key} m={m} />)}
      </div>
    </section>
  );
}

function Volume({ period }: { period: Period }) {
  const bucket = period.days > 30 ? "week" : "day";
  const claims = useClaimsAnalytics(period, bucket);
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Claims and decisions</CardTitle>
          <CardDescription>{claims.data?.definition}</CardDescription>
        </div>
      </CardHeader>
      <CardContent>
        {claims.isPending ? <Skeleton className="h-40" /> : claims.error ? <Alert>Could not load claim volumes.</Alert> : (
          <div className="grid gap-6 lg:grid-cols-3">
            <div>
              <h3 className="mb-2 text-sm font-medium">Claims reported per {bucket}</h3>
              <Columns points={claims.data.volume} label={`Claims reported per ${bucket}`} />
            </div>
            <div>
              <h3 className="mb-2 text-sm font-medium">By category</h3>
              <BarList label="Claims by category" bars={claims.data.by_category.map((c) => ({ label: humanize(c.category), value: c.count }))} />
            </div>
            <div>
              <h3 className="mb-2 text-sm font-medium">Decisions</h3>
              <BarList label="Decisions by outcome" bars={claims.data.decisions.map((d) => ({
                label: `${humanize(d.decision)} (${d.mode === "stp" ? "straight-through" : "human"})`,
                value: d.count,
                tone: DECISION_TONE[d.decision as keyof typeof DECISION_TONE] ?? "muted",
              }))} />
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ActivityList({ title, empty, children, testId }: { title: string; empty: boolean; children: React.ReactNode; testId: string }) {
  return (
    <div data-testid={testId}>
      <h3 className="mb-1 text-sm font-medium">{title}</h3>
      {empty ? <p className="text-sm text-muted-foreground">None.</p> : <ul className="space-y-1 text-sm">{children}</ul>}
    </div>
  );
}

function ActivityPanel({ period }: { period: Period }) {
  const activity = useActivity(period);
  if (activity.isPending) return <Skeleton className="h-40" />;
  if (activity.error) return <Alert>Could not load live activity.</Alert>;
  const a = activity.data;
  const claim = (id: string, ref: string, tab = "analysis") => <Link className="font-mono underline-offset-2 hover:underline" href={`/claims/${id}?tab=${tab}`}>{ref}</Link>;
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Live activity and exceptions</CardTitle>
          <CardDescription>Running and waiting processing runs, recent failures and review work that needs attention (now).</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="grid gap-6 md:grid-cols-2 xl:grid-cols-5">
        <ActivityList title={`Running (${a.running.length})`} empty={!a.running.length} testId="activity-running">
          {a.running.map((r) => <li key={r.execution_id}>{claim(r.claim_id, r.claim_reference)} · {r.current_node ? NODE_LABELS[r.current_node] ?? r.current_node : humanize(r.status)} · {Math.round(r.progress * 100)}%</li>)}
        </ActivityList>
        <ActivityList title="Waiting for people" empty={!Object.keys(a.waiting).length} testId="activity-waiting">
          {Object.entries(a.waiting).map(([kind, n]) => <li key={kind}>{humanize(kind)}: <span className="tabular-nums">{n}</span></li>)}
        </ActivityList>
        <ActivityList title="Recent failures" empty={!a.recent_failures.length} testId="activity-failures">
          {a.recent_failures.map((f) => <li key={f.execution_id}>{claim(f.claim_id, f.claim_reference)} <Badge tone="danger">{f.error_code ?? "failed"}</Badge></li>)}
        </ActivityList>
        <ActivityList title="Awaiting second approver" empty={!a.pending_approvals.length} testId="activity-second">
          {a.pending_approvals.map((r) => <li key={r.review_id}><Link className="font-mono underline-offset-2 hover:underline" href={`/reviews/${r.claim_id}/${r.review_id}`}>{r.claim_reference}</Link></li>)}
        </ActivityList>
        <ActivityList title="Overdue reviews" empty={!a.overdue_reviews.length} testId="activity-overdue">
          {a.overdue_reviews.map((r) => (
            <li key={r.review_id}>
              <Link className="font-mono underline-offset-2 hover:underline" href={`/reviews/${r.claim_id}/${r.review_id}`}>{r.claim_reference}</Link>
              {r.due_at ? <span className="text-danger"> · due {formatDateTime(r.due_at)}</span> : null}
            </li>
          ))}
        </ActivityList>
      </CardContent>
    </Card>
  );
}

export function Dashboard() {
  const [period, setPeriod] = useState<Period>({ days: 30, includeSynthetic: true });
  return (
    <div className="space-y-6" data-testid="dashboard">
      <PeriodControls period={period} onChange={setPeriod} />
      <Kpis period={period} />
      <ActivityPanel period={period} />
      <Volume period={period} />
    </div>
  );
}
