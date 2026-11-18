"use client";

import Link from "next/link";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateTime, humanize } from "@/features/claims/format";

import { useReviewQueue } from "./api";

const TONE: Record<string, BadgeTone> = { needs_review: "warning", failed: "danger", rejected: "danger" };

export function ReviewQueue() {
  const queue = useReviewQueue();
  return (
    <Card className="overflow-x-auto">
      <table className="w-full min-w-[760px] text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
            <th scope="col" className="px-4 py-2.5 font-medium">Document</th>
            <th scope="col" className="px-4 py-2.5 font-medium">Claim</th>
            <th scope="col" className="px-4 py-2.5 font-medium">Status</th>
            <th scope="col" className="px-4 py-2.5 font-medium">Why</th>
            <th scope="col" className="px-4 py-2.5 font-medium">Uploaded</th>
          </tr>
        </thead>
        <tbody>
          {queue.isPending ? (
            <tr><td colSpan={5} className="p-4"><Skeleton className="h-24" /></td></tr>
          ) : queue.error ? (
            <tr><td colSpan={5} className="p-4 text-danger" role="alert">{queue.error.message}</td></tr>
          ) : queue.data.length === 0 ? (
            <tr><td colSpan={5} className="p-8 text-center text-muted-foreground">Nothing needs review.</td></tr>
          ) : (
            queue.data.map(({ document: d, claim_reference }) => (
              <tr key={d.id} className="border-b border-border last:border-0">
                <td className="px-4 py-3">
                  <p className="font-medium">{d.original_filename}</p>
                  <p className="text-xs text-muted-foreground">{d.document_type ? humanize(d.document_type) : "Unclassified"}</p>
                </td>
                <td className="px-4 py-3">
                  <Link href={`/claims/${d.claim_id}?tab=documents`} className="font-mono text-primary hover:underline">{claim_reference}</Link>
                </td>
                <td className="px-4 py-3"><Badge tone={TONE[d.processing_status] ?? "neutral"}>{humanize(d.processing_status)}</Badge></td>
                <td className="px-4 py-3 text-xs">{d.error_detail ?? (d.review_reasons.map(humanize).join(", ") || "—")}</td>
                <td className="px-4 py-3 text-xs text-muted-foreground whitespace-nowrap">{formatDateTime(d.created_at)}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </Card>
  );
}
