import type { Metadata } from "next";
import { Suspense } from "react";

import { ClaimDetailView } from "@/features/claims/claim-detail";

export const metadata: Metadata = { title: "Claim" };

export default async function ClaimPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <Suspense>
      <ClaimDetailView id={id} />
    </Suspense>
  );
}
