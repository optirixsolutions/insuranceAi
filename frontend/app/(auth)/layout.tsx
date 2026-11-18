import type { ReactNode } from "react";

export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col bg-chrome">
      <header className="flex h-14 items-center gap-2.5 px-6">
        <span aria-hidden className="grid size-7 place-items-center rounded-md bg-primary text-xs font-bold text-white">
          C
        </span>
        <span className="text-[15px] font-semibold tracking-tight text-white">ClaimOS</span>
      </header>
      <main className="flex flex-1 items-start justify-center px-4 pb-16 pt-[8vh]">
        <div className="w-full max-w-md rounded-lg border border-chrome-border bg-surface p-6 shadow-xl sm:p-8">
          {children}
        </div>
      </main>
    </div>
  );
}
