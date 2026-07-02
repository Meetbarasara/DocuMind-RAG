"use client";

import { useMemo, useRef, useState } from "react";

import { API_BASE, DEMO_REGULATION, runCheck } from "@/lib/api";
import type { GapRow as Row } from "@/lib/types";
import GapRowCard from "./GapRow";
import SummaryCards from "./SummaryCards";

type Phase = "idle" | "running" | "done" | "error";
type Mode = "demo" | "live";

const reqNum = (id: string) => {
  const n = parseInt(id.split("-").pop() || "", 10);
  return Number.isNaN(n) ? 1e9 : n;
};

export default function CheckHero() {
  const [mode, setMode] = useState<Mode>("demo");
  const [token, setToken] = useState("");
  const [regulationId, setRegulationId] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [rows, setRows] = useState<Row[]>([]);
  const [total, setTotal] = useState(0);
  const [regName, setRegName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

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

  const canRun =
    phase !== "running" && (mode === "demo" || regulationId.trim().length > 0);

  async function run() {
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    setRows([]);
    setError(null);
    setTotal(0);
    setRegName("");
    setPhase("running");
    try {
      await runCheck(
        {
          regulationId: mode === "demo" ? DEMO_REGULATION.id : regulationId.trim(),
          demo: mode === "demo",
          token: token.trim() || undefined,
        },
        (e) => {
          if (ac.signal.aborted) return;
          if (e.type === "summary_init") {
            setTotal(e.total);
            setRegName(e.regulation?.name || "");
          } else if (e.type === "row") {
            setRows((prev) => [...prev, e.row]);
          } else if (e.type === "summary_final") {
            setPhase("done");
          } else if (e.type === "error") {
            setError(e.message);
          }
        },
        ac.signal,
      );
      if (!ac.signal.aborted) setPhase((p) => (p === "running" ? "done" : p));
    } catch (err) {
      if (ac.signal.aborted) return;
      setError(err instanceof Error ? err.message : "Check failed.");
      setPhase("error");
    }
  }

  const checked = rows.length;
  const pct = total ? Math.round((checked / total) * 100) : 0;

  return (
    <div className="space-y-6">
      {/* Control panel */}
      <div className="glass rounded-3xl p-5 sm:p-6">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div className="min-w-0">
            <label className="mb-2 block text-xs font-semibold uppercase tracking-wider text-[var(--muted)]">
              Regulation
            </label>
            {mode === "demo" ? (
              <div className="glass-soft flex items-center gap-2 rounded-xl px-3.5 py-2.5">
                <span className="st-conflict st-bar h-2 w-2 rounded-full" />
                <span className="text-sm text-[var(--fg)]">
                  {DEMO_REGULATION.name}
                </span>
              </div>
            ) : (
              <input
                value={regulationId}
                onChange={(e) => setRegulationId(e.target.value)}
                placeholder="regulation_id (from the seed step)"
                className="glass-soft w-full rounded-xl px-3.5 py-2.5 text-sm text-[var(--fg)] outline-none placeholder:text-white/30 focus:border-white/25 sm:w-80"
              />
            )}
          </div>

          <div className="flex items-center gap-3">
            <ModeToggle mode={mode} setMode={setMode} disabled={phase === "running"} />
            <button
              onClick={run}
              disabled={!canRun}
              className="accent-btn rounded-xl px-5 py-2.5 text-sm font-semibold"
            >
              {phase === "running"
                ? total
                  ? `Checking… ${checked}/${total}`
                  : "Checking…"
                : "Run check"}
            </button>
          </div>
        </div>

        {mode === "live" && (
          <div className="mt-4">
            <input
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="Access token (Bearer JWT)"
              type="password"
              className="glass-soft w-full rounded-xl px-3.5 py-2.5 text-sm text-[var(--fg)] outline-none placeholder:text-white/30 focus:border-white/25"
            />
            <p className="mt-2 text-xs text-[var(--muted)]">
              Live mode calls <span className="font-mono">{API_BASE}</span>. Your
              policy must already be uploaded to your account.
            </p>
          </div>
        )}
      </div>

      {error && (
        <div className="st-gap glass st-ring rounded-2xl px-4 py-3 text-sm text-[var(--fg)]">
          <span className="st-fg font-semibold">Couldn’t finish:</span> {error}
        </div>
      )}

      {phase === "idle" ? (
        <EmptyState />
      ) : (
        <div className="space-y-5">
          <SummaryCards counts={counts} />

          {/* Progress */}
          <div className="flex items-center justify-between text-sm text-[var(--muted)]">
            <span>
              {regName ? (
                <>
                  Checking <span className="text-[var(--fg)]">{regName}</span>
                </>
              ) : (
                "Preparing…"
              )}
            </span>
            <span className="tabular-nums">
              {phase === "running" ? `Checked ${checked} of ${total || "…"}` : `${checked} requirements`}
            </span>
          </div>
          <div className="h-1 overflow-hidden rounded-full bg-white/10">
            <div
              className="h-full rounded-full bg-[rgb(var(--accent))] transition-[width] duration-300"
              style={{ width: `${phase === "done" ? 100 : pct}%` }}
            />
          </div>

          {/* Rows */}
          <div className="space-y-2.5">
            {sortedRows.map((row) => (
              <GapRowCard key={row.requirement_id} row={row} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function ModeToggle({
  mode,
  setMode,
  disabled,
}: {
  mode: Mode;
  setMode: (m: Mode) => void;
  disabled: boolean;
}) {
  return (
    <div className="glass-soft flex rounded-xl p-1 text-sm">
      {(["demo", "live"] as Mode[]).map((m) => (
        <button
          key={m}
          disabled={disabled}
          onClick={() => setMode(m)}
          className={`rounded-lg px-3 py-1.5 font-medium capitalize transition-colors disabled:opacity-50 ${
            mode === m
              ? "bg-white/12 text-[var(--fg)]"
              : "text-[var(--muted)] hover:text-[var(--fg)]"
          }`}
        >
          {m}
        </button>
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="glass rounded-3xl px-6 py-16 text-center">
      <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-2xl bg-indigo-500/15 text-2xl">
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
