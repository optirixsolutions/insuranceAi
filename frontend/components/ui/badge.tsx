import { cva, type VariantProps } from "class-variance-authority";
import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset",
  {
    variants: {
      tone: {
        neutral: "bg-surface-muted text-muted-foreground ring-border",
        success: "bg-success-soft text-success ring-success/25",
        warning: "bg-warning-soft text-[oklch(0.5_0.12_70)] ring-warning/30",
        danger: "bg-danger-soft text-danger ring-danger/25",
        info: "bg-primary/8 text-primary ring-primary/20",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export type BadgeTone = NonNullable<VariantProps<typeof badgeVariants>["tone"]>;

export function Badge({
  className,
  tone,
  ...props
}: ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}
