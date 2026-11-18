"use client";

import { Menu, X } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { useMe } from "@/features/auth/use-me";

import { NAV_GROUPS, isActive, isAvailable, isPermitted, type NavItem } from "./nav-items";

function NavEntry({ item, pathname }: { item: NavItem; pathname: string }) {
  const Icon = item.icon;
  const base = "flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors";

  if (!isAvailable(item)) {
    return (
      <span
        aria-disabled="true"
        title={`${item.label} is delivered in Phase ${item.phase}`}
        className={cn(base, "cursor-not-allowed text-chrome-muted/60")}
      >
        <Icon className="size-4 shrink-0" aria-hidden />
        <span className="flex-1 truncate">{item.label}</span>
        <span className="rounded border border-chrome-border px-1.5 text-[10px] font-medium uppercase tracking-wide">
          P{item.phase}
        </span>
      </span>
    );
  }

  const active = isActive(item, pathname);
  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      className={cn(
        base,
        active
          ? "bg-chrome-elevated text-white shadow-[inset_2px_0_0_var(--color-accent)]"
          : "text-chrome-foreground/85 hover:bg-chrome-elevated hover:text-white",
      )}
    >
      <Icon className="size-4 shrink-0" aria-hidden />
      <span className="truncate">{item.label}</span>
    </Link>
  );
}

function NavList({ pathname }: { pathname: string }) {
  const permissions = useMe().data?.permissions ?? [];
  return (
    <nav aria-label="Main navigation" className="flex-1 space-y-6 overflow-y-auto px-3 py-5">
      {NAV_GROUPS.map((group) => (
        <div key={group.label}>
          <p className="px-3 pb-2 text-[11px] font-semibold uppercase tracking-wider text-chrome-muted">
            {group.label}
          </p>
          <ul className="space-y-0.5">
            {group.items.filter((item) => isPermitted(item, permissions)).map((item) => (
              <li key={item.href}>
                <NavEntry item={item} pathname={pathname} />
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  );
}

function Brand() {
  return (
    <div className="flex h-14 items-center gap-2.5 border-b border-chrome-border px-5">
      <span
        aria-hidden
        className="grid size-7 place-items-center rounded-md bg-primary text-xs font-bold tracking-tight text-white"
      >
        C
      </span>
      <span className="text-[15px] font-semibold tracking-tight text-white">ClaimOS</span>
    </div>
  );
}

export function Sidebar() {
  const pathname = usePathname();
  return (
    <aside className="hidden w-64 shrink-0 flex-col border-r border-chrome-border bg-chrome lg:flex">
      <Brand />
      <NavList pathname={pathname} />
    </aside>
  );
}

/** Drawer navigation for viewports below the `lg` breakpoint. */
export function MobileNav() {
  const pathname = usePathname();
  // The drawer is tied to the route it was opened on, so navigating closes it without an effect.
  const [openedOn, setOpenedOn] = useState<string | null>(null);
  const open = openedOn === pathname;
  const setOpen = (value: boolean) => setOpenedOn(value ? pathname : null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && setOpenedOn(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <div className="lg:hidden">
      <Button variant="ghost" size="icon" aria-label="Open navigation" aria-expanded={open} onClick={() => setOpen(true)}>
        <Menu aria-hidden />
      </Button>
      {open ? (
        <div className="fixed inset-0 z-40 flex" role="dialog" aria-modal="true" aria-label="Navigation">
          <button
            type="button"
            aria-label="Close navigation"
            className="absolute inset-0 bg-[oklch(0.15_0.03_262/0.5)]"
            onClick={() => setOpen(false)}
          />
          <aside className="relative flex w-72 max-w-[85vw] flex-col bg-chrome">
            <div className="relative">
              <Brand />
              <button
                type="button"
                aria-label="Close navigation"
                onClick={() => setOpen(false)}
                className="absolute right-3 top-3.5 rounded-md p-1 text-chrome-muted hover:text-white"
              >
                <X className="size-5" aria-hidden />
              </button>
            </div>
            <NavList pathname={pathname} />
          </aside>
        </div>
      ) : null}
    </div>
  );
}
