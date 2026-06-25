# DocuMind — Performance, Quality, Observability & Eval Plan

> Goal: take DocuMind from "clean working prototype" to **industry-grade** on the five pillars
> you care about — **latency, answer quality, caching, observability/monitoring, evaluation** —
> *without* turning it into a sprawling system you can't explain in an interview.
>
> **The one idea that makes this possible:** offload the heavy/stateful parts to **managed
> services** (Pinecone hybrid, Cohere rerank, Redis cache, LangSmith tracing). That makes the
> code **smaller and simpler** *and* faster at the same time. Every upgrade here is a clean,
> nameable interview talking point — and several of them *delete* code rather than add it.

## How this relates to the other two plan docs

- `CODE_REVIEW.md` → original correctness audit (all fixed).
- `PROJECT_AUDIT_AND_SLIMMING_PLAN.md` → slimming + remaining correctness items (A1/A2 done).
- **This doc supersedes** the slimming plan's **Part B4** (rerank decision), **Part C-3** (hybrid
  decision), and resolves **A4** (BM25 thread-safety/rebuild) — by replacing local components
  with managed ones. Where they conflict, follow this doc.
- Honest note: the **A1 fix I just committed** made the *local* BM25 rebuild correct. This plan
  proposes **removing local BM25 entirely** (Pinecone native hybrid, or dense-only). A1 was still
  worth doing — it keeps today's code correct until that migration lands — but it becomes moot
  once Step L1 below is executed. That's the right order: correct first, then re-architect.

---

## 1. Reconciling your reference roadmap with the actual code

Your `documind_performance_roadmap.txt` is mostly right. Verdict on each item, checked against the
real code:

| Reference suggestion | Verdict | Refinement |
|---|---|---|
| **P1: BM25 rebuild on the live query path** (`retrieval.py`) | ✅ Real | True — `_ensure_bm25_index` runs on the first query after any upload/delete and pulls the namespace into RAM. **Fix: Pinecone native hybrid** (sparse lives in Pinecone, no rebuild) — simpler than adding Celery. |
| **P2: Local CPU cross-encoder reranking** | ✅ Real | True bottleneck + it drags in `sentence-transformers`+`torch` (huge deps). **Fix: Cohere Rerank API** — removes the bottleneck *and* ~1 GB of dependencies. |
| **P3: Character-based chunking** | ✅ Real but minor | `CHUNK_SIZE=3000` chars via `chunk_by_title`. Switch to **token-based** splitting — folds neatly into dropping `unstructured` (slimming B2). |
| **STEP 1: Celery / BackgroundTasks for BM25** | ⚠️ Over-complex | Skip Celery (a whole broker+worker tier). Native hybrid removes the need; FastAPI `BackgroundTasks` is the fallback only if you keep local BM25. |
| **STEP 2: Cohere Rerank** (skip Triton) | ✅ Agree | Cohere = simple HTTP. **Triton Inference Server = explicitly out of scope** (too complex for this project). |
| **STEP 3: tiktoken token chunking** | ✅ Agree | Use `RecursiveCharacterTextSplitter.from_tiktoken_encoder` (≈512 tokens, 64 overlap). |
| **STEP 4: Remove the blocking SSE fallback in the frontend** | ✅ Agree | `frontend/pages/chat.py` retries a full blocking query on stream error — double the cost. Replace with a "Connection interrupted — Retry" button. |
| **Semantic caching with Redis** | ✅ Agree, your stated choice | Two tiers (exact → semantic), **keyed by namespace, invalidated on upload/delete** (the part the reference omits — it's a correctness must). |
| **User isolation via namespace** | ✅ Keep | Already enforced; keep the rule that every cache/retrieval key is namespaced. |

---

## 2. The target query path (after)

```
question
  │
  ├─▶ [Redis cache check]  ── hit ─▶ return cached answer+sources   (<50ms)   ★ caching
  │        (exact, then semantic by reusing the query embedding)
  │ miss
  ▼
[rewrite if history]  (skip when no history — already done)                    ★ latency
  ▼
[embed query once]  ── reuse this vector for BOTH cache lookup & retrieval     ★ latency
  ▼
[Pinecone retrieve]  (dense, or native hybrid dense+sparse — server-side)      ★ quality
  ▼
[Cohere Rerank → top 3]  (hosted, ~40-100ms, no local CPU/torch)               ★ quality+latency
  ▼
[generate, streaming]  grounded + cited, "answer only from context"           ★ quality
  ▼
[write to Redis cache]  +  [emit LangSmith trace: per-stage latency, tokens, cost]  ★ observability
```

Every box is one nameable concept. Multi-query / Jaccard dedup / memory summarization (the three
weakest features) are **removed or default-off** — they add latency and code for marginal gain.

---

## 3. The plan, by pillar (simple, code-grounded)

### Pillar L — LATENCY  (target: time-to-first-token ~500–800ms on a miss, <50ms on a cache hit)

**L1 — Remove local BM25; pick one retrieval design.** *(supersedes A1/A4, slimming C-3)*
- **Option A (recommended, simplest):** **Dense retrieval + Cohere rerank.** Delete BM25/hybrid
  entirely. A strong reranker recovers most of what hybrid buys. Fewer moving parts, nothing to
  rebuild, multi-worker-safe. Interview line: *"broad dense recall, then a cross-encoder reranker
  for precision."*
- **Option B (stretch):** **Pinecone native hybrid** (dotproduct index + sparse vectors via
  `pinecone-text`). Better on exact keywords/acronyms. Cost: you learn sparse encoders and encode
  a sparse vector at ingest + query. Still simpler than today's in-RAM rebuild.
- Files: `retrieval.py` (delete `_ensure_bm25_index`, `_list_all_documents`, `_hybrid_retrieve`,
  BM25 state), `config.py` (drop `USE_HYBRID_SEARCH`/`HYBRID_SEARCH_WEIGHT` for Option A).

**L2 — Offload reranking to Cohere Rerank API.** ✅ *Done.* *(supersedes B4)*
- Replaced the local `CrossEncoder` (`retrieval._rerank_documents`) with `cohere.ClientV2.rerank(
  model="rerank-v3.5", query=q, documents=[d.page_content...], top_n=RERANKER_TOP_K)`.
- **Removed `sentence-transformers` + `torch`** from requirements (massive install-size win) and
  deleted the startup cross-encoder warmup — Cohere is hosted, nothing to pre-load.
- Fallback is a **graceful skip**, not a second model: with no `COHERE_API_KEY`/SDK or on an API
  error, rerank returns the top `RERANKER_TOP_K` candidates in retrieval order — so tests/offline
  still work without dragging the heavy deps back in. Files: `retrieval.py`, `config.py`,
  `main.py`, `requirements.txt`, `.env.example`, tests.

**L3 — Make multi-query optional and OFF by default.**
- It costs an extra LLM round-trip (~400–800ms) + Nx retrievals for marginal recall once you have
  rerank. Flip `USE_MULTI_QUERY=False`; keep it as a "high-recall mode" flag. Files: `config.py`,
  `pipeline.py`, `generation.py`.

**L4 — Embed the query once, reuse it.** The cache lookup (Pillar C) and dense retrieval both need
the query vector — compute it once per request and pass it to both. Saves one embedding call on
every cache miss. Files: `pipeline.py`.

**L5 — Frontend: drop the blocking SSE fallback.** Replace the auto-retry-with-blocking-query in
`frontend/pages/chat.py` with a "Connection interrupted — Retry" button. Files: `chat.py`.

### Pillar Q — QUALITY

**Q1 — Token-based chunking.** Switch to `RecursiveCharacterTextSplitter.from_tiktoken_encoder
(chunk_size=512, chunk_overlap=64)`. Predictable context size, cleaner boundaries, predictable
cost. Folds into dropping `unstructured[all-docs]` (slimming B2). Files: `ingestion.py`, `config.py`.

**Q2 — Retrieval = recall × precision.** Dense (or hybrid) gives recall; Cohere rerank gives
precision. This two-stage shape is the single most important quality decision and it's easy to
defend.

**Q3 — Keep grounding guardrails (already present, make them explicit).** Strict "answer only from
the context, otherwise say you can't find it" prompt + inline `[Source: file, Page]` citations +
post-hoc citation verification. Simplify citation verification to **filename-level** (page numbers
are noisy) so its score is meaningful. Files: `generation.py`.

**Q4 — Tune `TOP_K` / `RERANKER_TOP_K` with the eval set, not by guessing** (see Pillar E). This is
how you *prove* quality instead of asserting it.

### Pillar C — CACHING (Redis)  *(your stated choice)*

Two tiers, both **namespaced per user** and **invalidated on upload/delete**:

**C1 — Exact-match cache (do this first, trivial, ~5ms).**
- Key: `qa:{namespace}:{sha256(normalized_question + filename_filter)}` → JSON(answer, sources).
- Check before the pipeline; write after. TTL ~1h. Handles repeated/identical questions.

**C2 — Semantic cache (stretch, the "wow" feature).**
- Reuse the query embedding (L4). Look up near-duplicate past questions for this namespace; if
  cosine ≥ ~0.95, serve the cached answer. Two simple implementations:
  - *Simplest:* keep the last N `(embedding, answer)` per namespace in Redis, cosine in Python.
  - *Scales:* **Redis Stack** vector index (RediSearch KNN) per namespace.
- Start with the simplest; upgrade only if needed.

**C3 — Invalidation (correctness — don't skip).** On upload/delete for a user, `SCAN`+`DEL` all
`qa:{namespace}:*` keys so answers never go stale. Files: `documents.py` (call an
`cache.invalidate(namespace)` in the upload/delete routes), new `src/components/cache.py`.

### Pillar O — OBSERVABILITY / MONITORING (LangSmith)

**O1 — Trace every query with LangSmith.** ✅ *Baseline done.* Because the pipeline runs on
LangChain, tracing is **env-driven and zero-instrumentation**: set `LANGSMITH_TRACING=true` +
`LANGSMITH_API_KEY` and every chain run is captured with latency, token counts, and $ cost. The
three LLM chains are now named via `.with_config(run_name=...)` → `query_rewrite`,
`multi_query_gen`, `rag_generate`, so traces are readable. Default is **OFF** (a test pins this —
no trace data leaves the process unless opted in). *Remaining (after L1/L2):* wrap the whole
request in one parent trace and add `@traceable` spans for the non-LangChain stages (embed,
retrieve, rerank) plus `cache_hit`/`namespace` tags — deferred on purpose so we instrument the
*simplified* path, not the one we're about to re-architect.

**O2 — Dashboards for free.** LangSmith then gives p50/p95/p99 latency, cost/query, error rate, and
(via O1's tag) **cache-hit rate** — the metrics that matter, without building Grafana.

**O3 — Keep the cheap stuff you have.** The request-id + total-latency middleware stays; add
per-stage timing logs at INFO as a fallback/complement to LangSmith.

**O4 — Feedback loop.** Add 👍/👎 in the Streamlit chat → send the score to the LangSmith trace.
Free labeled data that feeds Pillar E.

### Pillar E — EVALUATION  *(your next big focus — set it up to be easy)*

**E1 — Offline harness (move RAGAS out of the live API — slimming B5).** `scripts/run_eval.py` +
a versioned `data/eval/goldset.v1.jsonl` (~50 Q/A with labeled relevant chunk ids). Compute:
- **Retrieval:** Recall@k, MRR, nDCG (via `ranx` or ~30 lines of Python) from the labeled chunk ids.
- **Generation:** RAGAS faithfulness, answer_relevancy, context_precision/recall (keep RAGAS).

**E2 — CI regression gate.** Run a small slice on each PR (nightly for the full, costly set); fail
the build if a metric drops past a threshold vs the stored baseline. Now "I turned rerank on" comes
with a *number*.

**E3 — Online eval.** Sample production traces in LangSmith, score a subset for faithfulness, and
combine with O4's human 👍/👎. Closes the loop: prod data → eval set → tuning.

---

## 4. Latency budget (so you can say where every millisecond goes)

| Stage | Cache hit | Cache miss | Note |
|---|---:|---:|---|
| Redis lookup | ~5–40ms | ~5–40ms | exact + (optional) semantic |
| Query rewrite (LLM) | — | 0ms if no history; ~300ms with history | skipped on first turn |
| Embed query | — | ~50–100ms | computed **once**, reused (L4) |
| Pinecone retrieve | — | ~50–100ms | server-side; no local rebuild |
| Cohere rerank | — | ~40–100ms | hosted, replaces local CPU |
| LLM generation (first token) | — | ~300–600ms | streaming |
| **Time to first token** | **<50ms** | **~500–800ms** | matches your sub-800ms goal |

Biggest single win: **deleting multi-query** (removes a whole sequential LLM hop) and **removing
the BM25 rebuild** (removes multi-second spikes). Both *simplify* the code.

---

## 5. Tech stack — keep vs add (your learning checklist)

### Keep (and deepen) — already in the project
| Tech | Role | What to learn deeper |
|---|---|---|
| **FastAPI + Uvicorn** | API + async | `async`/`await`, `BackgroundTasks`, streaming responses |
| **Streamlit** | UI | session state, `st.chat_*`, SSE consumption |
| **OpenAI** (`gpt-4o-mini`, `text-embedding-3-small`) | LLM + embeddings | token usage, streaming, cost |
| **Pinecone** | vector DB | serverless, namespaces, **hybrid/sparse** (if Option B) |
| **Supabase** | auth + storage + metadata | JWT auth, RLS, storage |
| **LangChain** | orchestration | LCEL chains (LangSmith auto-traces these via env vars) |
| **tiktoken** | tokenizer | token-based chunking (Q1) |
| **RAGAS** | eval metrics | faithfulness/relevancy/precision/recall |
| **pytest** | tests | async tests, `httpx.ASGITransport` |

### Add / change — the new "industry" pieces
| Tech | Replaces / Adds | Role | What to learn | Complexity |
|---|---|---|---|---|
| **Redis** (`redis-py`; Redis Stack for semantic) | adds | caching tier (C1/C2) | key design, TTL, `SCAN`, (vector search for C2) | Low (exact) / Med (semantic) |
| **Cohere Rerank API** (`cohere`) | replaces local cross-encoder + `sentence-transformers`+`torch` | hosted reranking (L2) | `co.rerank`, top_n, relevance scores | Low |
| **Pinecone native hybrid** + `pinecone-text` | replaces in-process BM25 | server-side sparse+dense (L1 Option B) | sparse encoders (BM25/SPLADE), dotproduct index | Med (skip for Option A) |
| **LangSmith** (`langsmith`) | adds | tracing, latency/cost/token metrics, online eval (O1–O4) | env vars (`LANGSMITH_*`), `@traceable` for custom spans, scores | Low (env-driven for LangChain) |
| **ranx** (or stdlib) | adds | retrieval metrics in offline eval (E1) | Recall@k, MRR, nDCG | Low |
| `langchain-text-splitters` | already present | token-based chunking (Q1) | `from_tiktoken_encoder` | Low |

### Drop — slimming + perf wins
`sentence-transformers`, `torch` (via Cohere), `unstructured[all-docs]` (→ `pypdf`/`python-docx`),
`langchain-experimental`, `aiofiles`, `unstructured-client`, `pinecone-client` (done in A2),
`python-pptx`/`openpyxl`/`pdf2image` (with format slimming). Net: the install loses its multi-
hundred-MB ML tail and the dependency list becomes explainable.

---

## 6. How I'll handle each pillar — plain English (read this part)

**Latency.** Three levers, all of which also simplify the code: (1) stop doing slow work *inside*
the request — the BM25 keyword index that gets rebuilt mid-query moves into the database (Pinecone
hybrid) or is dropped; (2) stop running the heavy reranker model on our own CPU — a hosted Cohere
call does it in ~40ms and frees the server; (3) stop making extra LLM round-trips we don't need —
multi-query becomes optional and off by default. With a cache in front, repeat questions return in
under 50ms, and fresh questions stream their first token in roughly half a second. I can show you a
budget table that accounts for every millisecond.

**Quality.** Good RAG is two stages: cast a wide net (dense/hybrid retrieval = high recall), then
let a smarter model pick the best few (cross-encoder rerank = high precision). On top of that,
the LLM is told to answer *only* from the retrieved text, cite its sources inline, and say "I can't
find it" otherwise — so answers stay grounded. Chunking moves to token boundaries so context is
predictable. And I don't *guess* the knobs (how many chunks, what threshold) — I tune them against
a labeled question set and keep the numbers (see Eval).

**Caching.** A Redis layer sits in front of the whole pipeline, keyed per user. First it checks for
the exact same question (instant). Then, optionally, it checks for a *semantically* similar past
question by reusing the embedding we already computed — if you ask the same thing in different
words, you still get the cached answer in milliseconds. Crucially, whenever a user uploads or
deletes a document, I wipe that user's cache so they never get a stale answer.

**Observability.** Every query produces one trace in LangSmith, broken into steps (rewrite, embed,
retrieve, rerank, generate). Each step shows how long it took; the generation step shows tokens and
dollar cost. From those traces LangSmith builds the dashboards that matter — p95 latency, cost per
query, error rate, cache-hit rate — without me building monitoring infra. Users can thumbs-up/down
an answer, which attaches to the trace, giving me real labeled data for free.

**Evaluation.** Two layers. Offline, a small versioned "gold set" of questions with known correct
chunks/answers lets a script measure retrieval quality (did we fetch the right chunks?) and answer
quality (RAGAS: is it faithful and relevant?). That script runs in CI and fails the build if a
change makes things worse — so every tuning decision is backed by a number. Online, I sample real
traffic in LangSmith and score it, plus the human thumbs feedback, and feed the good/bad examples
back into the gold set. That's the loop big teams use.

---

## 7. Complexity guardrails — what we deliberately will NOT do

To keep it interview-explainable (this is as important as what we add):
- ❌ **No Celery / RabbitMQ / Kafka.** FastAPI `BackgroundTasks` covers any async need.
- ❌ **No Kubernetes / microservices.** One FastAPI app + one Streamlit app + managed services.
- ❌ **No self-hosted GPU / Triton inference.** Cohere's hosted API instead.
- ❌ **No custom/extra vector DB.** Pinecone only.
- ❌ **No multi-query / fuzzy dedup / memory-summarization** in the default path (off or deleted).
- ✅ Rule of thumb: if a feature can't be explained in one sentence and tied to a metric, it's out.

---

## 8. Suggested execution order (small, verifiable steps — each its own commit)

Do the **simplifying** perf wins early; they delete code and de-risk demos.

1. **L2 — Cohere rerank** ✅ *done* (removed `sentence-transformers`+`torch`; graceful skip fallback).
2. **L3/L4 — multi-query off by default + embed-once** (removes a sequential LLM hop).
3. **L5 — frontend SSE fallback → retry button** (tiny UX fix).
4. **C1 — Redis exact-match cache + C3 invalidation** (the latency headline; namespace-safe).
5. **O1–O3 — LangSmith tracing + per-stage timings** (now you can *measure* steps 1–4).
6. **Q1 — token-based chunking** (folds into dropping `unstructured`, slimming B2).
7. **L1 — retrieval design:** ship **Option A (dense + rerank)** first; revisit Option B (Pinecone
   native hybrid) only if eval shows lexical misses.
8. **E1/E2 — offline eval harness + CI gate**; then **O4/E3 — feedback loop + online eval**.
9. **C2 — semantic cache** (stretch, once exact-match + observability prove the win).

After step 5 you can *prove* each later change with LangSmith + eval numbers — which is exactly the
story that lands in an interview.

---

*Start by learning: Redis basics, the Cohere Rerank API, and LangSmith tracing — those three unlock
caching, latency, and observability respectively, and all three are low-complexity. Pinecone native
hybrid and sparse encoders are the only "medium" learning item, and Option A lets you defer it.*
