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
  policy_filename: string | null;
  policy_page: number | null;
}

export interface Summary {
  total: number;
  Covered: number;
  Partial: number;
  Gap: number;
  Conflict: number;
  "Needs review": number;
}

export interface Regulation {
  id: string;
  name: string;
  regulator?: string | null;
}

export type StreamEvent =
  | {
      type: "summary_init";
      total: number;
      regulation: { id?: string; name?: string; regulator?: string };
    }
  | { type: "row"; checked: number; total: number; row: GapRow }
  | { type: "summary_final"; summary: Summary; check_id: string | null }
  | { type: "error"; message: string };

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
