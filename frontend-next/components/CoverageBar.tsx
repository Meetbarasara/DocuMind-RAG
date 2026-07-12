import { STATUS_CLASS, STATUS_ORDER } from "@/lib/types";

// The coverage headline: a big "% fully covered" number over a stacked
// proportion bar (Covered / Partial / Gap / Conflict), so the mix is honest —
// not just a single flattering number. Shared by the dashboard and the results
// view. Colours come from each status' --c token via .st-bar.
export default function CoverageBar({
  counts,
}: {
  counts: Record<string, number>;
}) {
  const total = STATUS_ORDER.reduce((s, k) => s + (counts[k] || 0), 0);
  const covered = counts.Covered || 0;
  const pct = total ? Math.round((covered / total) * 100) : 0;
  const segments = STATUS_ORDER.filter((s) => (counts[s] || 0) > 0);

  return (
    <div className="space-y-2.5">
      <div className="flex items-baseline justify-between gap-3">
        <div className="flex items-baseline gap-2">
          <span className="text-3xl font-semibold tabular-nums text-[var(--fg)]">
            {pct}%
          </span>
          <span className="text-sm text-[var(--muted)]">fully covered</span>
        </div>
        <span className="shrink-0 text-xs tabular-nums text-[var(--muted)]">
          {covered} of {total} requirements
        </span>
      </div>
      <div className="flex h-2 overflow-hidden rounded-full bg-[var(--line)]">
        {segments.map((s) => (
          <div
            key={s}
            className={`${STATUS_CLASS[s]} st-bar h-full`}
            style={{ width: `${((counts[s] || 0) / total) * 100}%` }}
            title={`${s}: ${counts[s]}`}
          />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-[var(--muted)]">
        {segments.map((s) => (
          <span key={s} className="inline-flex items-center gap-1.5 tabular-nums">
            <span className={`${STATUS_CLASS[s]} st-bar h-2 w-2 rounded-full`} />
            {counts[s]} {s}
          </span>
        ))}
      </div>
    </div>
  );
}
