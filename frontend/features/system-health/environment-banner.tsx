"use client";

import { FlaskConical } from "lucide-react";

import { ADAPTER_LABELS } from "./schema";
import { useReadiness } from "./use-readiness";

/** Persistent banner whenever the backend is not production or runs development adapters. */
export function EnvironmentBanner() {
  const { data } = useReadiness();
  if (!data) return null;
  const isProduction = data.environment === "production";
  if (isProduction && data.development_adapters.length === 0) return null;

  const adapters = data.development_adapters.map((a) => ADAPTER_LABELS[a] ?? a);
  return (
    <div
      role="status"
      data-testid="environment-banner"
      className="flex items-center gap-2 border-b border-warning/30 bg-warning-soft px-6 py-2 text-xs text-[oklch(0.42_0.1_70)]"
    >
      <FlaskConical className="size-3.5 shrink-0" aria-hidden />
      <span>
        <strong className="font-semibold capitalize">{data.environment}</strong> environment. Use synthetic data only.
        {adapters.length > 0 ? ` Development adapters active: ${adapters.join(", ")}.` : null}
      </span>
    </div>
  );
}
