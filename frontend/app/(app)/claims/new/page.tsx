import type { Metadata } from "next";

import { NewClaimForm } from "@/features/claims/new-claim-form";

export const metadata: Metadata = { title: "New claim" };

export default function NewClaimPage() {
  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">New claim</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Intake checks run on submission: policy ownership, the version in force, amount, duplicates and priority.
        </p>
      </div>
      <NewClaimForm />
    </div>
  );
}
