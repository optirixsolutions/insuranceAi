"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";

import { OPEN_STATUSES, useClaimDecisions, useClaimReviews } from "./api";
import { DecisionHistory, ReviewWorkspace } from "./review-workspace";

/** Claim page "Decision" tab: the open review's workspace, or the recorded decisions. */
export function DecisionPanel({ claimId }: { claimId: string }) {
  const reviews = useClaimReviews(claimId);
  const decisions = useClaimDecisions(claimId);
  if (reviews.isPending || decisions.isPending) return <Skeleton className="h-48" />;
  if (reviews.error || decisions.error) return <Alert>Could not load decisions for this claim.</Alert>;
  const open = reviews.data.find((r) => OPEN_STATUSES.includes(r.status));
  if (open) return <ReviewWorkspace claimId={claimId} reviewId={open.id} showClaimLink={false} />;
  return (
    <Card>
      <CardHeader><CardTitle>Decisions</CardTitle></CardHeader>
      <CardContent><DecisionHistory decisions={decisions.data} /></CardContent>
    </Card>
  );
}
