"use client";

import { useEffect, useState } from "react";

import { listRegulations } from "@/lib/api";
import { useSession } from "@/components/SessionProvider";
import type { Regulation } from "@/lib/types";

function fmtDate(iso?: string): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: "numeric", month: "short", day: "numeric",
    });
  } catch {
    return "";
  }
}

export default function RegulationsPanel() {
  const { session } = useSession();
  const token = session?.accessToken;
  const [regs, setRegs] = useState<Regulation[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    setError(null);
    listRegulations(token)
      .then((r) => alive && setRegs(r))
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [token]);

  if (!token) return null;

  return (
    <div className="space-y-4">
      {error && (
        <div className="st-gap glass st-ring rounded-2xl px-4 py-3 text-sm text-[var(--fg)]">
          <span className="st-fg font-semibold">Something went wrong:</span> {error}
        </div>
      )}
      <div className="glass space-y-4 rounded-3xl p-5 sm:p-6">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wider text-[var(--muted)]">
            Regulations
          </h2>
          <span className="text-xs tabular-nums text-[var(--muted)]">
            {regs.length} available
          </span>
        </div>
        {regs.length ? (
          <ul className="space-y-2">
            {regs.map((r) => (
              <li key={r.id} className="glass-soft rounded-xl p-3.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="min-w-0 truncate text-sm font-medium text-[var(--fg)]">
                    {r.name}
                  </span>
                  {r.regulator && (
                    <span className="shrink-0 rounded-full border border-[var(--line)] bg-[var(--surface-soft)] px-2 py-0.5 text-[0.65rem] font-medium uppercase tracking-wide text-[var(--muted)]">
                      {r.regulator}
                    </span>
                  )}
                </div>
                <div className="mt-1 text-xs text-[var(--muted)]">
                  {r.circular_id ? (
                    <span className="font-mono">{r.circular_id}</span>
                  ) : (
                    "Reference regulation"
                  )}
                  {r.ingested_at ? ` · added ${fmtDate(r.ingested_at)}` : ""}
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm italic text-[var(--muted)]">
            No regulations available yet.
          </p>
        )}
        <p className="pt-1 text-xs text-[var(--muted)]">
          Pick a regulation on the <span className="text-[var(--fg)]">Gap check</span> screen to run a check.
        </p>
      </div>
    </div>
  );
}
