"use client";

import { ChevronLeft, ChevronRight, Download, RefreshCw } from "lucide-react";
import { useState } from "react";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Input, Select } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { humanize } from "@/features/claims/format";
import { API_BASE, ApiError } from "@/lib/api/client";
import { cn } from "@/lib/utils";

import {
  useCorrectField,
  useDocumentTypes,
  useFields,
  usePages,
  useReprocess,
  useSetType,
  useVerifyField,
  type ClaimDocument,
  type ExtractedField,
} from "./api";

const STATUS_TONE: Record<string, BadgeTone> = {
  verified: "success", corrected: "info", unverified: "neutral", low_confidence: "warning", conflict: "danger", rejected: "danger",
};
const errorText = (e: unknown) => (e instanceof ApiError ? e.userMessage : "Something went wrong.");

function displayValue(field: ExtractedField): string {
  const v = field.value_normalized as Record<string, string> | string | null;
  if (v && typeof v === "object" && "amount" in v) return `${v.currency ?? "?"} ${v.amount}`;
  return typeof v === "string" ? v : (field.value_text ?? "—");
}

function FieldRow({ doc, field, selected, onSelect, canEdit }: {
  doc: ClaimDocument; field: ExtractedField; selected: boolean; onSelect: () => void; canEdit: boolean;
}) {
  const correct = useCorrectField(doc);
  const verify = useVerifyField(doc);
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(field.value_text ?? "");
  const [reason, setReason] = useState("");
  return (
    <li className={cn("rounded-md border p-3 text-sm", selected ? "border-primary bg-primary/5" : "border-border")}>
      <button type="button" onClick={onSelect} className="w-full text-left" aria-pressed={selected}>
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{humanize(field.field_name)}</span>
          <Badge tone={STATUS_TONE[field.validation_status] ?? "neutral"}>{humanize(field.validation_status)}</Badge>
        </div>
        <p className="mt-1 font-medium">{displayValue(field)}</p>
        <p className="mt-0.5 text-xs text-muted-foreground">
          {field.source === "manual" ? "Entered by a person" : `Confidence ${(field.confidence * 100).toFixed(0)}% (model, not verified accuracy)`}
          {field.page_number ? ` · page ${field.page_number}` : ""}
        </p>
        {field.evidence_excerpt ? <p className="mt-1 rounded bg-surface-muted px-2 py-1 font-mono text-[11px]">“{field.evidence_excerpt}”</p> : null}
      </button>
      {canEdit && !editing ? (
        <div className="mt-2 flex gap-2">
          <Button size="sm" variant="secondary" onClick={() => setEditing(true)}>Correct</Button>
          {field.validation_status !== "verified" ? (
            <Button size="sm" variant="ghost" disabled={verify.isPending} onClick={() => verify.mutate(field.id)}>Mark verified</Button>
          ) : null}
        </div>
      ) : null}
      {editing ? (
        <form className="mt-2 space-y-2" onSubmit={(e) => {
          e.preventDefault();
          correct.mutate({ fieldId: field.id, value, reason }, { onSuccess: () => setEditing(false) });
        }}>
          {correct.error ? <Alert>{errorText(correct.error)}</Alert> : null}
          <Input aria-label={`Corrected ${humanize(field.field_name)}`} value={value} onChange={(e) => setValue(e.target.value)} />
          <Input aria-label="Reason for correction" placeholder="Reason (audited)" value={reason} onChange={(e) => setReason(e.target.value)} />
          <div className="flex gap-2">
            <Button size="sm" type="submit" disabled={correct.isPending || reason.trim().length < 3 || !value.trim()}>Save correction</Button>
            <Button size="sm" variant="ghost" type="button" onClick={() => setEditing(false)}>Cancel</Button>
          </div>
        </form>
      ) : null}
    </li>
  );
}

export function DocumentWorkspace({ doc, canEdit }: { doc: ClaimDocument; canEdit: boolean }) {
  const ready = doc.processing_status === "processed" || doc.processing_status === "needs_review";
  const pages = usePages(doc.id, ready);
  const fields = useFields(doc.id, ready);
  const types = useDocumentTypes();
  const setType = useSetType(doc);
  const reprocess = useReprocess(doc);
  const [pageNumber, setPageNumber] = useState(1);
  const [selected, setSelected] = useState<ExtractedField | null>(null);
  const [showText, setShowText] = useState(false);
  const page = pages.data?.find((p) => p.page_number === pageNumber);
  const highlight = selected?.page_number === pageNumber ? selected.bbox : null;

  return (
    <Card>
      <CardHeader>
        <div className="min-w-0">
          <CardTitle className="truncate">{doc.original_filename}</CardTitle>
          <p className="mt-1 text-xs text-muted-foreground">
            {doc.page_count ?? 0} page(s) · quality {doc.quality_score !== null ? `${Math.round(doc.quality_score * 100)}%` : "—"} · scan {humanize(doc.scan_status)}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="secondary" asChild>
            <a href={`${API_BASE}/documents/${doc.id}/content`} download><Download aria-hidden />Original</a>
          </Button>
          {canEdit ? (
            <Button size="sm" variant="ghost" disabled={reprocess.isPending || !ready} onClick={() => reprocess.mutate()}>
              <RefreshCw aria-hidden />Reprocess
            </Button>
          ) : null}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {doc.review_reasons.length ? (
          <Alert>Needs review: {doc.review_reasons.map(humanize).join(", ")}.</Alert>
        ) : null}
        {doc.duplicates.length ? (
          <Alert>
            Possible duplicate of {doc.duplicates.map((d) => `${d.claim_reference}${d.same_claim ? " (this claim)" : ""} via ${humanize(d.method)}`).join("; ")}.
          </Alert>
        ) : null}
        {doc.error_detail ? <Alert>{doc.error_detail}</Alert> : null}
        {canEdit ? (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-muted-foreground">Type:</span>
            <Select aria-label="Document type" className="w-56" value={doc.document_type ?? ""} disabled={setType.isPending}
              onChange={(e) => e.target.value && setType.mutate({ document_type: e.target.value, reason: "Set by reviewer" })}>
              <option value="">Unclassified</option>
              {types.data?.map((t) => <option key={t} value={t}>{humanize(t)}</option>)}
            </Select>
            <span className="text-xs text-muted-foreground">
              {doc.classification_source === "manual" ? "set by a person" : doc.classification_confidence !== null ? `rules, ${Math.round(doc.classification_confidence * 100)}%` : ""}
            </span>
          </div>
        ) : null}
        {!ready ? <p className="text-sm text-muted-foreground">{doc.processing_status === "queued" || doc.processing_status === "processing" ? "Processing…" : "No content available."}</p> : (
          <div className="grid gap-4 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
            <div className="space-y-2">
              <div className="flex items-center justify-between text-sm">
                <div className="flex items-center gap-1">
                  <Button size="icon" variant="ghost" aria-label="Previous page" disabled={pageNumber <= 1} onClick={() => setPageNumber((n) => n - 1)}><ChevronLeft aria-hidden /></Button>
                  <span>Page {pageNumber} of {doc.page_count ?? 1}</span>
                  <Button size="icon" variant="ghost" aria-label="Next page" disabled={pageNumber >= (doc.page_count ?? 1)} onClick={() => setPageNumber((n) => n + 1)}><ChevronRight aria-hidden /></Button>
                </div>
                <label className="flex items-center gap-2 text-xs text-muted-foreground">
                  <input type="checkbox" checked={showText} onChange={(e) => setShowText(e.target.checked)} />Show extracted text
                </label>
              </div>
              {page ? (
                <p className="text-xs text-muted-foreground">
                  {page.text_source === "text_layer" ? "Text layer" : page.text_source === "docx" ? "Word document" : `OCR (${page.text_source})`}
                  {page.ocr_confidence !== null ? ` · OCR confidence ${Math.round(page.ocr_confidence * 100)}%` : ""}
                  {page.rotation_applied ? ` · rotated ${page.rotation_applied}°` : ""}
                  {page.quality_flags.some((f) => !f.startsWith("rotated_")) ? ` · ${page.quality_flags.filter((f) => !f.startsWith("rotated_")).map(humanize).join(", ")}` : ""}
                </p>
              ) : null}
              {showText || !page?.has_image ? (
                <pre className="max-h-[70vh] overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-muted p-3 font-mono text-xs" data-testid="page-text">
                  {pages.isPending ? "Loading…" : page?.text || "(no text found on this page)"}
                </pre>
              ) : (
                <div className="relative overflow-hidden rounded-md border border-border bg-surface-muted">
                  {/* eslint-disable-next-line @next/next/no-img-element -- authenticated API image, not a static asset */}
                  <img src={`${API_BASE}/documents/${doc.id}/pages/${pageNumber}/image`} alt={`Page ${pageNumber} of ${doc.original_filename}`} className="block w-full" />
                  {highlight ? (
                    <div data-testid="evidence-highlight" aria-hidden className="pointer-events-none absolute rounded-sm border-2 border-accent bg-accent/20"
                      style={{ left: `${highlight[0]! * 100}%`, top: `${highlight[1]! * 100}%`, width: `${(highlight[2]! - highlight[0]!) * 100}%`, height: `${(highlight[3]! - highlight[1]!) * 100}%` }} />
                  ) : null}
                </div>
              )}
            </div>
            <div className="space-y-2">
              <h3 className="text-sm font-semibold">Extracted fields</h3>
              {fields.isPending ? <Skeleton className="h-32" /> : fields.data?.length === 0 ? (
                <p className="text-sm text-muted-foreground">No fields were recognised. Values can be entered after review.</p>
              ) : (
                <ul className="space-y-2" data-testid="extracted-fields">
                  {fields.data?.map((f) => (
                    <FieldRow key={f.id} doc={doc} field={f} canEdit={canEdit} selected={selected?.id === f.id}
                      onSelect={() => { setSelected(f); if (f.page_number) setPageNumber(f.page_number); }} />
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
