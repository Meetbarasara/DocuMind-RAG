"use client";

import { useState } from "react";

import { login, signup } from "@/lib/api";
import type { Session } from "@/lib/session";

export default function SignIn({
  onSignedIn,
}: {
  onSignedIn: (s: Session) => void;
}) {
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setNotice(null);
    setBusy(true);
    try {
      if (mode === "login") {
        onSignedIn(await login(email.trim(), password));
      } else {
        setNotice(await signup(email.trim(), password));
        setMode("login");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  const input =
    "glass-soft w-full rounded-xl px-3.5 py-2.5 text-sm text-[var(--fg)] outline-none placeholder:text-white/30 focus:border-white/25";

  return (
    <form onSubmit={submit} className="max-w-sm space-y-3">
      <div className="glass-soft flex w-fit rounded-xl p-1 text-sm">
        {(["login", "signup"] as const).map((m) => (
          <button
            type="button"
            key={m}
            onClick={() => {
              setMode(m);
              setError(null);
              setNotice(null);
            }}
            className={`rounded-lg px-3 py-1.5 font-medium transition-colors ${
              mode === m
                ? "bg-white/12 text-[var(--fg)]"
                : "text-[var(--muted)] hover:text-[var(--fg)]"
            }`}
          >
            {m === "login" ? "Sign in" : "Create account"}
          </button>
        ))}
      </div>
      <input
        type="email"
        required
        autoComplete="email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        placeholder="name@company.com"
        className={input}
      />
      <input
        type="password"
        required
        minLength={6}
        autoComplete={mode === "login" ? "current-password" : "new-password"}
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        placeholder="Password"
        className={input}
      />
      {error && <p className="st-gap st-fg text-sm">{error}</p>}
      {notice && <p className="st-covered st-fg text-sm">{notice}</p>}
      <button
        type="submit"
        disabled={busy}
        className="accent-btn rounded-xl px-5 py-2.5 text-sm font-semibold"
      >
        {busy ? "Working…" : mode === "login" ? "Sign in" : "Create account"}
      </button>
    </form>
  );
}
