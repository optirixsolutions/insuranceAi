import type { ReactNode } from "react";

import { AuthGate, UserMenu } from "@/features/auth/auth-gate";
import { MobileNav, Sidebar } from "@/features/navigation/sidebar";
import { EnvironmentBanner } from "@/features/system-health/environment-banner";

export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-dvh">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-md focus:bg-surface focus:px-3 focus:py-2"
      >
        Skip to content
      </a>
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <EnvironmentBanner />
        <header className="flex h-14 items-center gap-2 border-b border-border bg-surface px-4 sm:px-6">
          <MobileNav />
          <span className="text-sm font-semibold tracking-tight lg:hidden">ClaimOS</span>
          <UserMenu />
        </header>
        <main id="main" className="flex-1 px-4 py-8 sm:px-6">
          <div className="mx-auto w-full max-w-6xl">
            <AuthGate>{children}</AuthGate>
          </div>
        </main>
      </div>
    </div>
  );
}
