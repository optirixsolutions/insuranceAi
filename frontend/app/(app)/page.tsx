import type { Metadata } from "next";

import { Dashboard } from "@/features/dashboard/dashboard";
import { SystemHealthPanel } from "@/features/system-health/system-health-panel";

export const metadata: Metadata = { title: "Overview" };

export default function OverviewPage() {
  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Overview</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Operational figures for the claims you can see. Every figure is read from the database with the query
          described under it; none are estimated, and a dash means there is no data yet.
        </p>
      </div>
      <Dashboard />
      <SystemHealthPanel />
    </div>
  );
}
