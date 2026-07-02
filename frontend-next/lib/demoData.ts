// Demo data — the real gap table our engine produced on the synthetic
// Acme/RBI fixtures (cerebras/gpt-oss-120b), so the hero is fully viewable
// without a backend login. The same components render live data in real mode.

import type { GapRow, Regulation, StreamEvent, Summary } from "./types";

export const DEMO_REGULATION: Regulation = {
  id: "demo-rbi-kyc",
  name: "RBI KYC (synthetic demo)",
  regulator: "RBI",
};

const P = "acme_kyc_policy.pdf";

export const DEMO_ROWS: GapRow[] = [
  {
    requirement_id: "req-1",
    requirement:
      "Identify every customer using an Officially Valid Document (OVD) at the commencement of an account-based relationship.",
    rbi_page: 1, rbi_section: "1", status: "Covered", confidence: 0.96,
    rationale: "Policy requires a verified OVD for every customer at onboarding.",
    policy_quote:
      "At onboarding, every customer must submit an Officially Valid Document (OVD) - passport, Aadhaar, voter ID or driving licence. No account is opened without a verified OVD on file.",
    policy_filename: P, policy_page: 1,
  },
  {
    requirement_id: "req-2",
    requirement: "Categorise every customer as low, medium or high risk based on a risk assessment.",
    rbi_page: 1, rbi_section: "2", status: "Covered", confidence: 0.95,
    rationale: "Policy classifies each customer into low/medium/high risk at onboarding.",
    policy_quote:
      "Each customer is classified as low, medium or high risk at onboarding based on occupation, geography and expected transaction profile.",
    policy_filename: P, policy_page: 1,
  },
  {
    requirement_id: "req-3",
    requirement: "Apply customer due diligence proportionate to the assessed risk.",
    rbi_page: 1, rbi_section: "2", status: "Covered", confidence: 0.9,
    rationale: "Due diligence is explicitly applied in proportion to the risk category.",
    policy_quote: "Due diligence is applied in proportion to the risk category.",
    policy_filename: P, policy_page: 1,
  },
  {
    requirement_id: "req-4",
    requirement: "Carry out Customer Due Diligence (CDD) at the commencement of the relationship.",
    rbi_page: 1, rbi_section: "3", status: "Covered", confidence: 0.94,
    rationale: "CDD is performed at the start of every customer relationship.",
    policy_quote:
      "CDD is performed for every customer at the start of the relationship and whenever we doubt the accuracy of previously collected information.",
    policy_filename: P, policy_page: 1,
  },
  {
    requirement_id: "req-5",
    requirement: "Carry out CDD when there are doubts about previously obtained customer data.",
    rbi_page: 1, rbi_section: "3", status: "Covered", confidence: 0.9,
    rationale: "Policy re-runs CDD whenever the accuracy of prior data is in doubt.",
    policy_quote:
      "CDD is performed for every customer at the start of the relationship and whenever we doubt the accuracy of previously collected information.",
    policy_filename: P, policy_page: 1,
  },
  {
    requirement_id: "req-6",
    requirement: "Appoint a Principal Officer responsible for KYC compliance and monitoring.",
    rbi_page: 3, rbi_section: "9", status: "Covered", confidence: 0.92,
    rationale: "A Principal Officer is appointed for KYC compliance and internal monitoring.",
    policy_quote:
      "The company has appointed a Principal Officer who is responsible for overseeing KYC compliance and for internal monitoring of customer accounts.",
    policy_filename: P, policy_page: 3,
  },
  {
    requirement_id: "req-7",
    requirement:
      "For customers assessed as high risk, apply enhanced due diligence including obtaining the source of funds.",
    rbi_page: 2, rbi_section: "4", status: "Partial", confidence: 0.72,
    rationale: "Enhanced checks are discretionary and do not require source of funds.",
    policy_quote:
      "For customers who appear to carry higher risk, additional checks may be carried out at the discretion of the compliance team.",
    policy_filename: P, policy_page: 2,
  },
  {
    requirement_id: "req-8",
    requirement:
      "When onboarding via V-CIP, record the customer's live location (geo-tagging) and store a recording of the full interaction.",
    rbi_page: 2, rbi_section: "6", status: "Partial", confidence: 0.7,
    rationale: "Remote video onboarding exists but omits geo-tagging and recording.",
    policy_quote:
      "Customers may be onboarded remotely through a video call in which an officer verifies the customer's OVD and photograph.",
    policy_filename: P, policy_page: 2,
  },
  {
    requirement_id: "req-9",
    requirement: "Update the KYC records of high-risk customers at least once every two years.",
    rbi_page: 2, rbi_section: "5", status: "Partial", confidence: 0.75,
    rationale: "Updation is risk-based but sets no two-year cadence for high-risk customers.",
    policy_quote:
      "Customer KYC information is reviewed and updated from time to time based on the customer's risk category, so that our records remain current.",
    policy_filename: P, policy_page: 2,
  },
  {
    requirement_id: "req-10",
    requirement: "Carry out CDD when a specified transaction threshold is crossed.",
    rbi_page: 1, rbi_section: "3", status: "Gap", confidence: 0.88,
    rationale: "No transaction-threshold trigger for CDD is present in the policy.",
    policy_quote: "", policy_filename: null, policy_page: null,
  },
  {
    requirement_id: "req-11",
    requirement: "For legal-entity customers, identify the beneficial owner(s) and verify their identity.",
    rbi_page: 2, rbi_section: "7", status: "Gap", confidence: 0.9,
    rationale: "The policy contains no beneficial-ownership identification provision.",
    policy_quote: "", policy_filename: null, policy_page: null,
  },
  {
    requirement_id: "req-12",
    requirement:
      "File Suspicious Transaction Reports (STRs) and Cash Transaction Reports (CTRs) with FIU-IND within the prescribed timelines.",
    rbi_page: 3, rbi_section: "10", status: "Gap", confidence: 0.93,
    rationale: "No FIU-IND / STR / CTR reporting obligation is addressed.",
    policy_quote: "", policy_filename: null, policy_page: null,
  },
  {
    requirement_id: "req-13",
    requirement:
      "Maintain records of client identity and transactions for at least five years after the account is closed.",
    rbi_page: 3, rbi_section: "8", status: "Conflict", confidence: 0.95,
    rationale: "Policy mandates a 3-year retention, contradicting the 5-year minimum.",
    policy_quote:
      "Records of customer identity and transactions are retained for a period of three years after the account is closed.",
    policy_filename: P, policy_page: 2,
  },
];

function summarise(rows: GapRow[]): Summary {
  const c: Record<string, number> = {
    Covered: 0, Partial: 0, Gap: 0, Conflict: 0, "Needs review": 0,
  };
  for (const r of rows) if (r.status in c) c[r.status] += 1;
  return {
    total: rows.length,
    Covered: c.Covered, Partial: c.Partial, Gap: c.Gap, Conflict: c.Conflict,
    "Needs review": c["Needs review"],
  };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Replay DEMO_ROWS as the same event sequence the live SSE stream emits. */
export async function demoStream(
  onEvent: (e: StreamEvent) => void,
  signal?: AbortSignal,
) {
  onEvent({
    type: "summary_init",
    total: DEMO_ROWS.length,
    regulation: {
      id: DEMO_REGULATION.id,
      name: DEMO_REGULATION.name,
      regulator: DEMO_REGULATION.regulator ?? undefined,
    },
  });
  await sleep(500);
  let checked = 0;
  for (const row of DEMO_ROWS) {
    if (signal?.aborted) return;
    checked += 1;
    onEvent({ type: "row", checked, total: DEMO_ROWS.length, row });
    await sleep(360);
  }
  if (signal?.aborted) return;
  onEvent({
    type: "summary_final",
    summary: summarise(DEMO_ROWS),
    check_id: "demo",
  });
}
