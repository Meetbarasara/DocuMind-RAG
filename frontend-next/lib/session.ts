// Minimal localStorage-backed auth session. Every access is guarded so SSR
// (no window) and privacy-mode (throwing localStorage) degrade to "signed out"
// rather than crashing.

export interface Session {
  accessToken: string;
  refreshToken: string;
  email: string;
}

const KEY = "kyc.session";

export function getSession(): Session | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

export function setSession(s: Session): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    /* privacy mode / quota — session just won't persist across reloads */
  }
}

export function clearSession(): void {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
}
