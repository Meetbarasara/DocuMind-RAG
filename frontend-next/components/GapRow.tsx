"use client";

import { useState } from "react";

import { STATUS_CLASS, type GapRow as Row } from "@/lib/types";
import StatusPill from "./StatusPill";

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`h-4 w-4 shrink-0 text-[var(--muted)] transition-transform ${open ? "rotate-180" : ""}`}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
    >
      <path d="M6 8l4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export default function GapRowCard({ row }: { row: Row }) {
  const [open, setOpen] = useState(false);
  const cls = STATUS_CLASS[row.status] || "st-review";
  const rbiCite = `RBI${row.rbi_section ? ` §${row.rbi_section}` : ""}${
    row.rbi_page != null ? ` · p.${row.rbi_page}` : ""
  }`;
  const policyCite = row.policy_filename
    ? `${row.policy_filename}${row.policy_page != null ? ` · p.${row.policy_page}` : ""}`
    : null;
  // Show the verbatim source clause when we have it; fall back to the model's
  // quote for older persisted checks. "Verified" = the quote grounded in a real
  // policy clause (default to "has a citation" when the flag is absent).
  const clauseText = row.policy_clause || row.policy_quote;
  const verified = row.evidence_verified ?? row.policy_filename != null;

  return (
    <div className={`${cls} glass overflow-hidden rounded-2xl`}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-3 p-4 text-left transition-colors hover:bg-white/[0.03]"
        aria-expanded={open}
      >
        <span className="st-bar h-10 w-1 shrink-0 rounded-full" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-xs text-[var(--muted)]">
            <span className="font-mono">{row.requirement_id}</span>
            <span aria-hidden>·</span>
            <span>{rbiCite}</span>
          </div>
          <p className="mt-1 truncate text-sm text-[var(--fg)] sm:text-[0.95rem]">
            {row.requirement}
          </p>
        </div>
        <span className="hidden shrink-0 text-xs text-[var(--muted)] tabular-nums sm:block">
          {Math.round(row.confidence * 100)}%
        </span>
        <StatusPill status={row.status} />
        <Chevron open={open} />
      </button>

      {open && (
        <div className="border-t border-white/10 px-4 pb-4 pt-4">
          <div className="grid gap-3 md:grid-cols-2">
            {/* Your policy clause */}
            <div className="glass-soft rounded-xl p-3.5">
              <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-[var(--muted)]">
                Your policy
              </div>
              {clauseText ? (
                <>
                  <p className="text-sm leading-relaxed text-[var(--fg)]">
                    “{clauseText}”
                  </p>
                  <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1">
                    {policyCite && (
                      <span className="font-mono text-xs text-[var(--muted)]">
                        {policyCite}
                      </span>
                    )}
                    {verified && (
                      <span
                        className="inline-flex items-center gap-1 rounded-full border border-white/12 bg-white/5 px-1.5 py-0.5 text-[0.65rem] font-medium uppercase tracking-wide text-[var(--muted)]"
                        title="The cited quote was matched to this exact clause in your policy."
                      >
                        <svg viewBox="0 0 20 20" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="2.2">
                          <path d="M5 10.5l3.2 3.2L15 7" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                        Verified
                      </span>
                    )}
                  </div>
                </>
              ) : (
                <p className="text-sm italic text-[var(--muted)]">
                  No matching clause found in your policy.
                </p>
              )}
            </div>

            {/* RBI requirement */}
            <div className="glass-soft rounded-xl p-3.5">
              <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-[var(--muted)]">
                RBI requirement
              </div>
              <p className="text-sm leading-relaxed text-[var(--fg)]">
                {row.requirement}
              </p>
              <div className="mt-2 font-mono text-xs text-[var(--muted)]">
                {rbiCite}
              </div>
            </div>
          </div>

          <div className="mt-3 flex items-start gap-2 text-sm text-[var(--muted)]">
            <span className={`${cls} st-fg mt-0.5 shrink-0 font-semibold`}>
              {row.status}
            </span>
            <span>— {row.rationale}</span>
          </div>
        </div>
      )}
    </div>
  );
}
