"use client";

import { LogOut } from "lucide-react";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/client";

import { ROLE_LABELS } from "./schema";
import { useMe, useSignOut } from "./use-me";
import { NotificationBell } from "@/features/communications/notifications";

/** Renders children only for an authenticated session; sends everyone else to sign-in. */
export function AuthGate({ children }: { children: ReactNode }) {
  const { data, error, isPending } = useMe();
  const router = useRouter();
  const pathname = usePathname();
  const unauthenticated = error instanceof ApiError && error.status === 401;

  useEffect(() => {
    if (unauthenticated) router.replace(pathname === "/" ? "/login" : `/login?next=${encodeURIComponent(pathname)}`);
  }, [unauthenticated, pathname, router]);

  if (isPending || unauthenticated) {
    return (
      <div className="space-y-4" aria-label="Loading">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-40" />
      </div>
    );
  }
  if (error || !data) {
    return (
      <p role="alert" className="text-sm text-danger">
        Your account could not be loaded. {error instanceof ApiError ? error.message : null}
      </p>
    );
  }
  return <>{children}</>;
}

export function UserMenu() {
  const { data } = useMe();
  const signOut = useSignOut();
  if (!data) return null;
  const role = data.roles.map((r) => ROLE_LABELS[r] ?? r).join(", ");
  return (
    <div className="ml-auto flex items-center gap-3">
      <NotificationBell />
      <div className="hidden text-right sm:block">
        <p className="text-sm font-medium leading-tight">{data.user.display_name}</p>
        <p className="text-xs leading-tight text-muted-foreground">
          {data.organization.name} · {role}
        </p>
      </div>
      <Button variant="ghost" size="sm" onClick={() => signOut.mutate()} disabled={signOut.isPending}>
        <LogOut aria-hidden />
        <span className="hidden sm:inline">Sign out</span>
        <span className="sr-only sm:hidden">Sign out</span>
      </Button>
    </div>
  );
}
