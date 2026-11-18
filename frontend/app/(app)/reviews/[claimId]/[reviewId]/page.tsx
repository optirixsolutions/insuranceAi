import type { Metadata } from "next";

import { ReviewWorkspace } from "@/features/reviews/review-workspace";

export const metadata: Metadata = { title: "Review" };

export default async function ReviewPage({ params }: { params: Promise<{ claimId: string; reviewId: string }> }) {
  const { claimId, reviewId } = await params;
  return <ReviewWorkspace claimId={claimId} reviewId={reviewId} />;
}
