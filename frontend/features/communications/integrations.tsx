"use client";

import { useState, type FormEvent } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Field, Input } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateTime, humanize } from "@/features/claims/format";
import { ApiError } from "@/lib/api/client";

import { useDeliveries, useEventTypes, useIntegrationStatus, useTestMessage, useWebhookActions, useWebhooks } from "./api";

const errorText = (e: unknown) => (e instanceof ApiError ? e.userMessage : "Something went wrong.");
const DELIVERY_TONE = { delivered: "success", pending: "warning", failed: "warning", dead: "danger" } as const;

function Messaging() {
  const status = useIntegrationStatus();
  const test = useTestMessage();
  const [phone, setPhone] = useState("");
  if (status.isPending) return <Skeleton className="h-32" />;
  if (status.error) return <Alert>Could not load integration status.</Alert>;
  const s = status.data;
  return (
    <Card data-testid="messaging">
      <CardHeader>
        <div>
          <CardTitle>Email and SMS</CardTitle>
          <CardDescription>Notifications go to the in-app centre; reviewers are also emailed when a review is assigned to them or needs a second approver.</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <p>Email: <Badge tone={s.email.provider === "smtp" ? "success" : "neutral"}>{s.email.provider}</Badge> via {s.email.host} from {s.email.from}</p>
        <p>SMS: <Badge>{s.sms.provider}</Badge> {s.sms.sends_real_messages ? null : <span className="text-muted-foreground">Adapter only: messages are recorded, not sent to a carrier.</span>}</p>
        {!s.encryption_key_configured ? <Alert>No DATA_ENCRYPTION_KEY is set: webhook secrets use a development key. Production refuses this.</Alert> : null}
        <div className="flex flex-wrap items-end gap-2">
          <Button size="sm" variant="secondary" onClick={() => test.mutate({ channel: "email" })} disabled={test.isPending}>Send me a test email</Button>
          <Field id="sms-to" label="Test SMS number"><Input id="sms-to" value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+44 7700 900123" className="w-48" /></Field>
          <Button size="sm" variant="secondary" onClick={() => test.mutate({ channel: "sms", to: phone })} disabled={test.isPending || !phone}>Send test SMS</Button>
        </div>
        {test.error ? <Alert>{errorText(test.error)}</Alert> : null}
        {test.data ? <Alert tone={test.data.status === "sent" ? "success" : "danger"}>Test message {test.data.status} ({test.data.provider}){test.data.error ? `: ${test.data.error}` : ""}.</Alert> : null}
      </CardContent>
    </Card>
  );
}

function Deliveries({ id }: { id: string }) {
  const deliveries = useDeliveries(id);
  if (deliveries.isPending) return <Skeleton className="h-16" />;
  if (!deliveries.data?.length) return <p className="text-xs text-muted-foreground">No deliveries yet.</p>;
  return (
    <table className="w-full text-left text-xs" data-testid="deliveries">
      <thead className="text-muted-foreground"><tr><th className="py-1">Event</th><th>Status</th><th>Attempts</th><th>Response</th><th>Next attempt</th><th>Created</th></tr></thead>
      <tbody>
        {deliveries.data.map((d) => (
          <tr key={d.id} className="border-t border-border">
            <td className="py-1 font-mono">{d.event_type}</td>
            <td><Badge tone={DELIVERY_TONE[d.status as keyof typeof DELIVERY_TONE] ?? "neutral"}>{d.status}</Badge></td>
            <td className="tabular-nums">{d.attempt}</td>
            <td title={d.last_error ?? undefined}>{d.response_code ?? (d.last_error ? "error" : "—")}</td>
            <td>{d.next_attempt_at ? formatDateTime(d.next_attempt_at) : "—"}</td>
            <td>{formatDateTime(d.created_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Webhooks() {
  const hooks = useWebhooks();
  const types = useEventTypes();
  const { create, disable, ping } = useWebhookActions();
  const [selected, setSelected] = useState<string[]>([]);
  const [open, setOpen] = useState<string | null>(null);

  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const d = new FormData(e.currentTarget);
    create.mutate({ url: String(d.get("url") ?? "").trim(), description: String(d.get("description") ?? ""), event_types: selected });
  }

  return (
    <Card data-testid="webhooks">
      <CardHeader>
        <div>
          <CardTitle>Webhooks</CardTitle>
          <CardDescription>
            Events are POSTed as JSON with <code className="font-mono text-xs">X-ClaimOS-Signature: t=…,v1=…</code>, an HMAC-SHA256 of
            &quot;timestamp.body&quot; with your signing secret. Verify it and reject requests older than five minutes. Payloads carry
            identifiers and outcomes, never customer details. Failed deliveries are retried with backoff.
          </CardDescription>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <form onSubmit={submit} className="space-y-3" data-testid="webhook-form">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field id="url" label="Endpoint URL (https)"><Input id="url" name="url" required placeholder="https://example.com/claimos" /></Field>
            <Field id="description" label="Description"><Input id="description" name="description" maxLength={200} /></Field>
          </div>
          <fieldset className="flex flex-wrap gap-3 text-sm">
            <legend className="mb-1 text-xs text-muted-foreground">Events</legend>
            {(types.data ?? []).map((t) => (
              <label key={t} className="flex items-center gap-1.5">
                <input type="checkbox" checked={selected.includes(t)} onChange={(e) => setSelected(e.target.checked ? [...selected, t] : selected.filter((x) => x !== t))} />
                <span className="font-mono text-xs">{t}</span>
              </label>
            ))}
          </fieldset>
          <Button type="submit" size="sm" disabled={create.isPending || !selected.length}>Add webhook</Button>
          {create.error ? <Alert>{errorText(create.error)}</Alert> : null}
          {create.data ? (
            <Alert tone="success">
              Signing secret (shown once, store it now): <code className="break-all font-mono text-xs" data-testid="webhook-secret">{create.data.secret}</code>
            </Alert>
          ) : null}
        </form>
        {hooks.isPending ? <Skeleton className="h-16" /> : hooks.data?.length ? (
          <ul className="space-y-2">
            {hooks.data.map((h) => (
              <li key={h.id} className="rounded-md border border-border p-3 text-sm" data-testid="webhook">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-xs">{h.url}</span>
                  <Badge tone={h.active ? "success" : "neutral"}>{h.active ? "Active" : "Disabled"}</Badge>
                  <span className="text-xs text-muted-foreground">secret {h.secret_hint}…</span>
                  <span className="ml-auto flex gap-2">
                    <Button size="sm" variant="ghost" onClick={() => setOpen(open === h.id ? null : h.id)}>{open === h.id ? "Hide deliveries" : "Deliveries"}</Button>
                    {h.active ? <Button size="sm" variant="secondary" onClick={() => { ping.mutate(h.id); setOpen(h.id); }}>Send ping</Button> : null}
                    {h.active ? <Button size="sm" variant="danger" onClick={() => disable.mutate(h.id)}>Disable</Button> : null}
                  </span>
                </div>
                <p className="mt-1 text-xs text-muted-foreground">{h.description ? `${h.description} · ` : ""}{h.event_types.map(humanize).join(", ")}</p>
                {open === h.id ? <div className="mt-2"><Deliveries id={h.id} /></div> : null}
              </li>
            ))}
          </ul>
        ) : <p className="text-sm text-muted-foreground">No webhooks yet.</p>}
      </CardContent>
    </Card>
  );
}

export function IntegrationsAdmin() {
  return (
    <div className="space-y-6">
      <Messaging />
      <Webhooks />
    </div>
  );
}
