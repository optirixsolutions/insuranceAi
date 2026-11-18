"use client";

/** Small dependency-free charts. Every chart also renders its data as text (labels and values), so
 * nothing is conveyed by shape or colour alone, and screen readers get the same numbers. */

type Tone = "primary" | "success" | "warning" | "danger" | "muted";
const FILL: Record<Tone, string> = {
  primary: "fill-primary",
  success: "fill-success",
  warning: "fill-warning",
  danger: "fill-danger",
  muted: "fill-muted-foreground",
};

export type Bar = { label: string; value: number; tone?: Tone };

/** Horizontal bars with the value printed beside each bar. */
export function BarList({ bars, label, format = (v) => String(v) }: { bars: Bar[]; label: string; format?: (v: number) => string }) {
  if (!bars.length) return <p className="text-sm text-muted-foreground">No data in this period.</p>;
  const max = Math.max(...bars.map((b) => b.value), 1);
  return (
    <ul className="space-y-1.5" aria-label={label}>
      {bars.map((b) => (
        <li key={b.label} className="grid grid-cols-[minmax(6rem,10rem)_1fr_auto] items-center gap-2 text-sm">
          <span className="truncate" title={b.label}>{b.label}</span>
          <svg viewBox="0 0 100 8" preserveAspectRatio="none" className="h-2.5 w-full" aria-hidden>
            <rect x="0" y="0" width="100" height="8" rx="2" className="fill-border" />
            <rect x="0" y="0" width={(b.value / max) * 100} height="8" rx="2" className={FILL[b.tone ?? "primary"]} />
          </svg>
          <span className="w-14 text-right tabular-nums">{format(b.value)}</span>
        </li>
      ))}
    </ul>
  );
}

/** Vertical columns over time buckets; the table below the chart carries the exact values. */
export function Columns({ points, label }: { points: { bucket: string; count: number }[]; label: string }) {
  if (!points.length) return <p className="text-sm text-muted-foreground">No claims in this period.</p>;
  const max = Math.max(...points.map((p) => p.count), 1);
  const w = 100 / points.length;
  return (
    <figure>
      <svg viewBox="0 0 100 40" preserveAspectRatio="none" className="h-32 w-full" role="img" aria-label={label}>
        {points.map((p, i) => {
          const h = (p.count / max) * 38;
          return (
            <rect key={p.bucket} x={i * w + w * 0.15} y={40 - h} width={w * 0.7} height={h} rx="0.6" className="fill-primary">
              <title>{`${p.bucket}: ${p.count}`}</title>
            </rect>
          );
        })}
      </svg>
      <figcaption className="mt-1 flex justify-between text-xs text-muted-foreground">
        <span>{points[0]?.bucket}</span>
        <span>{points.length > 1 ? points[points.length - 1]?.bucket : null}</span>
      </figcaption>
      <table className="sr-only">
        <caption>{label}</caption>
        <tbody>{points.map((p) => <tr key={p.bucket}><th>{p.bucket}</th><td>{p.count}</td></tr>)}</tbody>
      </table>
    </figure>
  );
}
