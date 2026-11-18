import type { Metadata } from "next";

import { AiAgents } from "@/features/ai/ai-agents";
import { EvaluationsAdmin } from "@/features/intelligence/components";

export const metadata: Metadata = { title: "AI Agents" };

export default function AgentsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">AI Agents</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Models are only used after their capabilities are verified by a probe. Real claim data only goes to models
          an administrator has approved for it.
        </p>
      </div>
      <AiAgents />
      <EvaluationsAdmin />
    </div>
  );
}
