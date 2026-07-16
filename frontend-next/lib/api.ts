// API client for the compliance backend. runCheck() consumes the SSE stream of
// POST /api/compliance/check (or replays demo data). Kept framework-free so the
// same code powers demo mode and live mode.

import { demoStream, DEMO_REGULATION } from "./demoData";
import type { Session } from "./session";
import type {
  ChatEvent,
  CheckSummary,
  GapRow,
  PersistedCheck,
  Regulation,
  StreamEvent,
} from "./types";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// Hardcoding localhost broke LAN access: a page opened at http://10.x.x.x:3000
// still called http://localhost:8000 (unreachable or CORS-blocked) and every API
// call failed with "Failed to fetch". Default to the host the page was loaded
// from; NEXT_PUBLIC_API_BASE still overrides for real deployments.
export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, "") ||
  (typeof window !== "undefined"
    ? `http://${window.location.hostname}:8000`
    : "http://localhost:8000");

export interface RunOptions {
  regulationId: string;
  demo?: boolean;
  token?: string;
  policyLabel?: string;
}

function authHeaders(token?: string): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** Parse a Fetch SSE body, invoking onEvent for each `data:` JSON payload. */
async function readSSE<T>(
  body: ReadableStream<Uint8Array>,
  onEvent: (e: T) => void,
) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    // SSE events are separated by a blank line.
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const dataLine = frame
        .split("\n")
        .find((l) => l.startsWith("data:"));
      if (!dataLine) continue;
      const data = dataLine.slice(5).trim();
      if (data === "[DONE]") return;
      try {
        onEvent(JSON.parse(data) as T);
      } catch {
        /* ignore keep-alives / malformed frames */
      }
    }
  }
}

export async function runCheck(
  opts: RunOptions,
  onEvent: (e: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  if (opts.demo) {
    await demoStream(onEvent, signal);
    return;
  }
  const res = await fetch(`${API_BASE}/api/compliance/check`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(opts.token) },
    body: JSON.stringify({
      regulation_id: opts.regulationId,
      policy_label: opts.policyLabel,
    }),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(
      res.status === 401
        ? "Your session expired — sign in again."
        : await errorDetail(res, `Check failed (HTTP ${res.status}).`),
    );
  }
  await readSSE(res.body, onEvent);
}

/** Re-check a prior check against the CURRENT version of its regulation, streaming
 *  only the re-judged deltas + carried-forward rows (POST /api/compliance/recheck). */
export async function recheck(
  checkId: string,
  token: string | undefined,
  onEvent: (e: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/compliance/recheck`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(token) },
    body: JSON.stringify({ check_id: checkId }),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(
      res.status === 401
        ? "Your session expired — sign in again."
        : await errorDetail(res, `Re-check failed (HTTP ${res.status}).`),
    );
  }
  await readSSE(res.body, onEvent);
}

export async function listRegulations(token?: string): Promise<Regulation[]> {
  const res = await fetch(`${API_BASE}/api/compliance/regulations`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(await errorDetail(res, `Could not load regulations (HTTP ${res.status}).`));
  const data = await res.json();
  return (data.regulations || []) as Regulation[];
}

// ── Auth + documents (self-serve Live mode) ─────────────────────────────────

/** Flatten an error payload into readable text. FastAPI validation errors
 *  (HTTP 422) put an ARRAY of objects in `detail` — returning that as-is made
 *  React render the message as "[object Object]". */
function detailToText(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail.trim() || fallback;
  if (Array.isArray(detail)) {
    const msgs = detail.map((d) =>
      d && typeof d === "object" && "msg" in d
        ? String((d as { msg: unknown }).msg)
        : JSON.stringify(d),
    );
    if (msgs.length) return msgs.join("; ");
  }
  if (detail != null) {
    try {
      return JSON.stringify(detail);
    } catch {
      /* fall through to the fallback */
    }
  }
  return fallback;
}

async function errorDetail(res: Response, fallback: string): Promise<string> {
  try {
    const data = await res.json();
    return detailToText(data?.detail ?? data?.message, fallback);
  } catch {
    return fallback;
  }
}

export interface DocInfo {
  filename: string;
  file_type?: string;
  size_bytes?: number;
  uploaded_at?: string;
}

export async function login(email: string, password: string): Promise<Session> {
  const res = await fetch(`${API_BASE}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) throw new Error(await errorDetail(res, "Invalid email or password."));
  const data = await res.json();
  return {
    accessToken: data.access_token,
    refreshToken: data.refresh_token,
    email: data.email,
  };
}

/** Exchange the refresh token for a fresh access+refresh pair. Throws with
 *  `.status` set so callers can tell "refresh token rejected" (401 → sign the
 *  user out) from a transient failure (keep the session, retry later). */
export async function refreshSession(refreshToken: string): Promise<Session> {
  const res = await fetch(`${API_BASE}/api/auth/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  });
  if (!res.ok) {
    const err = new Error(await errorDetail(res, `Session refresh failed (HTTP ${res.status}).`)) as Error & { status?: number };
    err.status = res.status;
    throw err;
  }
  const data = await res.json();
  return {
    accessToken: data.access_token,
    refreshToken: data.refresh_token,
    email: data.email,
  };
}

export async function signup(email: string, password: string): Promise<string> {
  const res = await fetch(`${API_BASE}/api/auth/signup`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) throw new Error(await errorDetail(res, "Sign-up failed."));
  const data = await res.json();
  return (data.message as string) || "Account created. Check your email, then sign in.";
}

export async function listDocuments(token: string): Promise<DocInfo[]> {
  const res = await fetch(`${API_BASE}/api/documents/`, { headers: authHeaders(token) });
  if (!res.ok) throw new Error(await errorDetail(res, `Could not load documents (HTTP ${res.status}).`));
  const data = await res.json();
  return (data.documents || []) as DocInfo[];
}

/** Upload a policy file, then poll the background ingestion job to completion.
 *  Returns the chunk count on success; throws with a readable message on
 *  rejection, failure, or timeout. */
export async function uploadPolicy(
  file: File,
  token: string,
  onStatus?: (status: string) => void,
): Promise<number> {
  const form = new FormData();
  form.append("file", file);
  // Do NOT set Content-Type — the browser adds the multipart boundary itself.
  const res = await fetch(`${API_BASE}/api/documents/upload`, {
    method: "POST",
    headers: authHeaders(token),
    body: form,
  });
  if (res.status !== 202) throw new Error(await errorDetail(res, `Upload failed (HTTP ${res.status}).`));
  const { job_id: jobId } = await res.json();

  for (let i = 0; i < 150; i++) {            // ~5 min ceiling at 2s/poll
    await sleep(2000);
    const s = await fetch(`${API_BASE}/api/documents/upload-status/${jobId}`, {
      headers: authHeaders(token),
    });
    if (!s.ok) throw new Error(await errorDetail(s, `Status check failed (HTTP ${s.status}).`));
    const job = await s.json();
    onStatus?.(job.status);
    if (job.status === "completed") return job.chunks_ingested ?? 0;
    if (job.status === "failed") throw new Error(job.error || "Ingestion failed.");
  }
  throw new Error("Upload is taking too long — check back later.");
}

/** Delete one of the user's uploaded documents (storage + metadata + Pinecone
 *  vectors) via DELETE /api/documents/{filename}. */
export async function deleteDocument(filename: string, token: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/documents/${encodeURIComponent(filename)}`, {
    method: "DELETE",
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(await errorDetail(res, `Delete failed (HTTP ${res.status}).`));
}

/** Upload a regulation (circular) PDF, then poll the background extraction job.
 *  Extraction is an LLM step, so this can take minutes on a free judge tier. */
export async function uploadRegulation(
  file: File,
  name: string,
  token: string,
  opts?: { regulator?: string; onStatus?: (status: string) => void },
): Promise<{ regulationId: string; requirements: number }> {
  const form = new FormData();
  form.append("file", file);
  form.append("name", name);
  if (opts?.regulator) form.append("regulator", opts.regulator);
  const res = await fetch(`${API_BASE}/api/compliance/regulations`, {
    method: "POST",
    headers: authHeaders(token),
    body: form,
  });
  if (res.status !== 202) throw new Error(await errorDetail(res, `Upload failed (HTTP ${res.status}).`));
  const { job_id: jobId } = await res.json();

  for (let i = 0; i < 300; i++) {            // ~15 min ceiling (extraction is slow)
    await sleep(3000);
    const s = await fetch(`${API_BASE}/api/compliance/regulations/upload-status/${jobId}`, {
      headers: authHeaders(token),
    });
    if (!s.ok) throw new Error(await errorDetail(s, `Status check failed (HTTP ${s.status}).`));
    const job = await s.json();
    opts?.onStatus?.(job.status);
    if (job.status === "completed") {
      return { regulationId: job.regulation_id, requirements: job.requirements ?? 0 };
    }
    if (job.status === "failed") throw new Error(job.error || "Processing failed.");
  }
  throw new Error("Processing is taking too long — check back later.");
}

export async function listChecks(token: string): Promise<CheckSummary[]> {
  const res = await fetch(`${API_BASE}/api/compliance/checks`, { headers: authHeaders(token) });
  if (!res.ok) throw new Error(await errorDetail(res, `Could not load checks (HTTP ${res.status}).`));
  const data = await res.json();
  return (data.checks || []) as CheckSummary[];
}

export async function getCheck(token: string, id: string): Promise<PersistedCheck> {
  const res = await fetch(`${API_BASE}/api/compliance/checks/${id}`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(await errorDetail(res, `Could not load check (HTTP ${res.status}).`));
  return (await res.json()) as PersistedCheck;
}

/** Draft a suggested policy clause to close one gap row (POST /compliance/suggest).
 *  Grounded in the RBI requirement; returned as a draft for human review. */
export async function suggestFix(row: GapRow, token: string): Promise<string> {
  const res = await fetch(`${API_BASE}/api/compliance/suggest`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(token) },
    body: JSON.stringify({
      requirement: row.requirement,
      status: row.status,
      policy_clause: row.policy_clause || row.policy_quote || "",
      rationale: row.rationale || "",
    }),
  });
  if (!res.ok) throw new Error(await errorDetail(res, `Suggestion failed (HTTP ${res.status}).`));
  const data = await res.json();
  return (data.suggestion as string) || "";
}

/** Stream an answer to *question* over the user's own documents (the Ask
 *  screen), reusing POST /api/chat/query/stream. */
export async function askStream(
  question: string,
  token: string,
  onEvent: (e: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/chat/query/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(token) },
    body: JSON.stringify({ question }),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(
      res.status === 401
        ? "Your session expired — sign in again."
        : await errorDetail(res, `Ask failed (HTTP ${res.status}).`),
    );
  }
  await readSSE<ChatEvent>(res.body, onEvent);
}

export { DEMO_REGULATION };
