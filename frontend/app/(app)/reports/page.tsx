import type { Metadata } from "next";

import { RecentReports } from "@/features/communications/reports";

export const metadata: Metadata = { title: "Reports" };

export default function ReportsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Reports</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Recent claim reports. Each version is immutable, stored with its SHA-256, and checked again before every
          download. Downloads are recorded in the audit log.
        </p>
      </div>
      <RecentReports />
    </div>
  );
}
