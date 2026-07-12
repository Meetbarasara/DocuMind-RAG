"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { listChecks } from "@/lib/api";
import { useSession } from "@/components/SessionProvider";
import { fmtDateTime } from "@/lib/format";
import { STATUS_CLASS, type CheckSummary } from "@/lib/types";

const CORE = ["Covered", "Partial", "Gap", "Conflict"] as const;

// The full history of the user's past checks. Each links to its own URL
// (/checks/[id]) where it re-opens instantly from persistence — no re-run, no
// judge budget burned.
export default function ChecksList() {
  const { session } = useSession();
  const token = session?.accessToken;
  const [checks, setChecks] = useState<CheckSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    listChecks(token)
      .then((c) => alive && setChecks(c))
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [token]);

  if (!token) return null;

  return (
    <div className="space-y-4">
      {error && (
        <div className="st-gap glass st-ring rounded-2xl px-4 py-3 text-sm text-[var(--fg)]">
          <span className="st-fg font-semibold">Couldn’t load checks:</span> {error}
        </div>
      )}

      <div className="flex items-center justify-between">
        <span className="text-xs tabular-nums text-[var(--muted)]">
          {checks.length} check{checks.length === 1 ? "" : "s"}
        </span>
        <Link
          href="/check/new"
          className="accent-btn rounded-xl px-4 py-2 text-sm font-semibold"
        >
          New check
        </Link>
      </div>

      {checks.length ? (
        <ul className="space-y-2">
          {checks.map((c) => (
            <li key={c.id}>
              <Link
                href={`/checks/${c.id}`}
                className="glass-soft flex items-center justify-between gap-3 rounded-xl px-3.5 py-3 transition-colors hover:bg-[var(--hover)]"
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
      ) : loading ? (
        <p className="text-sm text-[var(--muted)]">Loading…</p>
      ) : (
        <div className="glass rounded-3xl px-6 py-14 text-center">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-2xl bg-[var(--accent-soft-bg)] text-2xl">
            🗂️
          </div>
          <h3 className="text-lg font-semibold text-[var(--fg)]">No checks yet</h3>
          <p className="mx-auto mt-2 max-w-sm text-sm text-[var(--muted)]">
            Run your first gap check to see how your policy measures up against a
            regulation.
          </p>
          <Link
            href="/check/new"
            className="accent-btn mt-5 inline-block rounded-xl px-5 py-2.5 text-sm font-semibold"
          >
            New check
          </Link>
        </div>
      )}
    </div>
  );
}
