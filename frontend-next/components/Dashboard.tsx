"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { listChecks, listDocuments, listRegulations, type DocInfo } from "@/lib/api";
import { useSession } from "@/components/SessionProvider";
import { fmtDateTime } from "@/lib/format";
import { STATUS_CLASS, type CheckSummary, type Regulation } from "@/lib/types";
import CoverageBar from "./CoverageBar";

const CORE = ["Covered", "Partial", "Gap", "Conflict"] as const;

// The post-login home. Leads with the latest check's coverage, then quick
// counts and recent checks. Before the first check it's an onboarding checklist.
export default function Dashboard() {
  const { session } = useSession();
  const token = session?.accessToken;

  const [checks, setChecks] = useState<CheckSummary[]>([]);
  const [docs, setDocs] = useState<DocInfo[]>([]);
  const [regs, setRegs] = useState<Regulation[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    Promise.allSettled([
      listChecks(token),
      listDocuments(token),
      listRegulations(token),
    ])
      .then(([c, d, r]) => {
        if (!alive) return;
        if (c.status === "fulfilled") setChecks(c.value);
        if (d.status === "fulfilled") setDocs(d.value);
        if (r.status === "fulfilled") setRegs(r.value);
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [token]);

  if (!token) return null;

  const latest = checks[0];

  return (
    <div className="space-y-6">
      {/* Coverage headline */}
      {latest ? (
        <div className="glass space-y-4 rounded-3xl p-5 sm:p-6">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="text-xs font-semibold uppercase tracking-wider text-[var(--muted)]">
                Latest check
              </div>
              <div className="mt-0.5 truncate text-sm text-[var(--fg)]">
                {latest.policy_label}
                <span className="ml-2 text-xs text-[var(--muted)]">
                  {fmtDateTime(latest.created_at)}
                </span>
              </div>
            </div>
            <Link
              href={`/checks/${latest.id}`}
              className="glass-soft shrink-0 rounded-xl px-3.5 py-2 text-sm font-medium text-[var(--fg)] transition-colors hover:bg-[var(--hover)]"
            >
              View
            </Link>
          </div>
          <CoverageBar counts={latest.summary as unknown as Record<string, number>} />
        </div>
      ) : (
        <Onboarding loading={loading} hasPolicy={docs.length > 0} hasReg={regs.length > 0} />
      )}

      {/* Quick stats — "…" while loading; a 0 must mean a real zero. */}
      <div className="grid grid-cols-3 gap-3">
        <StatTile href="/policies" label="Policies" value={loading ? "…" : docs.length} />
        <StatTile href="/regulations" label="Regulations" value={loading ? "…" : regs.length} />
        <StatTile href="/checks" label="Checks" value={loading ? "…" : checks.length} />
      </div>

      {/* Primary CTA */}
      <div className="glass flex flex-col items-start justify-between gap-3 rounded-3xl p-5 sm:flex-row sm:items-center sm:p-6">
        <div>
          <div className="text-sm font-semibold text-[var(--fg)]">Run a new gap check</div>
          <div className="text-sm text-[var(--muted)]">
            Pick a regulation and check your policy against it, cited clause by clause.
          </div>
        </div>
        <Link
          href="/check/new"
          className="accent-btn shrink-0 rounded-xl px-5 py-2.5 text-sm font-semibold"
        >
          New check
        </Link>
      </div>

      {/* Recent checks */}
      {checks.length > 0 && (
        <div className="space-y-2.5">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-[var(--muted)]">
              Recent checks
            </h2>
            {checks.length > 5 && (
              <Link href="/checks" className="text-xs text-[var(--muted)] underline hover:text-[var(--fg)]">
                View all
              </Link>
            )}
          </div>
          <ul className="space-y-2">
            {checks.slice(0, 5).map((c) => (
              <li key={c.id}>
                <Link
                  href={`/checks/${c.id}`}
                  className="glass-soft flex items-center justify-between gap-3 rounded-xl px-3.5 py-2.5 transition-colors hover:bg-[var(--hover)]"
                >
                  <div className="min-w-0">
                    <div className="truncate text-sm text-[var(--fg)]">{c.policy_label}</div>
                    <div className="text-xs text-[var(--muted)]">{fmtDateTime(c.created_at)}</div>
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
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function StatTile({ href, label, value }: { href: string; label: string; value: number | string }) {
  return (
    <Link
      href={href}
      className="glass rounded-2xl px-4 py-3.5 transition-colors hover:bg-[var(--hover)]"
    >
      <div className="text-3xl font-semibold tabular-nums text-[var(--fg)]">{value}</div>
      <div className="mt-0.5 text-sm text-[var(--muted)]">{label}</div>
    </Link>
  );
}

function Onboarding({
  loading,
  hasPolicy,
  hasReg,
}: {
  loading: boolean;
  hasPolicy: boolean;
  hasReg: boolean;
}) {
  const steps: [boolean, string, string, string][] = [
    [hasPolicy, "Upload your KYC policy", "/policies", "Add your internal policy document."],
    [hasReg, "Add a regulation", "/regulations", "Upload an RBI circular to check against."],
    [false, "Run your first check", "/check/new", "Get a cited, requirement-by-requirement gap table."],
  ];
  return (
    <div className="glass space-y-4 rounded-3xl p-5 sm:p-6">
      <div>
        <h2 className="text-lg font-semibold text-[var(--fg)]">Get started</h2>
        <p className="mt-1 text-sm text-[var(--muted)]">
          Three steps to your first cited gap analysis.
        </p>
      </div>
      <ol className="space-y-2">
        {steps.map(([done, title, href, sub], i) => (
          <li key={href}>
            <Link
              href={href}
              className="glass-soft flex items-center gap-3 rounded-xl px-3.5 py-3 transition-colors hover:bg-[var(--hover)]"
            >
              <span
                className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${
                  done
                    ? "st-covered st-chip"
                    : "bg-[var(--accent-soft-bg)] text-[var(--accent-soft-text)]"
                }`}
              >
                {done && !loading ? "✓" : i + 1}
              </span>
              <span className="min-w-0">
                <span className="block text-sm font-medium text-[var(--fg)]">{title}</span>
                <span className="block text-xs text-[var(--muted)]">{sub}</span>
              </span>
            </Link>
          </li>
        ))}
      </ol>
    </div>
  );
}
