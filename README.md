<div align="center">

# ⚖️ KYC Compliance Assistant

### Cited, requirement-by-requirement gap analysis for RBI KYC

*Upload your internal KYC policy, pick an RBI circular, and get a **cited gap table** — every requirement judged **Covered / Partial / Gap / Conflict**, each finding traced to the exact clause — in seconds.*

Built on **DocuMind**, a production-grade Retrieval-Augmented Generation engine.

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=flat&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-async_SSE-009688?style=flat&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Next.js](https://img.shields.io/badge/Next.js_16-React_19-000000?style=flat&logo=nextdotjs&logoColor=white)](https://nextjs.org)
[![Tailwind](https://img.shields.io/badge/Tailwind-v4-38BDF8?style=flat&logo=tailwindcss&logoColor=white)](https://tailwindcss.com)
[![Cerebras](https://img.shields.io/badge/Judge-Cerebras_gpt--oss--120b-F55036?style=flat)](https://cerebras.ai)
[![Groq](https://img.shields.io/badge/Groq-Llama_3.1_8B-F55036?style=flat&logo=groq&logoColor=white)](https://groq.com)
[![Pinecone](https://img.shields.io/badge/Pinecone-Vector_DB-000000?style=flat&logo=pinecone&logoColor=white)](https://pinecone.io)
[![Supabase](https://img.shields.io/badge/Supabase-Auth_+_Storage-3ECF8E?style=flat&logo=supabase&logoColor=white)](https://supabase.io)
[![CI](https://github.com/Meetbarasara/DocuMind-RAG/actions/workflows/ci.yml/badge.svg)](https://github.com/Meetbarasara/DocuMind-RAG/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

<!-- Add a demo GIF of the streamed gap table here, e.g. ![demo](docs/demo.gif) -->

---

## The problem

A compliance officer at an Indian fintech has to check their internal KYC policy against the **RBI Master Direction on KYC** — line by line, requirement by requirement. Does the policy cover Officially Valid Documents? Video-KYC geo-tagging? The 5-year record-retention minimum? It's slow, manual, and a missed requirement can mean a fine.

This tool does that check for them: **upload the policy, pick a regulation, and get a cited gap table** where every requirement is judged and every finding points to the exact clause in *your* policy and *its* RBI origin — side by side.

> **Assisted review, not legal advice.** Every finding is cited to a clause for a human to verify. Confidence scores and a "Needs review" state make uncertainty explicit — this augments a compliance officer, it never signs off for one.

---

## What it looks like

Run a check and a cited gap table streams in, one requirement at a time:

| Requirement (RBI) | Status | Your policy clause | RBI clause |
|---|---|---|---|
| Identify every customer with an OVD at onboarding | 🟢 **Covered** | *"At onboarding, every customer must submit an Officially Valid Document (OVD)…"* — `acme_kyc_policy.pdf · p.1` ✓ verified | §1 · p.1 |
| Maintain client records ≥ 5 years after closure | 🟣 **Conflict** | *"Records… are retained for a period of three years after the account is closed."* — `acme_kyc_policy.pdf · p.2` ✓ verified | §8 · p.3 |
| Identify beneficial owners of legal-entity customers | 🔴 **Gap** | *No matching clause found in your policy.* | §7 · p.2 |

- **🟢 Covered** — fully meets the requirement  ·  **🟡 Partial** — addresses it but incomplete/vague  ·  **🔴 Gap** — not addressed  ·  **🟣 Conflict** — a concrete rule that contradicts it (e.g. a 3-year retention where 5 is required)
- A **status summary** counts up as rows stream; each row expands to the **your-clause-vs-RBI-clause** comparison, cited on both sides.

---

## Why it's different: it's measured

Most RAG demos can't tell you how *correct* they are. This one is scored against a labeled benchmark, gated in CI:

- **Gap-analysis accuracy `0.92` · macro-F1 `0.91`** over the four statuses, on a labeled `(requirement, policy) → status` gold set. Macro-F1 so a rare-but-critical **Conflict** counts as much as a common Covered — a tool that never flags conflicts should score poorly even if it's "mostly right."
- **Evidence faithfulness** — the fraction of findings whose cited quote actually grounds in a real policy clause (clause-level citation verification, below).
- A committed baseline arms a **`run_compliance_eval --check` regression gate**, so a prompt change that quietly under-calls Conflicts fails the build.

*In a domain where a wrong answer is a fine, being able to quantify correctness is the whole pitch.*

---

## How a gap check works

```
Upload KYC policy ─┐
                   ├─▶ pick an RBI circular ─▶ Run check
Regulation (seeded)┘
     │
     │  requirements are extracted ONCE per regulation and cached
     ▼  then, for each requirement:
 [Retrieve]  top policy chunks — from YOUR private namespace only
     │       (hybrid dense+sparse + Cohere rerank, the 0.97-hit@k engine)
     ▼
 [Judge]     Cerebras gpt-oss-120b → { status, evidence quote, confidence, rationale }
     │       strict JSON; a bad row degrades to "Needs review", never crashes the check
     ▼
 [Verify]    ground the quote to a specific policy CLAUSE (difflib containment).
     │       Ungrounded quote → no citation → flagged for review (anti-hallucination)
     ▼
 SSE ▶ one cited row at a time ─▶ persisted to Supabase
                                   (re-open a past check instantly, no re-run)
```

Key design decisions (each prevents a specific failure):

- **The RBI citation is carried from the requirement's origin, never asked of the judge** — so the judge can't hallucinate a legal citation. It only cites *your policy* evidence.
- **Clause-level citation verification.** The judge's evidence quote is grounded to a specific clause in a retrieved chunk (not a fuzzy 512-token blob), with a graded containment score. A faithful-but-reworded quote still verifies; a fabricated one scores near zero and is flagged.
- **Change-tracking — re-check only what changed.** When a circular is updated, `diff_requirements` (pure text-similarity, no LLM) classifies each requirement **added / changed / unchanged / removed**; `POST /api/compliance/recheck` re-judges only the added + changed ones and carries the rest forward. A ~34-requirement re-run (≈15 min on a free judge tier) becomes a few calls.
- **Route models by difficulty.** The strong, slow **Cerebras 120B** does the hard judging; **Groq 8B** does cheap query rewriting + the Ask screen; **local mpnet** does retrieval. All on free tiers.

---

## Features

**Compliance**

| Feature | Details |
|---|---|
| ⚖️ **Cited gap table** | Requirement-by-requirement Covered / Partial / Gap / Conflict, streamed row-by-row, each finding cited to a clause |
| 🔎 **Clause-level citation verification** | The judge's evidence quote is grounded to a specific policy clause; ungrounded quotes are flagged "Needs review" |
| 🔄 **Change-tracking** | Diff a new circular against the prior check and re-judge only the deltas (added/changed), carrying unchanged verdicts forward |
| 🧑‍⚖️ **Swappable judge** | Cerebras `gpt-oss-120b` via an OpenAI-compatible endpoint; `JUDGE_PROVIDER`/`JUDGE_MODEL` swap to Groq/OpenRouter/a paid tier with a one-line env change |
| 📚 **Library** | Manage your policy documents (upload / delete) and browse available regulations |
| 💬 **Ask** | A focused Q&A fallback over your own policy docs, cited and streamed |
| 📊 **Compliance eval** | Labeled gap-analysis gold set → accuracy + macro-F1 + evidence faithfulness, gated in CI |
| 🧱 **Clause-aware chunking** | Regulations are chunked on clause/section boundaries (not fixed windows) so requirements aren't split mid-clause |

**RAG foundation (DocuMind)**

| Feature | Details |
|---|---|
| 🔍 **Hybrid retrieval + rerank** | Dense (or Pinecone native sparse+dense) → Cohere Rerank; retrieval **Hit@k 0.97** on a 4-doc gold set |
| ✍️ **Grounded, cited, streamed answers** | Every claim cited to file + page, verified against the real retrieved sources, delivered token-by-token over SSE |
| ⚡ **Caching** | Redis exact-match + semantic (near-duplicate question) cache — optional, off without `REDIS_URL` |
| 📈 **Observability** | LangSmith tracing (per-stage timings, token/cost) + 👍/👎 feedback loop — optional |
| 🔒 **Multi-user auth + isolation** | Supabase Auth (JWT); each company's policies live in a **private Pinecone namespace**; regulations in a shared one |
| 🐳 **Containerized + evaluated** | Docker Compose (FastAPI + Next.js), and *two* CI-gated eval suites (compliance + RAG) |

---

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│      Next.js 16 UI (glassmorphism)   ·   http://localhost:3000 │
│   Gap Check (streamed cited table) · Ask · Library            │
└────────────────────────────┬──────────────────────────────────┘
                             │ HTTP / SSE  (Bearer JWT)
┌────────────────────────────▼──────────────────────────────────┐
│                      FastAPI backend                           │
│  /api/compliance/*   /api/chat/*   /api/documents/*   /auth/*  │
└───────┬────────────────────┬───────────────────┬──────────────┘
        │                    │                   │
┌───────▼───────┐   ┌────────▼─────────┐  ┌──────▼──────────────┐
│  Gap engine   │   │  RAG pipeline    │  │      Supabase       │
│  extract →    │   │  retrieve →      │  │  Auth · Storage ·   │
│  judge →      │   │  rerank →        │  │  Postgres           │
│  verify → SSE │   │  generate → SSE  │  │  (regulations,      │
└───────┬───────┘   └────────┬─────────┘  │   checks, docs…)    │
        │                    │            └─────────────────────┘
┌───────▼────────────────────▼──────────────────────────────────┐
│   Models — routed by difficulty, all free-tier                 │
│   Cerebras gpt-oss-120b = the judge   ·   Groq 8B = rewrite/Ask │
│   local all-mpnet = embeddings        ·   Cohere = rerank      │
│   Pinecone (ONE index): ns "regulations" (shared) + per-user   │
└───────────────────────────────────────────────────────────────┘
```

**One vector index, split by namespace** — RBI circulars are ingested once into a shared `regulations` namespace; each company's policies stay in their own private `<user_id>` namespace. A check only ever retrieves from *your* namespace, so a gap check can never leak one company's policy to another.

---

## Tech stack

| Layer | Technology |
|---|---|
| **Frontend** | Next.js 16 (App Router, Turbopack) + React 19 + Tailwind v4, glassmorphism |
| **Backend** | FastAPI + Uvicorn (async, SSE streaming) |
| **The judge** | Cerebras `gpt-oss-120b` via an OpenAI-compatible endpoint (swappable) |
| **Chat / rewrite LLM** | Groq `llama-3.1-8b-instant` (high free daily limits) |
| **Embeddings** | Local `sentence-transformers/all-mpnet-base-v2` (768-dim, CPU, no API/quota) |
| **Vector DB** | Pinecone (namespace-isolated; dense, or dotproduct for native hybrid) |
| **Re-ranking** | Cohere Rerank API (optional) |
| **Auth + storage + DB** | Supabase (Auth, S3-compatible Storage, PostgreSQL with RLS) |
| **Caching / tracing** | Redis (optional) · LangSmith (optional) |
| **Parsing / chunking** | PyMuPDF + python-docx; token + clause/section-aware chunking |
| **Settings** | `pydantic-settings` (fail-fast secret validation at startup) |
| **Evaluation** | Custom gap-analysis metrics + RAGAS + retrieval metrics (offline, CI-gated) |
| **Containers** | Docker + Docker Compose |

> The original **Streamlit** UI (`frontend/`) still runs and is kept until the Next.js app reaches full parity, then retired. The Next.js app (`frontend-next/`) is the product.

---

## Quick start

### Prerequisites

- **Python 3.13** and **Node 20+** (or just Docker — see [DEPLOY.md](DEPLOY.md)).
- [Groq API key](https://console.groq.com/keys) (LLM) · [Cerebras API key](https://cloud.cerebras.ai) (the judge) — both free.
- [Pinecone](https://pinecone.io) index named `documind`, dimension `768`, metric `cosine` (or a `dotproduct` index for native hybrid). First run downloads the ~420 MB embedding model to your HF cache.
- [Supabase](https://supabase.com) project (free tier).

### 1. Backend

```bash
git clone https://github.com/Meetbarasara/DocuMind-RAG.git
cd DocuMind-RAG
python -m venv venv && source venv/bin/activate   # Windows: .\venv\Scripts\activate
pip install -e .                                   # add ".[dev]" for tests, ".[eval]" for RAGAS

cp .env.example .env    # then fill in the values below
```

```env
GROQ_API_KEY=gsk_...
CEREBRAS_API_KEY=csk_...          # the compliance judge (optional secret; needed for checks)
PINECONE_API_KEY=pcsk_...
PINECONE_INDEX_NAME=documind
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_ANON_KEY=eyJ...
SUPABASE_SERVICE_ROLE_KEY=eyJ...
CORS_ORIGINS=http://localhost:3000,http://localhost:8501
```

**Supabase, once:** create a private `documents` storage bucket, then run [`supabase_migration.sql`](supabase_migration.sql) in the SQL Editor (idempotent — creates the `regulations`, `compliance_checks`, `user_documents`, and `conversations`/`messages` tables with RLS).

```bash
python -m uvicorn src.api.main:app --reload --port 8000
```

### 2. Seed a regulation

A check needs something to check against. Point the seed script at an RBI circular PDF:

```bash
python -m scripts.seed_regulation --pdf data/compliance/rbi_kyc_requirements.pdf --name "RBI KYC (demo)"
```

This parses → extracts atomic requirements (clause-aware) → ingests into the shared `regulations` namespace → caches the requirement list. A synthetic fixture ships for a fast demo; a real RBI Master Direction chapter works the same way.

### 3. Frontend

```bash
cd frontend-next
npm install
npm run dev          # http://localhost:3000
```

Open **http://localhost:3000** — **Demo** mode replays a real cited gap table instantly (no login). **Live** mode: sign in → upload your policy → pick a regulation → **Run check**.

---

## API reference

Base URL `http://localhost:8000` · interactive docs at `/docs`. All non-auth endpoints require `Authorization: Bearer <token>`.

### Compliance

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/compliance/regulations` | List regulations available to check against |
| `POST` | `/api/compliance/check` | Run a gap check `{regulation_id}` → **SSE**: `summary_init` → one `row` per requirement (as judged) → `summary_final`; persists the cited table |
| `POST` | `/api/compliance/recheck` | Re-check a prior check `{check_id}` against the regulation's **current** version — re-judges only added/changed requirements, carries the rest forward, streams a `delta` |
| `GET` | `/api/compliance/checks` | List the user's past checks (summary counts) |
| `GET` | `/api/compliance/checks/{id}` | Fetch a past check's full cited gap table (instant, no re-run) |

### Auth · Documents · Chat · Conversations

| Group | Endpoints |
|---|---|
| **Auth** | `POST /api/auth/{signup,login,refresh,logout}` · `GET /api/auth/me` |
| **Documents** | `POST /api/documents/upload` (202 + background ingest) · `GET /api/documents/upload-status/{job_id}` · `GET /api/documents/` · `DELETE /api/documents/{filename}` |
| **Chat** | `POST /api/chat/query` · `POST /api/chat/query/stream` (SSE) · `POST /api/chat/feedback` |
| **Conversations** | `POST`/`GET /api/conversations` · `GET`/`POST /api/conversations/{id}/messages` · `DELETE /api/conversations/{id}` |

---

## Evaluation

The moat. Two offline, CI-gated eval suites over versioned gold sets — no live endpoint, so a bad number blocks the build instead of shipping.

**Compliance gap-analysis** — `scripts/run_compliance_eval.py`, over a labeled `(requirement, policy excerpt) → status` gold set covering all four statuses:

| Metric | Score |
|---|---|
| Accuracy | **0.92** |
| Macro-F1 (over Covered/Partial/Gap/Conflict) | **0.91** |
| Evidence faithfulness | grounded-quote rate on clause-asserting verdicts |

> The eval found a real bug and proved the fix: the judge was softening a 3-year-vs-5-year retention **Conflict** into "Partial" (under-stating a violation — the worst error direction in compliance). Sharpening the Conflict definition took macro-F1 `0.63 → 0.91` with no Partial regression, and a committed baseline now guards it in CI.

**RAG retrieval + generation** — `scripts/run_eval.py`, over 41 questions across 4 documents (page-level retrieval, RAGAS generation):

| Stage | Metric | Score |
|---|---|---|
| Retrieval | Hit@k / Recall@k / MRR | **0.97 / 0.96 / 0.92** |
| Generation (RAGAS) | Faithfulness / Answer relevancy | **0.80 / 0.74** |
| Generation (RAGAS) | Context precision / recall | **0.86 / 0.89** |
| Safety | Refusal rate on unanswerable questions | **1.00** |

Health: **250+ tests** (`pytest`), `ruff` + `pyflakes` clean, on every push.

---

## Configuration

Everything lives in `src/components/config.py` (`pydantic-settings`), env-overridable. The five required secrets (`GROQ_API_KEY`, `PINECONE_API_KEY`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`) **fail fast at startup** if missing or blank.

| Setting | Default | Description |
|---|---|---|
| `JUDGE_PROVIDER` / `JUDGE_MODEL` | `cerebras` / `gpt-oss-120b` | The compliance judge; swap to Groq/OpenRouter/a paid model with no code change |
| `CEREBRAS_API_KEY` | unset | The judge's key (optional secret — the app runs without it; compliance endpoints return a clear 503) |
| `USE_CLAUSE_AWARE_CHUNKING` | `true` | Clause/section-aware chunking for legal text (regulations) |
| `LLM_MODEL_NAME` | `llama-3.1-8b-instant` | Groq chat/rewrite model |
| `EMBEDDING_MODEL_NAME` | `all-mpnet-base-v2` | Local embedding model (768-dim, CPU) |
| `TOP_K` / `RERANKER_TOP_K` | `10` / `5` | Retrieval pool / kept-after-rerank (eval-tuned) |
| `USE_HYBRID_SEARCH` | `false` | Pinecone native sparse+dense — needs a `dotproduct` index + re-ingest |
| `USE_RERANKING` | `true` | Cohere Rerank — needs `COHERE_API_KEY`, else falls back to retrieval order |
| `REDIS_URL` | unset | Enables the query cache **and** shared (multi-worker) rate limiting |
| `LANGSMITH_TRACING` | `false` | LangSmith tracing + feedback — needs `LANGSMITH_API_KEY` |

---

## Deployment

`docker compose up --build` runs the full stack (FastAPI `api` on `:8000` + Next.js `frontend` on `:3000`). The Next app builds as a self-contained `output: "standalone"` image. See **[DEPLOY.md](DEPLOY.md)** for the build-time API-URL gotcha, CORS, single-worker vs. Redis-multi-worker scaling, and free-tier caveats (local mpnet needs ~1 GB RAM; a live check is slow on the free judge tier — demo mode is instant).

---

## Project structure

```
├── src/
│   ├── components/
│   │   ├── compliance.py     # the gap engine: extract_requirements · judge · verify · diff · run_check
│   │   ├── judge.py          # build_judge_llm — provider-agnostic (Cerebras/Groq/OpenRouter)
│   │   ├── ingestion.py      # parsing + token & clause/section-aware chunking
│   │   ├── retrieval.py      # hybrid search + Cohere rerank
│   │   ├── generation.py     # query rewrite + generation + SSE + citation verification
│   │   ├── evalution.py      # compliance metrics + RAGAS + retrieval metrics
│   │   ├── database.py       # Supabase: auth, storage, regulations, checks, docs
│   │   ├── cache.py · config.py · embeddings.py · sparse.py
│   ├── pipeline/pipeline.py  # end-to-end RAG orchestrator (ingest, retrieve, generate)
│   └── api/
│       ├── main.py           # FastAPI app; registers auth/documents/chat/conversations/compliance
│       └── router/compliance.py  # /api/compliance/* (check, recheck, regulations, checks)
├── frontend-next/            # Next.js 16 UI — CheckHero · GapRow · Library · Ask · ChecksHistory
├── frontend/                 # legacy Streamlit UI (kept until parity, then retired)
├── scripts/
│   ├── seed_regulation.py    # admin: parse → extract → ingest → cache a regulation
│   ├── run_compliance_eval.py# gap-analysis accuracy / macro-F1 / evidence faithfulness
│   └── run_eval.py           # RAG retrieval + RAGAS eval (CI gate)
├── data/eval/                # gold sets + committed baselines (compliance + RAG)
├── supabase_migration.sql · Dockerfile · docker-compose.yml · DEPLOY.md
└── BUGFIXES.md               # a log of every fix: root cause, the fix, and a plain-English explanation
```

---

## License

MIT © [Meet Barasara](https://github.com/Meetbarasara)
