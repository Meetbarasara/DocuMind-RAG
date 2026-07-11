"use client";

import { useEffect, useState } from "react";

import { deleteDocument, listDocuments, type DocInfo } from "@/lib/api";
import { useSession } from "@/components/SessionProvider";
import PolicyUpload from "./PolicyUpload";

export default function PoliciesPanel() {
  const { session } = useSession();
  const token = session?.accessToken;
  const [docs, setDocs] = useState<DocInfo[]>([]);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    listDocuments(token)
      .then((d) => alive && setDocs(d))
      .catch(() => {/* non-fatal */});
    return () => {
      alive = false;
    };
  }, [token]);

  function reload() {
    if (token) listDocuments(token).then(setDocs).catch(() => {});
  }

  async function onDelete(filename: string) {
    if (!token) return;
    if (!window.confirm(`Delete “${filename}”? This removes the file and its indexed vectors.`)) {
      return;
    }
    setError(null);
    setDeleting(filename);
    try {
      await deleteDocument(filename, token);
      setDocs((prev) => prev.filter((d) => d.filename !== filename));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed.");
    } finally {
      setDeleting(null);
    }
  }

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
            Your policies
          </h2>
          <span className="text-xs tabular-nums text-[var(--muted)]">
            {docs.length} document{docs.length === 1 ? "" : "s"}
          </span>
        </div>
        <PolicyUpload
          token={token}
          docs={docs}
          onChanged={reload}
          onDelete={onDelete}
          deleting={deleting}
        />
        {docs.length === 0 && (
          <p className="text-sm italic text-[var(--muted)]">
            Upload your KYC policy to check it against a regulation.
          </p>
        )}
      </div>
    </div>
  );
}
