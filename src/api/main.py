"""main.py — FastAPI application entry point for DocuMind.

Starts the API server with:
    - CORS middleware (allows Streamlit frontend)
    - Request-level logging middleware
    - Auth, Documents, and Chat routers
    - Health-check endpoint
"""

import time
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.dependencies import get_config
from src.api.router import auth, chat, documents, evaluate
from src.logger import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
#  Lifespan
# ──────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown logic."""
    logger.info("DocuMind API starting up…")
    yield
    logger.info("DocuMind API shutting down.")


# ──────────────────────────────────────────────────────────────────────────────
#  Application
# ──────────────────────────────────────────────────────────────────────────────

config = get_config()

app = FastAPI(
    title="DocuMind RAG API",
    description="Document intelligence powered by Pinecone + OpenAI + Supabase",
    version="1.0.0",
    lifespan=lifespan,
)

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
app.include_router(evaluate.router)


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
