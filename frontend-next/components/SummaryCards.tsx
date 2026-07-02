import { STATUS_CLASS, STATUS_ORDER } from "@/lib/types";

export default function SummaryCards({
  counts,
}: {
  counts: Record<string, number>;
}) {
  // Always show the four core states; only show "Needs review" if it occurred.
  const shown = STATUS_ORDER.filter(
    (s) => s !== "Needs review" || (counts["Needs review"] || 0) > 0,
  );
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {shown.map((s) => (
        <div
          key={s}
          className={`${STATUS_CLASS[s]} glass st-ring rounded-2xl px-4 py-3.5`}
        >
          <div className="st-fg text-3xl font-semibold tabular-nums">
            {counts[s] || 0}
          </div>
          <div className="mt-0.5 text-sm text-[var(--muted)]">{s}</div>
        </div>
      ))}
    </div>
  );
}
