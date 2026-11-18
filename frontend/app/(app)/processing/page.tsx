import type { Metadata } from "next";

import { ProcessingList } from "@/features/processing/processing-list";

export const metadata: Metadata = { title: "AI Processing" };

export default function ProcessingPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">AI Processing</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Checkpointed processing runs on your claims. Runs pause for people at document review, information requests and
          human review, and resume from their last checkpoint after a failure.
        </p>
      </div>
      <ProcessingList />
    </div>
  );
}
