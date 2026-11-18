import type { ReactNode } from "react";

import { SettingsTabs } from "./settings-tabs";

export default function SettingsLayout({ children }: { children: ReactNode }) {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="mt-1 text-sm text-muted-foreground">Organisation profile and access management.</p>
      </div>
      <SettingsTabs />
      {children}
    </div>
  );
}
