"use client";

import { ThumbsDown, ThumbsUp } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Select } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate, formatDateTime, formatMoney, humanize } from "@/features/claims/format";
import { formatMetric } from "@/features/dashboard/api";
import { useAgents, useModels, verifiedCaps } from "@/features/ai/api";
import { ApiError } from "@/lib/api/client";

import { useAnomalies, useEvalActions, useEvals, useFeedbackSummary, useGiveFeedback, usePrompts, useSimilar } from "./api";

const errorText = (e: unknown) => (e instanceof ApiError ? e.userMessage : "Something went wrong.");
const pct = (v: number | null) => formatMetric(v, "ratio");
const EVAL_TONE = { passed: "success", failed: "danger", error: "warning", queued: "neutral", running: "info" } as const;
const LLM_AGENTS = ["document", "policy", "fraud", "assessment", "decision"];

export function SimilarClaims({ claimId }: { claimId: string }) {
  const q = useSimilar(claimId);
  return (
    <Card data-testid="similar-claims">
      <CardHeader>
        <div>
          <CardTitle>Similar claims</CardTitle>
          <CardDescription>{q.data?.definition}</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        {q.isPending ? <Skeleton className="h-20" /> : q.error ? <Alert>Could not load similar claims.</Alert> : q.data.items.length === 0 ? (
          <p className="text-sm text-muted-foreground">No similar claims among those you can see.</p>
        ) : (
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-muted-foreground"><tr><th className="pb-2">Claim</th><th>Similarity</th><th>Customer</th><th>Incident</th><th>Claimed</th><th>Shared wording</th></tr></thead>
            <tbody>
              {q.data.items.map((s) => (
                <tr key={s.claim_id} className="border-t border-border">
                  <td className="py-1.5 font-mono"><Link className="underline-offset-2 hover:underline" href={`/claims/${s.claim_id}`}>{s.claim_reference}</Link></td>
                  <td><Badge tone={s.similarity >= 0.9 ? "warning" : "neutral"}>{(s.similarity * 100).toFixed(0)}%</Badge></td>
                  <td>{s.same_customer ? "Same customer" : <span className="font-medium">Different customer</span>}</td>
                  <td>{formatDate(s.incident_date)}</td>
                  <td className="tabular-nums">{formatMoney({ amount: s.claimed_amount.replace(/(\.\d{2})\d*$/, "$1"), currency: s.currency })}</td>
                  <td className="text-xs text-muted-foreground">{s.shared_terms.join(", ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </CardContent>
    </Card>
  );
}

export function AnomaliesPanel() {
  const q = useAnomalies();
  return (
    <Card data-testid="anomalies">
      <CardHeader>
        <div>
          <CardTitle>Portfolio anomalies (last 90 days)</CardTitle>
          <CardDescription>{q.data?.definition}</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {q.isPending ? <Skeleton className="h-20" /> : q.error ? <Alert>Could not load anomalies.</Alert> : (
          <>
            {q.data.anomalies.length === 0 ? <p className="text-sm text-muted-foreground">No anomalies found.</p> : (
              <ul className="space-y-2">
                {q.data.anomalies.map((a, i) => (
                  <li key={i} className="rounded-md border border-border p-2 text-sm" data-testid="anomaly">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone={a.severity === "high" ? "danger" : "warning"}>{humanize(a.type)}</Badge>
                      <span>{a.explanation}</span>
                    </div>
                    <div className="mt-1 flex flex-wrap gap-2 text-xs">
                      {a.claims.map((c) => <Link key={c.claim_id} className="font-mono underline-offset-2 hover:underline" href={`/claims/${c.claim_id}?tab=fraud`}>{c.claim_reference}</Link>)}
                    </div>
                  </li>
                ))}
              </ul>
            )}
            {q.data.notes.map((n) => <p key={n} className="text-xs text-muted-foreground">{n}</p>)}
          </>
        )}
      </CardContent>
    </Card>
  );
}

export function FeedbackButtons({ runId }: { runId: string }) {
  const give = useGiveFeedback(runId);
  const [given, setGiven] = useState<string | null>(null);
  const send = (label: "helpful" | "not_helpful") => give.mutate(label, { onSuccess: () => setGiven(label) });
  return (
    <span className="inline-flex items-center gap-1" data-testid={`feedback-${runId}`}>
      <Button size="icon" variant="ghost" aria-label="Helpful" aria-pressed={given === "helpful"} className="size-7" onClick={() => send("helpful")}><ThumbsUp aria-hidden /></Button>
      <Button size="icon" variant="ghost" aria-label="Not helpful" aria-pressed={given === "not_helpful"} className="size-7" onClick={() => send("not_helpful")}><ThumbsDown aria-hidden /></Button>
      {given ? <span className="text-xs text-muted-foreground">Thanks</span> : null}
    </span>
  );
}

export function FeedbackSummary() {
  const q = useFeedbackSummary();
  return (
    <Card data-testid="analytics-feedback">
      <CardHeader>
        <div>
          <CardTitle>Agent feedback</CardTitle>
          <CardDescription>{q.data?.definition}</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        {q.isPending ? <Skeleton className="h-16" /> : q.error ? <Alert>Could not load feedback.</Alert> : q.data.agents.length === 0 ? (
          <p className="text-sm text-muted-foreground">No feedback yet. It is recorded when people decide claims and when they rate agent results.</p>
        ) : (
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-muted-foreground"><tr><th className="pb-2">Agent</th><th>Agreement with people</th><th>Fraud precision</th><th>Helpful</th><th>Labels</th></tr></thead>
            <tbody>
              {q.data.agents.map((a) => (
                <tr key={a.agent_key} className="border-t border-border">
                  <td className="py-1.5">{humanize(a.agent_key)}</td>
                  <td className="tabular-nums">{pct(a.agreement_rate)}</td>
                  <td className="tabular-nums">{pct(a.precision)}</td>
                  <td className="tabular-nums">{pct(a.helpful_rate)}</td>
                  <td className="text-xs text-muted-foreground">{Object.entries(a.counts).map(([k, n]) => `${humanize(k)} ${n}`).join(" · ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </CardContent>
    </Card>
  );
}

export function EvaluationsAdmin() {
  const prompts = usePrompts();
  const evals = useEvals();
  const models = useModels();
  const agents = useAgents();
  const { run, compare, activate } = useEvalActions();
  const [agent, setAgent] = useState("fraud");
  const [picked, setPicked] = useState<string[]>([]);
  const usable = (models.data ?? []).filter((m) => verifiedCaps(m).includes("json_mode") && m.status === "active");
  const config = agents.data?.find((a) => a.agent_key === agent);
  const error = run.error ?? compare.error ?? activate.error;
  return (
    <Card data-testid="evaluations">
      <CardHeader>
        <div>
          <CardTitle>Evaluations, prompts and model comparison</CardTitle>
          <CardDescription>
            A prompt version or a model can only be activated for an agent after a regression run passes: the agent&apos;s labelled
            set must still pass, the candidate must actually answer on the live cases (synthetic data only), and no metric may be
            worse than the last passing run. Free models allow few requests per day, so runs can end in &quot;error&quot;.
          </CardDescription>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-sm">Agent{" "}
            <Select aria-label="Agent" value={agent} onChange={(e) => setAgent(e.target.value)} className="w-40">
              {LLM_AGENTS.map((a) => <option key={a} value={a}>{humanize(a)}</option>)}
            </Select>
          </label>
          <span className="text-sm text-muted-foreground">Running prompt {config ? (prompts.data?.find((p) => p.agent_key === agent && p.active_for_org)?.version ?? "v1") : "—"}</span>
        </div>
        {error ? <Alert>{errorText(error)}</Alert> : null}
        <div>
          <h3 className="mb-1 text-sm font-medium">Prompt versions</h3>
          {prompts.isPending ? <Skeleton className="h-12" /> : (
            <ul className="space-y-1 text-sm" data-testid="prompt-versions">
              {(prompts.data ?? []).filter((p) => p.agent_key === agent).map((p) => (
                <li key={p.id} className="flex flex-wrap items-center gap-2" data-testid={`prompt-${p.version}`}>
                  <span className="font-mono">{p.version}</span>
                  {p.active_for_org ? <Badge tone="success">Active</Badge> : null}
                  {p.latest_eval ? <Badge tone={EVAL_TONE[p.latest_eval.status as keyof typeof EVAL_TONE] ?? "neutral"}>Last run {p.latest_eval.status}</Badge> : <Badge>Not evaluated</Badge>}
                  <span className="font-mono text-xs text-muted-foreground">{p.content_sha256.slice(0, 12)}</span>
                  <span className="ml-auto flex gap-2">
                    <Button size="sm" variant="secondary" disabled={run.isPending} onClick={() => run.mutate({ agent_key: agent, prompt_version: p.version })}>Run evaluation</Button>
                    {!p.active_for_org ? <Button size="sm" disabled={activate.isPending} onClick={() => activate.mutate(p.id)}>Activate</Button> : null}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div>
          <h3 className="mb-1 text-sm font-medium">Compare models on the same cases</h3>
          <div className="flex flex-wrap gap-3 text-sm">
            {usable.length === 0 ? <span className="text-muted-foreground">No verified models yet: sync the catalogue and run capability tests above.</span> : usable.slice(0, 12).map((m) => (
              <label key={m.id} className="flex items-center gap-1.5">
                <input type="checkbox" checked={picked.includes(m.id)} onChange={(e) => setPicked(e.target.checked ? [...picked, m.id].slice(0, 5) : picked.filter((x) => x !== m.id))} />
                <span className="font-mono text-xs">{m.slug}</span>
              </label>
            ))}
          </div>
          <Button size="sm" className="mt-2" variant="secondary" disabled={!picked.length || compare.isPending} onClick={() => compare.mutate({ agent_key: agent, model_ids: picked })}>Compare {picked.length || ""} model(s)</Button>
        </div>
        <div className="overflow-x-auto">
          <h3 className="mb-1 text-sm font-medium">Runs</h3>
          {evals.isPending ? <Skeleton className="h-16" /> : !evals.data?.filter((r) => r.agent_key === agent).length ? <p className="text-sm text-muted-foreground">No evaluation runs for this agent yet.</p> : (
            <table className="w-full text-left text-sm" data-testid="eval-runs">
              <thead className="text-xs text-muted-foreground"><tr><th className="pb-2">Started</th><th>Candidate</th><th>Status</th><th>Cases</th><th>Model answers</th><th>Gated metrics</th><th>Why</th></tr></thead>
              <tbody>
                {evals.data.filter((r) => r.agent_key === agent).map((r) => {
                  const gated = r.metrics.filter((m) => m.threshold !== null);
                  return (
                    <tr key={r.id} className="border-t border-border align-top">
                      <td className="py-1.5">{formatDateTime(r.created_at)}</td>
                      <td><span className="font-mono text-xs">{r.prompt_version}{r.model_slug ? ` · ${r.model_slug}` : ""}</span></td>
                      <td><Badge tone={EVAL_TONE[r.status as keyof typeof EVAL_TONE] ?? "neutral"}>{r.status}</Badge></td>
                      <td className="tabular-nums">{r.cases}</td>
                      <td className="tabular-nums">{r.model_calls}</td>
                      <td className="tabular-nums">{gated.length ? `${gated.filter((m) => m.passed).length}/${gated.length}` : "—"}</td>
                      <td className="max-w-80 text-xs text-muted-foreground">{r.error ?? (r.regressions.length ? r.regressions.join("; ") : "")}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
