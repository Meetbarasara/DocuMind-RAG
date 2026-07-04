# Deploying the KYC Compliance Assistant

The stack is two containers, wired by `docker-compose.yml`:

| Service    | Image                          | Port | What it is                          |
|------------|--------------------------------|------|-------------------------------------|
| `api`      | `./Dockerfile` (Python 3.13)   | 8000 | FastAPI backend (the gap-analysis engine, auth, uploads) |
| `frontend` | `./frontend-next/Dockerfile`   | 3000 | Next.js UI (`output: "standalone"`) |

> The legacy Streamlit UI (`frontend/`) is no longer part of the deployment — the Next.js app (`frontend-next/`) is the product. Streamlit still runs locally (`streamlit run frontend/app.py`) until it's fully retired.

---

## 1. Prerequisites

- **Docker** + Docker Compose.
- A **`.env`** at the repo root (copy `.env.example`) with the backend secrets:
  `GROQ_API_KEY`, `PINECONE_API_KEY`, `PINECONE_INDEX_NAME`, `SUPABASE_URL`,
  `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, and — for compliance —
  `CEREBRAS_API_KEY` (the judge). Optional: `COHERE_API_KEY` (rerank),
  `REDIS_URL` (cache + shared rate limiting), `LANGSMITH_*` (tracing).
- The **Supabase migration** applied once (`supabase_migration.sql` — idempotent).
- At least one **seeded regulation** so a check has something to run against:
  `python -m scripts.seed_regulation --pdf <circular.pdf> --name "<name>"`.

## 2. Quick start (local / self-host)

```bash
docker compose up --build
```

- UI:  http://localhost:3000
- API: http://localhost:8000  (health: `/health`)

The `frontend` waits for the `api` health check before starting. Rebuild after code changes with `--build`.

## 3. Configuration that matters

- **`NEXT_PUBLIC_API_BASE` is baked at BUILD time.** Next inlines `NEXT_PUBLIC_*`
  into the client bundle, and that code runs in the **browser** — so it must be a
  URL the browser can reach. It defaults to `http://localhost:8000` (correct for
  local compose, where the api is published on the host). For a real deployment,
  set it to your public API URL:

  ```bash
  NEXT_PUBLIC_API_BASE=https://api.yourdomain.com docker compose up --build
  ```

  (It is a compose `build.arg`, not a runtime env var — changing it needs a rebuild.)
- **CORS.** The api must allow the UI's origin. Set `CORS_ORIGINS` in `.env` to
  include it, e.g. `CORS_ORIGINS=http://localhost:3000` (comma-separated for more).
- **Demo mode.** The UI defaults to a demo gap table that renders without a login,
  so the hero works instantly even before the backend is reachable. "Live" mode
  needs a signed-in account + a seeded regulation.

## 4. Workers & scaling

The api runs a **single uvicorn worker** by default. Two pieces of state are
in-process and assume that:

- The **background upload-job tracker** (`documents.py`) is an in-process dict — a
  job started on one worker isn't pollable from another. This has **no shared-store
  fallback**, so uploads require a single worker (or sticky sessions).
- The **semantic cache** and **rate limiter** are in-process **unless `REDIS_URL`
  is set** — then both use Redis and are safe across workers. Without Redis, each
  worker keeps its own cache + rate-limit counters (the limit is N× looser under
  N workers). The rate limiter fails open to in-memory if Redis can't initialise.

**Recommendation:** deploy with 1 worker (the default). To scale out, set
`REDIS_URL` **and** move upload-job tracking to a shared store first.

## 5. Resource & free-tier caveats

- **Embeddings run locally** (`sentence-transformers/all-mpnet-base-v2`, ~420 MB
  model + torch) — budget **~1–2 GB RAM**. The smallest free tiers (256–512 MB)
  won't fit; a host with ≥1 GB (e.g. Hugging Face Spaces — 16 GB free — or a small
  Fly/Render/Railway instance) will.
- **A live compliance check is slow on the free Cerebras tier** (rate-limit
  backoff — minutes for a large circular). Results are **persisted**, so re-opening
  a check is instant; demo mode never waits on the judge. For a snappier live demo,
  point `JUDGE_MODEL` at a faster model or a paid tier.
- **Groq free tier** is ~100k tokens/day (a few dozen answers) — demo-only.

## 6. Hosting pointers

- **Hugging Face Spaces** (Docker, 16 GB RAM free) — the strongest free fit for the
  api (mpnet fits comfortably).
- **Fly.io / Render / Railway** — small paid/allowance instances fit the api; each
  has a Next.js guide for the frontend.
- **Vercel** can host the Next.js frontend directly (set `NEXT_PUBLIC_API_BASE` to
  your separately-hosted api).

## 7. Verifying a deployment

- `GET /health` on the api returns `{"status": "ok"}` (or 200).
- The UI at `:3000` renders the demo gap table on load (proves the client bundle +
  `NEXT_PUBLIC_API_BASE` are wired).
- Sign in → upload a policy → pick a seeded regulation → **Run check** streams a
  cited gap table (proves the api, Cerebras judge, Pinecone, and Supabase are all
  reachable end-to-end).
