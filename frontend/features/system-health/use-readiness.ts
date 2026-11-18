"use client";

import { useQuery } from "@tanstack/react-query";

import { apiGet } from "@/lib/api/client";

import { readinessSchema } from "./schema";

export const READINESS_REFRESH_MS = 10_000;

export function useReadiness() {
  return useQuery({
    queryKey: ["system", "readiness"],
    // 503 still carries a valid readiness report (a required component is down).
    queryFn: ({ signal }) => apiGet("/health/ready", readinessSchema, { signal, acceptStatuses: [503] }),
    refetchInterval: READINESS_REFRESH_MS,
    refetchIntervalInBackground: false,
    retry: 1,
  });
}
