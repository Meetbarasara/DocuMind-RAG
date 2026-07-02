// API client for the compliance backend. runCheck() consumes the SSE stream of
// POST /api/compliance/check (or replays demo data). Kept framework-free so the
// same code powers demo mode and live mode.

import { demoStream, DEMO_REGULATION } from "./demoData";
import type { Regulation, StreamEvent } from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, "") || "http://localhost:8000";

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
async function readSSE(
  body: ReadableStream<Uint8Array>,
  onEvent: (e: StreamEvent) => void,
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
        onEvent(JSON.parse(data) as StreamEvent);
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
        ? "Not signed in (401). Add a token in Live settings."
        : `Check failed (HTTP ${res.status}).`,
    );
  }
  await readSSE(res.body, onEvent);
}

export async function listRegulations(token?: string): Promise<Regulation[]> {
  const res = await fetch(`${API_BASE}/api/compliance/regulations`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Could not load regulations (HTTP ${res.status}).`);
  const data = await res.json();
  return (data.regulations || []) as Regulation[];
}

export { DEMO_REGULATION };
