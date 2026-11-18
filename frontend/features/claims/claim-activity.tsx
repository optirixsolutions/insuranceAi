"use client";

import { Lock, Trash2 } from "lucide-react";
import Link from "next/link";
import { useState, type FormEvent } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Select } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { hasPermission, useMe } from "@/features/auth/use-me";
import { ApiError } from "@/lib/api/client";

import { useAddNote, useAssign, useAssignees, useDeleteNote, useNotes, useRelated, useTimeline, type ClaimDetail } from "./api";
import { formatDate, formatDateTime, humanize, STATUS_LABELS, statusTone } from "./format";

const errorText = (e: unknown) => (e instanceof ApiError ? e.userMessage : "Something went wrong.");
const ROLES = ["adjuster", "reviewer", "investigator"] as const;

export function AssignmentPanel({ claim }: { claim: ClaimDetail }) {
  const me = useMe().data;
  const canAssign = hasPermission(me, "claims.assign");
  const [role, setRole] = useState<(typeof ROLES)[number]>("adjuster");
  const assignees = useAssignees(role, canAssign);
  const assign = useAssign(claim.id);
  const current = claim.assignments.find((a) => a.role_on_claim === role);

  return (
    <Card>
      <CardHeader><CardTitle>Assignment</CardTitle></CardHeader>
      <CardContent className="space-y-3 text-sm">
        <dl className="space-y-1.5">
          {ROLES.map((r) => (
            <div key={r} className="flex justify-between gap-3">
              <dt className="text-muted-foreground">{humanize(r)}</dt>
              <dd className="font-medium">{claim.assignments.find((a) => a.role_on_claim === r)?.display_name ?? "—"}</dd>
            </div>
          ))}
        </dl>
        {canAssign ? (
          <div className="space-y-2 border-t border-border pt-3">
            {assign.error ? <Alert>{errorText(assign.error)}</Alert> : null}
            <div className="grid gap-2">
              <Select aria-label="Role" value={role} onChange={(e) => setRole(e.target.value as (typeof ROLES)[number])}>
                {ROLES.map((r) => <option key={r} value={r}>{humanize(r)}</option>)}
              </Select>
              <Select aria-label={`Assign ${role}`} value={current?.user_id ?? ""} disabled={assign.isPending || assignees.isPending}
                onChange={(e) => assign.mutate({ role, user_id: e.target.value || null })}>
                <option value="">Unassigned</option>
                {assignees.data?.map((u) => <option key={u.id} value={u.id}>{u.display_name}</option>)}
              </Select>
            </div>
            <p className="text-xs text-muted-foreground">Only users whose role allows the work are listed.</p>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function NotesPanel({ claimId }: { claimId: string }) {
  const me = useMe().data;
  const notes = useNotes(claimId);
  const add = useAddNote(claimId);
  const remove = useDeleteNote(claimId);
  const [body, setBody] = useState("");
  const [restricted, setRestricted] = useState(false);
  const canRestrict = hasPermission(me, "claims.assign") || hasPermission(me, "audit.read");

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!body.trim()) return;
    add.mutate({ body: body.trim(), visibility: restricted ? "restricted" : "internal" }, { onSuccess: () => { setBody(""); setRestricted(false); } });
  }

  return (
    <Card>
      <CardHeader><CardTitle>Internal notes</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <form onSubmit={onSubmit} className="space-y-2">
          {add.error ? <Alert>{errorText(add.error)}</Alert> : null}
          <label htmlFor="note-body" className="sr-only">New note</label>
          <textarea id="note-body" rows={3} value={body} onChange={(e) => setBody(e.target.value)} maxLength={10000}
            placeholder="Add a note for the claims team…"
            className="w-full rounded-md border border-input bg-surface px-3 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/20" />
          <div className="flex items-center justify-between gap-3">
            {canRestrict ? (
              <label className="flex items-center gap-2 text-xs text-muted-foreground">
                <input type="checkbox" checked={restricted} onChange={(e) => setRestricted(e.target.checked)} className="accent-[var(--color-primary)]" />
                Restricted (managers and oversight only)
              </label>
            ) : <span />}
            <Button size="sm" type="submit" disabled={add.isPending || !body.trim()}>Add note</Button>
          </div>
        </form>
        {notes.isPending ? <Skeleton className="h-16" /> : notes.data?.length === 0 ? (
          <p className="text-sm text-muted-foreground">No notes yet.</p>
        ) : (
          <ul className="space-y-3">
            {notes.data?.map((n) => (
              <li key={n.id} className="rounded-md border border-border p-3 text-sm">
                <div className="mb-1 flex items-center justify-between gap-2 text-xs text-muted-foreground">
                  <span>
                    <span className="font-medium text-foreground">{n.author_name}</span> · {formatDateTime(n.created_at)}
                    {n.visibility === "restricted" ? <Badge tone="warning" className="ml-2"><Lock className="size-3" aria-hidden />Restricted</Badge> : null}
                  </span>
                  {n.author_id === me?.user.id || hasPermission(me, "claims.assign") ? (
                    <button type="button" aria-label="Delete note" className="rounded p-1 hover:bg-surface-muted hover:text-danger"
                      onClick={() => remove.mutate(n.id)} disabled={remove.isPending}>
                      <Trash2 className="size-3.5" aria-hidden />
                    </button>
                  ) : null}
                </div>
                <p className="whitespace-pre-wrap break-words">{n.body}</p>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

export function TimelinePanel({ claimId }: { claimId: string }) {
  const timeline = useTimeline(claimId);
  return (
    <Card>
      <CardHeader><CardTitle>Activity timeline</CardTitle></CardHeader>
      <CardContent>
        {timeline.isPending ? <Skeleton className="h-24" /> : (
          <ol className="relative space-y-4 border-l border-border pl-5" data-testid="claim-timeline">
            {timeline.data?.map((e, i) => (
              <li key={`${e.at}-${i}`} className="relative text-sm">
                <span aria-hidden className="absolute -left-[25px] top-1.5 size-2.5 rounded-full border-2 border-surface bg-primary" />
                <p className="font-medium">{e.title}</p>
                <p className="text-xs text-muted-foreground">{formatDateTime(e.at)}{e.actor_name ? ` · ${e.actor_name}` : ""}</p>
                {e.detail ? <p className="mt-1 whitespace-pre-wrap break-words text-muted-foreground">{e.detail}</p> : null}
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}

export function RelatedPanel({ claimId }: { claimId: string }) {
  const related = useRelated(claimId);
  if (!related.data?.length) return null;
  return (
    <Card>
      <CardHeader><CardTitle>Related claims</CardTitle></CardHeader>
      <CardContent>
        <ul className="space-y-2 text-sm">
          {related.data.map((r) => (
            <li key={r.id} className="flex flex-wrap items-center justify-between gap-2">
              <Link href={`/claims/${r.id}`} className="font-mono text-primary hover:underline">{r.claim_reference}</Link>
              <span className="text-xs text-muted-foreground">{humanize(r.relation)} · incident {formatDate(r.incident_date)}</span>
              <Badge tone={statusTone(r.status)}>{STATUS_LABELS[r.status] ?? r.status}</Badge>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
