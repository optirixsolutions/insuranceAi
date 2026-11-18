"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { z } from "zod";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Field, Input, Select } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { hasPermission, useMe } from "@/features/auth/use-me";
import { formatDateTime } from "@/features/claims/format";
import { useProducts } from "@/features/policies/api";
import { ApiError, apiGet, apiRequest } from "@/lib/api/client";

const band = z.object({ min_age_years: z.number(), pct: z.string() });
const configSchema = z.object({
  motor_parts_depreciation: z.array(band),
  health_sub_limits: z.record(z.string(), z.string()),
  fraud_weights: z.record(z.string(), z.number()),
  fraud_llm_cap: z.number(),
  validation_amount_tolerance_pct: z.string(),
  automated_approval_allowed: z.boolean(),
  stp: z.object({
    enabled: z.boolean(), max_amount: z.string().nullable(), max_fraud_score: z.number(), claimant_flag_months: z.number(),
  }),
  four_eyes_threshold: z.string().nullable(),
  review_sla_hours: z.number(),
});
type Config = z.infer<typeof configSchema>;
const ruleSetSchema = z.object({
  id: z.string(), product_id: z.string(), version_no: z.number(), status: z.string(), config: configSchema,
  config_sha256: z.string(), change_note: z.string(), created_at: z.string(), published_at: z.string().nullable(),
  row_version: z.number(),
});
type RuleSet = z.infer<typeof ruleSetSchema>;
const STATUS_TONE = { draft: "warning", published: "success", retired: "neutral" } as const;
const errorText = (e: unknown) => (e instanceof ApiError ? e.userMessage : "Something went wrong.");

function useRuleSets(productId: string | undefined) {
  return useQuery({
    queryKey: ["rule-sets", productId],
    queryFn: ({ signal }) => apiGet(`/products/${productId}/rule-sets`, z.array(ruleSetSchema), { signal }),
    enabled: Boolean(productId),
  });
}

function Editor({ draft, canPublish }: { draft: RuleSet; canPublish: boolean }) {
  const qc = useQueryClient();
  const [c, setC] = useState<Config>(draft.config);
  const [note, setNote] = useState(draft.change_note);
  // The version this editor last saw from the server (a save returns the next one before the list refetches).
  const [version, setVersion] = useState(draft.row_version);
  const refresh = () => void qc.invalidateQueries({ queryKey: ["rule-sets", draft.product_id] });
  const save = useMutation({
    mutationFn: () => apiRequest("PATCH", `/rule-sets/${draft.id}`, ruleSetSchema, {
      body: { config: c, change_note: note }, headers: { "If-Match": `"${version}"` },
    }),
    onSuccess: (saved) => { setVersion(saved.row_version); refresh(); },
  });
  const publish = useMutation({
    mutationFn: () => apiRequest("POST", `/rule-sets/${draft.id}/publish`, ruleSetSchema, {
      headers: { "If-Match": `"${version}"` },
    }),
    onSuccess: refresh,
  });
  const setStp = (patch: Partial<Config["stp"]>) => setC({ ...c, stp: { ...c.stp, ...patch } });
  const bands = [...c.motor_parts_depreciation].sort((a, b) => a.min_age_years - b.min_age_years);
  return (
    <div className="space-y-4" data-testid="rule-set-editor">
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={c.automated_approval_allowed}
            onChange={(e) => setC({ ...c, automated_approval_allowed: e.target.checked })} />
          Compliance profile allows automated approval
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={c.stp.enabled} onChange={(e) => setStp({ enabled: e.target.checked })} />
          Straight-through processing enabled
        </label>
        <Field label="STP maximum payout" id="stp-max">
          <Input id="stp-max" inputMode="decimal" value={c.stp.max_amount ?? ""}
            onChange={(e) => setStp({ max_amount: e.target.value || null })} />
        </Field>
        <Field label="STP fraud score must be below" id="stp-fraud">
          <Input id="stp-fraud" type="number" min={0} max={100} value={c.stp.max_fraud_score}
            onChange={(e) => setStp({ max_fraud_score: Number(e.target.value) })} />
        </Field>
        <Field label="Second approver above (four-eyes)" id="four-eyes">
          <Input id="four-eyes" inputMode="decimal" value={c.four_eyes_threshold ?? ""}
            onChange={(e) => setC({ ...c, four_eyes_threshold: e.target.value || null })} />
        </Field>
        <Field label="Review due within (hours)" id="sla">
          <Input id="sla" type="number" min={1} max={720} value={c.review_sla_hours}
            onChange={(e) => setC({ ...c, review_sla_hours: Number(e.target.value) })} />
        </Field>
        <Field label="Amount tolerance for validation (%)" id="tol">
          <Input id="tol" inputMode="decimal" value={c.validation_amount_tolerance_pct}
            onChange={(e) => setC({ ...c, validation_amount_tolerance_pct: e.target.value })} />
        </Field>
        <Field label="Model contribution cap (fraud points)" id="cap">
          <Input id="cap" type="number" min={0} max={30} value={c.fraud_llm_cap}
            onChange={(e) => setC({ ...c, fraud_llm_cap: Number(e.target.value) })} />
        </Field>
      </div>
      <div>
        <h4 className="mb-1 text-sm font-medium">Motor parts depreciation by vehicle age</h4>
        <table className="text-sm">
          <thead className="text-xs text-muted-foreground">
            <tr><th className="pr-4 text-left">From age (years)</th><th className="text-left">Depreciation %</th></tr>
          </thead>
          <tbody>
            {bands.map((b, i) => (
              <tr key={b.min_age_years}>
                <td className="pr-4">{b.min_age_years}</td>
                <td>
                  <Input aria-label={`Depreciation from age ${b.min_age_years}`} className="w-24" inputMode="decimal" value={b.pct}
                    onChange={(e) => setC({ ...c, motor_parts_depreciation: bands.map((x, j) => (j === i ? { ...x, pct: e.target.value } : x)) })} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Field label="Change note" id="note">
        <Input id="note" value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} />
      </Field>
      {save.error ? <Alert>{errorText(save.error)}</Alert> : null}
      {publish.error ? <Alert>{errorText(publish.error)}</Alert> : null}
      <div className="flex gap-2">
        <Button size="sm" variant="secondary" onClick={() => save.mutate()} disabled={save.isPending}>Save draft</Button>
        {canPublish ? (
          <Button size="sm" onClick={() => publish.mutate()} disabled={publish.isPending || save.isPending}>Publish version {draft.version_no}</Button>
        ) : null}
      </div>
      <p className="text-xs text-muted-foreground">
        Publishing retires the current version. Published versions can never be edited; every agent run records the version it used.
      </p>
    </div>
  );
}

export function RuleSetsAdmin() {
  const products = useProducts();
  const me = useMe().data;
  const [productId, setProductId] = useState<string>();
  const selected = productId ?? products.data?.[0]?.id;
  const versions = useRuleSets(selected);
  const qc = useQueryClient();
  const create = useMutation({
    mutationFn: () => apiRequest("POST", `/products/${selected}/rule-sets`, ruleSetSchema, { body: { change_note: "" } }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["rule-sets", selected] }),
  });
  if (products.isPending) return <Skeleton className="h-48" />;
  if (!products.data?.length) return <Alert>Create a product first (Policy management).</Alert>;
  const draft = versions.data?.find((v) => v.status === "draft");
  const published = versions.data?.find((v) => v.status === "published");
  return (
    <Card>
      <CardHeader><CardTitle>Products &amp; rules</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <Field label="Product" id="product">
          <Select id="product" value={selected} onChange={(e) => setProductId(e.target.value)}>
            {products.data.map((p) => <option key={p.id} value={p.id}>{p.name} ({p.code})</option>)}
          </Select>
        </Field>
        <p className="text-sm" data-testid="active-rule-set">
          {published
            ? `Active: version ${published.version_no} (published ${formatDateTime(published.published_at ?? published.created_at)})` +
              (published.config.stp.enabled ? ` · STP up to ${published.config.stp.max_amount}` : " · STP off")
            : "Active: built-in defaults (STP off)"}
        </p>
        {versions.data?.length ? (
          <ul className="space-y-1 text-sm">
            {versions.data.map((v) => (
              <li key={v.id} className="flex items-center gap-2">
                <span className="font-mono">v{v.version_no}</span>
                <Badge tone={STATUS_TONE[v.status as keyof typeof STATUS_TONE] ?? "neutral"}>{v.status}</Badge>
                <span className="text-muted-foreground">{v.change_note}</span>
                <span className="ml-auto font-mono text-xs text-muted-foreground">{v.config_sha256.slice(0, 12)}</span>
              </li>
            ))}
          </ul>
        ) : null}
        {draft ? (
          <Editor key={`${draft.id}-${draft.row_version}`} draft={draft} canPublish={hasPermission(me, "rules.publish")} />
        ) : (
          <Button size="sm" onClick={() => create.mutate()} disabled={create.isPending}>New draft version</Button>
        )}
        {create.error ? <Alert>{errorText(create.error)}</Alert> : null}
      </CardContent>
    </Card>
  );
}
