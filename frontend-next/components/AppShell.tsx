"use client";

import { useEffect, useState } from "react";

import {
  clearSession,
  getSession,
  setSession as saveSession,
  type Session,
} from "@/lib/session";
import AskPanel from "./AskPanel";
import CheckHero from "./CheckHero";
import LibraryScreen from "./LibraryScreen";

type View = "check" | "ask" | "library";

const HEADERS: Record<View, { title: string; subtitle: string }> = {
  check: {
    title: "Compliance gap analysis",
    subtitle:
      "A cited, requirement-by-requirement gap table — each clause judged Covered, Partial, Gap, or Conflict.",
  },
  ask: {
    title: "Ask your policy",
    subtitle: "Ask a question and get a cited answer from your uploaded documents.",
  },
  library: {
    title: "Your library",
    subtitle: "Manage your uploaded policies and browse available regulations.",
  },
};

export default function AppShell() {
  const [session, setSession] = useState<Session | null>(null);
  const [view, setView] = useState<View>("check");

  useEffect(() => setSession(getSession()), []);        // rehydrate once, app-wide

  function onSignedIn(s: Session) {
    saveSession(s);
    setSession(s);
  }
  function onSignOut() {
    clearSession();
    setSession(null);
  }

  return (
    <>
      <header className="mb-6">
        <div className="mb-3 inline-flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--surface-soft)] px-3 py-1 text-xs font-medium text-[var(--muted)]">
          <span className="st-conflict st-bar h-1.5 w-1.5 rounded-full" />
          RBI KYC · India
        </div>
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-3xl font-semibold tracking-tight text-[var(--fg)] sm:text-4xl">
              {HEADERS[view].title}
            </h1>
            <p className="mt-2 max-w-2xl text-[15px] leading-relaxed text-[var(--muted)]">
              {HEADERS[view].subtitle}
            </p>
          </div>
          <Nav view={view} setView={setView} />
        </div>
      </header>

      {view === "check" ? (
        <CheckHero session={session} onSignedIn={onSignedIn} onSignOut={onSignOut} />
      ) : view === "ask" ? (
        <AskPanel session={session} onSignedIn={onSignedIn} />
      ) : (
        <LibraryScreen session={session} onSignedIn={onSignedIn} />
      )}

      <footer className="mt-10 border-t border-[var(--line)] pt-5 text-xs text-[var(--muted)]">
        Assisted review — not legal advice. Every finding is cited to a clause
        for a human to verify.
      </footer>
    </>
  );
}

function Nav({ view, setView }: { view: View; setView: (v: View) => void }) {
  const items: [View, string][] = [
    ["check", "Gap check"],
    ["ask", "Ask"],
    ["library", "Library"],
  ];
  return (
    <div className="glass-soft flex rounded-xl p-1 text-sm">
      {items.map(([v, label]) => (
        <button
          key={v}
          onClick={() => setView(v)}
          className={`rounded-lg px-3.5 py-1.5 font-medium transition-colors ${
            view === v
              ? "bg-[var(--active)] text-[var(--fg)]"
              : "text-[var(--muted)] hover:text-[var(--fg)]"
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
