"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import {
  API_BASE,
  DEMO_REGULATION,
  getCheck,
  listChecks,
  listDocuments,
  listRegulations,
  runCheck,
  type DocInfo,
} from "@/lib/api";
import type { Session } from "@/lib/session";
import type { CheckSummary, GapRow as Row, Regulation } from "@/lib/types";
import ChecksHistory from "./ChecksHistory";
import GapRowCard from "./GapRow";
import PolicyUpload from "./PolicyUpload";
import SignIn from "./SignIn";
import SummaryCards from "./SummaryCards";

type Phase = "idle" | "running" | "done" | "error";
type Mode = "demo" | "live";

const reqNum = (id: string) => {
  const n = parseInt(id.split("-").pop() || "", 10);
  return Number.isNaN(n) ? 1e9 : n;
};

function runLabel(phase: Phase, checked: number, total: number) {
  if (phase !== "running") return "Run check";
  return total ? `Checking… ${checked}/${total}` : "Checking…";
}

export default function CheckHero({
  session,
  onSignedIn,
  onSignOut,
}: {
  session: Session | null;
  onSignedIn: (s: Session) => void;
  onSignOut: () => void;
}) {
  const [mode, setMode] = useState<Mode>("demo");

  // Live data (session is owned by the app shell and passed in)
  const [regulations, setRegulations] = useState<Regulation[]>([]);
  const [regId, setRegId] = useState("");
  const [docs, setDocs] = useState<DocInfo[]>([]);
  const [checks, setChecks] = useState<CheckSummary[]>([]);
  const [liveError, setLiveError] = useState<string | null>(null);

  // Results
  const [phase, setPhase] = useState<Phase>("idle");
  const [rows, setRows] = useState<Row[]>([]);
  const [total, setTotal] = useState(0);
  const [regName, setRegName] = useState("");
  const [activeCheckId, setActiveCheckId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // When signed in, load the regulation list, the user's docs, and past checks.
  useEffect(() => {
    if (!session) {
      setRegulations([]);
      setDocs([]);
      setChecks([]);
      return;
    }
    let alive = true;
    setLiveError(null);
    listRegulations(session.accessToken)
      .then((r) => {
        if (!alive) return;
        setRegulations(r);
        setRegId((prev) => prev || r[0]?.id || "");
      })
      .catch((e) => alive && setLiveError(e instanceof Error ? e.message : String(e)));
    listDocuments(session.accessToken)
      .then((d) => alive && setDocs(d))
      .catch(() => {/* non-fatal */});
    listChecks(session.accessToken)
      .then((c) => alive && setChecks(c))
      .catch(() => {/* non-fatal */});
    return () => {
      alive = false;
    };
  }, [session]);

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

  const canRun = phase !== "running" && (mode === "demo" || (!!session && !!regId));

  function handleSignOut() {
    setRegId("");
    setActiveCheckId(null);
    onSignOut();
  }
  function reloadDocs() {
    if (session) listDocuments(session.accessToken).then(setDocs).catch(() => {});
  }

  async function run() {
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    setRows([]);
    setError(null);
    setTotal(0);
    setRegName("");
    setActiveCheckId(null);
    setPhase("running");
    try {
      await runCheck(
        {
          regulationId: mode === "demo" ? DEMO_REGULATION.id : regId,
          demo: mode === "demo",
          token: session?.accessToken,
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
      // A live run persists server-side — refresh the history so it shows up.
      if (mode === "live" && session && !ac.signal.aborted) {
        listChecks(session.accessToken).then(setChecks).catch(() => {});
      }
    } catch (err) {
      if (ac.signal.aborted) return;
      setError(err instanceof Error ? err.message : "Check failed.");
      setPhase("error");
    }
  }

  async function openCheck(id: string) {
    if (!session) return;
    abortRef.current?.abort();
    setError(null);
    setActiveCheckId(id);
    try {
      const check = await getCheck(session.accessToken, id);
      setRows(check.rows || []);
      setTotal(check.summary?.total ?? (check.rows?.length || 0));
      setRegName(check.policy_label || "");
      setPhase("done");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not open that check.");
    }
  }

  const checked = rows.length;
  const pct = total ? Math.round((checked / total) * 100) : 0;
  const label = runLabel(phase, checked, total);

  return (
    <div className="space-y-6">
      <div className="glass space-y-4 rounded-3xl p-5 sm:p-6">
        <div className="flex items-center justify-between gap-3">
          <ModeToggle mode={mode} setMode={setMode} disabled={phase === "running"} />
          {mode === "demo" && <RunButton onClick={run} disabled={!canRun} label={label} />}
        </div>

        {mode === "demo" ? (
          <DemoBody />
        ) : session ? (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div className="text-xs text-[var(--muted)]">
                Signed in as <span className="text-[var(--fg)]">{session.email}</span>
              </div>
              <button
                onClick={handleSignOut}
                className="text-xs text-[var(--muted)] transition-colors hover:text-[var(--fg)]"
              >
                Sign out
              </button>
            </div>
            {liveError && <p className="st-gap st-fg text-sm">{liveError}</p>}
            <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
              <div className="min-w-0 flex-1">
                <label className="mb-2 block text-xs font-semibold uppercase tracking-wider text-[var(--muted)]">
                  Regulation
                </label>
                {regulations.length ? (
                  <select
                    value={regId}
                    onChange={(e) => setRegId(e.target.value)}
                    className="glass-soft w-full rounded-xl px-3.5 py-2.5 text-sm text-[var(--fg)] outline-none focus:border-white/25 sm:w-80"
                  >
                    {regulations.map((r) => (
                      <option key={r.id} value={r.id} className="bg-[#0b1120]">
                        {r.name}
                      </option>
                    ))}
                  </select>
                ) : (
                  <p className="text-sm text-[var(--muted)]">
                    No regulations seeded yet — run the seed step.
                  </p>
                )}
              </div>
              <RunButton onClick={run} disabled={!canRun} label={label} />
            </div>
            <div>
              <label className="mb-2 block text-xs font-semibold uppercase tracking-wider text-[var(--muted)]">
                Your policy
              </label>
              <PolicyUpload token={session.accessToken} docs={docs} onChanged={reloadDocs} />
            </div>
            <ChecksHistory checks={checks} activeId={activeCheckId} onOpen={openCheck} />
          </div>
        ) : (
          <div className="space-y-3">
            <p className="text-sm text-[var(--muted)]">
              Sign in to check your own policy. Calls{" "}
              <span className="font-mono">{API_BASE}</span>.
            </p>
            <SignIn onSignedIn={onSignedIn} />
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
          <div className="h-1 overflow-hidden rounded-full bg-white/10">
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
      )}
    </div>
  );
}

function RunButton({
  onClick,
  disabled,
  label,
}: {
  onClick: () => void;
  disabled: boolean;
  label: string;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="accent-btn shrink-0 rounded-xl px-5 py-2.5 text-sm font-semibold"
    >
      {label}
    </button>
  );
}

function DemoBody() {
  return (
    <div>
      <label className="mb-2 block text-xs font-semibold uppercase tracking-wider text-[var(--muted)]">
        Regulation
      </label>
      <div className="glass-soft flex w-fit items-center gap-2 rounded-xl px-3.5 py-2.5">
        <span className="st-conflict st-bar h-2 w-2 rounded-full" />
        <span className="text-sm text-[var(--fg)]">{DEMO_REGULATION.name}</span>
      </div>
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
