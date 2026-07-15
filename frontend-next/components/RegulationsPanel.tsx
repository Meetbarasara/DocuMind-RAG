"use client";

import { useEffect, useRef, useState } from "react";

import { listRegulations, uploadRegulation } from "@/lib/api";
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
  // Same loading≠empty distinction as NewCheck: never claim "No regulations
  // yet" while the list is still being fetched.
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Add-a-regulation form state
  const inputRef = useRef<HTMLInputElement>(null);
  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    setError(null);
    listRegulations(token)
      .then((r) => alive && setRegs(r))
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [token]);

  async function onAdd(e: React.FormEvent) {
    e.preventDefault();
    if (!token || !file || !name.trim()) return;
    setError(null);
    setBusy(true);
    setStatus("uploading…");
    try {
      const { requirements } = await uploadRegulation(file, name.trim(), token, {
        onStatus: (s) => setStatus(s === "processing" ? "extracting requirements…" : s),
      });
      setStatus(`added · ${requirements} requirements`);
      setName("");
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
      listRegulations(token).then(setRegs).catch(() => {});
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed.");
      setStatus(null);
    } finally {
      setBusy(false);
    }
  }

  if (!token) return null;

  return (
    <div className="space-y-5">
      {error && (
        <div className="st-gap glass st-ring rounded-2xl px-4 py-3 text-sm text-[var(--fg)]">
          <span className="st-fg font-semibold">Something went wrong:</span> {error}
        </div>
      )}

      {/* Add a regulation */}
      <div className="glass space-y-3 rounded-3xl p-5 sm:p-6">
        <div>
          <h2 className="text-sm font-semibold uppercase tracking-wider text-[var(--muted)]">
            Add a regulation
          </h2>
          <p className="mt-1 text-xs text-[var(--muted)]">
            Upload an RBI circular (PDF, DOCX or TXT). We extract its requirements so you can check
            your policy against it — this can take a few minutes on the free tier.
          </p>
        </div>
        <form onSubmit={onAdd} className="space-y-2.5">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="RBI KYC Master Direction (2024 update)"
            className="glass-soft w-full rounded-xl px-3.5 py-2.5 text-sm text-[var(--fg)] outline-none placeholder:text-[var(--placeholder)] focus:border-[var(--line-strong)]"
          />
          <div className="flex flex-wrap items-center gap-3">
            <input
              ref={inputRef}
              type="file"
              accept=".pdf,.docx,.txt"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="hidden"
            />
            <button
              type="button"
              onClick={() => inputRef.current?.click()}
              className="glass-soft max-w-[16rem] truncate rounded-xl px-3.5 py-2.5 text-sm text-[var(--fg)] transition-colors hover:bg-[var(--hover)]"
            >
              {file ? file.name : "Choose file"}
            </button>
            <button
              type="submit"
              disabled={busy || !file || !name.trim()}
              className="accent-btn rounded-xl px-4 py-2.5 text-sm font-semibold disabled:opacity-50"
            >
              {busy ? "Adding…" : "Add regulation"}
            </button>
            {status && <span className="text-xs text-[var(--muted)]">{status}</span>}
          </div>
        </form>
      </div>

      {/* Available regulations */}
      <div className="glass space-y-4 rounded-3xl p-5 sm:p-6">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wider text-[var(--muted)]">
            Regulations
          </h2>
          <span className="text-xs tabular-nums text-[var(--muted)]">
            {loading ? "…" : `${regs.length} available`}
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
        ) : loading ? (
          <p className="text-sm text-[var(--muted)]">Loading…</p>
        ) : (
          <p className="text-sm italic text-[var(--muted)]">
            No regulations yet — add one above.
          </p>
        )}
        <p className="pt-1 text-xs text-[var(--muted)]">
          Pick a regulation on the <span className="text-[var(--fg)]">Gap check</span> screen to run a check.
        </p>
      </div>
    </div>
  );
}
