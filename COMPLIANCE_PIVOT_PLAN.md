# DocuMind → KYC Compliance Assistant — Build Plan

> Pivot: from a general "chat with your PDF" RAG into a focused **compliance gap-analysis** tool for **RBI KYC**. Deep and narrow: one workflow, done trustworthy. Everything here is cross-verified against the current codebase; design decisions that prevent a specific bug/UX failure are called out inline.

---

## Status — 2026-07-04 (Phase 1 ✅ complete · Phase 2 in progress · Phase 3 deferred)

**Phase 1 is done and its exit criteria are met:** a real streamed, cited gap table renders in the new Next.js UI, and a labeled gold set reports a measured accuracy gated in CI. Built + verified — `doc_type`/seed path, cached requirement extraction (with dedup), per-requirement retrieval, the swappable Cerebras judge with robust JSON, `POST /api/compliance/check` (SSE), persisted results, the compliance gold set + **macro-F1 0.91** (CI-gated), and the glass hero screen. The engine + eval + UI were **live-proven end-to-end** on the real stack (a real cited check incl. a Conflict, plus Ask + history), and a **real RBI Master Direction chapter** was seeded (`regulation_id 95919385…`, 34 requirements).

**Phase 2 progress:** Ask screen ✅ · real RBI Master Direction PDF ✅ (seeded) · **clause-level citation verification ✅** (§4.3/§7 — the judge's evidence quote is now grounded to a specific policy *clause* via a graded containment score, not a whole-chunk substring test; the verbatim clause + a "Verified" badge show in the gap row, and an `evidence_faithfulness` metric is gated in CI). **Remaining Phase 2:** change-tracking (diff a new circular vs the old, re-check only what changed), the Library screen. **Follow-ups:** retire Streamlit once at parity, doc-delete control in the new UI, clause/section-aware chunking for legal text, re-seed the real RBI reg complete (with the `max_tokens` fix). **Phase 3 (deployment) stays deferred** until quality/latency are demo-ready.

> This plan is the **design**; the running **status + gotchas** live in the memory file `project_documind_slimming_phase.md`. Current code: **`origin/master` is current** (PR #2 merged 2026-07-04 — the whole pivot, incl. this session's clause-level verification, is on `master`).

---

## 0. The wedge (scope discipline)

**One sentence:** a compliance officer uploads their internal KYC policy, picks an RBI circular, and gets a **cited, requirement-by-requirement gap table** (Covered / Partial / Gap / Conflict) in seconds — with the exact policy clause and RBI clause shown side by side.

**We will NOT build** (these are the "shallow and broad" trap): multi-regulation dashboards, auto-remediation, approval workflows, multi-tenant admin, other domains (GDPR/HIPAA). If it doesn't make the gap table more correct or more trustworthy, it's out.

**Definition of done for the whole pivot:** a real gap check on a real (or realistic) KYC policy + RBI circular, streamed into the glass UI, with every finding cited, and a measured accuracy number from an eval gold set.

---

## 1. What we reuse (verified — ~80% is already built)

| Capability | Status | Reused as-is? |
|---|---|---|
| PDF ingestion + token chunking | built | yes |
| Local embeddings (all-mpnet, 768-d) | built | yes |
| Hybrid retrieval + Cohere rerank (**hit@k 0.97**) | built | yes — pointed at a *requirement* instead of a *question* |
| Faithfulness-tuned generation (**0.80**) | built | prompt pattern reused for the judge |
| Citation verification (Feature D) | built | extended to clause-level |
| Per-user namespace isolation | built | yes — policies stay private |
| SSE streaming | built | reused to stream gap rows |
| Offline eval harness + CI gate | built | extended to compliance accuracy |

**Verified integration points** (so the plan has no false assumptions):
- `RAGPipeline.ingest_file(path, user_id, namespace)` → `effective_namespace = namespace or user_id`. So we can ingest into any namespace with **no pipeline change**.
- `RAGPipeline._get_retrieval_manager(namespace)` **rejects an empty namespace** (raises). ⇒ the shared regulations namespace must be a real non-empty string (we use `"regulations"`).
- `langchain-cerebras==0.6.0` + `cerebras-cloud-sdk==1.67.0` exist on PyPI.

---

## 2. Models — route by difficulty (all free)

The insight: don't use one model for everything. Match model strength to step difficulty.

| Step | Difficulty | Model | Cost |
|---|---|---|---|
| Query rewrite (Ask screen) | easy | Groq `llama-3.1-8b-instant` (existing) | free |
| Requirement extraction | medium | **judge model** (Cerebras 70B) | free |
| **The judge** (does policy satisfy requirement?) | **hard** | **Cerebras `gpt-oss-120b`** | free |
| Embeddings / retrieval / rerank | — | local mpnet + Cohere | free / already keyed |

**Integration (verified 2026-07-02):** reach Cerebras via its **OpenAI-compatible endpoint** using `ChatOpenAI(base_url="https://api.cerebras.ai/v1")` — **not** `langchain-cerebras`, whose 0.6.0 pins langchain-core 0.3.x and downgrades/breaks this 1.x stack (confirmed and reverted). Cerebras, Groq and OpenRouter are all OpenAI-compatible, so one client + a per-provider base_url covers all three.

**Swappability (prevents lock-in):** config `JUDGE_PROVIDER` (`cerebras` \| `groq` \| `openrouter`) + `JUDGE_MODEL`; `build_judge_llm()` factory in `src/components/judge.py`. Swapping free→paid (or Cerebras→DeepSeek) is a one-line env change, no code edit. **DONE + tested** (`tests/test_judge_factory.py`).

**`CEREBRAS_API_KEY` is an OPTIONAL secret** (like `COHERE_API_KEY`). The existing app keeps running without it; the compliance endpoints return a clear 503 "judge model not configured" instead of crashing at startup. (Do **not** add it to `_REQUIRED_SECRETS` — that would break the app for anyone not using compliance.)

---

## 3. Data model

### Pinecone (one index, split by namespace — NOT two databases)

```
ONE index (documind-hybrid, 768-d, dotproduct)
├─ namespace "regulations"        ← RBI circulars, ingested ONCE, shared by all users
└─ namespace "<user_id>"          ← each company's policy docs, private (existing scheme, unchanged)
```

Why not two databases: two indexes = double cost, double ops, cross-index query juggling, zero benefit. Namespaces already give hard separation. **Regulations are shared** (same RBI rules for everyone → ingest once, never duplicate per user); **policies are private** (per-user namespace). This is cleaner *and* cheaper than two DBs.

### Ingestion metadata (small addition)

Each chunk already carries `filename`, `page_number`. Add:
- `doc_type`: `"policy"` \| `"regulation"`
- regulation chunks also carry `regulator` (`"RBI"`), `circular_name`, `circular_id`.

### Supabase (two new tables)

- **`regulations`** — `id, name, regulator, circular_id, ingested_at, requirements jsonb`. The `requirements` column **caches the extracted requirement list** so we never re-extract (expensive) per check.
- **`compliance_checks`** — `id, user_id, policy_label, regulation_id, created_at, summary jsonb (counts), rows jsonb (the gap table)`. **Persist results** so re-opening a check is instant and doesn't re-burn LLM budget. RLS scoped to `user_id`, like the existing tables.

---

## 4. The gap-analysis engine (the deep core)

Data flow of one check — each sub-step has its correctness note.

### 4.1 Requirement extraction (once per regulation, cached)

Turn an RBI circular into a list of atomic, checkable requirements:
`{ req_id, text, rbi_source: {page, section} }`.

- **Bug guard — don't stuff the whole circular into one LLM call.** A Master Direction is long (context overflow + lossy). Extract **per section/chunk** (map step), then de-duplicate/merge. Each requirement records the RBI page/section it came from.
- **Cost guard — cache.** Store the result in `regulations.requirements`. Extraction runs once when a circular is first ingested (a seed/admin step), never per user check.
- **Why record `rbi_source` here:** so the judge never has to *cite RBI itself* (which risks a hallucinated citation). The RBI citation is carried from the requirement's origin. The judge only cites the **policy** evidence.

### 4.2 Per-requirement retrieval

For each requirement, retrieve top policy chunks **from the user's namespace only** (reuse hybrid + rerank). This is the 0.97-hit@k engine aimed at a requirement string.
- **Correctness (verified):** `_get_retrieval_manager(user_id).retrieve(requirement)` is bound to the user's namespace, which holds *only* their policy docs — regulations live in a **separate** namespace, so no `doc_type` filter is needed for separation (and the retrieval layer today only supports a `filename_filter`, not arbitrary metadata filters — so we deliberately rely on the namespace boundary, which is already enforced). We never query the regulations namespace here: the question is "what does *our policy* say?", not "what does RBI say?". `doc_type` metadata is still stored for display/future use.

### 4.3 The judge (the money step)

`judge(requirement, retrieved_policy_chunks)` → strict JSON:
```json
{
  "status": "Covered | Partial | Gap | Conflict",
  "policy_evidence": { "quote": "...", "filename": "...", "page": N },
  "confidence": 0.0-1.0,
  "rationale": "one sentence"
}
```
- **Bug guard — JSON robustness.** LLMs occasionally emit malformed JSON or prose around it. Mitigation: (a) request `response_format={"type":"json_object"}` (Cerebras is OpenAI-compatible), (b) a tolerant parser that extracts the first `{...}` block, (c) on parse failure, mark the row `status: "Needs review"` with the raw text — **never crash the whole check for one bad row.**
- **Safety (non-negotiable in compliance):** reuse the strict-grounding prompt; the `policy_evidence.quote` must be verifiable against the retrieved chunk (extend citation verification to check the quote is substring-ish of a retrieved chunk — flag if not). Add `confidence` + a "Needs review" state. This is **assisted review, never automated sign-off.**
- **RBI reference** on each row comes from the requirement's `rbi_source` (§4.1), not the judge.

### 4.4 Stream + persist

- Process requirements with **bounded concurrency** (e.g., 3–4 at a time — respects Cerebras free rate limits; unbounded would 429-storm, the exact failure we hit with RAGAS). Stream each row via SSE **as it completes** so the UI fills progressively.
- On completion, write the full result to `compliance_checks`.
- **UX guard — never a 60s dead spinner.** N requirements × (~1–2s judge) can be 30–60s. Streaming rows + a "Checked 7 of 24" counter keeps it alive; the user reads early findings while the rest compute.

---

## 5. API surface (minimal — 2 new endpoints)

- `POST /api/compliance/check` → body `{ regulation_id }` (policy namespace = the authed user). **Streams** SSE events: `summary_init` → `row` (one per requirement, as judged) → `summary_final` → `[DONE]`. Reuses the existing `StreamingResponse` pattern.
- `GET /api/compliance/checks` / `GET /api/compliance/checks/{id}` → list / fetch persisted results (instant re-open, no re-run).
- Upload gains an optional `doc_type` (defaults `"policy"`). Regulation ingestion is a separate seed/admin path (not user-facing in Phase 1).

That is the entire new backend surface. Auth, upload, per-user isolation are unchanged.

---

## 6. Frontend — Next.js + Tailwind, glassmorphism (3 screens, 1 hero)

New `frontend-next/` app; the FastAPI backend is unchanged (Streamlit stays until parity, then retires). Design system:

- **Canvas:** deep slate gradient. **Glass cards:** `bg white/6%`, `backdrop-blur-xl`, `1px white/12%` border, `rounded-2xl`. **One accent:** indigo (actions + focus). **Status palette:** green / amber / red / violet = Covered / Partial / Gap / Conflict.
- **Restraint / a11y:** glass only on cards/sidebar/panels; body text sits on solid-enough surfaces so contrast passes WCAG AA. No glass behind small text.
- Component base: shadcn/ui (accessible primitives) + Tailwind.

Screens:
1. **Library** — two zones: "Your policies" (upload) / "Regulations" (pick an RBI circular). Glass cards.
2. **The Check (hero)** — pick policy + circular → **Run check** → streaming cited gap table: status counts up top, then rows; a Gap row expands to **your clause vs the RBI clause, side by side, each cited** + the gap note. Progress counter while streaming. (Mockup already shared.)
3. **Ask** — a focused Q&A fallback (reuses the existing chat/query endpoint) for one-off questions.

**UX guards:** clear empty states ("Upload your KYC policy to start" / "Pick a regulation"); partial-failure handling ("3 requirements couldn't be checked — retry"); persisted checks reload instantly; every finding visibly cited + a standing "assisted review, not legal advice" note.

---

## 7. Evaluation — the moat (extend the existing harness)

A small **labeled RBI-KYC gold set**: `(requirement, policy excerpt) → { correct_status, correct_evidence_page }`. Measure:
- **Gap-analysis accuracy / macro-F1** over the 4 statuses.
- **Evidence faithfulness** (does the cited quote actually support the verdict).

Wire it into `run_eval` + the CI gate, exactly like the current retrieval/RAGAS metrics. Payoff: *"my gap-analysis scores X% on a labeled benchmark, gated in CI"* — almost no competitor can quantify this. This is the sales pitch in a domain where a wrong answer = a fine.

---

## 8. Risks & mitigations (the cross-verification)

| Risk | Why it bites | Mitigation |
|---|---|---|
| Judge JSON malformed | one bad row crashes the check | json mode + tolerant parser + per-row "Needs review" fallback (§4.3) |
| Rate-limit storm | Cerebras free per-min cap (we hit this with RAGAS) | bounded concurrency 3–4, backoff (§4.4) |
| Requirement extraction context overflow | long circular | map over sections, dedupe, cache (§4.1) |
| Judge cites hallucinated RBI clause | high-stakes wrong citation | RBI cite carried from requirement origin, not the judge (§4.1/4.3) |
| Legal text breaks naive chunking | cross-refs, definitions | clause/section-aware chunking for regulation docs |
| 8B too weak for judging | rough verdicts | judge on Cerebras 70B; `JUDGE_MODEL` swappable to DeepSeek/paid later |
| Over-trust ("you're compliant") | legal exposure | confidence + "Needs review" + "assisted review, not sign-off" framing everywhere |
| Cost of re-running | budget burn | cache extracted requirements + persist check results (§3) |

---

## 9. Phasing & exit criteria

**Phase 1 — the wedge, end-to-end (the demo).** Deliverables: `doc_type` metadata + regulation seed path; requirement extraction (cached); per-requirement retrieval; the judge (Cerebras, swappable) with robust JSON; `POST /api/compliance/check` streaming; persist results; a tiny gold set proving accuracy; the **hero screen** in the new Next.js app. Runs on the **synthetic** KYC policy + RBI-requirements doc (§ below).
- **Exit criteria:** a real streamed, cited gap table appears in the UI for the synthetic pair, *and* the gold set reports a first accuracy number (gated in CI).

**Phase 2 — depth.** Change-tracking (diff a new circular vs the old, re-check only what changed); Library + Ask screens; swap in the real RBI Master Direction PDF; clause-level citation verification.

**Phase 3 — deployment** (the deferred track). Now justified: a stronger reason to spend on hosting + (optionally) a paid judge model.

---

## 10. Open decisions

1. **Judge key** — free **Cerebras** key, judge = **`gpt-oss-120b`** (verified live 2026-07-02; the account's models are gpt-oss-120b / zai-glm-4.7 / gemma-4-31b — `llama-3.3-70b` 404s, it isn't offered). `JUDGE_MODEL` stays swappable to DeepSeek-via-OpenRouter for a final quality pass.
2. **Demo material** — no real RBI PDF yet → Phase 1 builds on a **synthetic** internal KYC policy (with deliberate gaps) + a synthetic RBI-KYC-requirements doc based on the real public rules (OVD, periodic updation cadence, V-CIP, record retention, risk categories). Real RBI Master Direction swaps in at Phase 2.
3. **Streamlit** — kept running until the Next.js app reaches parity, then retired. No big-bang rewrite.
