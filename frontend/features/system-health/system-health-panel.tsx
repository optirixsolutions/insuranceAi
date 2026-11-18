"use client";

import { AlertTriangle, RefreshCw } from "lucide-react";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/client";

import { COMPONENT_LABELS, type ComponentStatus } from "./schema";
import { useReadiness } from "./use-readiness";

const STATUS_TONE: Record<ComponentStatus, BadgeTone> = { ok: "success", degraded: "warning", down: "danger" };
const STATUS_LABEL: Record<ComponentStatus, string> = {
  ok: "Operational",
  degraded: "Degraded",
  down: "Unavailable",
};

const timeFormatter = new Intl.DateTimeFormat(undefined, { timeStyle: "medium" });

export function SystemHealthPanel() {
  const { data, error, isPending, isFetching, refetch } = useReadiness();

  return (
    <Card aria-labelledby="system-health-title">
      <CardHeader>
        <div>
          <CardTitle id="system-health-title">System health</CardTitle>
          <CardDescription>
            Live readiness of platform dependencies, reported by <code className="font-mono text-xs">/api/v1/health/ready</code>.
          </CardDescription>
        </div>
        <div className="flex items-center gap-3">
          {data ? (
            <Badge tone={STATUS_TONE[data.status]} data-testid="overall-status">
              {STATUS_LABEL[data.status]}
            </Badge>
          ) : null}
          <Button variant="secondary" size="sm" onClick={() => void refetch()} disabled={isFetching}>
            <RefreshCw className={isFetching ? "animate-spin" : undefined} aria-hidden />
            Refresh
          </Button>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        {isPending ? (
          <div className="space-y-3 px-5 py-4" aria-label="Loading system health">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-6" />
            ))}
          </div>
        ) : error && !data ? (
          <div role="alert" className="flex items-start gap-3 px-5 py-4 text-sm text-danger">
            <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
            <div>
              <p className="font-medium">Health information is unavailable.</p>
              <p className="text-muted-foreground">
                {error instanceof ApiError ? error.message : "Unexpected error"}
                {error instanceof ApiError && error.requestId ? ` (request ${error.requestId})` : null}
              </p>
            </div>
          </div>
        ) : data ? (
          <>
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <th scope="col" className="px-5 py-2 font-medium">Component</th>
                  <th scope="col" className="px-5 py-2 font-medium">Status</th>
                  <th scope="col" className="hidden px-5 py-2 font-medium sm:table-cell">Detail</th>
                  <th scope="col" className="px-5 py-2 text-right font-medium">Latency</th>
                </tr>
              </thead>
              <tbody>
                {data.components.map((component) => (
                  <tr key={component.name} className="border-b border-border last:border-0" data-testid={`component-${component.name}`}>
                    <td className="px-5 py-3 font-medium">
                      {COMPONENT_LABELS[component.name] ?? component.name}
                      {!component.required ? <span className="ml-2 text-xs font-normal text-muted-foreground">optional</span> : null}
                    </td>
                    <td className="px-5 py-3">
                      <Badge tone={STATUS_TONE[component.status]}>{STATUS_LABEL[component.status]}</Badge>
                    </td>
                    <td className="hidden px-5 py-3 text-muted-foreground sm:table-cell">{component.detail ?? "—"}</td>
                    <td className="px-5 py-3 text-right font-mono text-xs tabular-nums text-muted-foreground">
                      {component.latency_ms.toFixed(1)} ms
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="border-t border-border px-5 py-3 text-xs text-muted-foreground">
              Version {data.version} · environment {data.environment} · checked{" "}
              {timeFormatter.format(new Date(data.checked_at))}
              {error ? " · last refresh failed, showing previous result" : null}
            </p>
          </>
        ) : null}
      </CardContent>
    </Card>
  );
}
