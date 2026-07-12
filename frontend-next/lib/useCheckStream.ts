"use client";

import { useCallback, useRef, useState } from "react";

import type { DeltaCounts, GapRow, StreamEvent } from "./types";

export type Phase = "idle" | "running" | "done" | "error";

// The three compliance streams — POST /check, POST /recheck, and the demo replay
// — all emit the same summary_init → row* → summary_final sequence. This hook
// consumes that sequence into render-ready state, so the new-check screen, the
// re-check on a saved check, and the public demo each just hand `start` a
// different runner instead of duplicating the SSE bookkeeping.
export type Runner = (
  onEvent: (e: StreamEvent) => void,
  signal: AbortSignal,
) => Promise<void>;

export interface CheckStream {
  rows: GapRow[];
  total: number;
  regName: string;
  delta: DeltaCounts | null;
  phase: Phase;
  error: string | null;
  checkId: string | null;
  start: (runner: Runner) => Promise<void>;
  reset: () => void;
  abort: () => void;
}

export function useCheckStream(): CheckStream {
  const [rows, setRows] = useState<GapRow[]>([]);
  const [total, setTotal] = useState(0);
  const [regName, setRegName] = useState("");
  const [delta, setDelta] = useState<DeltaCounts | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [checkId, setCheckId] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const abort = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setRows([]);
    setTotal(0);
    setRegName("");
    setDelta(null);
    setPhase("idle");
    setError(null);
    setCheckId(null);
  }, []);

  const start = useCallback(async (runner: Runner) => {
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    setRows([]);
    setTotal(0);
    setRegName("");
    setDelta(null);
    setError(null);
    setCheckId(null);
    setPhase("running");
    try {
      await runner((e) => {
        if (ac.signal.aborted) return;
        if (e.type === "summary_init") {
          setTotal(e.total);
          setRegName(e.regulation?.name || "");
          if (e.delta) setDelta(e.delta);
        } else if (e.type === "row") {
          setRows((prev) => [...prev, e.row]);
        } else if (e.type === "summary_final") {
          if (e.delta) setDelta(e.delta);
          setCheckId(e.check_id);
          setPhase("done");
        } else if (e.type === "error") {
          setError(e.message);
        }
      }, ac.signal);
      if (!ac.signal.aborted) setPhase((p) => (p === "running" ? "done" : p));
    } catch (err) {
      if (ac.signal.aborted) return;
      setError(err instanceof Error ? err.message : "Check failed.");
      setPhase("error");
    }
  }, []);

  return { rows, total, regName, delta, phase, error, checkId, start, reset, abort };
}
