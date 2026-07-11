"use client";

import { useEffect, useState } from "react";

import {
  deleteDocument,
  listDocuments,
  listRegulations,
  type DocInfo,
} from "@/lib/api";
import type { Session } from "@/lib/session";
import type { Regulation } from "@/lib/types";
import PolicyUpload from "./PolicyUpload";
import SignIn from "./SignIn";

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

export default function LibraryScreen({
  session,
  onSignedIn,
}: {
  session: Session | null;
  onSignedIn: (s: Session) => void;
}) {
  const [docs, setDocs] = useState<DocInfo[]>([]);
  const [regs, setRegs] = useState<Regulation[]>([]);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!session) {
      setDocs([]);
      setRegs([]);
      return;
    }
    let alive = true;
    setError(null);
    listDocuments(session.accessToken)
      .then((d) => alive && setDocs(d))
      .catch(() => {/* non-fatal */});
    listRegulations(session.accessToken)
      .then((r) => alive && setRegs(r))
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [session]);

  function reloadDocs() {
    if (session) listDocuments(session.accessToken).then(setDocs).catch(() => {});
  }

  async function onDelete(filename: string) {
    if (!session) return;
    if (!window.confirm(`Delete “${filename}”? This removes the file and its indexed vectors.`)) {
      return;
    }
    setError(null);
    setDeleting(filename);
    try {
      await deleteDocument(filename, session.accessToken);
      setDocs((prev) => prev.filter((d) => d.filename !== filename));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed.");
    } finally {
      setDeleting(null);
    }
  }

  if (!session) {
    return (
      <div className="glass space-y-3 rounded-3xl p-5 sm:p-6">
        <p className="text-sm text-[var(--muted)]">
          Sign in to manage your policies and browse available regulations.
        </p>
        <SignIn onSignedIn={onSignedIn} />
      </div>
    );
  }

  return (
    <div className="space-y-5">
      {error && (
        <div className="st-gap glass st-ring rounded-2xl px-4 py-3 text-sm text-[var(--fg)]">
          <span className="st-fg font-semibold">Something went wrong:</span> {error}
        </div>
      )}
      <div className="grid gap-5 md:grid-cols-2">
        {/* Your policies */}
        <section className="glass space-y-4 rounded-3xl p-5 sm:p-6">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-[var(--muted)]">
              Your policies
            </h2>
            <span className="text-xs text-[var(--muted)] tabular-nums">
              {docs.length} document{docs.length === 1 ? "" : "s"}
            </span>
          </div>
          <PolicyUpload
            token={session.accessToken}
            docs={docs}
            onChanged={reloadDocs}
            onDelete={onDelete}
            deleting={deleting}
          />
          {docs.length === 0 && (
            <p className="text-sm italic text-[var(--muted)]">
              Upload your KYC policy to check it against a regulation.
            </p>
          )}
        </section>

        {/* Regulations */}
        <section className="glass space-y-4 rounded-3xl p-5 sm:p-6">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-[var(--muted)]">
              Regulations
            </h2>
            <span className="text-xs text-[var(--muted)] tabular-nums">
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
                    {r.ingested_at ? ` · seeded ${fmtDate(r.ingested_at)}` : ""}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm italic text-[var(--muted)]">
              No regulations seeded yet — run the seed step.
            </p>
          )}
          <p className="pt-1 text-xs text-[var(--muted)]">
            Pick a regulation on the <span className="text-[var(--fg)]">Gap check</span> screen to run a check.
          </p>
        </section>
      </div>
    </div>
  );
}
