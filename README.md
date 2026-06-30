<div align="center">

# 🧠 DocuMind

### AI-Powered Document Intelligence Platform

*Ask anything about your documents. Get cited, grounded answers in seconds.*

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?style=flat&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.42+-FF4B4B?style=flat&logo=streamlit&logoColor=white)](https://streamlit.io)
[![Pinecone](https://img.shields.io/badge/Pinecone-Vector_DB-000000?style=flat&logo=pinecone&logoColor=white)](https://pinecone.io)
[![Groq](https://img.shields.io/badge/Groq-Llama_3.3_70B-F55036?style=flat&logo=groq&logoColor=white)](https://groq.com)
[![Embeddings](https://img.shields.io/badge/Embeddings-local_all--mpnet-FFD21E?style=flat&logo=huggingface&logoColor=black)](https://huggingface.co/sentence-transformers/all-mpnet-base-v2)
[![Supabase](https://img.shields.io/badge/Supabase-Auth_+_Storage-3ECF8E?style=flat&logo=supabase&logoColor=white)](https://supabase.io)
[![CI](https://github.com/Meetbarasara/DocuMind-RAG/actions/workflows/ci.yml/badge.svg)](https://github.com/Meetbarasara/DocuMind-RAG/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

---

## What is DocuMind?

DocuMind is a production-grade **Retrieval-Augmented Generation (RAG)** platform that lets you upload a document (PDF, DOCX, or TXT) and have an intelligent conversation with its contents.

Every answer is:
- **Grounded** — only uses information from your uploaded documents
- **Cited** — every claim links back to the exact source file and page number
- **Streamed** — tokens arrive in real-time, no waiting for the full response
- **Contextual** — multi-turn conversation with automatic query rewriting for follow-ups

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      Streamlit Frontend                         │
│         Login · Chat (SSE streaming) · Document Manager         │
└────────────────────────┬────────────────────────────────────────┘
                         │ HTTP / SSE
┌────────────────────────▼────────────────────────────────────────┐
│                    FastAPI Backend                               │
│   /api/auth/*    /api/documents/*    /api/chat/*    /health     │
└──────┬─────────────────┬──────────────────┬─────────────────────┘
       │                 │                  │
┌──────▼──────┐  ┌───────▼───────┐  ┌──────▼──────────────────────┐
│  Supabase   │  │  RAG Pipeline │  │       RAG Pipeline           │
│  Auth +     │  │  Ingestion    │  │  Cache → Retrieve → Rerank   │
│  Storage    │  │  (background) │  │  → Generate · Citations      │
└─────────────┘  └───────┬───────┘  └──────┬──────────────────────┘
                         │                  │
                ┌────────▼──────────────────▼────────┐
                │          External Services          │
                │  Pinecone (vectors) · Groq (LLM)    │
                │  + optional: Cohere (rerank) ·       │
                │    Redis (cache) · LangSmith (trace) │
                └────────────────────────────────────┘
```

### RAG Pipeline — Step by Step

```
Document Upload
      │
      ▼
 [Accept]     ── validate, schedule ──▶  202 + job id (returns immediately)
      │
      ▼ (background)
 [Ingestion]  ── PyMuPDF / python-docx ──▶  Per-page text + extracted images
      │
      ▼
 [Chunking]   ── 512 tiktoken tokens ──▶  LangChain Documents with metadata
      │
      ▼
 [Embedding]  ── all-mpnet-base-v2 (local) ──▶  768-dim vectors (+ sparse, if hybrid is on)
      │
      ▼
 [Pinecone]   ── namespace = user_id ──▶  Per-user vector isolation; job → completed
      │
User Question
      │
      ▼
 [Cache?]     ── Redis exact/semantic match ──▶  HIT: answer in ms, skip everything below
      │ MISS
      ▼
 [Rewrite]    ── Llama-3.3-70B (Groq) ──▶  Standalone query (resolves pronouns)
      │
      ▼
 [Retrieve]   ── dense cosine, or native hybrid ──▶  Top-K candidates above threshold
      │
      ▼
 [Rerank]     ── Cohere Rerank API ──▶  Most relevant few, reordered
      │
      ▼
 [Generate]   ── Llama-3.3-70B (Groq) ──▶  Grounded, cited answer
      │
      ▼
 SSE Stream ──▶  Token-by-token to UI; cache the answer; 👍/👎 → LangSmith
```

---

## Features

| Feature | Details |
|---|---|
| 📄 **Document ingestion** | PDF, DOCX, TXT via PyMuPDF + python-docx (images extracted too); upload returns immediately, ingestion runs in the background |
| 🧩 **Token-based chunking** | Token-boundary splitting via tiktoken — predictable context size + cost |
| 🔍 **Hybrid retrieval** | Dense (cosine) search, or Pinecone *native* server-side sparse+dense fusion (off by default — needs a dotproduct index) |
| 🎯 **Re-ranking** | Cohere Rerank API narrows the candidate set to the most relevant chunks (graceful fallback to retrieval order without a key) |
| ✍️ **Query rewriting** | Automatic follow-up resolution using conversation history |
| 💬 **Streaming responses** | Server-Sent Events (SSE) for real-time token delivery |
| 🖼️ **Multimodal answers** | Pages with figures/tables are rendered as images; the LLM reads the actual page for those answers |
| 📚 **Inline citations** | `[Source: filename, Page X]` in every answer, verified against the real retrieved sources |
| ⚡ **Caching** | Redis exact-match + semantic (near-duplicate question) cache — repeat questions skip retrieval + the LLM entirely (off by default — needs `REDIS_URL`) |
| 📊 **Observability** | LangSmith tracing (per-stage timings, token/cost) + a 👍/👎 feedback loop, both off by default — needs `LANGSMITH_*` |
| 🧪 **Offline evaluation** | Retrieval (Hit@k/Recall@k/MRR) + RAGAS generation metrics against a versioned gold set, with a CI regression gate — see [`scripts/run_eval.py`](scripts/run_eval.py) |
| 🔒 **Multi-user auth** | Supabase Auth (JWT) with per-user Pinecone namespace isolation |
| ☁️ **Cloud storage** | Files stored in Supabase Storage, metadata in PostgreSQL |
| 🐳 **Containerized** | `Dockerfile` + `docker-compose.yml` (API + frontend) |
| 🔄 **CI pipeline** | GitHub Actions: lint → syntax check → import validation → tests |

---

## Tech Stack

| Layer | Technology |
|---|---|
| **LLM** | Groq `llama-3.3-70b-versatile` (hosted, generous free tier) |
| **Embeddings** | Local `sentence-transformers/all-mpnet-base-v2` (768-dim, CPU, no API/quota) |
| **Vector DB** | Pinecone (serverless; cosine, or dotproduct for native hybrid) |
| **Re-ranking** | Cohere Rerank API (optional) |
| **Caching** | Redis (optional — exact-match + semantic) |
| **Observability** | LangSmith (optional — tracing, per-stage timing, feedback) |
| **Document parsing** | PyMuPDF (PDF) + python-docx (DOCX) |
| **RAG framework** | LangChain + `langchain-pinecone` |
| **Settings** | `pydantic-settings` (fail-fast secret validation at startup) |
| **Backend API** | FastAPI + Uvicorn |
| **Frontend** | Streamlit |
| **Auth + Storage** | Supabase (PostgreSQL + S3-compatible storage) |
| **Evaluation** | RAGAS (offline harness, not a live endpoint) |
| **HTTP client** | httpx (async SSE streaming) |
| **Containers** | Docker + Docker Compose |

---

## Project Structure

```
DocuMind/
├── src/
│   ├── components/
│   │   ├── config.py          # pydantic-settings: typed, fail-fast, env-driven config
│   │   ├── ingestion.py       # Document parsing & token-based chunking
│   │   ├── embeddings.py      # local sentence-transformers embed + Pinecone upsert (dense or native hybrid)
│   │   ├── retrieval.py       # Dense/hybrid search + Cohere re-rank
│   │   ├── sparse.py          # Stateless lexical encoder for native hybrid (no extra dep)
│   │   ├── generation.py      # Query rewriting + LLM generation + SSE + feedback run_id
│   │   ├── cache.py           # Redis exact-match + semantic query cache
│   │   ├── database.py        # Supabase auth + file storage + metadata
│   │   └── evalution.py       # RAGAS + retrieval metrics (used by scripts/run_eval.py)
│   ├── pipeline/
│   │   └── pipeline.py        # End-to-end RAG orchestrator
│   ├── api/
│   │   ├── main.py            # FastAPI app + CORS + logging middleware
│   │   ├── dependencies.py    # Singleton DI: Config, DB, Pipeline
│   │   └── router/
│   │       ├── auth.py        # POST /api/auth/{signup,login,logout,me}
│   │       ├── documents.py   # Upload (background ingestion) + list + delete
│   │       └── chat.py        # POST /api/chat/{query[/stream],feedback}
│   ├── logger.py              # Rotating file + stream logger
│   ├── exception.py           # Custom exception with traceback detail
│   └── utils.py               # Filename sanitization, chat history formatting
├── frontend/
│   ├── app.py                 # Streamlit entry point + routing
│   ├── utils.py               # httpx API client + session state helpers
│   └── pages/
│       ├── login.py           # Sign-in / Sign-up UI
│       ├── chat.py            # Streaming chat + citations + 👍/👎 feedback
│       └── documents.py       # Upload + list + delete documents
├── scripts/
│   └── run_eval.py            # Offline eval harness (retrieval metrics + RAGAS + CI gate)
├── docs/                       # Sample documents for testing / the eval gold set
├── data/eval/                  # Gold set + committed baseline for the CI regression gate
├── logs/                       # Rotating log files (auto-created)
├── .github/
│   └── workflows/
│       └── ci.yml             # GitHub Actions CI
├── Dockerfile                  # Shared image for the API + frontend services
├── docker-compose.yml          # Runs both services together
├── supabase_migration.sql     # DB schema — run once in Supabase SQL Editor
├── .env.example               # Environment variables template
├── requirements.txt
└── setup.py
```

---

## Setup

### Prerequisites

- Python 3.11+ (or Docker, if you'd rather skip the venv — see step 4)
- [Groq API key](https://console.groq.com/keys) — free, powers the LLM. (Embeddings run locally,
  so there's no embedding API key to get.)
- [Pinecone account](https://pinecone.io) — create an index named `documind` (dimension: `768`,
  metric: `cosine`). Native hybrid search (off by default) needs a *second*, `dotproduct` index instead.
  (The local `all-mpnet-base-v2` embedding model is 768-dim — if you have a 1536-dim index left over
  from an OpenAI setup, re-create it at 768. The first run downloads the model, ~420MB, to your HF cache.)
- [Supabase project](https://supabase.com) — free tier works fine

### 1. Clone & install

Skip this step if you're using Docker (step 4, option A) — the image installs everything.

```bash
git clone https://github.com/Meetbarasara/DocuMind-RAG.git
cd DocuMind-RAG
python -m venv venv

# Windows
.\venv\Scripts\activate

# macOS / Linux
source venv/bin/activate

pip install -e .
```

Running tests or linting locally? `pip install -e ".[dev]"` (pytest, ruff, pyflakes, fakeredis).
Running the offline eval harness? `pip install -e ".[eval]"` (RAGAS — not needed for the live app).

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
GROQ_API_KEY=gsk_...
PINECONE_API_KEY=pcsk_...
PINECONE_INDEX_NAME=documind
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_ANON_KEY=eyJ...
SUPABASE_SERVICE_ROLE_KEY=eyJ...
```

### 3. Set up Supabase

**Storage bucket** — not created automatically; create it once before the first upload (the app
assumes `documents` already exists and will fail uploads otherwise):
```python
from supabase import create_client
c = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
c.storage.create_bucket("documents", options={"public": False})
```

**Database table** — run `supabase_migration.sql` in the [Supabase SQL Editor](https://supabase.com/dashboard/project/_/sql/new):
```bash
# The file is at the project root
cat supabase_migration.sql
```

### 4. Run the application

**Option A — Docker (one command, both services):**
```bash
docker compose up --build
```

**Option B — manually:**

**Terminal 1 — FastAPI backend:**
```bash
python -m uvicorn src.api.main:app --reload --port 8000
```

**Terminal 2 — Streamlit frontend:**
```bash
streamlit run frontend/app.py
```

Open **http://localhost:8501** in your browser. `Config`'s fail-fast validation means the app
refuses to boot if a required secret (`GROQ_API_KEY`, `PINECONE_API_KEY`, `SUPABASE_*`) is
missing or blank — check the startup error if it doesn't come up.

---

## API Reference

Base URL: `http://localhost:8000`

### Auth

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/auth/signup` | Create account (`email`, `password`) |
| `POST` | `/api/auth/login` | Sign in → returns `access_token` + `refresh_token` |
| `POST` | `/api/auth/refresh` | Exchange a `refresh_token` for a fresh token pair (renew an expiring session) |
| `POST` | `/api/auth/logout` | Invalidate session |
| `GET` | `/api/auth/me` | Get current user info |

### Documents

> All endpoints require `Authorization: Bearer <token>`

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/documents/upload` | Accept a file (multipart) → `202 {job_id, status, filename}`; ingestion (Storage + Pinecone) runs in the background |
| `GET` | `/api/documents/upload-status/{job_id}` | Poll a background ingestion job → `{status, chunks_ingested, error}` |
| `GET` | `/api/documents/` | List user's uploaded documents |
| `DELETE` | `/api/documents/{filename}` | Delete from Storage + Pinecone |

### Chat

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/chat/query` | Blocking Q&A → `{answer, sources, ...}` |
| `POST` | `/api/chat/query/stream` | Streaming Q&A (SSE) → token-by-token, plus a `meta` event with a LangSmith `run_id` when tracing is on |
| `POST` | `/api/chat/feedback` | Record a 👍/👎 (`{run_id, score}`) as a LangSmith feedback score; no-op when tracing is off |

### Conversations (persistent chat history)

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/conversations` | Start a new conversation → `{id, title}` |
| `GET` | `/api/conversations` | List the user's conversations (most-recent first) |
| `GET` | `/api/conversations/{id}/messages` | Load a conversation's messages |
| `POST` | `/api/conversations/{id}/messages` | Append a message (`{role, content, sources?, run_id?}`) |
| `DELETE` | `/api/conversations/{id}` | Delete a conversation (messages cascade) |

> **Evaluation isn't a live endpoint.** It's an offline harness (`scripts/run_eval.py`) over a
> versioned gold set (`data/eval/`) — retrieval metrics (Hit@k/Recall@k/MRR) + RAGAS generation
> metrics + a refusal-rate check, with a CI gate that fails the build on a regression against the
> committed baseline. Run it on demand: `python -m scripts.run_eval`.

#### Example: Query

```bash
curl -X POST http://localhost:8000/api/chat/query \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What are the key findings?",
    "chat_history": [],
    "filename_filter": null
  }'
```

**Response:**
```json
{
  "answer": "The key findings are... [Source: report.pdf, Page: 3]",
  "sources": [
    {
      "source_id": 1,
      "filename": "report.pdf",
      "page": 3,
      "chunk_type": "text",
      "chunk_id": "report.pdf::a1b2c3...",
      "has_visual": false
    }
  ],
  "rewritten_query": "What are the key findings?",
  "num_sources_used": 3,
  "namespace": "eb332ef7-..."
}
```
> The streaming endpoint's `sources` event additionally includes a `content` snippet per source
> (a short preview of the chunk text) — the blocking endpoint above does not.

Interactive docs: **http://localhost:8000/docs**

---

## Configuration Reference

All settings live in `src/components/config.py` (`pydantic-settings`) and are overridable via
`.env` — the five required secrets (`GROQ_API_KEY`, `PINECONE_API_KEY`, `SUPABASE_URL`,
`SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`) fail fast at startup if missing or blank.

**Core**

| Setting | Default | Description |
|---|---|---|
| `EMBEDDING_MODEL_NAME` | `sentence-transformers/all-mpnet-base-v2` | Local embedding model (768-dim, CPU) |
| `LLM_MODEL_NAME` | `llama-3.3-70b-versatile` | Groq-hosted chat model |
| `CHUNK_SIZE_TOKENS` | `512` | Max tokens per chunk |
| `CHUNK_OVERLAP_TOKENS` | `64` | Token overlap between chunks |
| `TOP_K` | `5` | Chunks retrieved per query |
| `SIMILARITY_THRESHOLD` | `0.50` | Min cosine score to keep |
| `LLM_TEMPERATURE` | `0.1` | LLM creativity (lower = more factual) |
| `RERANKER_TOP_K` | `3` | Chunks kept after Cohere rerank |
| `EMBEDDING_BATCH_SIZE` | `100` | Vectors per Pinecone upsert batch (native-hybrid upsert path) |
| `MAX_UPLOAD_SIZE_BYTES` | `50MB` | Upload size cap, enforced before buffering the file |
| `API_PORT` | `8000` | FastAPI server port |

**Feature flags / optional services** (all need the matching key/URL to actually activate)

| Setting | Default | Description |
|---|---|---|
| `USE_HYBRID_SEARCH` | `false` | Pinecone native sparse+dense fusion — needs a `dotproduct` index + re-ingest |
| `USE_RERANKING` | `true` | Cohere Rerank API — needs `COHERE_API_KEY`, else falls back to retrieval order |
| `REDIS_URL` | unset | Exact-match query cache — unset disables it (fail-open no-op) |
| `USE_SEMANTIC_CACHE` | `true` | Serve a near-identical past question (cosine on its embedding); needs `REDIS_URL` |
| `LANGSMITH_TRACING` | `false` | Trace every chain to LangSmith — needs `LANGSMITH_API_KEY` |
| `USE_IMAGE_ANSWERING` | `false` | Render PDF pages with figures/tables and answer over the page image — needs a vision-capable LLM (Groq Llama-3.3-70B is text-only) |
| `USE_CITATION_VERIFICATION` | `true` | Flag whether each `[Source: ...]` citation names a real retrieved file |

---

## How It Works

### Ingestion

1. Upload is validated (filename, extension, size cap) and accepted — `202` + a job id are
   returned immediately; everything below runs in the background
2. File is saved to Supabase Storage
3. PyMuPDF (PDF) / python-docx (DOCX) extracts per-page text + embedded images; pages with
   figures/tables are also rendered to an image (for multimodal answers later)
4. Text is split into ~512-token chunks (64-token overlap) via `tiktoken` — predictable context
   size and cost, independent of the source format
5. Each chunk becomes a LangChain `Document` with metadata (`filename`, `page_number`, `chunk_type`)
6. SHA-256 deduplication removes identical chunks
7. A local sentence-transformers model embeds each chunk → 768-dim vector (+ sparse, if native hybrid is on)
8. Vectors are upserted to Pinecone under the user's namespace; job status flips to `completed`,
   polled via `GET /api/documents/upload-status/{job_id}`

### Querying

1. An exact or semantically-near-identical past question is served straight from Redis, if caching
   is on (skips everything below)
2. If chat history exists → LLM rewrites the query to be standalone
3. The query is embedded once and searched in Pinecone (dense cosine, or native hybrid fusion)
4. Cohere re-ranks the candidates down to the most relevant few
5. Retrieved chunks are formatted with source labels (plus the page's rendered image, for chunks
   with figures/tables)
6. LLM generates a grounded answer with inline citations, verified against the real sources
7. Response streams token-by-token via SSE; the answer is cached for next time

---

## CI / CD

GitHub Actions runs on every push and pull request:

```
push / PR
    │
    ├── lint-and-typecheck   ── ruff + pyflakes
    │
    ├── test                 ── syntax check + pytest (all with mocked env vars)
    │
    └── api-import-check     ── verifies FastAPI app imports cleanly
```

---

## License

MIT © [Meet Barasara](https://github.com/Meetbarasara)
