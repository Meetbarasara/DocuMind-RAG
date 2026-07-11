"use client";

import { useRef, useState } from "react";

import { uploadPolicy, type DocInfo } from "@/lib/api";

function TrashIcon() {
  return (
    <svg viewBox="0 0 20 20" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.7">
      <path d="M4 6h12M8 6V4.5A1.5 1.5 0 0 1 9.5 3h1A1.5 1.5 0 0 1 12 4.5V6m2 0v9a1.5 1.5 0 0 1-1.5 1.5h-5A1.5 1.5 0 0 1 6 15V6"
        strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export default function PolicyUpload({
  token,
  docs,
  onChanged,
  onDelete,
  deleting,
}: {
  token: string;
  docs: DocInfo[];
  onChanged: () => void;
  onDelete?: (filename: string) => void;   // when set, each doc gets a delete button
  deleting?: string | null;                // filename currently being deleted
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function onPick(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setError(null);
    setBusy(true);
    setStatus("uploading…");
    try {
      const n = await uploadPolicy(file, token, (s) =>
        setStatus(s === "processing" ? "ingesting…" : s),
      );
      setStatus(`added · ${n} chunks`);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed.");
      setStatus(null);
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  return (
    <div className="space-y-2">
      <input
        ref={inputRef}
        type="file"
        accept=".pdf,.docx,.txt"
        onChange={onPick}
        className="hidden"
      />
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          disabled={busy}
          onClick={() => inputRef.current?.click()}
          className="glass-soft rounded-xl px-3.5 py-2.5 text-sm text-[var(--fg)] transition-colors hover:bg-[var(--hover)] disabled:opacity-50"
        >
          {busy ? "Uploading…" : "Upload policy"}
        </button>
        <span className="text-xs text-[var(--muted)]">
          {status ?? "PDF, DOCX or TXT · ingested into your private namespace"}
        </span>
      </div>
      {error && <p className="st-gap st-fg text-sm">{error}</p>}
      {docs.length > 0 && (
        <ul className="space-y-1 pt-1 text-xs">
          {docs.map((d) => (
            <li key={d.filename} className="flex items-center gap-2">
              <span className="st-covered st-bar h-1.5 w-1.5 shrink-0 rounded-full" />
              <span className="min-w-0 flex-1 truncate text-[var(--fg)]">{d.filename}</span>
              {onDelete && (
                <button
                  type="button"
                  onClick={() => onDelete(d.filename)}
                  disabled={deleting === d.filename}
                  title={`Delete ${d.filename}`}
                  aria-label={`Delete ${d.filename}`}
                  className="shrink-0 rounded-md p-1 text-[var(--muted)] transition-colors hover:bg-[var(--hover)] hover:text-[var(--danger)] disabled:opacity-50"
                >
                  {deleting === d.filename ? "…" : <TrashIcon />}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
