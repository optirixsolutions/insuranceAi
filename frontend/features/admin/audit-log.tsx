"use client";

import { ShieldCheck } from "lucide-react";
import { Fragment, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Alert, Field, Input } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";

import { useAuditLog, useVerifyAuditChain, type AuditEvent } from "./api";

const timeFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" });

function Changes({ event }: { event: AuditEvent }) {
  if (!event.before && !event.after) return <span className="text-muted-foreground">—</span>;
  return (
    <pre className="max-w-md overflow-x-auto whitespace-pre-wrap break-words rounded bg-surface-muted p-2 font-mono text-[11px] leading-relaxed">
      {event.before ? `before: ${JSON.stringify(event.before)}\n` : null}
      {event.after ? `after:  ${JSON.stringify(event.after)}` : null}
    </pre>
  );
}

export function AuditLog() {
  const [action, setAction] = useState("");
  const log = useAuditLog({ action });
  const verify = useVerifyAuditChain();
  const [expanded, setExpanded] = useState<string | null>(null);
  const events = log.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <div className="flex-1 sm:max-w-xs">
          <Field id="audit-action" label="Action starts with">
            <Input id="audit-action" placeholder="e.g. user. or auth.login" value={action} onChange={(e) => setAction(e.target.value.trim())} />
          </Field>
        </div>
        <Button variant="secondary" className="sm:ml-auto" onClick={() => verify.mutate()} disabled={verify.isPending}>
          <ShieldCheck aria-hidden />
          {verify.isPending ? "Verifying…" : "Verify integrity"}
        </Button>
      </div>

      {verify.data ? (
        verify.data.valid ? (
          <Alert tone="success">Hash chain intact: {verify.data.events_checked} events verified.</Alert>
        ) : (
          <Alert>
            Integrity check failed at event #{verify.data.first_invalid_seq}: {verify.data.reason}. Escalate to your security team.
          </Alert>
        )
      ) : null}
      {verify.error ? <Alert>{verify.error.message}</Alert> : null}

      <Card className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th scope="col" className="px-4 py-2.5 font-medium">#</th>
              <th scope="col" className="px-4 py-2.5 font-medium">Time</th>
              <th scope="col" className="px-4 py-2.5 font-medium">Actor</th>
              <th scope="col" className="px-4 py-2.5 font-medium">Action</th>
              <th scope="col" className="px-4 py-2.5 font-medium">Entity</th>
              <th scope="col" className="px-4 py-2.5 font-medium"><span className="sr-only">Details</span></th>
            </tr>
          </thead>
          <tbody>
            {log.isPending ? (
              <tr><td colSpan={6} className="p-4"><Skeleton className="h-24" /></td></tr>
            ) : log.error ? (
              <tr><td colSpan={6} className="p-4 text-danger" role="alert">{log.error.message}</td></tr>
            ) : events.length === 0 ? (
              <tr><td colSpan={6} className="p-6 text-center text-muted-foreground">No events match this filter.</td></tr>
            ) : (
              events.map((event) => (
                <Fragment key={event.id}>
                  <tr className="border-b border-border">
                    <td className="px-4 py-2.5 font-mono text-xs tabular-nums text-muted-foreground">{event.seq}</td>
                    <td className="px-4 py-2.5 whitespace-nowrap text-xs">{timeFormat.format(new Date(event.occurred_at))}</td>
                    <td className="px-4 py-2.5">{event.actor_name ?? <span className="text-muted-foreground">{event.actor_type}</span>}</td>
                    <td className="px-4 py-2.5"><Badge tone="info" className="font-mono">{event.action}</Badge></td>
                    <td className="px-4 py-2.5 text-xs text-muted-foreground">{event.entity_type}</td>
                    <td className="px-4 py-2.5 text-right">
                      <Button variant="ghost" size="sm" aria-expanded={expanded === event.id}
                        onClick={() => setExpanded(expanded === event.id ? null : event.id)}>
                        {expanded === event.id ? "Hide" : "Details"}
                      </Button>
                    </td>
                  </tr>
                  {expanded === event.id ? (
                    <tr className="border-b border-border bg-surface-muted/40">
                      <td />
                      <td colSpan={5} className="px-4 py-3 text-xs">
                        <dl className="mb-2 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
                          <dt className="text-muted-foreground">Entity id</dt><dd className="font-mono">{event.entity_id ?? "—"}</dd>
                          <dt className="text-muted-foreground">Request id</dt><dd className="font-mono">{event.request_id ?? "—"}</dd>
                          <dt className="text-muted-foreground">IP address</dt><dd className="font-mono">{event.ip ?? "—"}</dd>
                        </dl>
                        <Changes event={event} />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              ))
            )}
          </tbody>
        </table>
      </Card>
      {log.hasNextPage ? (
        <div className="flex justify-center">
          <Button variant="secondary" onClick={() => void log.fetchNextPage()} disabled={log.isFetchingNextPage}>
            {log.isFetchingNextPage ? "Loading…" : "Load older events"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
