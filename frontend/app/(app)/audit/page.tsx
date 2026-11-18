import type { Metadata } from "next";

import { AuditLog } from "@/features/admin/audit-log";

export const metadata: Metadata = { title: "Audit Logs" };

export default function AuditPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Audit Logs</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Append-only record of every security-relevant action, protected by a tamper-evident hash chain.
        </p>
      </div>
      <AuditLog />
    </div>
  );
}
