"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";

import { ApiError, apiGet, apiRequest, noContent } from "@/lib/api/client";

import { meSchema, type Me } from "./schema";

export const ME_QUERY_KEY = ["auth", "me"] as const;

export function useMe() {
  return useQuery({
    queryKey: ME_QUERY_KEY,
    queryFn: ({ signal }) => apiGet("/auth/me", meSchema, { signal }),
    retry: (count, error) => !(error instanceof ApiError && error.status === 401) && count < 1,
    staleTime: 60_000,
  });
}

export function hasPermission(me: Me | undefined, permission: string): boolean {
  return me?.permissions.includes(permission) ?? false;
}

export function useSignOut() {
  const queryClient = useQueryClient();
  const router = useRouter();
  return useMutation({
    mutationFn: () => apiRequest("POST", "/auth/logout", noContent),
    onSettled: () => {
      queryClient.clear();
      router.replace("/login");
    },
  });
}

/** Only allow same-site relative redirects after sign-in (prevents open redirects). */
export function safeNextPath(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return "/";
  return next;
}
