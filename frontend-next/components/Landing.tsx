"use client";

import Link from "next/link";

import { DEMO_REGULATION, runCheck } from "@/lib/api";
import { useSession } from "@/components/SessionProvider";
import { useCheckStream } from "@/lib/useCheckStream";
import CheckResults from "./CheckResults";

// The public showcase at /. A logged-out visitor can replay a real gap analysis
// (demo mode — no backend, no login) and then sign in to check their own policy.
export default function Landing() {
  const { session, ready } = useSession();
  const stream = useCheckStream();

  const appHref = session ? "/dashboard" : "/login";
  const appLabel = session && ready ? "Open app" : "Sign in";

  function runDemo() {
    stream.start((onEvent, signal) =>
      runCheck({ regulationId: DEMO_REGULATION.id, demo: true }, onEvent, signal),
    );
  }

  const runLabel =
    stream.phase === "running"
      ? "Running demo…"
      : stream.phase === "done"
        ? "Replay demo"
        : "Run the demo";

  return (
    <div className="mx-auto w-full max-w-4xl px-5 py-6 sm:px-8">
      <header className="flex items-center justify-between py-2">
        <div className="flex items-center gap-2.5">
          <span className="accent-btn flex h-8 w-8 items-center justify-center rounded-lg text-base">
            ⚖
          </span>
          <div className="leading-tight">
            <div className="text-sm font-semibold text-[var(--fg)]">KYC Compliance</div>
            <div className="text-[0.7rem] text-[var(--muted)]">Gap analysis</div>
          </div>
        </div>
        <Link
          href={appHref}
          className="glass-soft rounded-xl px-4 py-2 text-sm font-medium text-[var(--fg)] transition-colors hover:bg-[var(--hover)]"
        >
          {appLabel}
        </Link>
      </header>

      <section className="py-10 text-center sm:py-14">
        <div className="mx-auto mb-5 inline-flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--surface-soft)] px-3 py-1 text-xs text-[var(--muted)]">
          <span className="st-covered st-bar h-1.5 w-1.5 rounded-full" />
          RBI KYC · cited · eval-gated
        </div>
        <h1 className="mx-auto max-w-2xl text-3xl font-semibold leading-tight text-[var(--fg)] sm:text-4xl">
          Check your KYC policy against an RBI circular — cited, clause by clause.
        </h1>
        <p className="mx-auto mt-4 max-w-xl text-base text-[var(--muted)]">
          Upload your internal policy, pick a regulation, and get a
          requirement-by-requirement gap table — Covered, Partial, Gap or
          Conflict — each finding grounded in the exact policy clause.
        </p>
        <div className="mt-7 flex flex-wrap items-center justify-center gap-3">
          <button
            onClick={runDemo}
            disabled={stream.phase === "running"}
            className="accent-btn rounded-xl px-5 py-2.5 text-sm font-semibold"
          >
            {runLabel}
          </button>
          <Link
            href={appHref}
            className="glass-soft rounded-xl px-5 py-2.5 text-sm font-medium text-[var(--fg)] transition-colors hover:bg-[var(--hover)]"
          >
            {appLabel}
          </Link>
        </div>
        <p className="mt-3 text-xs text-[var(--muted)]">
          No login needed — the demo replays a real gap analysis.
        </p>
      </section>

      {stream.phase === "idle" ? (
        <Features />
      ) : (
        <section className="pt-2">
          <div className="mb-3 text-xs font-semibold uppercase tracking-wider text-[var(--muted)]">
            Demo · {DEMO_REGULATION.name}
          </div>
          <CheckResults
            rows={stream.rows}
            total={stream.total}
            phase={stream.phase}
            regName={stream.regName}
            delta={stream.delta}
          />
        </section>
      )}

      <footer className="mt-14 border-t border-[var(--line)] pt-5 text-center text-xs text-[var(--muted)]">
        Assisted review, not legal advice. Built on the DocuMind RAG engine —
        hybrid retrieval + rerank, a Cerebras judge, and a CI-gated compliance
        eval (macro-F1 0.91).
      </footer>
    </div>
  );
}

function Features() {
  const items: [string, string][] = [
    [
      "Cited to the clause",
      "Every verdict grounds to the exact policy clause via a graded containment score — not a whole-page guess.",
    ],
    [
      "Change-tracking",
      "Re-upload an updated circular and only the changed requirements are re-judged; the rest carry forward.",
    ],
    [
      "Eval-gated quality",
      "Gap-analysis accuracy and macro-F1 are measured on a labeled gold set and gated in CI.",
    ],
  ];
  return (
    <section className="grid gap-3 py-4 sm:grid-cols-3">
      {items.map(([title, body]) => (
        <div key={title} className="glass rounded-2xl p-5">
          <div className="text-sm font-semibold text-[var(--fg)]">{title}</div>
          <p className="mt-1.5 text-sm text-[var(--muted)]">{body}</p>
        </div>
      ))}
    </section>
  );
}
