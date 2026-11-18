"use client";

import { Bot, Cpu, Play, PlayCircle, ShieldAlert } from "lucide-react";
import { useState } from "react";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { hasPermission, useMe } from "@/features/auth/use-me";
import { formatDateTime, humanize } from "@/features/claims/format";
import { FeedbackButtons } from "@/features/intelligence/components";
import { ApiError } from "@/lib/api/client";

import {
  AGENT_LABELS,
  AGENT_ORDER,
  type AgentFinding,
  type AgentKey,
  type AgentRun,
  latestRuns,
  useAgentRun,
  useAgentRuns,
  useStartAgent,
  useStartPipeline,
} from "./api";

const RUN_TONE: Record<string, BadgeTone> = {
  succeeded: "success", partial: "warning", failed: "danger", timed_out: "danger", running: "info", skipped: "neutral",
};
const SEV_TONE: Record<string, BadgeTone> = { critical: "danger", error: "danger", warning: "warning", info: "info" };
const REC_TONE: Record<string, BadgeTone> = {
  APPROVE_RECOMMENDED: "success", REJECT_RECOMMENDED: "danger", MANUAL_REVIEW_REQUIRED: "warning",
  MORE_INFORMATION_REQUIRED: "warning", PENDING_VERIFICATION: "info",
};
const REC_LABEL: Record<string, string> = {
  APPROVE_RECOMMENDED: "Approve recommended", REJECT_RECOMMENDED: "Reject recommended", MANUAL_REVIEW_REQUIRED: "Manual review required",
  MORE_INFORMATION_REQUIRED: "More information required", PENDING_VERIFICATION: "Pending verification",
};
type Json = Record<string, unknown>;
const arr = (v: unknown): Json[] => (Array.isArray(v) ? (v as Json[]) : []);
const str = (v: unknown): string => (v === null || v === undefined ? "—" : String(v));
const errorText = (e: unknown) => (e instanceof ApiError ? e.userMessage : "Something went wrong.");

function Findings({ findings }: { findings: AgentFinding[] }) {
  if (!findings.length) return <p className="text-sm text-muted-foreground">No findings.</p>;
  return (
    <ul className="space-y-1.5">
      {findings.map((f, i) => (
        <li key={`${f.code}-${i}`} className="flex items-start gap-2 text-sm">
          <Badge tone={SEV_TONE[f.severity] ?? "neutral"}>{f.severity}</Badge>
          <span><span className="font-mono text-xs">{f.code}</span> {f.message}</span>
        </li>
      ))}
    </ul>
  );
}

function RunMeta({ run }: { run: AgentRun }) {
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted-foreground sm:grid-cols-4">
      <div><dt className="font-medium">Attempt</dt><dd>{run.attempt}</dd></div>
      <div><dt className="font-medium">Duration</dt><dd>{run.duration_ms === null ? "—" : `${run.duration_ms} ms`}</dd></div>
      <div><dt className="font-medium">Model</dt><dd className="truncate">{run.used_llm ? run.model_slug : "Deterministic only"}</dd></div>
      <div><dt className="font-medium">Tokens</dt><dd>{run.prompt_tokens + run.completion_tokens}</dd></div>
      <div className="col-span-2 sm:col-span-4"><dt className="font-medium">Rules / input</dt><dd className="truncate font-mono">{run.rule_set_version} · {run.input_hash.slice(0, 16)}</dd></div>
    </dl>
  );
}

function Summary({ agent, output }: { agent: AgentKey; output: Json }) {
  const rows: [string, string][] = [];
  if (agent === "intake") rows.push(["Priority", str(output.priority)], ["Missing documents", arr(output.missing_documents).join(", ") || "None"]);
  if (agent === "document") rows.push(...arr(output.documents).map((d): [string, string] => [str(d.filename), `${humanize(str(d.document_type))} · ${arr(d.fields).length} fields · ${str(d.readability)}`]));
  if (agent === "policy") rows.push(["In force", str(output.policy_active_on_incident_date)], ["Version", str(output.version_selection_reason)],
    ...arr(output.coverages).map((c): [string, string] => [`Coverage ${str(c.coverage_code)}`, humanize(str(c.status))]));
  if (agent === "fraud") rows.push(["Risk", `${str(output.risk_score)} / 100 (${str(output.risk_level)})`], ["Indicators", arr(output.indicators).map((i) => str(i.rule_code)).join(", ") || "None"]);
  if (agent === "assessment") rows.push(["Status", humanize(str(output.status))], ["Eligible", `${str(output.gross_eligible)} ${str(output.currency)}`],
    ["Deductible", str(output.deductible_applied)], ["Payout", `${str(output.eligible_payout)} ${str(output.currency)}`]);
  if (agent === "validation") rows.push(["Worst finding", str(output.max_severity)], ["Contradictions", String(arr(output.contradictions).length)]);
  if (agent === "decision") rows.push(["Recommendation", REC_LABEL[str(output.recommendation)] ?? humanize(str(output.recommendation))], ["Matrix rule", str(output.matrix_rule)], ["Explanation", str(output.explanation)]);
  if (agent === "compliance") rows.push(["Status", humanize(str(output.status))], ["Human review", str(output.human_review_required)]);
  return (
    <dl className="space-y-1 text-sm">
      {rows.map(([k, v]) => (
        <div key={k} className="grid grid-cols-[10rem_1fr] gap-2"><dt className="text-muted-foreground">{k}</dt><dd className="break-words">{v}</dd></div>
      ))}
    </dl>
  );
}

function RunDetail({ runId, agent }: { runId: string; agent: AgentKey }) {
  const detail = useAgentRun(runId);
  if (detail.isPending) return <Skeleton className="h-24" />;
  if (detail.error) return <Alert>{errorText(detail.error)}</Alert>;
  const d = detail.data;
  return (
    <div className="space-y-4" data-testid={`agent-detail-${agent}`}>
      <RunMeta run={d} />
      {d.llm_note ? <p className="flex items-start gap-1.5 text-xs text-muted-foreground"><Cpu className="mt-0.5 size-3.5" aria-hidden />{d.llm_note}</p> : null}
      {d.error_detail ? <Alert>{d.error_code}: {d.error_detail}</Alert> : null}
      {d.output ? <Summary agent={agent} output={d.output} /> : null}
      <div><h4 className="mb-1.5 text-sm font-medium">Findings</h4><Findings findings={d.findings} /></div>
      {d.missing.length ? (
        <div><h4 className="mb-1.5 text-sm font-medium">Missing</h4>
          <ul className="list-disc pl-5 text-sm">{d.missing.map((m) => <li key={m.item}>{m.item} — {m.reason}</li>)}</ul></div>
      ) : null}
      {d.output ? (
        <details className="text-xs"><summary className="cursor-pointer text-muted-foreground">Validated output ({d.output_schema})</summary>
          <pre className="mt-2 max-h-80 overflow-auto rounded-md bg-muted p-3">{JSON.stringify(d.output, null, 2)}</pre></details>
      ) : null}
    </div>
  );
}

export function AgentRunsPanel({ claimId }: { claimId: string }) {
  const runs = useAgentRuns(claimId);
  const me = useMe();
  const canRun = hasPermission(me.data, "processing.start");
  const start = useStartAgent(claimId);
  const pipeline = useStartPipeline(claimId);
  const [open, setOpen] = useState<AgentKey | null>(null);
  const latest = latestRuns(runs.data);
  const busy = Object.values(latest).some((r) => r?.status === "running");
  const decision = latest.decision;
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-4">
          <CardTitle className="flex items-center gap-2"><Bot className="size-4" aria-hidden />Agents</CardTitle>
          {canRun ? (
            <Button size="sm" onClick={() => pipeline.mutate()} disabled={pipeline.isPending || busy}>
              <PlayCircle className="size-4" aria-hidden />Run all agents
            </Button>
          ) : null}
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">
            Each agent applies deterministic rules first; a verified model may only add grounded evidence or escalate to a
            person. Recommendations are advisory and every claim is reviewed by a handler.
          </p>
          {pipeline.error || start.error ? <Alert>{errorText(pipeline.error ?? start.error)}</Alert> : null}
          {decision?.status === "succeeded" || decision?.status === "partial" ? <DecisionBanner runId={decision.id} /> : null}
          {runs.isPending ? <Skeleton className="h-40" /> : (
            <ul className="divide-y divide-border rounded-md border border-border">
              {AGENT_ORDER.map((key) => {
                const run = latest[key];
                return (
                  <li key={key} data-testid={`agent-${key}`} className="p-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <button type="button" className="flex-1 text-left text-sm font-medium" aria-expanded={open === key}
                        onClick={() => setOpen(open === key ? null : key)} disabled={!run}>{AGENT_LABELS[key]}</button>
                      {run ? <Badge tone={RUN_TONE[run.status] ?? "neutral"}>{humanize(run.status)}</Badge> : <Badge>Not run</Badge>}
                      {run?.escalate ? <Badge tone="warning"><ShieldAlert className="size-3" aria-hidden />Escalates</Badge> : null}
                      {run?.used_llm ? <Badge tone="info">Model-assisted</Badge> : null}
                      {run?.finished_at ? <span className="text-xs text-muted-foreground">{formatDateTime(run.finished_at)}</span> : null}
                      {run && run.status !== "running" ? <FeedbackButtons runId={run.id} /> : null}
                      {canRun ? (
                        <Button size="sm" variant="secondary" aria-label={`Run ${AGENT_LABELS[key]}`}
                          disabled={start.isPending || run?.status === "running"} onClick={() => start.mutate(key)}>
                          <Play className="size-3.5" aria-hidden />Run
                        </Button>
                      ) : null}
                    </div>
                    {open === key && run ? <div className="mt-3"><RunDetail runId={run.id} agent={key} /></div> : null}
                  </li>
                );
              })}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function DecisionBanner({ runId }: { runId: string }) {
  const d = useAgentRun(runId).data?.output;
  if (!d) return null;
  const rec = str(d.recommendation);
  return (
    <div className="rounded-md border border-border p-3" data-testid="decision-banner">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">Recommendation</span>
        <Badge tone={REC_TONE[rec] ?? "neutral"}>{REC_LABEL[rec] ?? humanize(rec)}</Badge>
        {d.llm_escalated ? <Badge tone="warning">Escalated by model review</Badge> : null}
        <span className="text-xs text-muted-foreground">rule {str(d.matrix_rule)} · {d.explanation_source === "llm" ? "model-written explanation" : "templated explanation"}</span>
      </div>
      <p className="mt-2 text-sm">{str(d.explanation)}</p>
    </div>
  );
}

function useOutput(claimId: string, agent: AgentKey) {
  const runs = useAgentRuns(claimId);
  const run = latestRuns(runs.data)[agent];
  const detail = useAgentRun(run && run.status !== "running" ? run.id : null);
  return { runs, run, detail };
}

export function CoveragePanel({ claimId }: { claimId: string }) {
  const { runs, run, detail } = useOutput(claimId, "policy");
  if (runs.isPending || (run && detail.isPending)) return <Skeleton className="h-40" />;
  const o = detail.data?.output;
  if (!o) return <Alert>Run the policy verification agent to see coverage.</Alert>;
  return (
    <Card data-testid="coverage-panel">
      <CardHeader><CardTitle>Policy coverage</CardTitle></CardHeader>
      <CardContent className="space-y-4 text-sm">
        <p>{str(o.version_selection_reason)}. In force on the incident date: <strong>{str(o.policy_active_on_incident_date)}</strong>;
          waiting period satisfied: <strong>{str(o.waiting_period_satisfied)}</strong>; holder match: <strong>{str(o.holder_match)}</strong>.</p>
        <table className="w-full text-left">
          <thead className="text-xs text-muted-foreground"><tr><th className="py-1">Coverage</th><th>Status</th><th>Reasons</th><th>Clause</th></tr></thead>
          <tbody>{arr(o.coverages).map((c) => (
            <tr key={str(c.coverage_code)} className="border-t border-border"><td className="py-1.5 font-mono">{str(c.coverage_code)}</td>
              <td><Badge tone={c.status === "covered" ? "success" : c.status === "not_covered" ? "danger" : "warning"}>{humanize(str(c.status))}</Badge></td>
              <td>{arr(c.reasons).join(", ")}</td><td className="font-mono text-xs">{arr(c.clause_ids).map(String).map((x) => x.slice(0, 8)).join(", ")}</td></tr>
          ))}</tbody>
        </table>
        {(["exclusions_triggered", "exclusions_need_interpretation", "exclusions_not_triggered"] as const).map((group) => arr(o[group]).length ? (
          <div key={group}><h4 className="mb-1 font-medium">{humanize(group.replace("exclusions_", "exclusions: "))}</h4>
            <ul className="list-disc pl-5">{arr(o[group]).map((e) => <li key={str(e.exclusion_code)}><span className="font-mono">{str(e.exclusion_code)}</span> — {str(e.reason)} <span className="text-xs text-muted-foreground">({str(e.source)})</span></li>)}</ul></div>
        ) : null)}
        <p className="text-xs text-muted-foreground">Deductible {str((o.deductible as Json | null)?.amount)} · limit remaining {str((o.limit_remaining as Json | null)?.amount)} {str((o.limit_remaining as Json | null)?.currency)}</p>
      </CardContent>
    </Card>
  );
}

export function FraudPanel({ claimId }: { claimId: string }) {
  const { runs, run, detail } = useOutput(claimId, "fraud");
  if (runs.isPending || (run && detail.isPending)) return <Skeleton className="h-40" />;
  const o = detail.data?.output;
  if (!o) return <Alert>Run the fraud investigation agent to see indicators.</Alert>;
  const level = str(o.risk_level);
  return (
    <Card data-testid="fraud-panel">
      <CardHeader><CardTitle>Fraud investigation</CardTitle></CardHeader>
      <CardContent className="space-y-4 text-sm">
        <div className="flex items-center gap-3">
          <span className="text-3xl font-semibold tabular-nums">{str(o.risk_score)}</span>
          <Badge tone={level === "low" ? "success" : level === "medium" ? "warning" : "danger"}>{humanize(level)} risk</Badge>
          <span className="text-xs text-muted-foreground">framework {str(o.framework_version)} · no score leads to automatic rejection</span>
        </div>
        {arr(o.indicators).length ? (
          <ul className="space-y-2">{arr(o.indicators).map((i, n) => (
            <li key={`${str(i.rule_code)}-${n}`} className="rounded-md border border-border p-2">
              <div className="flex flex-wrap items-center gap-2"><span className="font-mono text-xs">{str(i.rule_code)}</span>
                <Badge tone={SEV_TONE[str(i.severity)] ?? "neutral"}>{str(i.severity)}</Badge>
                <span className="text-xs text-muted-foreground">+{str(i.contribution)} pts · weight {str(i.weight)} · {str(i.source)}</span></div>
              <p className="mt-1">{str(i.description)}</p>
              <ul className="mt-1 list-disc pl-5 text-xs text-muted-foreground">{arr(i.evidence).slice(0, 3).map((e, k) => <li key={k}>{str(e.excerpt ?? e.kind)}</li>)}</ul>
            </li>
          ))}</ul>
        ) : <p className="text-muted-foreground">No indicators found.</p>}
        {arr(o.checks_not_run).length ? <p className="text-xs text-muted-foreground">Not run: {arr(o.checks_not_run).map(String).join("; ")}</p> : null}
      </CardContent>
    </Card>
  );
}
