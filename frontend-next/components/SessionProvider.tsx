"use client";

import { createContext, useContext, useEffect, useState } from "react";

import {
  clearSession,
  getSession,
  setSession as persistSession,
  type Session,
} from "@/lib/session";

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
