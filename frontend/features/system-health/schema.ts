import { z } from "zod";

export const componentStatusSchema = z.enum(["ok", "degraded", "down"]);
export type ComponentStatus = z.infer<typeof componentStatusSchema>;

export const readinessSchema = z.object({
  status: componentStatusSchema,
  version: z.string(),
  environment: z.string(),
  checked_at: z.string(),
  components: z.array(
    z.object({
      name: z.string(),
      status: componentStatusSchema,
      required: z.boolean(),
      latency_ms: z.number(),
      detail: z.string().nullable(),
    }),
  ),
  development_adapters: z.array(z.string()),
});
export type Readiness = z.infer<typeof readinessSchema>;

export const COMPONENT_LABELS: Record<string, string> = {
  database: "PostgreSQL",
  migrations: "Database migrations",
  redis: "Redis",
  object_storage: "Object storage",
  workers: "Background workers",
};

export const ADAPTER_LABELS: Record<string, string> = {
  local_filesystem_storage: "Local filesystem storage",
  unencrypted_object_storage: "Object storage without server-side encryption",
};
