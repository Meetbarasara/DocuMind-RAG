<div align="center">

# 🧠 DocuMind

### AI-Powered Document Intelligence Platform

*Ask anything about your documents. Get cited, grounded answers in seconds.*

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?style=flat&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.42+-FF4B4B?style=flat&logo=streamlit&logoColor=white)](https://streamlit.io)
[![Pinecone](https://img.shields.io/badge/Pinecone-Vector_DB-000000?style=flat&logo=pinecone&logoColor=white)](https://pinecone.io)
[![OpenAI](https://img.shields.io/badge/OpenAI-GPT--4o--mini-412991?style=flat&logo=openai&logoColor=white)](https://openai.com)
[![Supabase](https://img.shields.io/badge/Supabase-Auth_+_Storage-3ECF8E?style=flat&logo=supabase&logoColor=white)](https://supabase.io)
[![CI](https://github.com/Meetbarasara/DocuMind-RAG/actions/workflows/ci.yml/badge.svg)](https://github.com/Meetbarasara/DocuMind-RAG/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

---

## What is DocuMind?

DocuMind is a production-grade **Retrieval-Augmented Generation (RAG)** platform that lets you upload any document (PDF, DOCX, PPTX, XLSX, CSV, TXT, HTML) and have an intelligent conversation with its contents.

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
│  Auth +     │  │  Ingestion    │  │  Retrieval → Generation      │
│  Storage    │  │  Embedding    │  │  Query Rewrite · Citations   │
└─────────────┘  └───────┬───────┘  └──────┬──────────────────────┘
                         │                  │
                ┌────────▼──────────────────▼────────┐
                │          External Services          │
                │  Pinecone (vectors) · OpenAI (LLM)  │
                └────────────────────────────────────┘
```

### RAG Pipeline — Step by Step

```
Document Upload
      │
      ▼
 [Ingestion]  ── unstructured ──▶  Parse: text, tables, images
      │
      ▼
 [Chunking]   ── overlap 500 ──▶  LangChain Documents with metadata
      │
      ▼
 [Embedding]  ── text-embedding-3-small ──▶  1536-dim vectors
      │
      ▼
 [Pinecone]   ── namespace = user_id ──▶  Per-user vector isolation
      │
User Question
      │
      ▼
 [Rewrite]    ── gpt-4o-mini ──▶  Standalone query (resolves pronouns)
      │
      ▼
 [Retrieve]   ── cosine similarity ──▶  Top-K chunks above threshold
      │
      ▼
 [Generate]   ── gpt-4o-mini ──▶  Grounded answer with [Source: file, Page X]
      │
      ▼
 SSE Stream ──▶  Token-by-token to UI
```

---

## Features

| Feature | Details |
|---|---|
| 📄 **Multi-format ingestion** | PDF, DOCX, PPTX, XLSX, CSV, TXT, HTML |
| 🧩 **Smart chunking** | Semantic chunking via `unstructured` with configurable overlap |
| 🔢 **Vector embeddings** | `text-embedding-3-small` (1536-dim), batched upsert to Pinecone |
| 🔍 **Similarity retrieval** | Cosine search with score threshold filtering |
| ✍️ **Query rewriting** | Automatic follow-up resolution using conversation history |
| 💬 **Streaming responses** | Server-Sent Events (SSE) for real-time token delivery |
| 📚 **Inline citations** | `[Source: filename, Page X]` in every answer |
| 🔒 **Multi-user auth** | Supabase Auth (JWT) with per-user Pinecone namespace isolation |
| ☁️ **Cloud storage** | Files stored in Supabase Storage, metadata in PostgreSQL |
| 🧪 **RAGAS evaluation** | Faithfulness, answer relevancy, context precision/recall |
| 🔄 **CI pipeline** | GitHub Actions: lint → syntax check → import validation |

---

## Tech Stack

| Layer | Technology |
|---|---|
| **LLM** | OpenAI `gpt-4o-mini` |
| **Embeddings** | OpenAI `text-embedding-3-small` |
| **Vector DB** | Pinecone (serverless, cosine metric) |
| **Document parsing** | `unstructured[all-docs]` |
| **RAG framework** | LangChain + `langchain-pinecone` |
| **Backend API** | FastAPI + Uvicorn |
| **Frontend** | Streamlit |
| **Auth + Storage** | Supabase (PostgreSQL + S3-compatible storage) |
| **Evaluation** | RAGAS |
| **HTTP client** | httpx (async SSE streaming) |

---

## Project Structure

```
DocuMind/
├── src/
│   ├── components/
│   │   ├── config.py          # Centralized dataclass config
│   │   ├── ingestion.py       # Document parsing & chunking
│   │   ├── embeddings.py      # OpenAI embed + Pinecone upsert
│   │   ├── retrieval.py       # Similarity search with filters
│   │   ├── generation.py      # Query rewriting + LLM generation + SSE
│   │   ├── database.py        # Supabase auth + file storage + metadata
│   │   └── evalution.py       # RAGAS evaluation metrics
│   ├── pipeline/
│   │   └── pipeline.py        # End-to-end RAG orchestrator
│   ├── api/
│   │   ├── main.py            # FastAPI app + CORS + logging middleware
│   │   ├── dependencies.py    # Singleton DI: Config, DB, Pipeline
│   │   └── router/
│   │       ├── auth.py        # POST /api/auth/{signup,login,logout,me}
│   │       ├── documents.py   # POST/GET/DELETE /api/documents/
│   │       └── chat.py        # POST /api/chat/query[/stream]
│   ├── logger.py              # Rotating file + stream logger
│   ├── exception.py           # Custom exception with traceback detail
│   └── utils.py               # Element helpers, chat history formatting
├── frontend/
│   ├── app.py                 # Streamlit entry point + routing
│   ├── utils.py               # httpx API client + session state helpers
│   └── pages/
│       ├── login.py           # Sign-in / Sign-up UI
│       ├── chat.py            # Streaming chat + citations + doc filter
│       └── documents.py       # Upload + list + delete documents
├── docs/                      # Sample documents for testing
├── logs/                      # Rotating log files (auto-created)
├── .github/
│   └── workflows/
│       └── ci.yml             # GitHub Actions CI
├── supabase_migration.sql     # DB schema — run once in Supabase SQL Editor
├── .env.example               # Environment variables template
├── requirements.txt
└── setup.py
```

---

## Setup

### Prerequisites

- Python 3.11+
- [OpenAI API key](https://platform.openai.com/api-keys)
- [Pinecone account](https://pinecone.io) — create an index named `documind` (dimension: `1536`, metric: `cosine`)
- [Supabase project](https://supabase.com) — free tier works fine

### 1. Clone & install

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

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
OPENAI_API_KEY=sk-...
PINECONE_API_KEY=pcsk_...
PINECONE_INDEX_NAME=documind
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_ANON_KEY=eyJ...
SUPABASE_SERVICE_ROLE_KEY=eyJ...
```

### 3. Set up Supabase

**Storage bucket** — created automatically on first startup, or run:
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

**Terminal 1 — FastAPI backend:**
```bash
python -m uvicorn src.api.main:app --reload --port 8000
```

**Terminal 2 — Streamlit frontend:**
```bash
streamlit run frontend/app.py
```

Open **http://localhost:8501** in your browser.

---

## API Reference

Base URL: `http://localhost:8000`

### Auth

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/auth/signup` | Create account (`email`, `password`) |
| `POST` | `/api/auth/login` | Sign in → returns `access_token` |
| `POST` | `/api/auth/logout` | Invalidate session |
| `GET` | `/api/auth/me` | Get current user info |

### Documents

> All endpoints require `Authorization: Bearer <token>`

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/documents/upload` | Upload file (multipart) → Storage + Pinecone |
| `GET` | `/api/documents/` | List user's uploaded documents |
| `DELETE` | `/api/documents/{filename}` | Delete from Storage + Pinecone |

### Chat

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/chat/query` | Blocking Q&A → `{answer, sources, ...}` |
| `POST` | `/api/chat/query/stream` | Streaming Q&A (SSE) → token-by-token |

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
      "content": "..."
    }
  ],
  "rewritten_query": "What are the key findings?",
  "num_sources_used": 3,
  "namespace": "eb332ef7-..."
}
```

Interactive docs: **http://localhost:8000/docs**

---

## Configuration Reference

All settings live in `src/components/config.py` and are overridable via `.env`:

| Setting | Default | Description |
|---|---|---|
| `EMBEDDING_MODEL_NAME` | `text-embedding-3-small` | OpenAI embedding model |
| `LLM_MODEL_NAME` | `gpt-4o-mini` | OpenAI chat model |
| `CHUNK_SIZE` | `3000` | Max chars per chunk |
| `CHUNK_OVERLAP` | `500` | Overlap between chunks |
| `TOP_K` | `5` | Chunks retrieved per query |
| `SIMILARITY_THRESHOLD` | `0.30` | Min cosine score to keep |
| `LLM_TEMPERATURE` | `0.1` | LLM creativity (lower = more factual) |
| `EMBEDDING_BATCH_SIZE` | `100` | Vectors per Pinecone upsert batch |
| `API_PORT` | `8000` | FastAPI server port |

---

## How It Works

### Ingestion

1. File is uploaded via API → saved to Supabase Storage
2. `unstructured` parses the document into elements (text, tables, images)
3. Elements are chunked with semantic boundaries and overlap
4. Each chunk becomes a LangChain `Document` with metadata (`filename`, `page_number`, `chunk_type`)
5. SHA-256 deduplication removes identical chunks
6. OpenAI embeds each chunk → 1536-dim vector
7. Vectors are batch-upserted to Pinecone under the user's namespace

### Querying

1. If chat history exists → LLM rewrites the query to be standalone
2. Rewritten query is embedded and searched in Pinecone (cosine similarity)
3. Top-K results above the similarity threshold are retrieved
4. Retrieved chunks are formatted with source labels
5. LLM generates a grounded answer with inline citations
6. Response streams token-by-token via SSE

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
