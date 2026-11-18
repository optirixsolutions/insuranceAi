import type { Metadata } from "next";
import { Suspense } from "react";

import { ClaimsList } from "@/features/claims/claims-list";

export const metadata: Metadata = { title: "Claims" };

export default function ClaimsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Claims</h1>
        <p className="mt-1 text-sm text-muted-foreground">Every claim you are allowed to see, straight from the database.</p>
      </div>
      <Suspense>
        <ClaimsList />
      </Suspense>
    </div>
  );
}
