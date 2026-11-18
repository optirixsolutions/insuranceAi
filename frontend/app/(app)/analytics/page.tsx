import type { Metadata } from "next";

import { AnalyticsView } from "@/features/dashboard/analytics-view";

export const metadata: Metadata = { title: "Analytics" };

export default function AnalyticsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Analytics</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Processing times, agent and model monitoring, fraud and extraction quality for the claims you can see.
          Every figure is one database query; its definition is shown with it.
        </p>
      </div>
      <AnalyticsView />
    </div>
  );
}
