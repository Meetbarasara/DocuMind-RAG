"use client";

import { useState } from "react";

import { suggestFix } from "@/lib/api";
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

export default function GapRowCard({ row, token }: { row: Row; token?: string }) {
  const [open, setOpen] = useState(false);
  const cls = STATUS_CLASS[row.status] || "st-review";
  // A grounded "Suggest a fix" only makes sense where there's something to close,
  // and only when signed in (the endpoint is authed — the public demo has no token).
  const actionable =
    row.status === "Gap" || row.status === "Partial" || row.status === "Conflict";
  const [suggestion, setSuggestion] = useState<string | null>(null);
  const [suggesting, setSuggesting] = useState(false);
  const [suggestErr, setSuggestErr] = useState<string | null>(null);

  async function getSuggestion() {
    if (!token) return;
    setSuggesting(true);
    setSuggestErr(null);
    try {
      setSuggestion(await suggestFix(row, token));
    } catch (e) {
      setSuggestErr(e instanceof Error ? e.message : "Couldn’t draft a suggestion.");
    } finally {
      setSuggesting(false);
    }
  }
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
  // Change-tracking: flag the deltas a re-check re-judged (carried-forward rows
  // get no chip — "same as before").
  const changeChip =
    row.change === "added" ? "New" : row.change === "changed" ? "Changed" : null;

  return (
    <div className={`${cls} glass overflow-hidden rounded-2xl`}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-3 p-4 text-left transition-colors hover:bg-[var(--hover)]"
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
        {changeChip && (
          <span className="hidden shrink-0 rounded-full border border-[var(--accent-soft-border)] bg-[var(--accent-soft-bg)] px-2 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wide text-[var(--accent-soft-text)] sm:inline">
            {changeChip}
          </span>
        )}
        <span className="hidden shrink-0 text-xs text-[var(--muted)] tabular-nums sm:block">
          {Math.round(row.confidence * 100)}%
        </span>
        <StatusPill status={row.status} />
        <Chevron open={open} />
      </button>

      {open && (
        <div className="border-t border-[var(--line)] px-4 pb-4 pt-4">
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
                        className="inline-flex items-center gap-1 rounded-full border border-[var(--line)] bg-[var(--surface-soft)] px-1.5 py-0.5 text-[0.65rem] font-medium uppercase tracking-wide text-[var(--muted)]"
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

          {token && actionable && (
            <div className="mt-3">
              {!suggestion && (
                <button
                  onClick={getSuggestion}
                  disabled={suggesting}
                  className="glass-soft rounded-lg px-3 py-1.5 text-xs font-medium text-[var(--fg)] transition-colors hover:bg-[var(--hover)] disabled:opacity-50"
                >
                  {suggesting ? "Drafting…" : "Suggest a fix"}
                </button>
              )}
              {suggestErr && <p className="st-gap st-fg mt-2 text-xs">{suggestErr}</p>}
              {suggestion && (
                <div className="rounded-xl border border-[var(--accent-soft-border)] bg-[var(--accent-soft-bg)] p-3.5">
                  <div className="mb-1.5 flex items-center justify-between gap-2">
                    <span className="text-xs font-semibold uppercase tracking-wider text-[var(--accent-soft-text)]">
                      Suggested fix · draft
                    </span>
                    <button
                      onClick={getSuggestion}
                      disabled={suggesting}
                      className="text-xs text-[var(--muted)] transition-colors hover:text-[var(--fg)] disabled:opacity-50"
                    >
                      {suggesting ? "…" : "Regenerate"}
                    </button>
                  </div>
                  <p className="whitespace-pre-wrap text-sm leading-relaxed text-[var(--fg)]">
                    {suggestion}
                  </p>
                  <p className="mt-2 text-[0.7rem] text-[var(--muted)]">
                    Draft policy language — review with your compliance team before adopting. Not legal advice.
                  </p>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
