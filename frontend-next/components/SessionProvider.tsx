"use client";

import { createContext, useContext, useEffect, useState } from "react";

import { refreshSession } from "@/lib/api";
import {
  clearSession,
  getSession,
  setSession as persistSession,
  type Session,
} from "@/lib/session";

/** Seconds-since-epoch expiry of a JWT, or null if it can't be decoded. */
function tokenExp(jwt: string): number | null {
  try {
    const payload = JSON.parse(
      atob(jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")),
    );
    return typeof payload.exp === "number" ? payload.exp : null;
  } catch {
    return null;
  }
}

interface SessionCtx {
  session: Session | null;
  ready: boolean; // false until the localStorage session has been read (client-only)
  signIn: (s: Session) => void;
  signOut: () => void;
}

const Ctx = createContext<SessionCtx | null>(null);

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);

  // Rehydrate once on mount (localStorage isn't available during SSR).
  useEffect(() => {
    setSession(getSession());
    setReady(true);
  }, []);

  function signIn(s: Session) {
    persistSession(s);
    setSession(s);
  }
  function signOut() {
    clearSession();
    setSession(null);
  }

  // Keep the session alive: Supabase access tokens expire after ~1h and the
  // app previously never renewed them — an open tab (or a returning visitor)
  // silently started failing every call. Renew when <10 min of life remains,
  // checked on rehydrate, once a minute, and when the tab regains focus. A
  // rejected refresh token (401) signs out; a transient failure keeps the
  // session and simply retries on the next tick.
  useEffect(() => {
    if (!ready || !session?.refreshToken) return;
    let alive = true;
    const maybeRefresh = async () => {
      const exp = tokenExp(session.accessToken);
      if (exp === null || exp - Date.now() / 1000 > 10 * 60) return;
      try {
        const renewed = await refreshSession(session.refreshToken);
        if (alive) {
          persistSession(renewed);
          setSession(renewed);
        }
      } catch (err) {
        if (alive && (err as { status?: number }).status === 401) {
          clearSession();
          setSession(null);
        }
      }
    };
    maybeRefresh();
    const timer = setInterval(maybeRefresh, 60_000);
    window.addEventListener("focus", maybeRefresh);
    return () => {
      alive = false;
      clearInterval(timer);
      window.removeEventListener("focus", maybeRefresh);
    };
  }, [ready, session]);

  return (
    <Ctx.Provider value={{ session, ready, signIn, signOut }}>
      {children}
    </Ctx.Provider>
  );
}

export function useSession(): SessionCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useSession must be used inside <SessionProvider>");
  return ctx;
}
