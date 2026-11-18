import type { Metadata } from "next";

import { IntegrationsAdmin } from "@/features/communications/integrations";

export const metadata: Metadata = { title: "Integrations" };

export default function IntegrationsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Integrations</h1>
        <p className="mt-1 text-sm text-muted-foreground">Outbound webhooks and the email and SMS adapters used for notifications.</p>
      </div>
      <IntegrationsAdmin />
    </div>
  );
}
