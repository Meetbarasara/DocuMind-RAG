"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { getCheck, recheck } from "@/lib/api";
import { useSession } from "@/components/SessionProvider";
import { fmtDateTime } from "@/lib/format";
import { useCheckStream } from "@/lib/useCheckStream";
import type { PersistedCheck } from "@/lib/types";
import CheckResults from "./CheckResults";

// One persisted check at its own URL. Opens instantly from storage; a "Re-check"
// re-judges only what changed in the regulation since (POST /recheck) and streams
// the result inline, saving it as a new check. The page keys this component on
// the id, so navigating between checks gets fresh state.
export default function CheckDetail({ id }: { id: string }) {
  const { session } = useSession();
  const token = session?.accessToken;

  const [check, setCheck] = useState<PersistedCheck | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const stream = useCheckStream();
  const streaming = stream.phase !== "idle";

  useEffect(() => {
    if (!token || !id) return;
    let alive = true;
    getCheck(token, id)
      .then((c) => alive && setCheck(c))
      .catch((e) => alive && setLoadError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [token, id]);

  function doRecheck() {
    if (!token) return;
    stream.start((onEvent, signal) => recheck(id, token, onEvent, signal));
  }

  if (!token) return null;
  if (loading) return <p className="text-sm text-[var(--muted)]">Loading…</p>;
  if (!check) {
    return (
      <div className="st-gap glass st-ring rounded-2xl px-4 py-3 text-sm text-[var(--fg)]">
        <span className="st-fg font-semibold">Couldn’t open this check:</span>{" "}
        {loadError || "Not found."}{" "}
        <Link href="/checks" className="underline">
          Back to checks
        </Link>
      </div>
    );
  }

  // Show the live re-check stream once it starts; otherwise the persisted check.
  const rows = streaming ? stream.rows : check.rows;
  const total = streaming ? stream.total : check.summary?.total ?? check.rows.length;
  const phase = streaming ? stream.phase : "done";
  const regName = streaming ? stream.regName : check.policy_label;
  const delta = streaming ? stream.delta : check.summary?.delta ?? null;
  const running = stream.phase === "running";

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <Link
            href="/checks"
            className="text-xs text-[var(--muted)] transition-colors hover:text-[var(--fg)]"
          >
            ← All checks
          </Link>
          <div className="mt-1 truncate text-sm font-medium text-[var(--fg)]">
            {check.policy_label}
          </div>
          <div className="text-xs text-[var(--muted)]">{fmtDateTime(check.created_at)}</div>
        </div>
        <button
          onClick={doRecheck}
          disabled={running}
          title="Re-check against the current version of this regulation — only added or changed requirements are re-judged."
          className="glass-soft shrink-0 rounded-xl px-4 py-2 text-sm font-medium text-[var(--fg)] transition-colors hover:bg-[var(--hover)] disabled:opacity-50"
        >
          {running ? "Re-checking…" : "Re-check"}
        </button>
      </div>

      {stream.error && (
        <div className="st-gap glass st-ring rounded-2xl px-4 py-3 text-sm text-[var(--fg)]">
          <span className="st-fg font-semibold">Couldn’t finish:</span> {stream.error}
        </div>
      )}

      {stream.phase === "done" && stream.checkId && stream.checkId !== id && (
        <div className="glass-soft rounded-xl px-4 py-2.5 text-sm text-[var(--muted)]">
          Saved as a new check ·{" "}
          <Link
            href={`/checks/${stream.checkId}`}
            className="font-medium text-[var(--fg)] underline"
          >
            open it
          </Link>
        </div>
      )}

      <CheckResults rows={rows} total={total} phase={phase} regName={regName} delta={delta} token={token} />
    </div>
  );
}
