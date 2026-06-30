"""main.py — FastAPI application entry point for DocuMind.

Starts the API server with:
    - CORS middleware (allows Streamlit frontend)
    - Request-level logging middleware
    - SlowAPI rate limiting (protects Pinecone + Gemini quota)
    - Auth, Documents, Chat, and Evaluate routers
    - Health-check endpoint
"""

import time
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.dependencies import get_config, get_pipeline
from src.api.router import auth, chat, conversations, documents
from src.logger import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
#  Rate Limiting (SEC-7 fix)
#  The Limiter previously lived here but nothing ever decorated a route with
#  it — main.py imports the routers, so the routers couldn't import the
#  limiter back out of main.py without a circular import. It now lives in
#  src/api/limiter.py, and auth.py/documents.py/chat.py apply
#  @limiter.limit(...) directly on signup/login/upload/query.
# ──────────────────────────────────────────────────────────────────────────────

try:
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    from src.api.limiter import limiter

    _rate_limiting_available = True
    logger.info("SlowAPI rate limiter initialised (60 req/min default)")
except ImportError:
    limiter = None
    _rate_limiting_available = False
    logger.warning("slowapi not installed — rate limiting disabled")


# ──────────────────────────────────────────────────────────────────────────────
#  Lifespan
# ──────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks.

    L2: reranking moved to Cohere's hosted Rerank API, so there is no local
    cross-encoder model to pre-warm anymore — the previous warmup block (and
    the sentence-transformers/torch dependency it loaded) was removed.
    """
    logger.info("DocuMind API starting up…")
    pipeline = get_pipeline()

    # O1: surface whether LangSmith tracing is on so it's obvious in the logs
    # which runs will (or won't) show up in the dashboard.
    if pipeline.config.LANGSMITH_TRACING:
        logger.info("LangSmith tracing ENABLED (project=%s)", pipeline.config.LANGSMITH_PROJECT)
    else:
        logger.info("LangSmith tracing disabled (set LANGSMITH_TRACING=true + LANGSMITH_API_KEY to enable)")

    yield
    logger.info("DocuMind API shutting down.")


# ──────────────────────────────────────────────────────────────────────────────
#  Application
# ──────────────────────────────────────────────────────────────────────────────

config = get_config()

app = FastAPI(
    title="DocuMind RAG API",
    description="Document intelligence powered by Pinecone + Gemini + Supabase",
    version="1.0.0",
    lifespan=lifespan,
)

# ── Rate limiting middleware (Bug 6 fix) ─────────────────────────────────────
if _rate_limiting_available:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS ─────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request logging middleware ────────────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = str(uuid.uuid4())[:8]
    start = time.perf_counter()
    logger.info("[%s] → %s %s", request_id, request.method, request.url.path)

    try:
        response = await call_next(request)
    except Exception as exc:
        logger.exception("[%s] Unhandled exception: %s", request_id, exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "[%s] ← %s %s | %d | %.1fms",
        request_id, request.method, request.url.path, response.status_code, elapsed_ms,
    )
    return response


# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(chat.router)
app.include_router(conversations.router)


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health", tags=["system"])
async def health():
    """Return a simple liveness probe."""
    return {"status": "ok", "service": "DocuMind RAG API"}


# ──────────────────────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "src.api.main:app",
        host=config.API_HOST,
        port=config.API_PORT,
        reload=True,
        log_level="info",
    )
