import type { Metadata } from "next";

import { AnomaliesPanel } from "@/features/intelligence/components";
import { ReviewQueue } from "@/features/reviews/review-queue";

export const metadata: Metadata = { title: "Fraud Investigation" };

export default function FraudPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Fraud Investigation</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Claims in human review whose fraud analysis scored high or critical. Approving one needs fraud-override
          authority and always a second approver. The fraud agent never rejects a claim on its own.
        </p>
      </div>
      <AnomaliesPanel />
      <ReviewQueue risk="high" />
    </div>
  );
}
