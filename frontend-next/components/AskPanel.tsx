"use client";

import { useRef, useState } from "react";

import { askStream } from "@/lib/api";
import type { Session } from "@/lib/session";
import type { ChatSource } from "@/lib/types";
import SignIn from "./SignIn";

type Phase = "idle" | "thinking" | "streaming" | "done" | "error";

// Retrieval runs (and streams its sources) before the model answers, so even a
// greeting or an unanswerable question comes back with the top-k chunks attached.
// When the model refuses, those sources are misleading — suppress them. Matches
// both refusal strings the backend can emit (generation.py / pipeline.py).
const REFUSAL_PREFIXES = [
  "i cannot find information about your question",
  "i couldn't find any relevant information",
];
function isRefusal(answer: string): boolean {
  const a = answer.trim().toLowerCase();
  return REFUSAL_PREFIXES.some((r) => a.startsWith(r));
}

function srcCite(s: ChatSource): string {
  const f = s.filename ?? "source";
  const p = s.page;
  return p != null && p !== "N/A" && p !== "" ? `${f} · p.${p}` : String(f);
}

export default function AskPanel({
  session,
  onSignedIn,
}: {
  session: Session | null;
  onSignedIn: (s: Session) => void;
}) {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [sources, setSources] = useState<ChatSource[]>([]);
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  async function ask() {
    if (!session) return;
    const q = question.trim();
    if (!q) return;
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    setAnswer("");
    setSources([]);
    setError(null);
    setPhase("thinking");
    try {
      await askStream(
        q,
        session.accessToken,
        (e) => {
          if (ac.signal.aborted) return;
          if (e.type === "sources") setSources((e.sources as ChatSource[]) || []);
          else if (e.type === "token") {
            setAnswer((prev) => prev + e.content);
            setPhase("streaming");
          } else if (e.type === "error") setError(e.message);
        },
        ac.signal,
      );
      if (!ac.signal.aborted) setPhase((p) => (p === "error" ? p : "done"));
    } catch (err) {
      if (ac.signal.aborted) return;
      setError(err instanceof Error ? err.message : "Ask failed.");
      setPhase("error");
    }
  }

  if (!session) {
    return (
      <div className="glass space-y-3 rounded-3xl p-5 sm:p-6">
        <p className="text-sm text-[var(--muted)]">
          Sign in to ask questions about your uploaded policy.
        </p>
        <SignIn onSignedIn={onSignedIn} />
      </div>
    );
  }

  const busy = phase === "thinking" || phase === "streaming";
  return (
    <div className="space-y-5">
      <div className="glass space-y-3 rounded-3xl p-5 sm:p-6">
        <label className="block text-xs font-semibold uppercase tracking-wider text-[var(--muted)]">
          Question
        </label>
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) ask();
          }}
          rows={2}
          placeholder="e.g. How long do we retain KYC records?"
          className="glass-soft w-full resize-none rounded-xl px-3.5 py-2.5 text-sm text-[var(--fg)] outline-none placeholder:text-[var(--placeholder)] focus:border-[var(--line-strong)]"
        />
        <div className="flex items-center justify-between">
          <span className="text-xs text-[var(--muted)]">
            Answers from your uploaded documents, cited.
          </span>
          <button
            onClick={ask}
            disabled={busy || !question.trim()}
            className="accent-btn rounded-xl px-5 py-2.5 text-sm font-semibold"
          >
            {busy ? "Answering…" : "Ask"}
          </button>
        </div>
      </div>

      {error && (
        <div className="st-gap glass st-ring rounded-2xl px-4 py-3 text-sm text-[var(--fg)]">
          <span className="st-fg font-semibold">Couldn’t answer:</span> {error}
        </div>
      )}

      {phase !== "idle" && !error && (
        <div className="glass space-y-4 rounded-3xl p-5 sm:p-6">
          <div className="whitespace-pre-wrap text-sm leading-relaxed text-[var(--fg)]">
            {answer || (
              phase === "thinking" ? (
                <span className="text-[var(--muted)]">Thinking…</span>
              ) : null
            )}
          </div>
          {phase === "done" && !isRefusal(answer) && sources.length > 0 && (
            <div className="border-t border-[var(--line)] pt-3">
              <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-[var(--muted)]">
                Sources
              </div>
              <ul className="space-y-2">
                {sources.map((s, i) => (
                  <li key={i} className="glass-soft rounded-lg px-3 py-2">
                    <div className="font-mono text-xs text-[var(--fg)]">{srcCite(s)}</div>
                    {s.content ? (
                      <div className="mt-1 line-clamp-2 text-xs text-[var(--muted)]">
                        {String(s.content)}
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
