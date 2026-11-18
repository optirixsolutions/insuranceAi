"use client";

import Link from "next/link";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateTime, formatMoney, humanize, STATUS_LABELS, statusTone } from "@/features/claims/format";

import {
  formatMetric,
  useAgentAnalytics,
  useAssessments,
  useExtractionAccuracy,
  useFraudAnalytics,
  useProcessingTimes,
  type Period,
} from "./api";
import { BarList } from "./charts";
import { PeriodControls } from "./dashboard";
import { FeedbackSummary } from "@/features/intelligence/components";

const ms = (v: number | null) => formatMetric(v, "ms");
const pct = (v: number | null) => formatMetric(v, "ratio");
const RISK_TONE = { low: "success", medium: "warning", high: "danger", critical: "danger" } as const;

function Section({ title, definition, children, testId }: { title: string; definition?: string; children: React.ReactNode; testId: string }) {
  return (
    <Card data-testid={testId}>
      <CardHeader>
        <div>
          <CardTitle>{title}</CardTitle>
          {definition ? <CardDescription>{definition}</CardDescription> : null}
        </div>
      </CardHeader>
      <CardContent className="overflow-x-auto">{children}</CardContent>
    </Card>
  );
}

function Table({ head, rows, empty }: { head: string[]; rows: React.ReactNode[][]; empty: string }) {
  if (!rows.length) return <p className="text-sm text-muted-foreground">{empty}</p>;
  return (
    <table className="w-full text-left text-sm">
      <thead className="text-xs text-muted-foreground"><tr>{head.map((h) => <th key={h} className="pb-2 pr-4 font-medium">{h}</th>)}</tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i} className="border-t border-border">{r.map((c, j) => <td key={j} className="py-1.5 pr-4 tabular-nums">{c}</td>)}</tr>
        ))}
      </tbody>
    </table>
  );
}

function ProcessingTimes({ period }: { period: Period }) {
  const q = useProcessingTimes(period);
  return (
    <Section title="Processing times by product" definition={q.data?.definition} testId="analytics-times">
      {q.isPending ? <Skeleton className="h-24" /> : q.error ? <Alert>Could not load processing times.</Alert> : (
        <Table empty="No completed processing runs in this period." head={["Product", "Runs", "Processing p50", "p90", "p95", "Wall p50", "p90", "p95", "Wall average"]}
          rows={q.data.products.map((p) => [p.product_name, p.runs, ms(p.machine_p50_ms), ms(p.machine_p90_ms), ms(p.machine_p95_ms), ms(p.wall_p50_ms), ms(p.wall_p90_ms), ms(p.wall_p95_ms), ms(p.wall_avg_ms)])} />
      )}
    </Section>
  );
}

function Agents({ period }: { period: Period }) {
  const q = useAgentAnalytics(period);
  return (
    <Section title="Agent monitoring" definition={q.data?.definition} testId="analytics-agents">
      {q.isPending ? <Skeleton className="h-24" /> : q.error ? <Alert>Could not load agent metrics.</Alert> : (
        <div className="space-y-4">
          <Table empty="No agent runs in this period." head={["Agent", "Runs", "Failed", "Failure rate", "Retries", "p50", "p95", "Used a model", "Escalated", "Tokens", "Cost"]}
            rows={q.data.agents.map((a) => [humanize(a.agent_key), a.runs, a.failed, pct(a.failure_rate), a.retries, ms(a.p50_ms), ms(a.p95_ms), pct(a.llm_rate), pct(a.escalation_rate), a.tokens, formatMetric(a.cost_usd, "usd")])} />
          <p className="text-sm" data-testid="cost-per-claim">
            Model cost per claim: <strong>{formatMetric(q.data.cost_per_claim_usd, "usd")}</strong> across {q.data.claims} claim(s).
          </p>
          <h3 className="text-sm font-medium">Models</h3>
          <Table empty="No model calls in this period." head={["Model", "Calls", "Errors", "Error rate", "p95 latency", "Cost"]}
            rows={q.data.models.map((m) => [<span key="s" className="font-mono text-xs">{m.model_slug}</span>, m.calls, m.errors, pct(m.error_rate), ms(m.p95_latency_ms), formatMetric(m.cost_usd, "usd")])} />
        </div>
      )}
    </Section>
  );
}

function Fraud({ period }: { period: Period }) {
  const q = useFraudAnalytics(period);
  return (
    <Section title="Fraud" definition={q.data?.definition} testId="analytics-fraud">
      {q.isPending ? <Skeleton className="h-24" /> : q.error ? <Alert>Could not load fraud metrics.</Alert> : (
        <div className="grid gap-6 md:grid-cols-3">
          <div>
            <h3 className="mb-2 text-sm font-medium">Risk level (latest per claim)</h3>
            <BarList label="Claims by fraud risk level" bars={["low", "medium", "high", "critical"].filter((l) => q.data.by_level[l]).map((l) => ({
              label: humanize(l), value: q.data.by_level[l] ?? 0, tone: RISK_TONE[l as keyof typeof RISK_TONE] === "success" ? "success" : RISK_TONE[l as keyof typeof RISK_TONE],
            }))} />
          </div>
          <div>
            <h3 className="mb-2 text-sm font-medium">Most frequent indicators</h3>
            <BarList label="Fraud indicators" bars={q.data.top_indicators.map((i) => ({ label: i.code, value: i.count, tone: "warning" }))} />
          </div>
          <dl className="space-y-2 text-sm">
            <div><dt className="text-xs text-muted-foreground">Alerts (high or critical)</dt><dd className="text-xl font-semibold tabular-nums">{q.data.alerts}</dd></div>
            <div>
              <dt className="text-xs text-muted-foreground">False-positive rate (labelled only)</dt>
              <dd className="text-xl font-semibold tabular-nums" data-testid="false-positive-rate">{pct(q.data.false_positive_rate)}</dd>
              <dd className="text-xs text-muted-foreground">{q.data.false_positives} of {q.data.labelled_flagged} flagged claims with a final human decision were approved.</dd>
            </div>
          </dl>
        </div>
      )}
    </Section>
  );
}

function Extraction({ period }: { period: Period }) {
  const q = useExtractionAccuracy(period);
  return (
    <Section title="Extraction accuracy" definition={q.data?.definition} testId="analytics-extraction">
      {q.isPending ? <Skeleton className="h-24" /> : q.error ? <Alert>Could not load extraction accuracy.</Alert> : (
        <Table empty="No machine-extracted fields in this period." head={["Document type", "Fields", "Corrected by a person", "Accuracy", "Ungrounded"]}
          rows={q.data.document_types.map((d) => [humanize(d.document_type), d.fields, d.corrected, pct(d.accuracy), d.ungrounded])} />
      )}
    </Section>
  );
}

export function AnalyticsView() {
  const [period, setPeriod] = useState<Period>({ days: 30, includeSynthetic: true });
  return (
    <div className="space-y-6">
      <PeriodControls period={period} onChange={setPeriod} />
      <ProcessingTimes period={period} />
      <Agents period={period} />
      <Fraud period={period} />
      <Extraction period={period} />
      <FeedbackSummary />
    </div>
  );
}

export function AssessmentsList() {
  const [synthetic, setSynthetic] = useState(true);
  const q = useAssessments(synthetic);
  return (
    <div className="space-y-4">
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={synthetic} onChange={(e) => setSynthetic(e.target.checked)} /> Include synthetic data
      </label>
      {q.isPending ? <Skeleton className="h-48" /> : q.error ? <Alert>Could not load assessments.</Alert> : (
        <Card>
          <CardContent className="overflow-x-auto p-0">
            {q.data.length === 0 ? <p className="p-4 text-sm text-muted-foreground">No assessments yet. They appear once claims are processed.</p> : (
              <table className="w-full text-left text-sm" data-testid="assessments">
                <thead className="text-xs text-muted-foreground">
                  <tr><th className="p-3">Claim</th><th>Claim status</th><th>Assessment</th><th>Eligible payout</th><th>Rule set</th><th>Assessed</th></tr>
                </thead>
                <tbody>
                  {q.data.map((a) => (
                    <tr key={a.claim_id} className="border-t border-border">
                      <td className="p-3 font-mono"><Link className="underline-offset-2 hover:underline" href={`/claims/${a.claim_id}?tab=agents`}>{a.claim_reference}</Link></td>
                      <td><Badge tone={statusTone(a.claim_status)}>{STATUS_LABELS[a.claim_status] ?? a.claim_status}</Badge></td>
                      <td>{a.assessment_status ? humanize(a.assessment_status) : "—"}</td>
                      <td className="tabular-nums">{a.eligible_payout && a.currency ? formatMoney({ amount: a.eligible_payout, currency: a.currency }) : "—"}</td>
                      <td className="font-mono text-xs">{a.rule_set_version}</td>
                      <td>{a.assessed_at ? formatDateTime(a.assessed_at) : "—"}</td>
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
