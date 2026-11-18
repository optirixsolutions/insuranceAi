"use client";

import { FlaskConical, RefreshCw } from "lucide-react";
import { useState } from "react";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Field, Select } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateTime, humanize } from "@/features/claims/format";
import { ApiError } from "@/lib/api/client";

import {
  useAgents,
  useAiStatus,
  useJob,
  useModels,
  useProbeModel,
  useStartTestRun,
  useSyncCatalog,
  useUpdateAgent,
  useUpdateModel,
  useUsage,
  verifiedCaps,
  type AgentConfig,
  type AiModel,
} from "./api";

const CAPS = ["json_mode", "json_schema", "tools", "vision"] as const;
const PROBE_TONE: Record<string, BadgeTone> = { never: "neutral", running: "info", passed: "success", partial: "warning", failed: "danger" };
const errorText = (e: unknown) => (e instanceof ApiError ? e.userMessage : "Something went wrong.");

function CapabilityCell({ model, cap }: { model: AiModel; cap: string }) {
  const verified = model.verified_capabilities[cap];
  const declared = model.declared_capabilities[cap];
  if (verified) return <Badge tone={verified.ok ? "success" : "danger"} title={verified.detail}>{verified.ok ? "verified" : "failed"}</Badge>;
  return <span className="text-xs text-muted-foreground">{declared ? "claimed" : "—"}</span>;
}

function DataPolicy({ model }: { model: AiModel }) {
  const update = useUpdateModel();
  const [note, setNote] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  return (
    <div className="space-y-1">
      <Select aria-label={`Data policy for ${model.slug}`} value={pending ?? model.data_policy} className="h-8 text-xs"
        onChange={(e) => {
          const value = e.target.value;
          if (value === "no_training" || value === "zero_retention") setPending(value);
          else update.mutate({ model, changes: { data_policy: value } });
        }}>
        <option value="unknown">Unknown</option>
        <option value="may_log">May log prompts</option>
        <option value="no_training">No training (approved)</option>
        <option value="zero_retention">Zero retention (approved)</option>
      </Select>
      {pending ? (
        <form className="space-y-1" onSubmit={(e) => { e.preventDefault(); update.mutate({ model, changes: { data_policy: pending, data_policy_note: note } }, { onSuccess: () => setPending(null) }); }}>
          <input aria-label="Approval note" className="h-8 w-full rounded border border-input px-2 text-xs" placeholder="Cite the provider terms (required)"
            value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="flex gap-1">
            <Button size="sm" type="submit" disabled={note.trim().length < 5}>Approve</Button>
            <Button size="sm" variant="ghost" type="button" onClick={() => setPending(null)}>Cancel</Button>
          </div>
        </form>
      ) : null}
      {update.error ? <p className="text-xs text-danger">{errorText(update.error)}</p> : null}
    </div>
  );
}

function ModelsTable({ keyConfigured }: { keyConfigured: boolean }) {
  const models = useModels();
  const probe = useProbeModel();
  const update = useUpdateModel();
  return (
    <Card className="overflow-x-auto">
      <table className="w-full min-w-[1000px] text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
            <th className="px-3 py-2.5 font-medium">Model</th>
            {CAPS.map((c) => <th key={c} className="px-3 py-2.5 font-medium">{humanize(c)}</th>)}
            <th className="px-3 py-2.5 font-medium">Probe</th>
            <th className="px-3 py-2.5 font-medium">Data policy</th>
            <th className="px-3 py-2.5 font-medium">Status</th>
          </tr>
        </thead>
        <tbody>
          {models.isPending ? <tr><td colSpan={8} className="p-4"><Skeleton className="h-24" /></td></tr> :
            models.data?.length === 0 ? <tr><td colSpan={8} className="p-6 text-center text-muted-foreground">No models yet. Sync the catalogue.</td></tr> :
            models.data?.map((m) => (
              <tr key={m.id} className="border-b border-border align-top last:border-0" data-testid={`model-${m.slug}`}>
                <td className="px-3 py-2.5">
                  <p className="font-mono text-xs font-medium">{m.slug}</p>
                  <p className="text-xs text-muted-foreground">{m.is_free ? "free" : `$${m.prompt_price_per_mtok}/M in`} · {m.context_length?.toLocaleString() ?? "?"} ctx{m.in_catalog ? "" : " · withdrawn"}</p>
                </td>
                {CAPS.map((c) => <td key={c} className="px-3 py-2.5"><CapabilityCell model={m} cap={c} /></td>)}
                <td className="px-3 py-2.5">
                  <Badge tone={PROBE_TONE[m.probe_status] ?? "neutral"}>{m.probe_status}</Badge>
                  {m.probed_at ? <p className="mt-1 text-[11px] text-muted-foreground">{formatDateTime(m.probed_at)}</p> : null}
                  <Button size="sm" variant="ghost" className="mt-1" disabled={!keyConfigured || probe.isPending || m.probe_status === "running"}
                    title={keyConfigured ? undefined : "Configure OPENROUTER_API_KEY to run probes"} onClick={() => probe.mutate(m)}>
                    <FlaskConical aria-hidden />Test
                  </Button>
                </td>
                <td className="px-3 py-2.5 w-52"><DataPolicy model={m} /></td>
                <td className="px-3 py-2.5">
                  <Button size="sm" variant="secondary" onClick={() => update.mutate({ model: m, changes: { status: m.status === "active" ? "disabled" : "active" } })}>
                    {m.status === "active" ? "Disable" : "Enable"}
                  </Button>
                </td>
              </tr>
            ))}
        </tbody>
      </table>
      {probe.error ? <div className="p-3"><Alert>{errorText(probe.error)}</Alert></div> : null}
    </Card>
  );
}

function AgentRow({ agent, models }: { agent: AgentConfig; models: AiModel[] }) {
  const update = useUpdateAgent();
  const eligible = models.filter((m) => m.status === "active" && agent.required_capabilities.every((c) => verifiedCaps(m).includes(c)));
  const current = models.find((m) => m.id === agent.primary_model_id);
  return (
    <tr className="border-b border-border align-top last:border-0" data-testid={`agent-${agent.agent_key}`}>
      <td className="px-3 py-2.5"><p className="font-medium">{humanize(agent.agent_key)}</p><p className="text-xs text-muted-foreground">{agent.description}</p></td>
      <td className="px-3 py-2.5 text-xs">{agent.required_capabilities.map(humanize).join(", ")}</td>
      <td className="px-3 py-2.5 w-72">
        <Select aria-label={`Model for ${agent.agent_key}`} value={agent.primary_model_id ?? ""} className="h-8 text-xs"
          onChange={(e) => update.mutate({ agent, changes: { primary_model_id: e.target.value || null } })}>
          <option value="">Not assigned</option>
          {current && !eligible.includes(current) ? <option value={current.id}>{current.slug} (not verified)</option> : null}
          {eligible.map((m) => <option key={m.id} value={m.id}>{m.slug}</option>)}
        </Select>
        {current && !eligible.includes(current) ? <p className="mt-1 text-xs text-warning">Not verified: calls will be refused until it passes the probe.</p> : null}
        {update.error ? <p className="mt-1 text-xs text-danger">{errorText(update.error)}</p> : null}
      </td>
      <td className="px-3 py-2.5 text-xs">{agent.fallback_model_ids.length ? `${agent.fallback_model_ids.length} fallback(s)` : "None"}</td>
      <td className="px-3 py-2.5 text-xs">{agent.timeout_seconds}s · {agent.max_retries} repairs · ≥{Math.round(agent.confidence_threshold * 100)}%</td>
    </tr>
  );
}

function TestRun({ agents }: { agents: AgentConfig[] }) {
  const start = useStartTestRun();
  const [agentKey, setAgentKey] = useState("document");
  const [text, setText] = useState("Synthetic note: on 8 Oct 2026 the insured's Toyota was rear-ended at a junction in Leeds by a delivery van. Repair estimate from Bridgewater Auto Body: 1,475.00 GBP.");
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useJob(jobId);
  return (
    <Card>
      <CardHeader><div><CardTitle>Gateway test run</CardTitle><CardDescription>Sends synthetic text through routing, retries, JSON repair and fallback for an agent. Never paste real personal data.</CardDescription></div></CardHeader>
      <CardContent className="space-y-3">
        <div className="sm:w-64"><Field id="test-agent" label="Agent">
          <Select id="test-agent" value={agentKey} onChange={(e) => setAgentKey(e.target.value)}>
            {agents.map((a) => <option key={a.agent_key} value={a.agent_key}>{humanize(a.agent_key)}</option>)}
          </Select>
        </Field></div>
        <textarea aria-label="Synthetic test text" rows={3} value={text} onChange={(e) => setText(e.target.value)} className="w-full rounded-md border border-input px-3 py-2 text-sm" />
        <Button disabled={start.isPending || text.trim().length < 20} onClick={() => start.mutate({ agent_key: agentKey, text }, { onSuccess: (j) => setJobId(j.job_id) })}>Run</Button>
        {start.error ? <Alert>{errorText(start.error)}</Alert> : null}
        {job.data ? <pre className="max-h-64 overflow-auto rounded-md bg-surface-muted p-3 font-mono text-xs" data-testid="test-run-result">{job.data.status}{job.data.result ? `\n${JSON.stringify(job.data.result, null, 2)}` : ""}</pre> : null}
      </CardContent>
    </Card>
  );
}

export function AiAgents() {
  const status = useAiStatus();
  const sync = useSyncCatalog();
  const models = useModels();
  const agents = useAgents();
  const usage = useUsage();
  const keyConfigured = status.data?.key_status === "valid" || status.data?.key_status === "not_applicable";
  return (
    <div className="space-y-6">
      {status.data?.key_status === "not_configured" ? (
        <Alert>No OpenRouter API key is configured on the server, so models cannot be called. Set OPENROUTER_API_KEY in the server environment; it is never sent to the browser.</Alert>
      ) : status.data?.key_status === "rejected" ? (
        <Alert>OpenRouter rejected the configured API key ({status.data.key_detail}). Replace OPENROUTER_API_KEY on the server; model calls are disabled until then.</Alert>
      ) : status.data?.key_status === "unreachable" ? (
        <Alert>OpenRouter could not be reached to validate the key ({status.data.key_detail}).</Alert>
      ) : status.data?.key_status === "valid" && status.data.key_detail ? (
        <Alert tone="success">OpenRouter key valid ({status.data.key_detail}).</Alert>
      ) : null}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          {status.data ? `${status.data.models_registered} models registered · ${status.data.models_verified} verified · provider ${status.data.provider}` : " "}
        </p>
        <Button variant="secondary" disabled={sync.isPending} onClick={() => sync.mutate()}><RefreshCw aria-hidden className={sync.isPending ? "animate-spin" : undefined} />Sync catalogue</Button>
      </div>
      {sync.error ? <Alert>{errorText(sync.error)}</Alert> : null}
      {sync.data ? <Alert tone="success">Catalogue synced: {sync.data.added} added, {sync.data.updated} updated, {sync.data.withdrawn} withdrawn (of {sync.data.catalog_size} models upstream).</Alert> : null}

      <section className="space-y-2"><h2 className="text-sm font-semibold">Agent model assignment</h2>
        <Card className="overflow-x-auto">
          <table className="w-full min-w-[860px] text-sm">
            <thead><tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th className="px-3 py-2.5 font-medium">Agent</th><th className="px-3 py-2.5 font-medium">Needs</th><th className="px-3 py-2.5 font-medium">Primary model</th><th className="px-3 py-2.5 font-medium">Fallbacks</th><th className="px-3 py-2.5 font-medium">Limits</th>
            </tr></thead>
            <tbody>{agents.data?.map((a) => <AgentRow key={a.agent_key} agent={a} models={models.data ?? []} />)}</tbody>
          </table>
        </Card>
      </section>

      <section className="space-y-2"><h2 className="text-sm font-semibold">Model registry</h2><ModelsTable keyConfigured={keyConfigured} /></section>

      <section className="space-y-2"><h2 className="text-sm font-semibold">Usage (7 days)</h2>
        <Card className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-sm">
            <thead><tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th className="px-3 py-2.5 font-medium">Agent</th><th className="px-3 py-2.5 font-medium">Model</th><th className="px-3 py-2.5 text-right font-medium">Calls</th><th className="px-3 py-2.5 text-right font-medium">Success</th><th className="px-3 py-2.5 text-right font-medium">Tokens</th><th className="px-3 py-2.5 text-right font-medium">Cost</th><th className="px-3 py-2.5 text-right font-medium">p50</th>
            </tr></thead>
            <tbody>
              {usage.data?.length === 0 ? <tr><td colSpan={7} className="p-6 text-center text-muted-foreground">No model calls yet.</td></tr> :
                usage.data?.map((u) => (
                  <tr key={`${u.agent_key}-${u.model_slug}`} className="border-b border-border last:border-0">
                    <td className="px-3 py-2">{humanize(u.agent_key)}</td><td className="px-3 py-2 font-mono text-xs">{u.model_slug}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{u.calls}</td><td className="px-3 py-2 text-right tabular-nums">{u.calls ? Math.round((u.succeeded / u.calls) * 100) : 0}%</td>
                    <td className="px-3 py-2 text-right tabular-nums">{(u.prompt_tokens + u.completion_tokens).toLocaleString()}</td>
                    <td className="px-3 py-2 text-right font-mono text-xs">${u.cost_usd}</td><td className="px-3 py-2 text-right tabular-nums">{u.p50_latency_ms} ms</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </Card>
      </section>

      {agents.data ? <TestRun agents={agents.data} /> : null}
    </div>
  );
}
