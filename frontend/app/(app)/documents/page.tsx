import type { Metadata } from "next";

import { ReviewQueue } from "@/features/documents/review-queue";

export const metadata: Metadata = { title: "Document Intelligence" };

export default function DocumentsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Document Intelligence</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Documents that need a person: unreadable scans, uncertain classification, low-confidence fields,
          possible duplicates and rejected uploads.
        </p>
      </div>
      <ReviewQueue />
    </div>
  );
}
