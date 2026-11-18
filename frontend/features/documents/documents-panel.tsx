"use client";

import { FileUp, Loader2 } from "lucide-react";
import { useRef, useState, type DragEvent } from "react";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Alert } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { hasPermission, useMe } from "@/features/auth/use-me";
import { humanize } from "@/features/claims/format";
import { ApiError } from "@/lib/api/client";
import { cn } from "@/lib/utils";

import { useClaimDocuments, useUploadDocuments, type UploadResult } from "./api";
import { DocumentWorkspace } from "./document-workspace";

const STATUS_TONE: Record<string, BadgeTone> = {
  queued: "neutral", processing: "info", processed: "success", needs_review: "warning", failed: "danger", rejected: "danger",
};
const ACCEPT = ".pdf,.png,.jpg,.jpeg,.tif,.tiff,.docx,application/pdf,image/png,image/jpeg,image/tiff";

export function DocumentsPanel({ claimId, canEdit }: { claimId: string; canEdit: boolean }) {
  const docs = useClaimDocuments(claimId);
  const upload = useUploadDocuments(claimId);
  const me = useMe().data;
  const canUpload = canEdit && hasPermission(me, "documents.upload");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [results, setResults] = useState<UploadResult[]>([]);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const selected = docs.data?.find((d) => d.id === selectedId) ?? docs.data?.[0] ?? null;

  function send(list: FileList | null) {
    if (!list?.length) return;
    upload.mutate(Array.from(list), { onSuccess: setResults });
  }
  function onDrop(event: DragEvent) {
    event.preventDefault();
    setDragging(false);
    send(event.dataTransfer.files);
  }

  return (
    <div className="space-y-4">
      {canUpload ? (
        <div
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={cn("flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed px-6 py-8 text-center transition-colors",
            dragging ? "border-primary bg-primary/5" : "border-border bg-surface")}
          data-testid="dropzone"
        >
          {upload.isPending ? <Loader2 className="size-6 animate-spin text-primary" aria-hidden /> : <FileUp className="size-6 text-muted-foreground" aria-hidden />}
          <p className="text-sm font-medium">Drop claim documents here or{" "}
            <button type="button" className="text-primary underline-offset-2 hover:underline" onClick={() => input.current?.click()}>choose files</button>
          </p>
          <p className="text-xs text-muted-foreground">PDF, PNG, JPEG, TIFF or DOCX · up to 25 MB each · scanned for malware before processing</p>
          <input ref={input} type="file" multiple accept={ACCEPT} className="sr-only" aria-label="Upload documents"
            onChange={(e) => { send(e.target.files); e.target.value = ""; }} />
        </div>
      ) : null}
      {upload.error ? <Alert>{upload.error instanceof ApiError ? upload.error.userMessage : "Upload failed."}</Alert> : null}
      {results.some((r) => !r.accepted) ? (
        <Alert>
          {results.filter((r) => !r.accepted).map((r) => <span key={r.filename} className="block">{r.filename}: {r.error}</span>)}
        </Alert>
      ) : null}

      {docs.isPending ? <Skeleton className="h-24" /> : docs.data?.length === 0 ? (
        <p className="text-sm text-muted-foreground">No documents yet.</p>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,3fr)]">
          <Card className="h-fit">
            <ul className="divide-y divide-border" data-testid="document-list">
              {docs.data?.map((d) => (
                <li key={d.id}>
                  <button type="button" onClick={() => setSelectedId(d.id)} aria-current={selected?.id === d.id ? "true" : undefined}
                    className={cn("w-full px-3 py-2.5 text-left text-sm hover:bg-surface-muted", selected?.id === d.id && "bg-surface-muted")}>
                    <p className="truncate font-medium">{d.original_filename}</p>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                      <Badge tone={STATUS_TONE[d.processing_status] ?? "neutral"}>{humanize(d.processing_status)}</Badge>
                      {d.document_type ? <span className="text-xs text-muted-foreground">{humanize(d.document_type)}</span> : null}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </Card>
          {selected ? <DocumentWorkspace key={selected.id} doc={selected} canEdit={canEdit} /> : null}
        </div>
      )}
    </div>
  );
}
