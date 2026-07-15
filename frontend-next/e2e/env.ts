// Shared plumbing for the E2E harness: locate the repo's real .env (same
// walk-up behavior as the backend's load_dotenv, so the harness works from the
// main checkout AND from a git worktree) and the project venv's Python.

import fs from "node:fs";
import path from "node:path";

export function findUp(rel: string, from: string = __dirname): string | null {
  let dir = from;
  for (;;) {
    const candidate = path.join(dir, rel);
    if (fs.existsSync(candidate)) return candidate;
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

/** Minimal .env parser — KEY=VALUE lines, ignores comments/blanks. */
export function loadDotenv(): Record<string, string> {
  const file = findUp(".env");
  if (!file) return {};
  const out: Record<string, string> = {};
  for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    if (line.trim().startsWith("#")) continue;
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (m) out[m[1]] = m[2].replace(/^["']|["']$/g, "");
  }
  return out;
}

/** The project venv's Python (walks up like .env), falling back to PATH. */
export function findPython(): string {
  const rel =
    process.platform === "win32" ? "venv/Scripts/python.exe" : "venv/bin/python";
  return process.env.DOCUMIND_PYTHON || findUp(rel) || "python";
}

// Backend AuthRequest uses pydantic EmailStr, which rejects reserved TLDs
// (.test, .example, .localhost) — so E2E accounts need a plausible, routable
// -looking domain. Unregistered; only the signup spec could ever trigger a
// (bounced) confirmation email to it, and only while "Confirm email" is ON.
export const E2E_EMAIL_DOMAIN = "documind-e2e-tests.com";

export function supabaseEnv() {
  const env = { ...loadDotenv(), ...process.env };
  const url = env.SUPABASE_URL;
  const service = env.SUPABASE_SERVICE_ROLE_KEY;
  const anon = env.SUPABASE_ANON_KEY;
  if (!url || !service || !anon) {
    throw new Error(
      "SUPABASE_URL / SUPABASE_ANON_KEY / SUPABASE_SERVICE_ROLE_KEY not found " +
        "(looked in process env and the nearest .env walking up from e2e/).",
    );
  }
  return { url: url.replace(/\/$/, ""), service, anon };
}
