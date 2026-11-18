import type { Metadata } from "next";

import { PolicyDetailView } from "@/features/policies/policy-detail";

export const metadata: Metadata = { title: "Policy" };

export default async function PolicyPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <PolicyDetailView id={id} />;
}
