"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { listDocuments, listRegulations, runCheck, type DocInfo } from "@/lib/api";
import { useSession } from "@/components/SessionProvider";
import { useCheckStream } from "@/lib/useCheckStream";
import type { Regulation } from "@/lib/types";
import CheckResults from "./CheckResults";
import PolicyUpload from "./PolicyUpload";

// Run a fresh gap check: pick a regulation, make sure a policy is uploaded, then
// stream the cited gap table inline. The shell owns auth, so this screen assumes
// a signed-in session.
export default function NewCheck() {
  const { session } = useSession();
  const token = session?.accessToken;

  const [regulations, setRegulations] = useState<Regulation[]>([]);
  // "Loading" and "empty" are different facts: without this flag the screen
  // asserted "No regulations yet" for the moments the list was still in
  // flight — a false claim (and a race for anything reading the screen).
  const [regsLoading, setRegsLoading] = useState(true);
  const [regId, setRegId] = useState("");
  const [docs, setDocs] = useState<DocInfo[]>([]);
  const [liveError, setLiveError] = useState<string | null>(null);

  const stream = useCheckStream();

  useEffect(() => {
    if (!token) return;
    let alive = true;
    setLiveError(null);
    listRegulations(token)
      .then((r) => {
        if (!alive) return;
        setRegulations(r);
        setRegId((prev) => prev || r[0]?.id || "");
      })
      .catch((e) => alive && setLiveError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setRegsLoading(false));
    listDocuments(token)
      .then((d) => alive && setDocs(d))
      .catch(() => {/* non-fatal */});
    return () => {
      alive = false;
    };
  }, [token]);

  function reloadDocs() {
    if (token) listDocuments(token).then(setDocs).catch(() => {});
  }

  function run() {
    if (!token || !regId) return;
    stream.start((onEvent, signal) =>
      runCheck({ regulationId: regId, token }, onEvent, signal),
    );
  }

  if (!token) return null;

  const running = stream.phase === "running";
  const canRun = !running && !!regId;
  const label = running
    ? stream.total
      ? `Checking… ${stream.rows.length}/${stream.total}`
      : "Checking…"
    : "Run check";

  return (
    <div className="space-y-6">
      <div className="glass space-y-4 rounded-3xl p-5 sm:p-6">
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
                className="glass-soft w-full rounded-xl px-3.5 py-2.5 text-sm text-[var(--fg)] outline-none focus:border-[var(--line-strong)] sm:w-80"
              >
                {regulations.map((r) => (
                  <option key={r.id} value={r.id} className="bg-white text-[var(--fg)]">
                    {r.name}
                  </option>
                ))}
              </select>
            ) : regsLoading ? (
              <p className="text-sm text-[var(--muted)]">Loading regulations…</p>
            ) : (
              <p className="text-sm text-[var(--muted)]">
                No regulations yet —{" "}
                <Link href="/regulations" className="text-[var(--fg)] underline">
                  add one
                </Link>
                .
              </p>
            )}
          </div>
          <button
            onClick={run}
            disabled={!canRun}
            className="accent-btn shrink-0 rounded-xl px-5 py-2.5 text-sm font-semibold"
          >
            {label}
          </button>
        </div>
        <div>
          <label className="mb-2 block text-xs font-semibold uppercase tracking-wider text-[var(--muted)]">
            Your policy
          </label>
          <PolicyUpload token={token} docs={docs} onChanged={reloadDocs} />
        </div>
      </div>

      {stream.error && (
        <div className="st-gap glass st-ring rounded-2xl px-4 py-3 text-sm text-[var(--fg)]">
          <span className="st-fg font-semibold">Couldn’t finish:</span> {stream.error}
        </div>
      )}

      {stream.phase === "done" && stream.checkId && stream.checkId !== "demo" && (
        <div className="glass-soft rounded-xl px-4 py-2.5 text-sm text-[var(--muted)]">
          Saved to your history ·{" "}
          <Link
            href={`/checks/${stream.checkId}`}
            className="font-medium text-[var(--fg)] underline"
          >
            open at its own URL
          </Link>
        </div>
      )}

      <CheckResults
        rows={stream.rows}
        total={stream.total}
        phase={stream.phase}
        regName={stream.regName}
        delta={stream.delta}
        token={token}
      />
    </div>
  );
}
