"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Field, Input, Select } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { hasPermission, useMe } from "@/features/auth/use-me";
import { formatDate, formatDateTime, humanize } from "@/features/claims/format";
import { ApiError } from "@/lib/api/client";

import { OPEN_STATUSES, useDeciders, useReviewAction, useWorkspace, type ReviewAction, type Workspace } from "./api";
import { money, REVIEW_TONE, RISK_TONE } from "./review-queue";

const errorText = (e: unknown) => (e instanceof ApiError ? e.userMessage : "Something went wrong.");
/** Which recommendation each action agrees with (mirrors rules_engine/review.py AGREES). */
const AGREES: Record<string, string[]> = {
  approve: ["APPROVE_RECOMMENDED"],
  reject: ["REJECT_RECOMMENDED"],
  "request-information": ["MORE_INFORMATION_REQUIRED", "PENDING_VERIFICATION"],
  return: ["MANUAL_REVIEW_REQUIRED"],
};
const TABS = [
  { key: "approve", label: "Approve" },
  { key: "reject", label: "Reject" },
  { key: "request-information", label: "Request information" },
  { key: "return", label: "Return for reassessment" },
  { key: "escalate", label: "Escalate" },
] as const;
type Tab = (typeof TABS)[number]["key"];
const REJECT_CODES = ["EXCLUSION", "NOT_COVERED", "POLICY_INACTIVE", "FRAUD_CONFIRMED", "INSUFFICIENT_EVIDENCE", "OTHER"];

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-sm">{children}</dd>
    </div>
  );
}

function Summary({ ws }: { ws: Workspace }) {
  const r = ws.review;
  return (
    <Card>
      <CardHeader><CardTitle>Recommendation</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <dl className="grid gap-3 sm:grid-cols-3">
          <Fact label="Recommendation">
            <strong data-testid="review-recommendation">{r.recommendation ? humanize(r.recommendation) : "None (escalated without one)"}</strong>
            {ws.matrix_rule ? <span className="ml-1 text-xs text-muted-foreground">rule {ws.matrix_rule}</span> : null}
          </Fact>
          <Fact label="Calculated payout">{r.payout ? money(r.payout, r.currency) : "Not calculated"}</Fact>
          <Fact label="Claimed">{money(r.claimed_amount, r.currency)}</Fact>
          <Fact label="Fraud risk">{r.fraud_level ? <Badge tone={RISK_TONE[r.fraud_level] ?? "neutral"}>{humanize(r.fraud_level)}</Badge> : "—"}</Fact>
          <Fact label="Priority">{r.priority_score}</Fact>
          <Fact label="Due">{r.due_at ? formatDateTime(r.due_at) : "—"}{r.overdue ? <span className="ml-1 text-danger">overdue</span> : null}</Fact>
        </dl>
        {ws.recommendation_explanation ? <p className="text-sm">{ws.recommendation_explanation}</p> : null}
        {[...r.reasons, ...ws.contradictions].length ? (
          <div>
            <h4 className="text-xs font-medium text-muted-foreground">Why a person must decide</h4>
            <ul className="mt-1 list-disc pl-5 text-sm" data-testid="review-reasons">
              {[...r.reasons, ...ws.contradictions].map((x) => <li key={x}>{x}</li>)}
            </ul>
          </div>
        ) : null}
        <div>
          <h4 className="text-xs font-medium text-muted-foreground">Claim description</h4>
          <p className="mt-1 whitespace-pre-wrap text-sm">{ws.claim_description}</p>
        </div>
      </CardContent>
    </Card>
  );
}

function Authorities({ ws }: { ws: Workspace }) {
  return (
    <Card>
      <CardHeader><CardTitle>Your decision authority</CardTitle></CardHeader>
      <CardContent>
        {ws.my_authorities.length === 0 ? (
          <p className="text-sm text-muted-foreground" data-testid="no-authority">
            You have no approval authority. An administrator can grant it under Settings → Users.
          </p>
        ) : (
          <ul className="space-y-1 text-sm">
            {ws.my_authorities.map((a, i) => (
              <li key={i}>
                Up to <strong>{money(a.max_approval_amount, a.currency)}</strong>
                {a.product_id ? " (one product)" : " (all products)"}
                {a.can_reject ? " · can reject" : ""}{a.can_override_fraud ? " · fraud override" : ""}
                <span className="text-muted-foreground"> · from {formatDate(a.valid_from)}{a.valid_to ? ` to ${formatDate(a.valid_to)}` : ""}</span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function SecondApproval({ ws, meId }: { ws: Workspace; meId: string | undefined }) {
  const act = useReviewAction(ws.review.claim_id, ws.review.id);
  const [note, setNote] = useState("");
  const p = ws.pending_decision!;
  const mine = p.first_decider === meId;
  return (
    <div className="space-y-3" data-testid="second-approval">
      <p className="text-sm">
        <strong>{ws.first_decider_name ?? "A reviewer"}</strong> decided to <strong>{p.decision === "approved" ? "approve" : "reject"}</strong>
        {p.approved_amount ? ` ${money(p.approved_amount, ws.review.currency)}` : ""}: {p.reason}
      </p>
      {p.override ? <p className="text-sm">Override reason: {p.override_reason}</p> : null}
      {mine ? (
        <Alert tone="success">Waiting for a second approver. Someone else with enough authority must confirm.</Alert>
      ) : (
        <>
          <Field id="second-note" label="Note"><Input id="second-note" value={note} maxLength={1000} onChange={(e) => setNote(e.target.value)} /></Field>
          <div className="flex gap-2">
            <Button size="sm" disabled={act.isPending} onClick={() => act.mutate({ action: "second-approval", body: { approve: true, note } })}>Confirm decision</Button>
            <Button size="sm" variant="secondary" disabled={act.isPending}
              onClick={() => act.mutate({ action: "second-approval", body: { approve: false, note } })}>Send back</Button>
          </div>
        </>
      )}
      {act.error ? <Alert>{errorText(act.error)}</Alert> : null}
    </div>
  );
}

function ActionForm({ ws, tab }: { ws: Workspace; tab: Tab }) {
  const r = ws.review;
  const act = useReviewAction(r.claim_id, r.id);
  const deciders = useDeciders(tab === "escalate");
  const [amount, setAmount] = useState(r.payout ?? "");
  const override = Boolean(r.recommendation) && tab !== "escalate" && !(AGREES[tab] ?? []).includes(r.recommendation!)
    || (tab === "approve" && r.payout !== null && amount !== "" && Number(amount) !== Number(r.payout));

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const d = new FormData(event.currentTarget);
    const text = (k: string) => String(d.get(k) ?? "").trim();
    const over = text("override_reason") || undefined;
    const bodies: Record<Tab, Record<string, unknown>> = {
      approve: { approved_amount: amount, reason: text("reason"), override_reason: over },
      reject: {
        reason_code: text("reason_code"), reason: text("reason"), override_reason: over,
        clause_ids: text("clause_ids").split(",").map((c) => c.trim()).filter(Boolean),
      },
      "request-information": {
        items: text("items").split("\n").map((c) => c.trim()).filter(Boolean), message: text("reason"), override_reason: over,
      },
      return: { reason: text("reason"), instructions: text("instructions"), override_reason: over },
      escalate: { to_user_id: text("to_user_id") || null, reason: text("reason") },
    };
    act.mutate({ action: tab as ReviewAction, body: bodies[tab] });
  }

  return (
    <form onSubmit={submit} className="space-y-3" data-testid={`form-${tab}`}>
      {tab === "approve" ? (
        <Field id="approved_amount" label={`Approved amount (${r.currency})`} hint={r.payout ? `Calculated payout: ${r.payout}` : undefined}>
          <Input id="approved_amount" name="approved_amount" inputMode="decimal" required value={amount} onChange={(e) => setAmount(e.target.value)} />
        </Field>
      ) : null}
      {tab === "reject" ? (
        <>
          <Field id="reason_code" label="Rejection reason code">
            <Select id="reason_code" name="reason_code">{REJECT_CODES.map((c) => <option key={c} value={c}>{humanize(c)}</option>)}</Select>
          </Field>
          <Field id="clause_ids" label="Policy clauses (comma separated)"><Input id="clause_ids" name="clause_ids" /></Field>
        </>
      ) : null}
      {tab === "request-information" ? (
        <Field id="items" label="Items needed (one per line)">
          <textarea id="items" name="items" required rows={3} className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm" />
        </Field>
      ) : null}
      {tab === "escalate" ? (
        <Field id="to_user_id" label="Escalate to">
          <Select id="to_user_id" name="to_user_id">
            <option value="">Back to the shared queue</option>
            {(deciders.data ?? []).map((u) => <option key={u.id} value={u.id}>{u.display_name}</option>)}
          </Select>
        </Field>
      ) : null}
      <Field id="reason" label={tab === "request-information" ? "Message to the claimant" : "Reason"}>
        <Input id="reason" name="reason" required maxLength={1000} />
      </Field>
      {tab === "return" ? (
        <Field id="instructions" label="Instructions for reassessment"><Input id="instructions" name="instructions" maxLength={2000} /></Field>
      ) : null}
      {tab !== "escalate" ? (
        <Field id="override_reason" label={override ? "Override reason (required)" : "Override reason"}
          hint={override ? `This differs from the recommendation${r.recommendation ? ` (${humanize(r.recommendation)})` : ""} or the calculated payout; a second approver will be needed for approvals and rejections.` : "Only needed when you disagree with the recommendation."}>
          <Input id="override_reason" name="override_reason" required={override} maxLength={1000} />
        </Field>
      ) : null}
      {act.error ? <Alert>{errorText(act.error)}</Alert> : null}
      {act.data ? <Alert tone="success">Saved: review is now {humanize(act.data.status)}{act.data.outcome ? ` (${humanize(act.data.outcome)})` : ""}.</Alert> : null}
      <Button type="submit" size="sm" variant={tab === "reject" ? "danger" : "primary"} disabled={act.isPending}>
        {TABS.find((t) => t.key === tab)?.label}
      </Button>
    </form>
  );
}

function Actions({ ws, meId, canDecide, canEscalate }: { ws: Workspace; meId: string | undefined; canDecide: boolean; canEscalate: boolean }) {
  const r = ws.review;
  const take = useReviewAction(r.claim_id, r.id);
  const [tab, setTab] = useState<Tab>("approve");
  if (!OPEN_STATUSES.includes(r.status)) {
    return <p className="text-sm" data-testid="review-closed">This review is {humanize(r.status)}{r.outcome ? `: ${humanize(r.outcome)}` : ""}.</p>;
  }
  if (r.status === "awaiting_second_approval" && ws.pending_decision) return <SecondApproval ws={ws} meId={meId} />;
  if (!ws.waiting) return <Alert>The processing run is no longer waiting for this review.</Alert>;
  const tabs = TABS.filter((t) => (t.key === "escalate" ? canEscalate : canDecide) && (t.key !== "return" || ws.actions.includes("reassess")));
  const heldByOther = r.status === "in_progress" && r.assigned_to !== meId;
  return (
    <div className="space-y-3">
      {canDecide && r.assigned_to !== meId ? (
        <div className="flex items-center gap-2">
          <Button size="sm" variant="secondary" disabled={take.isPending || heldByOther} onClick={() => take.mutate({ action: "claim" })}>Take ownership</Button>
          {heldByOther ? <span className="text-sm text-muted-foreground">{r.assigned_to_name} is working on this review.</span> : null}
          {take.error ? <Alert>{errorText(take.error)}</Alert> : null}
        </div>
      ) : null}
      {tabs.length === 0 ? <p className="text-sm text-muted-foreground">You can view this review but not act on it.</p> : (
        <>
          <div role="tablist" aria-label="Review actions" className="flex flex-wrap gap-1">
            {tabs.map((t) => (
              <button key={t.key} type="button" role="tab" aria-selected={tab === t.key} onClick={() => setTab(t.key)}
                className={`rounded-md border px-2.5 py-1 text-sm ${tab === t.key ? "border-primary bg-primary/10" : "border-border"}`}>
                {t.label}
              </button>
            ))}
          </div>
          <ActionForm key={tab} ws={ws} tab={tabs.some((t) => t.key === tab) ? tab : (tabs[0]?.key ?? "escalate")} />
        </>
      )}
    </div>
  );
}

export function DecisionHistory({ decisions }: { decisions: Workspace["decisions"] }) {
  if (!decisions.length) return <p className="text-sm text-muted-foreground">No decisions recorded yet.</p>;
  return (
    <ul className="space-y-2 text-sm" data-testid="decision-history">
      {decisions.map((d) => (
        <li key={d.id} className="rounded-md border border-border p-2">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={d.decision === "approved" ? "success" : d.decision === "rejected" ? "danger" : "info"}>{humanize(d.decision)}</Badge>
            <span>{d.decision_mode === "stp" ? "Straight-through" : "Human"}</span>
            {d.approved_amount ? <span className="tabular-nums">{money(d.approved_amount, d.currency)}</span> : null}
            {d.override ? <Badge tone="warning">Override</Badge> : null}
            {d.second_approver_id ? <Badge tone="info">Four-eyes</Badge> : null}
            <span className="ml-auto text-xs text-muted-foreground">{formatDateTime(d.created_at)}</span>
          </div>
          {d.reason ? <p className="mt-1">{d.reason_code ? `${humanize(d.reason_code)}: ` : ""}{d.reason}</p> : null}
          {d.override_reason ? <p className="text-muted-foreground">Override: {d.override_reason}</p> : null}
          {d.clause_ids.length ? <p className="text-muted-foreground">Clauses: {d.clause_ids.join(", ")}</p> : null}
        </li>
      ))}
    </ul>
  );
}

export function ReviewWorkspace({ claimId, reviewId, showClaimLink = true }: { claimId: string; reviewId: string; showClaimLink?: boolean }) {
  const ws = useWorkspace(claimId, reviewId);
  const me = useMe().data;
  if (ws.isPending) return <Skeleton className="h-64" />;
  if (ws.error) return <Alert>{ws.error instanceof ApiError && ws.error.status === 404 ? "This review does not exist or is not visible to you." : errorText(ws.error)}</Alert>;
  const w = ws.data;
  const r = w.review;
  return (
    <div className="space-y-4" data-testid="review-workspace">
      <div className="flex flex-wrap items-center gap-3">
        {showClaimLink ? <Link href={`/claims/${claimId}`} className="font-mono text-lg font-semibold underline-offset-2 hover:underline">{r.claim_reference}</Link> : null}
        <Badge tone={REVIEW_TONE[r.status] ?? "neutral"} data-testid="review-status">{humanize(r.status)}</Badge>
        {r.four_eyes_required ? <Badge tone="warning">Four-eyes</Badge> : null}
        <span className="text-sm text-muted-foreground">{r.assigned_to_name ? `Assigned to ${r.assigned_to_name}` : "Unassigned"}</span>
      </div>
      <div className="grid gap-4 lg:grid-cols-[1fr_24rem]">
        <div className="space-y-4">
          <Summary ws={w} />
          <Card>
            <CardHeader><CardTitle>Decision history</CardTitle></CardHeader>
            <CardContent><DecisionHistory decisions={w.decisions} /></CardContent>
          </Card>
        </div>
        <div className="space-y-4">
          <Card>
            <CardHeader><CardTitle>Decide</CardTitle></CardHeader>
            <CardContent>
              <Actions ws={w} meId={me?.user.id} canDecide={hasPermission(me, "reviews.decide")} canEscalate={hasPermission(me, "reviews.escalate")} />
            </CardContent>
          </Card>
          <Authorities ws={w} />
        </div>
      </div>
    </div>
  );
}
