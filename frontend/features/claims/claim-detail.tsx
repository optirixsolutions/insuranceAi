"use client";

import { ArrowLeft, FlaskConical } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Field, Input, Select, fieldA11y } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/client";
import { cn } from "@/lib/utils";

import { useChangeStatus, useClaim, useUpdateClaim, type ClaimDetail } from "./api";
import { AgentRunsPanel, CoveragePanel, FraudPanel } from "@/features/agents/agent-panels";
import { DocumentsPanel } from "@/features/documents/documents-panel";
import { ProcessingPanel } from "@/features/processing/processing-panel";
import { ReportsPanel } from "@/features/communications/reports";
import { SimilarClaims } from "@/features/intelligence/components";
import { DecisionPanel } from "@/features/reviews/decision-panel";

import { AssignmentPanel, NotesPanel, RelatedPanel, TimelinePanel } from "./claim-activity";
import { formatDate, formatDateTime, formatMoney, humanize, PRIORITY_TONE, SEVERITY_TONE, STATUS_LABELS, statusTone } from "./format";

const TABS = [
  { key: "overview", label: "Overview", phase: 3 },
  { key: "documents", label: "Documents", phase: 4 },
  { key: "agents", label: "Agents", phase: 6 },
  { key: "analysis", label: "Processing", phase: 7 },
  { key: "coverage", label: "Policy Coverage", phase: 6 },
  { key: "fraud", label: "Fraud Investigation", phase: 6 },
  { key: "financial", label: "Financial Assessment", phase: 8 },
  { key: "decision", label: "Decision", phase: 9 },
  { key: "reports", label: "Reports", phase: 11 },
  { key: "activity", label: "Activity Timeline", phase: 3 },
] as const;
const errorText = (e: unknown) => (e instanceof ApiError ? e.userMessage : "Something went wrong.");

function Detail({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-sm">{children}</dd>
    </div>
  );
}

function EditClaim({ claim }: { claim: ClaimDetail }) {
  const update = useUpdateClaim(claim);
  const [saved, setSaved] = useState(false);
  const editableFacts = claim.status === "SUBMITTED" || claim.status === "AWAITING_INFORMATION";

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaved(false);
    const data = new FormData(event.currentTarget);
    const changes: Record<string, unknown> = {};
    const description = String(data.get("edit_description") ?? "").trim();
    const priority = String(data.get("edit_priority") ?? "");
    const amount = String(data.get("edit_amount") ?? "").trim();
    if (description !== claim.description) changes.description = description;
    if (priority !== claim.priority) changes.priority = priority;
    if (editableFacts && amount && amount !== claim.claimed_amount.amount) changes.claimed_amount = amount;
    if (Object.keys(changes).length === 0) return;
    update.mutate(changes, { onSuccess: () => setSaved(true) });
  }

  return (
    <form onSubmit={onSubmit} className="space-y-3">
      {update.error ? (
        <Alert>{update.error instanceof ApiError && update.error.status === 412 ? "Someone else changed this claim. Reload to continue." : errorText(update.error)}</Alert>
      ) : null}
      {saved ? <Alert tone="success">Claim updated.</Alert> : null}
      <div className="grid gap-3 sm:grid-cols-2">
        <Field id="edit_amount" label={`Claimed amount (${claim.claimed_amount.currency})`} hint={editableFacts ? undefined : "Locked once processing starts."}>
          <Input {...fieldA11y("edit_amount", undefined, editableFacts ? undefined : "x")} defaultValue={claim.claimed_amount.amount} disabled={!editableFacts} inputMode="decimal" />
        </Field>
        <Field id="edit_priority" label="Priority">
          <Select {...fieldA11y("edit_priority")} defaultValue={claim.priority}>
            {["urgent", "high", "normal", "low"].map((p) => <option key={p} value={p}>{humanize(p)}</option>)}
          </Select>
        </Field>
      </div>
      <Field id="edit_description" label="Description">
        <textarea {...fieldA11y("edit_description")} rows={4} defaultValue={claim.description}
          className="w-full rounded-md border border-input bg-surface px-3 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/20" />
      </Field>
      <div className="flex justify-end"><Button size="sm" type="submit" disabled={update.isPending}>Save changes</Button></div>
    </form>
  );
}

function StatusChange({ claim }: { claim: ClaimDetail }) {
  const change = useChangeStatus(claim);
  const [target, setTarget] = useState(claim.allowed_status_changes[0] ?? "");
  const [reason, setReason] = useState("");
  if (claim.allowed_status_changes.length === 0) return null;
  return (
    <form className="space-y-2 border-t border-border pt-4"
      onSubmit={(e) => { e.preventDefault(); change.mutate({ status: target, reason: reason.trim() }); }}>
      <p className="text-sm font-medium">Change status</p>
      {change.error ? <Alert>{errorText(change.error)}</Alert> : null}
      <div className="grid gap-2 sm:grid-cols-[minmax(0,12rem)_1fr_auto]">
        <Select aria-label="New status" value={target} onChange={(e) => setTarget(e.target.value)}>
          {claim.allowed_status_changes.map((s) => <option key={s} value={s}>{STATUS_LABELS[s] ?? s}</option>)}
        </Select>
        <Input aria-label="Reason" placeholder="Reason (recorded in the audit log)" value={reason} onChange={(e) => setReason(e.target.value)} minLength={5} />
        <Button type="submit" size="md" variant="secondary" disabled={change.isPending || reason.trim().length < 5}>Apply</Button>
      </div>
      <p className="text-xs text-muted-foreground">Approval and rejection are only possible through the review workflow (Phase 9).</p>
    </form>
  );
}

function Overview({ claim }: { claim: ClaimDetail }) {
  const location = claim.incident_location as Record<string, string> | null;
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
      <div className="space-y-6">
        {claim.findings.length ? (
          <Card>
            <CardHeader><CardTitle>Intake findings</CardTitle></CardHeader>
            <CardContent>
              <ul className="space-y-2" data-testid="intake-findings">
                {claim.findings.map((f) => (
                  <li key={f.code} className="flex items-start gap-2 text-sm">
                    <Badge tone={SEVERITY_TONE[f.severity] ?? "neutral"}>{f.severity}</Badge>
                    <span>{f.message}</span>
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>
        ) : null}
        <Card>
          <CardHeader><CardTitle>Claim details</CardTitle></CardHeader>
          <CardContent className="space-y-5">
            <dl className="grid gap-4 sm:grid-cols-3">
              <Detail label="Customer">{claim.customer_name}</Detail>
              <Detail label="Policy"><Link className="font-mono text-primary hover:underline" href={`/policies/${claim.policy_id}`}>{claim.policy_number}</Link></Detail>
              <Detail label="Product">{claim.product_name}</Detail>
              <Detail label="Type of loss">{humanize(claim.loss_type)}</Detail>
              <Detail label="Incident">{formatDate(claim.incident_date)}{claim.incident_time ? ` ${claim.incident_time.slice(0, 5)}` : ""}</Detail>
              <Detail label="Location">{location ? [location.description, location.city, location.country].filter(Boolean).join(", ") || "—" : "—"}</Detail>
              <Detail label="Claimed"><span className="font-mono tabular-nums">{formatMoney(claim.claimed_amount)}</span></Detail>
              <Detail label="Reported">{formatDateTime(claim.reported_at)} via {claim.channel}</Detail>
              <Detail label="Policy version">
                {claim.policy_version
                  ? `v${claim.policy_version.version_no}: ${formatDate(claim.policy_version.effective_from)} – ${formatDate(claim.policy_version.effective_to)}`
                  : <span className="text-danger">None in force</span>}
              </Detail>
            </dl>
            {claim.can_edit ? <EditClaim key={claim.row_version} claim={claim} /> : <p className="whitespace-pre-wrap text-sm">{claim.description}</p>}
            {claim.can_edit ? <StatusChange key={claim.status} claim={claim} /> : null}
          </CardContent>
        </Card>
      </div>
      <div className="space-y-6">
        <AssignmentPanel claim={claim} />
        <Card>
          <CardHeader><CardTitle>Required documents</CardTitle></CardHeader>
          <CardContent>
            <ul className="space-y-1.5 text-sm">
              {claim.required_documents.map((d) => {
                const received = claim.documents_received.includes(d);
                return (
                  <li key={d} className="flex items-center justify-between" data-testid={`required-${d}`}>
                    <span>{humanize(d)}</span>
                    <Badge tone={received ? "success" : "warning"}>{received ? "Received" : "Missing"}</Badge>
                  </li>
                );
              })}
            </ul>
            <p className="mt-3 text-xs text-muted-foreground">Based on the classified documents in the Documents tab.</p>
          </CardContent>
        </Card>
        <RelatedPanel claimId={claim.id} />
      </div>
    </div>
  );
}

export function ClaimDetailView({ id }: { id: string }) {
  const claim = useClaim(id);
  const params = useSearchParams();
  const initialTab = TABS.find((t) => t.key === params.get("tab"))?.key ?? "overview";
  const [tab, setTab] = useState<(typeof TABS)[number]["key"]>(initialTab);
  const justCreated = params.get("created") === "1";

  if (claim.isPending) return <Skeleton className="h-64" />;
  if (claim.error) return <Alert>{claim.error instanceof ApiError && claim.error.status === 404 ? "This claim does not exist or is not assigned to you." : claim.error.message}</Alert>;
  const c = claim.data;

  return (
    <div className="space-y-6">
      <Link href="/claims" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"><ArrowLeft className="size-4" aria-hidden />All claims</Link>
      {justCreated ? <Alert tone="success">Claim {c.claim_reference} submitted. Intake checks have run; see the findings below.</Alert> : null}
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-mono text-2xl font-semibold tracking-tight">{c.claim_reference}</h1>
        <Badge tone={statusTone(c.status)} data-testid="claim-status">{STATUS_LABELS[c.status] ?? c.status}</Badge>
        <Badge tone={PRIORITY_TONE[c.priority]}>{humanize(c.priority)} priority</Badge>
        {c.is_synthetic ? <Badge><FlaskConical className="size-3" aria-hidden />Synthetic</Badge> : null}
      </div>
      <div role="tablist" aria-label="Claim sections" className="flex gap-1 overflow-x-auto border-b border-border [scrollbar-width:thin]">
        {TABS.map((t) => {
          const available = t.phase <= 7 || t.key === "decision" || t.key === "reports"; // Financial Assessment (P8 tab) not built yet
          return (
            <button key={t.key} role="tab" type="button" aria-selected={tab === t.key} disabled={!available}
              title={available ? undefined : `Delivered in Phase ${t.phase}`} onClick={() => setTab(t.key)}
              className={cn("-mb-px whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium",
                tab === t.key ? "border-primary text-foreground" : "border-transparent text-muted-foreground hover:text-foreground",
                !available && "cursor-not-allowed opacity-50 hover:text-muted-foreground")}>
              {t.label}{available ? null : <span className="ml-1.5 text-[10px] uppercase">P{t.phase}</span>}
            </button>
          );
        })}
      </div>
      {tab === "overview" ? <Overview claim={c} /> : tab === "documents" ? (
        <DocumentsPanel claimId={c.id} canEdit={c.can_edit} />
      ) : tab === "analysis" ? (
        <ProcessingPanel claimId={c.id} claimStatus={c.status} />
      ) : tab === "agents" ? (
        <AgentRunsPanel claimId={c.id} />
      ) : tab === "coverage" ? (
        <CoveragePanel claimId={c.id} />
      ) : tab === "fraud" ? (
        <div className="space-y-6">
          <FraudPanel claimId={c.id} />
          <SimilarClaims claimId={c.id} />
        </div>
      ) : tab === "decision" ? (
        <DecisionPanel claimId={c.id} />
      ) : tab === "reports" ? (
        <ReportsPanel claimId={c.id} />
      ) : (
        <div className="grid gap-6 lg:grid-cols-2">
          <TimelinePanel claimId={c.id} />
          <NotesPanel claimId={c.id} />
        </div>
      )}
    </div>
  );
}
