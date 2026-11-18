import type { Metadata } from "next";

import { PolicyManagement } from "@/features/policies/policy-management";

export const metadata: Metadata = { title: "Policy Management" };

export default function PoliciesPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Policy Management</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Structured, versioned policy terms are the source of truth for coverage decisions.
        </p>
      </div>
      <PolicyManagement />
    </div>
  );
}
