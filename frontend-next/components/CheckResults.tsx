"use client";

import { useMemo, useState } from "react";

import type { DeltaCounts, GapRow as Row } from "@/lib/types";
import { STATUS_CLASS, STATUS_ORDER } from "@/lib/types";
import type { Phase } from "@/lib/useCheckStream";
import CoverageBar from "./CoverageBar";
import GapRowCard from "./GapRow";
import SummaryCards from "./SummaryCards";

// Requirements sort by their trailing number (req-2 before req-10), not lexically.
const reqNum = (id: string) => {
  const n = parseInt(id.split("-").pop() || "", 10);
  return Number.isNaN(n) ? 1e9 : n;
};

const CSV_HEADERS = [
  "requirement_id", "requirement", "status", "confidence",
  "rbi_section", "rbi_page", "policy_filename", "policy_page",
  "policy_clause", "rationale",
];

function rowsToCsv(rows: Row[]): string {
  const esc = (v: unknown) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const body = rows.map((r) =>
    [
      r.requirement_id, r.requirement, r.status, r.confidence,
      r.rbi_section, r.rbi_page, r.policy_filename, r.policy_page,
      r.policy_clause || r.policy_quote, r.rationale,
    ].map(esc).join(","),
  );
  return [CSV_HEADERS.join(","), ...body].join("\n");
}

function downloadFile(name: string, text: string, type: string) {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

// The cited gap table — a coverage headline, a status filter + search + export
// toolbar, then one card per requirement. Purely presentational: the same view
// renders a live-streaming check, a persisted check re-opened by URL, and the
// public demo.
export default function CheckResults({
  rows,
  total,
  phase,
  regName,
  delta,
  token,
}: {
  rows: Row[];
  total: number;
  phase: Phase;
  regName: string;
  delta?: DeltaCounts | null;
  // When present (signed-in screens), each Gap/Partial/Conflict row can draft a
  // suggested fix. Omitted on the public demo, which hides the button.
  token?: string;
}) {
  const [filter, setFilter] = useState("All");
  const [query, setQuery] = useState("");

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

  const visibleRows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return sortedRows.filter((r) => {
      if (filter !== "All" && r.status !== filter) return false;
      if (!q) return true;
      const hay = `${r.requirement_id} ${r.requirement} ${r.rationale} ${r.policy_clause || ""} ${r.policy_quote || ""}`;
      return hay.toLowerCase().includes(q);
    });
  }, [sortedRows, filter, query]);

  if (phase === "idle") return <EmptyState />;

  const checked = rows.length;
  const pct = total ? Math.round((checked / total) * 100) : 0;
  const done = phase === "done";
  const chips = ["All", ...STATUS_ORDER.filter((s) => (counts[s] || 0) > 0)];

  return (
    <div className="space-y-5">
      {delta && <DeltaBanner delta={delta} />}

      {/* Coverage headline once complete; live count tiles while streaming. */}
      {done ? (
        <div className="glass rounded-2xl p-4 sm:p-5">
          <CoverageBar counts={counts} />
        </div>
      ) : (
        <>
          <SummaryCards counts={counts} />
          <div className="flex items-center justify-between text-sm text-[var(--muted)]">
            <span>
              {regName ? (
                <>Checking <span className="text-[var(--fg)]">{regName}</span></>
              ) : (
                "Preparing…"
              )}
            </span>
            <span className="tabular-nums">Checked {checked} of {total || "…"}</span>
          </div>
          <div className="h-1 overflow-hidden rounded-full bg-[var(--line)]">
            <div
              className="h-full rounded-full bg-[rgb(var(--accent))] transition-[width] duration-300"
              style={{ width: `${pct}%` }}
            />
          </div>
        </>
      )}

      {/* Filter + search + export */}
      {rows.length > 0 && (
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-wrap items-center gap-1.5">
            {chips.map((f) => {
              const active = filter === f;
              const n = f === "All" ? rows.length : counts[f] || 0;
              const cls = STATUS_CLASS[f] || "";
              const style = active
                ? f === "All"
                  ? "bg-[var(--active)] text-[var(--fg)]"
                  : `${cls} st-chip`
                : "glass-soft text-[var(--muted)] hover:text-[var(--fg)]";
              return (
                <button
                  key={f}
                  onClick={() => setFilter(f)}
                  className={`${style} inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium transition-colors`}
                >
                  {f !== "All" && <span className={`${cls} st-bar h-1.5 w-1.5 rounded-full`} />}
                  {f} <span className="tabular-nums opacity-70">{n}</span>
                </button>
              );
            })}
          </div>
          <div className="flex items-center gap-2">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search requirements…"
              className="glass-soft w-full rounded-xl px-3 py-1.5 text-sm text-[var(--fg)] outline-none placeholder:text-[var(--placeholder)] focus:border-[var(--line-strong)] sm:w-52"
            />
            {done && (
              <button
                onClick={() => downloadFile("gap-check.csv", rowsToCsv(sortedRows), "text/csv;charset=utf-8")}
                title="Export the full gap table as CSV"
                className="glass-soft shrink-0 rounded-xl px-3 py-1.5 text-sm text-[var(--fg)] transition-colors hover:bg-[var(--hover)]"
              >
                Export
              </button>
            )}
          </div>
        </div>
      )}

      <div className="space-y-2.5">
        {visibleRows.length ? (
          visibleRows.map((row) => (
            <GapRowCard key={row.requirement_id} row={row} token={token} />
          ))
        ) : (
          <p className="glass-soft rounded-xl px-4 py-6 text-center text-sm text-[var(--muted)]">
            No requirements match {filter !== "All" ? `“${filter}”` : "your search"}.
          </p>
        )}
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
