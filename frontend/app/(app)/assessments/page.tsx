import type { Metadata } from "next";

import { AssessmentsList } from "@/features/dashboard/analytics-view";

export const metadata: Metadata = { title: "Assessments" };

export default function AssessmentsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Assessments</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          The latest financial assessment of each claim, with the rule-set version it was calculated under.
        </p>
      </div>
      <AssessmentsList />
    </div>
  );
}
