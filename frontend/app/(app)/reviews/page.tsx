import type { Metadata } from "next";

import { ReviewQueue } from "@/features/reviews/review-queue";

export const metadata: Metadata = { title: "Human Review" };

export default function ReviewsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Human Review</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Claims waiting for a person, highest priority first. Decisions are checked against your approval authority;
          large approvals, high fraud risk and overrides of the recommendation need a second approver.
        </p>
      </div>
      <ReviewQueue />
    </div>
  );
}
