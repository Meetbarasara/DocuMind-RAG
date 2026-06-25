# DocuMind — Independent Audit & Slimming Plan

> A fresh, line-by-line audit of the **current** code (after `CODE_REVIEW.md` was fully
> remediated in commit `5c60cc1`), plus a concrete plan to make the project **smaller,
> sharper, and production-quality** — small enough to explain end-to-end in an interview.
>
> This is a **plan document**. No code is changed here. Any agent can execute it section by
> section. Each fix in Part A follows the repo's established workflow
> (`reproduce → fix → verify → BUGFIXES.md → commit`, see `feedback_bug_fix_documentation`).

---

## 0. How to use this report

- **Part A** = correctness/logic/error findings on the code *as it is today*. These are NEW
  (not the already-fixed `CODE_REVIEW.md` items). Fix these before deleting anything.
- **Part B** = the slimming plan: what to delete and why, to get from "kitchen-sink demo" to
  "sharp, explainable core." **This is the heart of what you asked for.**
- **Part C** = the minimal set of production-grade hardening steps worth doing on the *slimmed*
  codebase (don't harden code you're about to delete).
- **Part D** = the target shape + the order to execute everything in.
- **Appendix** = file-by-file disposition table, dependency before/after, dead-config list.

**Baseline facts verified for this report (so you can trust the rest):**
- ✅ `pytest tests/` → **89 passed** (27.8s) on the current branch. Green baseline.
- ✅ The `CODE_REVIEW.md` items (BUG-1…15, SEC-1…9, LOW nits) are genuinely fixed in code.
- ✅ Installed stack: `langchain 1.2.14`, `langchain-pinecone 0.2.13`, `pinecone 7.3.0`
  **and** `pinecone-client 6.0.0` (both present — see A2), `unstructured 0.22.10`,
  `ragas 0.4.3`, `sentence-transformers 5.6.0`, `rank-bm25 0.2.2`.
- ✅ BM25 internal API used by the hybrid path (`preprocess_func`/`vectorizer.get_scores`/`docs`)
  resolves against the installed version — that path is *not* silently broken.

---

## 1. Executive summary

The code is in good shape **correctness-wise** — the previous review's bugs are fixed and the
test suite is green. The problem you actually have now is **scope, not bugs**: the project tries
to be a kitchen-sink RAG platform (7 upload formats + 5 more unreachable parsers, 6 toggleable
"quality features", live RAGAS eval endpoints, image+table extraction, a 1.5 GB-class
`unstructured[all-docs]` dependency) and that breadth is what makes it hard to explain and
heavy to run.

There are also **a handful of genuine new issues** (Part A) — the most important being a
**likely-broken hybrid search** (the BM25 rebuild embeds an empty string, an anti-pattern the
team already removed elsewhere in BUG-7) that tests can't catch because they mock the vector
store. That is the same failure shape as the original BUG-1: *green CI, dead feature.*

### The thesis: shrink to a core you can defend

| Dimension | Today | Target after this plan |
|---|---|---|
| Upload formats | 7 supported + 5 dead handlers | **3** (PDF, DOCX, TXT) |
| RAG "quality" feature flags | 6 (A–F) | **2–3** kept, rest deleted/optional |
| Heaviest dependency | `unstructured[all-docs]` (ML layout, detectron2, torch…) | lightweight per-format parsers |
| Eval | live `/api/evaluate/*` routes + `ragas`+`datasets` in the API process | **offline harness** (separate project) |
| Direct dependencies | ~28 pinned lines (several dead) | **~15**, all actually imported |
| Image extraction | placeholder text embedded into the index | **removed** |
| "Explain it in an interview" | hard — too many moving parts | one whiteboard diagram |

Everything below is in service of that table.

---

## 2. Current state — what's already solid (do NOT redo)

So no one wastes effort re-fixing fixed things:

- Async pipeline is correctly awaited end-to-end (`pipeline.query` / `query_stream`, `chat.py`).
- LLM calls use `ainvoke`/`astream`; blocking Supabase/RAGAS calls are wrapped in
  `asyncio.to_thread`. Event loop is not blocked.
- Filenames are sanitized to basenames everywhere (`sanitize_filename`, SEC-2).
- Errors are centralized: handlers log a real error + return a generic `(ref: …)` message
  (`error_utils.log_and_get_ref`). **Good pattern — keep it.**
- Rate limiting is actually wired (`@limiter.limit(...)` on signup/login/upload/query/eval).
- Upload size cap + storage/pinecone rollback on partial failure (`documents.py`).
- Per-namespace `RetrievalManager` cache is LRU-bounded.
- Namespace `""` is rejected (no silent shared-namespace writes).
- `delete_document_by_filename` uses `index.list(prefix=…)` (BUG-7) — reliable enumeration.
- Tests + CI exist and pass; `conftest.py` resets the rate-limiter between tests.

---

## 3. Part A — Correctness, logic & error findings (fresh)

Severity-ordered. IDs are new (`A-N`) to avoid collision with `CODE_REVIEW.md`.
Each: **what / where / evidence / why it matters / how to verify / fix steps.**

### 🔴 A1 (HIGH) — Hybrid search likely silently degrades to dense-only (empty-string embedding in BM25 rebuild)

- **Where:** [retrieval.py:88](src/components/retrieval.py:88), inside `_ensure_bm25_index`:
  ```python
  all_docs = self.vectorstore.similarity_search(query="", k=10_000, filter=None)
  ```
- **What:** To "list everything in the namespace" for the BM25 corpus, the code runs a vector
  similarity search with an **empty query string**. That calls `OpenAIEmbeddings.embed_query("")`.
- **Why it matters:** This is the *exact anti-pattern BUG-7 was created to remove* from
  `delete_document_by_filename` ("an embedded empty string fed into a ranked top-k vector search…
  not a guaranteed exhaustive enumeration"). It survived here. Two failure modes:
  1. **If OpenAI rejects empty input** (embeddings endpoint can 400 on empty/empty-token input):
     `_ensure_bm25_index` throws → caught at [retrieval.py:105](src/components/retrieval.py:105)
     → `_bm25_retriever = None` → every hybrid query returns **dense-only**. Your headline
     "Hybrid Search (Feature A)" is then **dead in production**, and **no test catches it**
     because tests mock the vector store. This is BUG-1's ghost: green CI, dead feature.
  2. **Even if empty input is accepted**, you pull up to 10,000 vectors into RAM and rebuild the
     whole BM25 index on the first query after every upload/delete — see A4.
- **How to verify (do this first — it's a real-API question):**
  ```bash
  # one-off, real OPENAI_API_KEY in env:
  python -c "from langchain_openai import OpenAIEmbeddings; \
             print(len(OpenAIEmbeddings(model='text-embedding-3-small').embed_query('')))"
  ```
  - If it raises → A1 mode 1 confirmed (hybrid is dead).
  - If it returns a vector → A1 mode 1 is not active, but still fix for correctness/consistency.
- **Fix (same approach as BUG-7, robust either way):** replace the empty-query search with a real
  listing. Enumerate IDs with `index.list()` then `index.fetch()` to rebuild documents:
  1. In `_ensure_bm25_index`, drop the `similarity_search(query="", …)` call.
  2. Page through `self.vectorstore.index.list(namespace=self.config.PINECONE_NAMESPACE)` to get IDs.
  3. `self.vectorstore.index.fetch(ids=batch, namespace=…)` and reconstruct
     `Document(page_content=md['text'], metadata=md)` from each vector's metadata
     (the chunk text is stored in metadata by `langchain-pinecone`; confirm the text key).
  4. Build `BM25Retriever.from_documents(...)` from those.
  5. Keep the existing `_bm25_dirty` retry-on-failure semantics.
- **Verify after fix:** add a test that asserts `_ensure_bm25_index` never calls
  `similarity_search` with an empty string, and (integration) that a 2nd uploaded file's keyword
  terms are retrievable. Re-run `pytest tests/`.
- **Note:** if you adopt Part B4 "drop hybrid, go dense-only + rerank," this entire method is
  deleted and A1 disappears. Decide B4 first; only fix A1 if you keep hybrid.

### 🟠 A2 (MEDIUM) — `requirements.txt` pins `pinecone-client==6.0.0` but the code runs on `pinecone==7.3.0`

- **Where:** [requirements.txt:6](requirements.txt:6) → `pinecone-client==6.0.0`.
- **Evidence:** `pip list` shows **both** `pinecone 7.3.0` (pulled by `langchain-pinecone 0.2.13`)
  **and** `pinecone-client 6.0.0` installed in the venv. `pinecone-client` is the deprecated
  package name; `pinecone` is the current one. They share the `pinecone` import namespace.
- **Why it matters:** A clean `pip install -r requirements.txt` on a fresh machine installs the
  *wrong/old* client and a conflicting duplicate. Reproducibility + "works on my machine" risk —
  the kind of thing that breaks a live interview demo.
- **Fix:**
  1. Delete the `pinecone-client==6.0.0` line from `requirements.txt`.
  2. Let `langchain-pinecone` pull `pinecone` transitively, **or** pin `pinecone==7.3.0` explicitly.
  3. `pip uninstall pinecone-client` in the venv; re-run `pytest tests/` to confirm green.
- **Plain English:** you're shipping the recipe for the old engine while driving the new one.

### 🟠 A3 (MEDIUM) — Dead config fields + README claims a feature that isn't implemented

- **Where:** [config.py:39](src/components/config.py:39) `STREAMING` and
  [config.py:47](src/components/config.py:47) `EMBEDDING_BATCH_SIZE`.
- **Evidence:** grep shows neither is read anywhere in `src/` (only defined). README
  ([README.md:99](README.md:99), [README.md:334](README.md:334)) claims *"batched upsert to
  Pinecone"* / `EMBEDDING_BATCH_SIZE` "Vectors per Pinecone upsert batch" — but
  `embeddings.create_vector_store` calls `vector_store.add_documents(...)` once with no batching
  parameter. The batch size is whatever `langchain-pinecone` defaults to, not your config.
- **Why it matters:** Interviewers read READMEs and `git grep`. A config knob that does nothing,
  and a README claiming control you don't have, reads as "cargo-culted." Small but cheap to fix.
- **Fix:** either (a) delete `STREAMING` + `EMBEDDING_BATCH_SIZE` and correct the README, or
  (b) actually implement batching if you want the talking point (`add_documents` in slices of
  `EMBEDDING_BATCH_SIZE`). Recommend (a) for slimming.

### 🟠 A4 (MEDIUM) — BM25 rebuild is not concurrency-safe and is O(whole namespace) per rebuild

- **Where:** `_ensure_bm25_index` ([retrieval.py:74](src/components/retrieval.py:74)).
- **What:** `RetrievalManager` is cached and **shared** per namespace, and the rebuild is invoked
  from worker threads (`run_in_executor` in `_multi_query_retrieve_async`). Two concurrent queries
  for the same user can both see `_bm25_dirty == True` and race on
  `_bm25_retriever` / `_bm25_docs`. It also loads the **entire namespace** into RAM and rebuilds
  from scratch on the first query after every upload/delete.
- **Why it matters:** Under real concurrency this is a data race (one request can observe a
  half-built index); at scale the full-namespace rebuild is a latency + memory spike. It's the
  unresolved architectural half of the original BUG-4.
- **Fix options (pick per Part B4):**
  - If keeping hybrid: guard the rebuild with a `threading.Lock` on the manager, and/or move BM25
    to a **persistent sparse index** (Pinecone sparse vectors) so there's no in-process rebuild.
  - If dropping hybrid (recommended for slimming): this method is deleted — race gone.

### 🟡 A5 (MEDIUM) — Image "chunks" embed useless placeholder text into the vector index

- **Where:** `_create_image_description` ([utils.py:123](src/utils.py:123)) +
  the image branch in `build_langchain_documents` ([ingestion.py:225](src/components/ingestion.py:225)).
- **What:** When an image has no alt-text/caption (the common case), the "description" embedded
  into Pinecone is literally:
  `"Image content on page N. Contains visual information related to document content."`
- **Why it matters:** That string carries no document-specific signal, but it becomes a real
  vector that can be retrieved and fed to the LLM as "context," diluting answer quality. There is
  **no vision model** actually describing the image. It's pure noise in the index. (Also largely
  inert in the default `PDF_PARSE_STRATEGY="fast"`, which doesn't extract images at all — so this
  is complexity that mostly does nothing, and actively hurts when it does fire.)
- **Fix:** delete image handling entirely (see B6). If you ever want real image QA, do it
  properly with a vision model — but that's a *different* project.

### 🟡 A6 (LOW) — Jaccard dedup (Feature E) never runs over the merged multi-query pool

- **Where:** `_deduplicate_chunks` runs inside `retrieve_candidates`
  ([retrieval.py:368](src/components/retrieval.py:368)) — i.e. **per sub-query** — while the
  cross-query merge in `_multi_query_retrieve_async`
  ([pipeline.py:197](src/pipeline/pipeline.py:197)) dedups only by **exact MD5** of content.
- **What:** Near-duplicate (but not byte-identical) chunks surfaced by different sub-queries
  survive into the final re-rank. The "fuzzy" dedup never sees the merged set.
- **Why it matters:** Minor quality/efficiency nit (re-ranker may waste a slot on a near-dup). Not
  a correctness bug. Mostly irrelevant once multi-query is removed (B4) — then a single retrieve
  path runs dedup once.
- **Fix:** if keeping multi-query, run `_deduplicate_chunks` once on the merged pool before
  `rerank`. If removing multi-query, no action.

### 🟡 A7 (LOW) — README ↔ code drift (config defaults, env-overridability, overlap)

- **Evidence:**
  - README says `SIMILARITY_THRESHOLD = 0.30` ([README.md:331](README.md:331)); code is `0.50`
    ([config.py:30](src/components/config.py:30)).
  - README: *"All settings … overridable via `.env`"* ([README.md:322](README.md:322)). In
    reality only `PDF_PARSE_STRATEGY`, `CORS_ORIGINS`, and the secret keys read from env; the rest
    are hardcoded dataclass defaults.
  - README ingestion diagram says `overlap 500` as if it's a headline; it's applied but its real
    effect under `chunk_by_title` is modest.
- **Why it matters:** Doc/code drift is the cheapest possible "attention to detail" signal to get
  right, and the easiest to get caught on.
- **Fix:** reconcile the README numbers to the actual `Config`, and either make the documented
  knobs truly env-driven (via `pydantic-settings`, see Part C) or stop claiming they are.

### 🟡 A8 (LOW) — Hybrid path depends on rank_bm25/LangChain **internal** attributes

- **Where:** [retrieval.py:290-295](src/components/retrieval.py:290) reads
  `_bm25_retriever.preprocess_func`, `.vectorizer.get_scores(...)`, `.docs`.
- **Status:** **Verified working** against the installed versions (built a retriever and called
  the exact path — it resolves). So this is not currently broken.
- **Why it's still a note:** these are undocumented internals; a `langchain-community` /
  `rank-bm25` bump can break them, and the failure is swallowed into dense-only (silent). If you
  keep hybrid, pin those two deps tightly and add a test that asserts the score-gating actually
  removes zero-overlap docs.

### 🟡 A9 (LOW) — Eval endpoints aren't admin-gated and load heavy deps into the API process

- **Where:** `evaluate.py` routes require `get_current_user` but **any** authenticated user can
  run RAGAS batch eval (rate-limited 2/min). `ragas` + `datasets` are imported into the live API.
- **Why it matters:** RAGAS makes many LLM calls per row — a logged-in user can burn your OpenAI
  budget, and the API image carries `datasets`/`pyarrow`/etc. for a feature with no UI.
- **Fix:** see **B5** — move eval out of the request path into an offline harness. (Aligns with
  your plan to build eval as its own focused effort.)

### 🟢 A10 (LOW) — Constructors mutate global `os.environ`; secrets read with no fail-fast

- **Where:** `EmbeddingManager.__init__` ([embeddings.py:29](src/components/embeddings.py:29)) and
  `RetrievalManager.__init__` ([retrieval.py:35](src/components/retrieval.py:35)) do
  `os.environ["PINECONE_API_KEY"] = …`. `Config` reads secrets as dataclass defaults with no
  validation ([config.py:75-84](src/components/config.py:75)); a missing key becomes `None: str`.
- **Why it matters:** Mutating process-global env from a constructor is a surprising side effect;
  missing-secret failures surface late with confusing errors instead of at startup.
- **Fix:** pass the key directly to the Pinecone client instead of via env; validate required
  settings at startup (Part C, `pydantic-settings`).

---

## 4. Part B — The slimming plan (delete for quality)

This is the part that makes the project **small and explainable**. Do Part A first (don't ship
known bugs), then execute these. For each: **what to remove · blast radius · payoff · interview angle.**

### B1 — Cut upload-format sprawl: 7 formats → 3 (PDF, DOCX, TXT)

- **Today:** `SUPPORTED_FILE_TYPES = (pdf, docx, pptx, txt, xlsx, csv, html)`
  ([config.py:89](src/components/config.py:89)), and `_PARTITION_MAP`
  ([ingestion.py:42](src/components/ingestion.py:42)) *additionally* wires up
  `.md, .json, .xml, .htm, .eml, .msg` — **none of which are reachable**, because uploads are
  validated against `SUPPORTED_FILE_TYPES`. So `partition_email`, `partition_json`,
  `partition_xml` and 5 map entries are **dead code**.
- **Remove:**
  1. From `_PARTITION_MAP`: `.json`, `.xml`, `.eml`, `.msg`, `.md` (and the imports
     `partition_email`, `partition_json`, `partition_xml`, `partition_text` if md/txt go).
  2. From `SUPPORTED_FILE_TYPES`: drop `pptx, xlsx, csv, html` → keep `pdf, docx, txt`.
  3. Mirror in the frontend: `SUPPORTED_TYPES` ([documents.py:15](frontend/pages/documents.py:15)).
  4. Update `conftest.py` unstructured stubs to match the trimmed import set.
- **Why these three:** PDF is the demo centerpiece; DOCX exercises real table extraction; TXT is
  the trivial happy path. PPTX/XLSX/CSV/HTML add format-specific edge cases and dependencies
  (`python-pptx`, `openpyxl`) for little narrative value.
- **Payoff:** removes 3 direct deps' *reason to exist* (see B3), deletes dead handlers, makes the
  ingestion story "we parse PDFs and Word docs" — one sentence.
- **Interview angle:** "I scoped ingestion to the formats that matter and kept the parser surface
  small on purpose" beats "it supports twelve formats" (which invites "show me CSV edge cases").

### B2 — Replace `unstructured[all-docs]` with lightweight, per-format parsers

- **Today:** [requirements.txt:9](requirements.txt:9) `unstructured[all-docs]==0.22.10`. The
  `[all-docs]` extra pulls the **entire** stack: layout-detection ML models, `detectron2`/YOLOX
  pathways, `onnx`, `opencv`, `nltk`, `pdf2image`, etc. The `hi_res` PDF strategy
  ([ingestion.py:85](src/components/ingestion.py:85)) needs them — but your **default is `fast`**,
  which is just `pdfminer` text extraction.
- **Two options (pick one):**
  - **B2a (lean, recommended):** drop `unstructured` entirely. Use `pypdf` for PDF text and
    `python-docx` for DOCX; TXT is a file read. Implement a tiny `chunk_text(text, size, overlap)`
    (recursive char split — you already depend on `langchain-text-splitters`, which provides
    `RecursiveCharacterTextSplitter`). You lose `unstructured`'s element classification, but with
    PDF/DOCX/TXT and no images that classification is mostly unused anyway.
  - **B2b (middle):** keep `unstructured` but with **narrow extras** only:
    `unstructured==0.22.10` (no `[all-docs]`) + the specific format deps you keep. Drop `hi_res`
    and image extraction (B6) so the heavy ML deps never install.
- **Payoff:** this is the single biggest install-size and complexity win. `unstructured[all-docs]`
  is the dependency that makes `pip install` take minutes and the Docker image huge.
- **Interview angle:** "I chose direct parsers over a heavyweight framework once I saw I only
  needed text from PDF/DOCX — the framework's ML layout features were never on the hot path."
- **Blast radius:** `ingestion.py` rewrites its parse step; `_PARTITION_MAP` and the
  element-introspection helpers in `utils.py` shrink dramatically. Re-run the ingestion-related
  tests; update `conftest.py` stubs.

### B3 — Remove dead / redundant dependencies

Verified **not imported anywhere** in `src/`, `frontend/`, or `tests/` (grep):

| Dependency | Status | Action |
|---|---|---|
| `langchain-experimental==0.4.1` | dead (no import) | **remove** |
| `aiofiles==25.1.0` | dead — code uses sync `Path.write_bytes` | **remove** |
| `unstructured-client==0.42.12` | dead — that's the *API* client, unused | **remove** |
| `langchain-text-splitters==1.1.1` | not imported directly (kept only if B2a uses it) | remove or keep-for-B2a |
| `pinecone-client==6.0.0` | conflicts with `pinecone 7.3.0` (A2) | **remove** |
| `pdf2image==1.17.0` | only needed by `unstructured` hi_res | remove with B2/B6 |
| `pypdf==6.9.2` | transitive (or direct if B2a) | keep only if B2a |
| `python-pptx`, `openpyxl` | only for pptx/xlsx (dropped in B1) | **remove** |
| `langchain-chroma` (in venv, not in reqs) | leftover from a prior Chroma impl | uninstall from venv |

- **Payoff:** ~7 fewer direct dependency lines; `requirements.txt` becomes a list of things you
  can actually point to and explain.
- **Process:** after editing, recreate the venv clean (`python -m venv venv && pip install -e .`)
  and run `pytest tests/` to prove nothing relied on a transitive you removed.

### B4 — Reduce the six "quality features" (A–F) to a defensible core

You have six toggles in `Config`. Quantity here works against you: each is code to maintain and a
question to answer. Recommended dispositions (a menu — pick per how much you want to defend):

| Flag | Feature | Keep? | Reasoning |
|---|---|---|---|
| **B** | Cross-encoder **re-ranking** | ✅ **Keep** | Strongest, most explainable quality win. Cheap to reason about ("retrieve broad with vectors, then a smarter model re-orders the top candidates"). This is your headline feature. |
| **A** | **Hybrid** BM25+dense (RRF) | ⚠️ Keep **only if** you fix A1+A4 properly (persistent sparse index) | Great talking point (RRF) *if it actually works*. As-is it's fragile and possibly dead. If you won't invest in a persistent sparse index, **drop to dense-only** and say so honestly. |
| **C** | **Multi-query** retrieval | ❌ **Drop / default-off** | Most expensive: an extra LLM call + 3–4× the retrievals + it's what made the BUG-6 ordering bug subtle. Marginal recall benefit for a single-doc Q&A demo. Biggest latency/cost sink on the hot path. |
| **D** | **Citation verification** | ✂️ **Simplify** | Keep a filename-level check (drop page-number matching, which is noisy because chunked PDFs often lack page numbers). Or keep as-is but acknowledge limits. |
| **E** | Retrieval-time **Jaccard dedup** | ❌ **Drop** | Exact-content dedup already happens at embed time (SHA-256). On 3–9 docs the fuzzy pass barely matters (A6). |
| **F** | Memory **summarization** | ❌ **Drop / default-off** | Adds an LLM call + a cache + token-budget logic. For chats under the window it's a no-op; for long chats it's a latency add. A plain "last N turns" window is enough and trivially explainable. |

- **Net:** 6 flags → **keep B (rerank) + a clean dense retrieve**, optionally keep A if you do it
  right. That removes multi-query, summarization, and dedup code paths (and an LLM round-trip from
  the hot path), and collapses the query pipeline to:
  `rewrite-if-history → dense retrieve(top-k) → cross-encoder rerank(top-3) → generate`.
- **Interview angle:** "I implemented six retrieval enhancements, measured them, and **kept the
  two that earned their complexity**" is a *far* stronger story than six flags you can't justify.
  (This also sets up your eval project — you'll have real numbers behind the choice.)
- **Blast radius:** `generation.generate_multi_queries`, the multi-query helper in `pipeline.py`,
  `_deduplicate_chunks`, and Feature-F code in `utils.py` get deleted; `query`/`query_stream`
  simplify; related tests get removed/trimmed.

### B5 — Move RAGAS evaluation out of the live API into an offline harness

- **Today:** `/api/evaluate/{single,batch}` routes (`evaluate.py`) + `EvaluationManager` import
  `ragas` + `datasets` into the API process; there's no frontend for them and no gold set.
- **Do:**
  1. Delete the `evaluate` router and its `get_eval_manager` wiring from `main.py` /
     `dependencies.py`.
  2. Move `EvaluationManager` into a standalone script, e.g. `scripts/run_eval.py`, that loads a
     versioned gold set and prints/saves scores. (This is exactly the shape of the comprehensive
     eval plan in `CODE_REVIEW.md` §6 — the offline, gold-set, CI-regression design.)
  3. Drop `ragas` + `datasets` from the API's runtime requirements (move to a `dev`/`eval` extra).
- **Why:** removes two heavy deps + an abuse vector (A9) from the live service, and **keeps your
  eval ambitions intact** — just in the right place (offline, reproducible, version-controlled).
- **Interview angle:** "Eval is a batch, offline concern with a gold set and CI regression gates —
  not a live endpoint a user can hit." That's the correct mental model and signals maturity.

### B6 — Delete image (and optionally table) handling

- **Remove:** the image branch in `build_langchain_documents`
  ([ingestion.py:225](src/components/ingestion.py:225)), `_create_image_description`,
  `_element_has_image_payload`, `_IMAGE_EXTRA_FIELDS`, the `extract_image_block_*` kwargs in the
  `hi_res` partitioner, and `pdf2image`.
- **Why:** A5 — it embeds noise, needs `hi_res` (heavy) to fire at all, and has no vision model
  behind it. Tables are more defensible (DOCX tables extract as real HTML); keep tables **only if**
  you keep DOCX and find them useful, otherwise drop the table branch too for maximum slimming.
- **Payoff:** removes the most "magical but fake" part of the pipeline and a chunk of `utils.py`.

---

## 5. Part C — Production-grade hardening (minimal, do on the slimmed code)

Only the high-leverage items. Don't gold-plate a demo.

1. **Typed settings + fail-fast secrets (replaces A10):** adopt `pydantic-settings`
   (`BaseSettings`). Required secrets (`OPENAI_API_KEY`, `PINECONE_*`, `SUPABASE_*`) validated at
   startup → the app refuses to boot misconfigured instead of failing on the first request. This
   also makes the README's "overridable via env" claim (A7) actually true.
2. **Background ingestion (or document the tradeoff):** upload currently does parse + embed +
   upsert inside the request (wrapped in `to_thread`, so it doesn't block the loop, but the HTTP
   call still waits seconds–minutes). For production, return `202 Accepted` + a job id and process
   via a background task/queue; the UI polls status. **Minimum viable:** keep it synchronous but
   document the limit and enforce the existing size cap. (Big PDFs on `fast` are seconds, so this
   is optional for a demo — but name it as a known tradeoff.)
3. **Decide hybrid honestly (ties to A1/A4/B4):** either implement a **persistent sparse index**
   (Pinecone sparse vectors) so "hybrid" is real and multi-worker-safe, or go **dense-only** and
   update README/feature list. Don't ship a feature flag that silently no-ops.
4. **Observability:** you already have request-id logging + total latency in the middleware.
   Add **per-stage timings** (rewrite / retrieve / rerank / generate) at INFO, and capture OpenAI
   token usage via a LangChain callback so you can talk about cost/latency with numbers.
5. **One real end-to-end test:** add an `httpx.AsyncClient` + `ASGITransport` test that hits
   `/api/chat/query` with a mocked pipeline and asserts a 200 + shape. This is the test class that
   would have caught the original BUG-1 (and would catch A1's API surface). CI already runs
   `pytest`; just add the test.
6. **Containerize:** a small `Dockerfile` (slim base, `pip install -e .`, non-root user,
   `HEALTHCHECK` hitting `/health`) + a `docker-compose.yml` running API + frontend. With the
   slimmed deps (B2/B3) the image is finally a sane size. This is a concrete "I can ship it" signal.
7. **Pin what you keep, drop what you don't:** after B3, every line in `requirements.txt` should be
   imported somewhere. Split a `[dev]`/`[eval]` extra for `pytest`, `ragas`, `datasets`, lint.

---

## 6. Part D — Target architecture & execution order

### Target "after" shape (what you'll be able to whiteboard)

```
Streamlit (login · chat · documents)
        │  HTTP / SSE
FastAPI  ─ auth ─ documents ─ chat            (eval is now an offline script)
        │
RAGPipeline
   ingest:  parse (pypdf / python-docx / txt) → chunk (recursive split)
            → SHA-256 dedup → embed (text-embedding-3-small) → upsert (Pinecone, ns=user_id)
   query:   rewrite-if-history → dense retrieve(top_k) → cross-encoder rerank(top_3)
            → generate (gpt-4o-mini, cited) → SSE stream
Supabase: auth (JWT) · storage · user_documents metadata
```

Three formats. One retrieval path. One headline quality feature (re-ranking). Eval offline.
That is a system you can explain on a whiteboard in three minutes and defend every box of.

### Execution order (each step = its own commit, run `pytest` + lint between, log to BUGFIXES.md)

1. **A2** — fix `pinecone-client`/`pinecone` (5 min, removes demo-breaking risk).
2. **A1** — verify empty-string embedding; fix the BM25 rebuild (or note it'll vanish in step 6).
3. **A3, A7** — delete dead config + reconcile README (cheap, high signal).
4. **B5** — extract eval to offline script; drop `ragas`/`datasets` from API.
5. **B6** — delete image handling.
6. **B4** — trim feature flags (drop multi-query, dedup, summarization; keep rerank; decide hybrid).
   - This is where A4/A5/A6 mostly disappear as a side effect.
7. **B1** — cut formats to PDF/DOCX/TXT; update frontend + conftest stubs.
8. **B2** — swap `unstructured[all-docs]` for lightweight parsers.
9. **B3** — purge dead deps; rebuild venv clean; full `pytest`.
10. **Part C** — settings/fail-fast, per-stage timings, one e2e test, Dockerfile.

Do **A first, then B (delete), then C (harden)**. Deleting before hardening avoids polishing code
you're about to remove.

---

## 7. Appendix

### 7.1 File-by-file disposition

| File | Disposition |
|---|---|
| `src/components/config.py` | **Trim** — drop `STREAMING`, `EMBEDDING_BATCH_SIZE`, unused feature flags; shrink `SUPPORTED_FILE_TYPES`; migrate to `pydantic-settings`. |
| `src/components/ingestion.py` | **Heavy trim** — drop dead format handlers (B1), image branch (B6), swap parser stack (B2). |
| `src/components/embeddings.py` | Keep; remove `os.environ` mutation (A10). |
| `src/components/retrieval.py` | **Trim** — fix/remove BM25 (A1/A4/B4); drop `_deduplicate_chunks` (B4); keep dense + rerank. |
| `src/components/generation.py` | **Trim** — remove `generate_multi_queries` (B4); simplify citation verify (B4-D). |
| `src/components/database.py` | Keep — solid. |
| `src/components/evalution.py` | **Move** → `scripts/run_eval.py` (B5). |
| `src/pipeline/pipeline.py` | **Trim** — remove multi-query helper; linear query path (B4). |
| `src/api/main.py` | **Trim** — drop `evaluate` router (B5). |
| `src/api/router/evaluate.py` | **Delete** (B5). |
| `src/api/router/{auth,documents,chat}.py` | Keep — good. |
| `src/api/{dependencies,error_utils,limiter}.py` | Keep — good patterns. |
| `src/utils.py` | **Trim** — remove image/summarization helpers (B4-F, B6); keep filename + history. |
| `src/{logger,exception}.py` | Keep. |
| `frontend/*` | **Minor** — update `SUPPORTED_TYPES` (B1). |
| `tests/*` | **Trim** — remove tests for deleted features; **add** one e2e API test (Part C). |
| `requirements.txt` / `setup.py` | **Rewrite** per B2/B3 + `[dev]`/`[eval]` extras. |
| `docs/Smart_Signal…pdf` | Keep — it's the demo fixture. |
| `BUGFIXES.md` / `CODE_REVIEW.md` | Keep — interview narrative assets. |

### 7.2 Dependency before → after (illustrative, B2a path)

**Remove:** `unstructured[all-docs]`, `unstructured-client`, `langchain-experimental`,
`langchain-chroma` (venv), `aiofiles`, `pinecone-client`, `python-pptx`, `openpyxl`, `pdf2image`,
and `ragas`+`datasets` (→ `[eval]` extra).
**Add (B2a):** `pypdf`, `python-docx` (and keep `langchain-text-splitters`).
**Net:** ~28 direct lines → ~15, and the install loses its multi-hundred-MB ML tail.

### 7.3 Dead/empty config fields to delete (verified by grep)

- `STREAMING` — never read.
- `EMBEDDING_BATCH_SIZE` — never read (README implies it controls batching; it doesn't).
- After B4: `USE_MULTI_QUERY`, `MULTI_QUERY_COUNT`, `USE_CHUNK_DEDUP`, `CHUNK_DEDUP_THRESHOLD`,
  `USE_MEMORY_SUMMARIZATION`, `MEMORY_SUMMARIZATION_WINDOW`, and (if hybrid dropped)
  `USE_HYBRID_SEARCH`, `HYBRID_SEARCH_WEIGHT`.

### 7.4 Things explicitly **not** flagged (checked, fine)

- BM25 internal-API access works on the installed versions (A8 is version-risk only, not a live bug).
- `record_upload` upsert has its matching `UNIQUE (user_id, filename)` constraint in
  `supabase_migration.sql`.
- RLS policies exist; the service-role design is intentional (documented as SEC-3 tradeoff).
- Async/await, rate limiting, filename sanitization, error masking, rollbacks — all correct.

---

*End of report. Suggested next action: execute Part D step 1 (A2) and step 2 (verify A1), since
both are fast and both de-risk a live demo. Then proceed through the slimming steps in order.*
