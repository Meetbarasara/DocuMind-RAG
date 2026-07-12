"use client";

import { useMemo } from "react";

import type { DeltaCounts, GapRow as Row } from "@/lib/types";
import type { Phase } from "@/lib/useCheckStream";
import GapRowCard from "./GapRow";
import SummaryCards from "./SummaryCards";

// Requirements sort by their trailing number (req-2 before req-10), not lexically.
const reqNum = (id: string) => {
  const n = parseInt(id.split("-").pop() || "", 10);
  return Number.isNaN(n) ? 1e9 : n;
};

// The cited gap table — summary counts, a progress line, then one card per
// requirement. Purely presentational: the same view renders a live-streaming
// check, a persisted check re-opened by URL, and the public demo.
export default function CheckResults({
  rows,
  total,
  phase,
  regName,
  delta,
}: {
  rows: Row[];
  total: number;
  phase: Phase;
  regName: string;
  delta?: DeltaCounts | null;
}) {
  const counts = useMemo(() => {
    const c: Record<string, number> = {
      Covered: 0, Partial: 0, Gap: 0, Conflict: 0, "Needs review": 0,
    };
    for (const r of rows) if (r.status in c) c[r.status] += 1;
    return c;
  }, [rows]);

  const sortedRows = useMemo(
    () => [...rows].sort((a, b) => reqNum(a.requirement_id) - reqNum(b.requirement_id)),
    [rows],
  );

  if (phase === "idle") return <EmptyState />;

  const checked = rows.length;
  const pct = total ? Math.round((checked / total) * 100) : 0;

  return (
    <div className="space-y-5">
      {delta && <DeltaBanner delta={delta} />}
      <SummaryCards counts={counts} />
      <div className="flex items-center justify-between text-sm text-[var(--muted)]">
        <span>
          {regName ? (
            <>
              {phase === "running" ? "Checking" : "Showing"}{" "}
              <span className="text-[var(--fg)]">{regName}</span>
            </>
          ) : (
            "Preparing…"
          )}
        </span>
        <span className="tabular-nums">
          {phase === "running"
            ? `Checked ${checked} of ${total || "…"}`
            : `${checked} requirements`}
        </span>
      </div>
      <div className="h-1 overflow-hidden rounded-full bg-[var(--line)]">
        <div
          className="h-full rounded-full bg-[rgb(var(--accent))] transition-[width] duration-300"
          style={{ width: `${phase === "done" ? 100 : pct}%` }}
        />
      </div>
      <div className="space-y-2.5">
        {sortedRows.map((row) => (
          <GapRowCard key={row.requirement_id} row={row} />
        ))}
      </div>
    </div>
  );
}

export function DeltaBanner({ delta }: { delta: DeltaCounts }) {
  const items: [string, number][] = [
    ["added", delta.added],
    ["changed", delta.changed],
    ["unchanged", delta.unchanged],
    ["removed", delta.removed],
  ];
  return (
    <div className="glass-soft flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-xl px-4 py-2.5 text-sm">
      <span className="font-medium text-[var(--fg)]">Change-tracked re-check</span>
      <span className="hidden text-[var(--muted)] sm:inline">
        — only added &amp; changed requirements were re-judged; the rest carried forward.
      </span>
      <div className="ml-auto flex flex-wrap items-center gap-3 text-[var(--muted)]">
        {items.map(([label, n]) => (
          <span key={label} className="tabular-nums">
            <span className="font-semibold text-[var(--fg)]">{n}</span> {label}
          </span>
        ))}
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="glass rounded-3xl px-6 py-16 text-center">
      <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-2xl bg-[var(--accent-soft-bg)] text-2xl">
        📋
      </div>
      <h3 className="text-lg font-semibold text-[var(--fg)]">
        Run a check to see your cited gap table
      </h3>
      <p className="mx-auto mt-2 max-w-md text-sm text-[var(--muted)]">
        Every requirement is judged against your policy and shown Covered,
        Partial, Gap, or Conflict — each finding cited to the exact clause.
      </p>
    </div>
  );
}
