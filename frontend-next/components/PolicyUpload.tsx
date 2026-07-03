"use client";

import { useRef, useState } from "react";

import { uploadPolicy, type DocInfo } from "@/lib/api";

export default function PolicyUpload({
  token,
  docs,
  onChanged,
}: {
  token: string;
  docs: DocInfo[];
  onChanged: () => void;
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
          className="glass-soft rounded-xl px-3.5 py-2.5 text-sm text-[var(--fg)] transition-colors hover:bg-white/[0.06] disabled:opacity-50"
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
              <span className="st-covered st-bar h-1.5 w-1.5 rounded-full" />
              <span className="text-[var(--fg)]">{d.filename}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
