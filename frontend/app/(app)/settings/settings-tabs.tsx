"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { hasPermission, useMe } from "@/features/auth/use-me";
import { cn } from "@/lib/utils";

const TABS = [
  { href: "/settings", label: "Organisation", permission: null },
  { href: "/settings/security", label: "Security", permission: null },
  { href: "/settings/users", label: "Users", permission: "users.manage" },
  { href: "/settings/rules", label: "Products & rules", permission: "products.manage" },
] as const;

export function SettingsTabs() {
  const pathname = usePathname();
  const me = useMe().data;
  return (
    <nav aria-label="Settings sections" className="flex gap-1 border-b border-border">
      {TABS.filter((t) => !t.permission || hasPermission(me, t.permission)).map((tab) => {
        const active = pathname === tab.href;
        return (
          <Link
            key={tab.href}
            href={tab.href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors",
              active ? "border-primary text-foreground" : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
