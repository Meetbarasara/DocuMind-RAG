"use client";

import { STATUS_CLASS, type CheckSummary } from "@/lib/types";

const CORE = ["Covered", "Partial", "Gap", "Conflict"] as const;

function fmtDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return "";
  }
}

function RecheckIcon() {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.7">
      <path d="M15.5 8A6 6 0 1 0 16 11" strokeLinecap="round" />
      <path d="M15.5 4v4h-4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export default function ChecksHistory({
  checks,
  activeId,
  onOpen,
  onRecheck,
}: {
  checks: CheckSummary[];
  activeId: string | null;
  onOpen: (id: string) => void;
  onRecheck: (id: string) => void;
}) {
  if (!checks.length) return null;
  return (
    <div>
      <label className="mb-2 block text-xs font-semibold uppercase tracking-wider text-[var(--muted)]">
        Recent checks
      </label>
      <ul className="space-y-2">
        {checks.map((c) => (
          <li key={c.id}>
            <div
              style={
                activeId === c.id
                  ? { boxShadow: "inset 0 0 0 1px rgba(99,102,241,0.55)" }
                  : undefined
              }
              className="glass-soft flex items-center gap-1 rounded-xl"
            >
              <button
                onClick={() => onOpen(c.id)}
                className="flex min-w-0 flex-1 items-center justify-between gap-3 rounded-l-xl px-3.5 py-2.5 text-left transition-colors hover:bg-white/[0.06]"
              >
                <div className="min-w-0">
                  <div className="truncate text-sm text-[var(--fg)]">
                    {c.policy_label}
                  </div>
                  <div className="text-xs text-[var(--muted)]">
                    {fmtDate(c.created_at)}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-1.5">
                  {CORE.map((s) =>
                    (c.summary?.[s] ?? 0) > 0 ? (
                      <span
                        key={s}
                        className={`${STATUS_CLASS[s]} st-chip inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-xs font-semibold tabular-nums`}
                      >
                        <span className="st-bar h-1.5 w-1.5 rounded-full" />
                        {c.summary[s]}
                      </span>
                    ) : null,
                  )}
                </div>
              </button>
              <button
                onClick={() => onRecheck(c.id)}
                title="Re-check against the current version of this regulation — only what changed is re-judged."
                aria-label="Re-check against update"
                className="mr-1 shrink-0 rounded-lg p-2 text-[var(--muted)] transition-colors hover:bg-white/[0.06] hover:text-[var(--fg)]"
              >
                <RecheckIcon />
              </button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
