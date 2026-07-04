// Shared types — mirror the SSE contract of POST /api/compliance/check.

export type Status =
  | "Covered"
  | "Partial"
  | "Gap"
  | "Conflict"
  | "Needs review";

export interface GapRow {
  requirement_id: string;
  requirement: string;
  rbi_page: number | null;
  rbi_section: string | null;
  status: Status | string;
  confidence: number;
  rationale: string;
  policy_quote: string;
  // Clause-level citation verification (see compliance._verify_evidence). Optional
  // so persisted checks from before this change still render.
  policy_clause?: string; // the verbatim source clause the quote grounds to
  policy_filename: string | null;
  policy_page: number | null;
  evidence_score?: number; // 0-1 grounding score
  evidence_verified?: boolean; // the quote grounded in a real clause
  // Change-tracking (a re-check row): what happened to this requirement since the
  // prior check, and whether its verdict was carried forward (not re-judged).
  change?: "unchanged" | "changed" | "added";
  carried_forward?: boolean;
}

// How a regulation's requirements changed between two versions (change-tracking).
export interface DeltaCounts {
  unchanged: number;
  changed: number;
  added: number;
  removed: number;
}

export interface Summary {
  total: number;
  Covered: number;
  Partial: number;
  Gap: number;
  Conflict: number;
  "Needs review": number;
  delta?: DeltaCounts; // present on a persisted re-check
}

export interface Regulation {
  id: string;
  name: string;
  regulator?: string | null;
}

export interface CheckSummary {
  id: string;
  policy_label: string;
  regulation_id: string | null;
  summary: Summary;
  created_at: string;
}

export interface PersistedCheck extends CheckSummary {
  rows: GapRow[];
}

export type StreamEvent =
  | {
      type: "summary_init";
      total: number;
      regulation: { id?: string; name?: string; regulator?: string };
      delta?: DeltaCounts; // present on a re-check (POST /recheck)
    }
  | { type: "row"; checked: number; total: number; row: GapRow }
  | { type: "summary_final"; summary: Summary; check_id: string | null; delta?: DeltaCounts }
  | { type: "error"; message: string };

// SSE events from POST /api/chat/query/stream (the Ask screen).
export type ChatEvent =
  | { type: "sources"; sources: ChatSource[] }
  | { type: "token"; content: string }
  | { type: "citation_verification"; verified?: number; total?: number }
  | { type: "meta"; run_id?: string }
  | { type: "error"; message: string };

export interface ChatSource {
  filename?: string;
  page?: number | string;
  content?: string;
  [k: string]: unknown;
}

export const STATUS_ORDER: Status[] = [
  "Covered",
  "Partial",
  "Gap",
  "Conflict",
  "Needs review",
];

// status -> the CSS accent class defined in globals.css
export const STATUS_CLASS: Record<string, string> = {
  Covered: "st-covered",
  Partial: "st-partial",
  Gap: "st-gap",
  Conflict: "st-conflict",
  "Needs review": "st-review",
};
