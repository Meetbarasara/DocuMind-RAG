# Scaling plan — from 1 replica to many

How this app goes from a single container to many, to serve more concurrent
users and process more PDFs in parallel. Written against the real code, with the
honest blockers called out.

## Where we are today

The app is **correct and safe on a single replica**, and deliberately so. Three
pieces of state decide whether it can run on *many* replicas:

| State | Where | Multi-replica safe today? |
|---|---|---|
| Upload/ingestion **job status** | in-process dict `_upload_jobs` ([documents.py](src/api/router/documents.py)), `_reg_jobs` ([compliance.py](src/api/router/compliance.py)) | ❌ **No** — a job tracked on replica A is a 404 when the poll lands on replica B |
| **Rate-limit** counters | [limiter.py](src/api/limiter.py) | ✅ Yes *when `REDIS_URL` is set* (else per-replica, N× looser) |
| **Query cache** (exact + semantic) | [cache.py](src/components/cache.py) | ✅ Yes *when `REDIS_URL` is set* (fail-open, per-user keys) |

So two of three are already shared-store-ready — **the job tracker is the one
code change that gates horizontal scaling.**

> On Azure Container Apps you scale by **replicas** (containers), autoscaled on
> HTTP concurrency / CPU / queue depth — not by `uvicorn --workers`. Same state
> problem, same fix; "8 workers" ≈ `--max-replicas 8` with a scale rule.

---

## Phase 1 — make it replica-safe (the real unlock)

The minimum to run N API replicas correctly.

1. **Move job status to a shared store.** Replace the in-process `_upload_jobs` /
   `_reg_jobs` dicts with a small `upload_jobs` table in Supabase (or a Redis
   hash keyed by `job_id`, TTL'd). The upload endpoint writes the job row; the
   background task updates it; `/upload-status/{id}` reads it — so *any* replica
   can answer the poll. Keep the exact 202 + poll contract the frontend already
   uses (no UI change, and the E2E upload tests still cover it).
2. **Turn on Redis** (`REDIS_URL` — you already have Upstash). Instantly makes
   the cache and rate limiter correct across replicas; no code change.
3. **Bump the replica ceiling + a scale rule** (see the ACA config below).

After Phase 1 the app scales horizontally: uploads, checks, rate limits and
cache all behave correctly no matter which replica a request hits.

**Effort:** small–medium. One shared-store module + swapping the two dict call
sites. The upload flow is already covered by the Playwright E2E, so a regression
would be caught immediately.

---

## Phase 2 — dedicated "PDF workers" (decouple ingestion)

Today an upload runs parse → embed → upsert **inside the API process** via
FastAPI `BackgroundTasks`. That's fine at low volume but has three limits at
scale: heavy PDF/embedding CPU competes with request-serving on the same replica;
a job dies if the replica is scaled down mid-ingestion (BackgroundTasks isn't
durable); and there's no retry.

The scale-out pattern — the "PDF workers" you're picturing:

```
  upload  ─►  API replica  ─►  enqueue {job_id, file ref}  ─►  [ Queue ]
   (fast 202, returns immediately)                                 │
                                                                   ▼
                                    ┌──────────  PDF-worker replicas  ──────────┐
                                    │  dequeue → parse → chunk → embed → upsert  │
                                    │  → update job row in the shared store       │
                                    └─────────────────────────────────────────────┘
                              (KEDA autoscales these 0 → N by queue depth)
```

- **Queue:** Azure Storage Queue or Service Bus (cheap, managed). The API just
  drops a message and returns 202.
- **Workers:** a *second* container image (reusing `src/`) run as an **Azure
  Container Apps Job** or a worker app, **KEDA-autoscaled on queue length** —
  scale to zero when the backlog is empty, burst to N when PDFs pile up. This is
  the elegant, event-driven part and a strong interview talking point.
- **Durability + retries:** a message stays on the queue until a worker acks it,
  so a killed worker just means another picks the PDF up. Poison-message handling
  after K retries.

**Effort:** medium–large (new worker entrypoint, queue client, KEDA scale rule).
Worth it when ingestion volume actually justifies decoupling — not before.

---

## The embedding-model RAM reality

Every API/worker replica loads its own `all-mpnet` model (~1–1.5 GB resident).
So replicas are **memory-heavy**, which caps how many you run cheaply:

- **8 always-on replicas ≈ 8–12 GB RAM ≈ $300+/mo** on ACA — not worth it for a
  demo. **Autoscale 1→8 on demand instead** (floor 1–2, burst under load).
- **True horizontal scale** eventually means **not duplicating the model per
  replica**: move embeddings to a hosted endpoint (or one shared embedding
  service), so API/worker replicas become lightweight and stateless and you can
  run many cheaply. That's the real "max users" move — at the cost of the
  "everything runs locally / free" property.

---

## Azure Container Apps autoscaling config

Once Phase 1 is in, scaling is a config change, not a redeploy:

```bash
# API: sit at 2 warm replicas, burst to 8 when each is handling ~20 concurrent
# HTTP requests. (Needs Phase 1 — shared job store + REDIS_URL — to be correct.)
az containerapp update -n documind-api -g documind-rg \
  --min-replicas 2 --max-replicas 8 \
  --scale-rule-name http-concurrency \
  --scale-rule-type http \
  --scale-rule-http-concurrency 20

# Phase 2 PDF workers (illustrative): scale 0 → N by Azure Storage Queue depth.
# az containerapp create -n documind-pdf-worker ... \
#   --min-replicas 0 --max-replicas 8 \
#   --scale-rule-name queue --scale-rule-type azure-queue \
#   --scale-rule-metadata queueName=ingest queueLength=5 ...
```

---

## Recommendation for this project

1. **Do Phase 1** (shared job store + `REDIS_URL`) — it's the genuine "handle
   more users" unlock, a bounded change, and already guarded by the E2E upload
   tests. After it, set `--min-replicas 2 --max-replicas 8` with the HTTP scale
   rule and you *actually* scale.
2. **Design, don't yet build, Phase 2** — the queue + KEDA PDF workers are the
   right answer at real ingestion volume and a great architecture story, but
   they're infrastructure you don't need for a demo. This doc is that design.
3. **Keep the model local for now** (per-replica RAM is fine at 1–8 replicas);
   note the "offload embeddings to scale past that" path for the interview.
