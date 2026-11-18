"use client";

import Link from "next/link";
import { Activity, CheckCircle2, CircleDashed, CircleX, Loader2, Pause, Play, RotateCcw, Square } from "lucide-react";
import { useState } from "react";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { hasPermission, useMe } from "@/features/auth/use-me";
import { formatDateTime, humanize } from "@/features/claims/format";
import { ApiError } from "@/lib/api/client";

import {
  ACTIVE,
  type Execution,
  MAIN_PATH,
  NODE_LABELS,
  type WorkflowEvent,
  useCancelProcessing,
  useClaimExecutions,
  useExecutionEvents,
  useResume,
  useRetry,
  useStartProcessing,
} from "./api";

export const EXEC_TONE: Record<string, BadgeTone> = {
  queued: "neutral", running: "info", interrupted: "warning", completed: "success", failed: "danger", cancelled: "neutral",
};
const ACTION_LABEL: Record<string, string> = {
  fields_verified: "Fields verified: reprocess",
  continue: "Continue without verification",
  information_supplied: "Information supplied: re-check",
  escalate: "Escalate to review",
  reassess: "Return for reassessment",
  request_information: "Request information",
};
const errorText = (e: unknown) => (e instanceof ApiError ? e.userMessage : "Something went wrong.");
const str = (v: unknown) => (v === null || v === undefined ? "" : String(v));

function nodeStates(events: WorkflowEvent[]): Record<string, { state: "done" | "running" | "failed"; summary: string; ms?: number }> {
  const out: Record<string, { state: "done" | "running" | "failed"; summary: string; ms?: number }> = {};
  for (const e of events) {
    if (!e.node) continue;
    if (e.type === "node_started") out[e.node] = { state: "running", summary: "" };
    if (e.type === "node_completed") out[e.node] = { state: "done", summary: str(e.summary), ms: Number(e.duration_ms ?? 0) };
    if (e.type === "node_failed") out[e.node] = { state: "failed", summary: `${str(e.error)}: ${str(e.detail)}` };
  }
  return out;
}

function Interrupt({ execution, canStart, canReview }: { execution: Execution; canStart: boolean; canReview: boolean }) {
  const resume = useResume();
  const [note, setNote] = useState("");
  const i = execution.interrupt ?? {};
  const kind = str(i.kind);
  const actions = Array.isArray(i.actions) ? (i.actions as string[]) : [];
  const allowed = kind !== "human_review" && canStart; // human review is decided in the review workspace
  const items = (Array.isArray(i.items) ? i.items : Array.isArray(i.requested_items) ? i.requested_items : []) as string[];
  const reasons = Array.isArray(i.mandatory_review_reasons) ? (i.mandatory_review_reasons as string[]) : [];
  const contradictions = Array.isArray(i.contradictions) ? (i.contradictions as string[]) : [];
  return (
    <div className="rounded-md border border-warning/50 bg-warning/5 p-3" data-testid="interrupt-card">
      <div className="flex items-center gap-2 text-sm font-medium"><Pause className="size-4" aria-hidden />
        Waiting for {kind === "human_review" ? "a reviewer" : kind === "document_review" ? "document verification" : "more information"}
      </div>
      {kind === "human_review" ? (
        <p className="mt-1 text-sm">Recommendation <strong>{humanize(str(i.recommendation))}</strong> (rule {str(i.matrix_rule)}). {str(i.explanation)}</p>
      ) : null}
      {[...items, ...reasons, ...contradictions].length ? (
        <ul className="mt-2 list-disc pl-5 text-sm">{[...items, ...reasons, ...contradictions].slice(0, 12).map((x) => <li key={x}>{x}</li>)}</ul>
      ) : null}
      {kind === "human_review" && i.review_id ? (
        <Link href={`/reviews/${execution.claim_id}/${str(i.review_id)}`} className="mt-3 inline-block text-sm font-medium text-primary underline-offset-2 hover:underline">
          {canReview ? "Open the review workspace" : "View the review"}
        </Link>
      ) : null}
      {allowed && actions.length ? (
        <div className="mt-3 space-y-2">
          <input className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm" placeholder="Note (optional)"
            aria-label="Resume note" value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} />
          <div className="flex flex-wrap gap-2">
            {actions.map((a) => (
              <Button key={a} size="sm" variant="secondary" disabled={resume.isPending}
                onClick={() => resume.mutate({ id: execution.id, interruptId: str(i.interrupt_id), action: a, note })}>
                {ACTION_LABEL[a] ?? humanize(a)}
              </Button>
            ))}
          </div>
          {resume.error ? <Alert>{errorText(resume.error)}</Alert> : null}
        </div>
      ) : null}
    </div>
  );
}

function Timeline({ execution }: { execution: Execution }) {
  const { events, live } = useExecutionEvents(execution.id);
  const states = nodeStates(events);
  const extra = Object.keys(states).filter((n) => !MAIN_PATH.includes(n));
  const progress = Math.round(Number(execution.progress) * 100);
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <div className="h-2 flex-1 overflow-hidden rounded bg-muted" role="progressbar" aria-valuenow={progress} aria-valuemin={0} aria-valuemax={100}
          aria-label="Processing progress">
          <div className="h-full bg-primary transition-all" style={{ width: `${progress}%` }} />
        </div>
        <span className="w-10 text-right text-sm tabular-nums" data-testid="progress">{progress}%</span>
        <Badge tone={live ? "success" : "neutral"}>{live ? "Live" : "Offline"}</Badge>
      </div>
      <ol className="space-y-1.5" data-testid="node-timeline">
        {[...MAIN_PATH, ...extra].map((node) => {
          const s = states[node];
          const Icon = !s ? CircleDashed : s.state === "done" ? CheckCircle2 : s.state === "failed" ? CircleX : Loader2;
          return (
            <li key={node} data-testid={`node-${node}`} data-state={s?.state ?? "pending"} className="flex items-start gap-2 text-sm">
              <Icon className={`mt-0.5 size-4 shrink-0 ${s?.state === "running" ? "animate-spin" : ""} ${s?.state === "done" ? "text-success" : s?.state === "failed" ? "text-danger" : "text-muted-foreground"}`} aria-hidden />
              <span className="w-48 shrink-0 font-medium">{NODE_LABELS[node] ?? node}</span>
              <span className="flex-1 text-muted-foreground">{s?.summary}</span>
              {s?.ms !== undefined ? <span className="text-xs tabular-nums text-muted-foreground">{s.ms} ms</span> : null}
            </li>
          );
        })}
      </ol>
      <details className="text-xs">
        <summary className="cursor-pointer text-muted-foreground">Event log ({events.length})</summary>
        <ul className="mt-2 max-h-64 space-y-0.5 overflow-auto font-mono">
          {events.map((e) => <li key={e.seq}>#{e.seq} {e.type} {e.node ?? ""} {e.type === "status_changed" ? `${str(e.from)} → ${str(e.to)}` : ""}</li>)}
        </ul>
      </details>
    </div>
  );
}

export function ProcessingPanel({ claimId, claimStatus }: { claimId: string; claimStatus: string }) {
  const executions = useClaimExecutions(claimId);
  const me = useMe();
  const canStart = hasPermission(me.data, "processing.start");
  const canCancel = hasPermission(me.data, "processing.cancel");
  const canReview = hasPermission(me.data, "reviews.decide");
  const start = useStartProcessing(claimId);
  const cancel = useCancelProcessing(claimId);
  const retry = useRetry();
  if (executions.isPending) return <Skeleton className="h-64" />;
  const latest = executions.data?.[0];
  const active = latest && ACTIVE.includes(latest.status);
  const startable = ["SUBMITTED", "AWAITING_INFORMATION", "FAILED"].includes(claimStatus) && !active;
  const error = start.error ?? cancel.error ?? retry.error;
  return (
    <Card data-testid="processing-panel">
      <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3">
        <CardTitle className="flex items-center gap-2"><Activity className="size-4" aria-hidden />Processing</CardTitle>
        <div className="flex flex-wrap items-center gap-2">
          {latest ? <Badge tone={EXEC_TONE[latest.status] ?? "neutral"} data-testid="execution-status">{humanize(latest.status)}</Badge> : null}
          {canStart && startable ? (
            <Button size="sm" onClick={() => start.mutate(undefined)} disabled={start.isPending}><Play className="size-4" aria-hidden />Start processing</Button>
          ) : null}
          {canStart && latest?.status === "failed" ? (
            <Button size="sm" variant="secondary" onClick={() => retry.mutate(latest.id)} disabled={retry.isPending}><RotateCcw className="size-4" aria-hidden />Retry from checkpoint</Button>
          ) : null}
          {canCancel && active ? (
            <Button size="sm" variant="danger" onClick={() => cancel.mutate(undefined)} disabled={cancel.isPending || Boolean(latest?.cancel_requested_at)}>
              <Square className="size-4" aria-hidden />{latest?.cancel_requested_at ? "Cancelling…" : "Cancel"}
            </Button>
          ) : null}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {error ? <Alert>{errorText(error)}</Alert> : null}
        {!latest ? <p className="text-sm text-muted-foreground">Not processed yet. Processing runs the eight agents as a checkpointed workflow; every recommendation is reviewed by a person.</p> : (
          <>
            <p className="text-xs text-muted-foreground">
              Run started {formatDateTime(latest.created_at)} · {latest.iteration_count} node visits
              {latest.recovery_count ? ` · recovered ${latest.recovery_count}× after a worker stopped` : ""}
              {latest.reassessment_count ? ` · ${latest.reassessment_count} reassessment(s)` : ""}
            </p>
            {latest.status === "failed" ? <Alert>{latest.error_code}: {latest.error_detail}</Alert> : null}
            {latest.status === "interrupted" ? <Interrupt execution={latest} canStart={canStart} canReview={canReview} /> : null}
            <Timeline key={latest.id} execution={latest} />
          </>
        )}
      </CardContent>
    </Card>
  );
}
