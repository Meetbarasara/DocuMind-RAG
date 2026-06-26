import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

load_dotenv()

# Project root = two levels up from this file (src/components/config.py → project root)
_PROJECT_ROOT = str(Path(__file__).parent.parent.parent)


@dataclass
class Config:
    """Centralized configuration for the DocuMind RAG pipeline."""

    # ── Model settings ────────────────────────────────────────────────
    EMBEDDING_MODEL_NAME: str = "text-embedding-3-small"
    LLM_MODEL_NAME: str = "gpt-4o-mini"

    # ── Chunking parameters ───────────────────────────────────────────
    CHUNK_SIZE: int = 3000
    NEW_AFTER_N_CHARS: int = 2400
    COMBINE_TEXT_UNDER_N_CHARS: int = 500
    CHUNK_OVERLAP: int = 500

    # ── Retrieval parameters ──────────────────────────────────────────
    TOP_K: int = 5
    SIMILARITY_THRESHOLD: float = 0.50
    # Latency Optimization #6: bounds RAGPipeline's per-namespace
    # RetrievalManager cache (LRU-evicted) so it can't grow unbounded as
    # distinct users/namespaces accumulate over the process's lifetime.
    MAX_CACHED_RETRIEVAL_MANAGERS: int = 100

    # ── Generation parameters ─────────────────────────────────────────
    LLM_TEMPERATURE: float = 0.1
    LLM_MAX_TOKENS: int = 2048
    STREAMING: bool = True

    # ── PDF parsing strategy ──────────────────────────────────────────
    # "fast"   = pdfminer text extraction (~2-5s per PDF, text-only)
    # "hi_res" = ML layout detection (~120-200s on CPU, extracts tables+images)
    PDF_PARSE_STRATEGY: str = os.getenv("PDF_PARSE_STRATEGY", "fast")

    # ── Embedding parameters ──────────────────────────────────────────
    EMBEDDING_BATCH_SIZE: int = 100

    # ── RAG Quality Feature Flags ─────────────────────────────────────
    # Feature A: Hybrid Search — combine BM25 keyword + dense vector search
    USE_HYBRID_SEARCH: bool = True
    HYBRID_SEARCH_WEIGHT: float = 0.5       # 0 = dense only, 1 = BM25 only

    # Feature B: Re-ranking via Cohere Rerank API (hosted; L2)
    # L2: replaced the local sentence-transformers cross-encoder (heavy CPU +
    # ~1GB torch) with Cohere's hosted Rerank API. Needs COHERE_API_KEY; when
    # absent, reranking degrades gracefully to retrieval order (see retrieval.py).
    USE_RERANKING: bool = True
    COHERE_RERANK_MODEL: str = "rerank-v3.5"
    RERANKER_TOP_K: int = 3                 # keep top N after re-ranking

    # Feature C: Multi-Query Retrieval
    # L3: OFF by default — it adds an LLM round-trip + N extra retrievals to the
    # hot path for marginal recall now that Cohere reranking handles precision.
    # Flip on for a high-recall mode when latency is less critical.
    USE_MULTI_QUERY: bool = False
    MULTI_QUERY_COUNT: int = 3              # variants generated when enabled

    # Feature D: Citation Verification (post-generation)
    USE_CITATION_VERIFICATION: bool = True

    # Feature E: Chunk Overlap Deduplication at Retrieval
    USE_CHUNK_DEDUP: bool = True
    CHUNK_DEDUP_THRESHOLD: float = 0.85     # Jaccard similarity threshold

    # Feature F: Conversation Memory Summarization
    USE_MEMORY_SUMMARIZATION: bool = True
    MEMORY_SUMMARIZATION_WINDOW: int = 6    # summarize after this many messages

    # ── API keys & services ───────────────────────────────────────────
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY")
    # L2: hosted reranking. Optional — without it, reranking is skipped.
    COHERE_API_KEY: str = os.getenv("COHERE_API_KEY")

    SUPABASE_URL: str = os.getenv("SUPABASE_URL")
    SUPABASE_ANON_KEY: str = os.getenv("SUPABASE_ANON_KEY")
    SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    SUPABASE_STORAGE_BUCKET: str = "documents"

    PINECONE_API_KEY: str = os.getenv("PINECONE_API_KEY")
    PINECONE_INDEX_NAME: str = os.getenv("PINECONE_INDEX_NAME", "documind")
    PINECONE_NAMESPACE: str = ""             # must be set per-user at runtime

    # ── Observability: LangSmith (O1) ─────────────────────────────────
    # LangChain auto-traces every chain (rewrite/multi-query/generate) to
    # LangSmith when LANGSMITH_TRACING is true and LANGSMITH_API_KEY is set —
    # langchain-core reads these straight from the environment, and the
    # load_dotenv() at the top of this module puts any .env values there.
    # Default OFF so no trace data ever leaves the process unless opted in.
    LANGSMITH_TRACING: bool = os.getenv("LANGSMITH_TRACING", "false").strip().lower() == "true"
    LANGSMITH_PROJECT: str = os.getenv("LANGSMITH_PROJECT", "documind")
    LANGSMITH_API_KEY: str = os.getenv("LANGSMITH_API_KEY")

    # ── Caching: Redis query cache (C1) ───────────────────────────────
    # Exact-match, per-namespace cache in front of the pipeline. Empty
    # REDIS_URL => cache disabled (fail-open no-op), so the app runs
    # unchanged until Redis is configured. Invalidated on every
    # ingest/delete so a user never gets a stale answer (C3).
    REDIS_URL: str = os.getenv("REDIS_URL", "")
    CACHE_TTL_SECONDS: int = int(os.getenv("CACHE_TTL_SECONDS", "3600"))

    # ── File handling ─────────────────────────────────────────────────
    # Temp directory for files downloaded from Supabase during processing
    UPLOAD_DIR: str = os.path.join(_PROJECT_ROOT, "tmp_uploads")
    SUPPORTED_FILE_TYPES: tuple = (
        "pdf", "docx", "pptx", "txt", "xlsx", "csv", "html",
    )
    # SEC-6: cap upload size so a single request can't exhaust memory/CPU.
    MAX_UPLOAD_SIZE_BYTES: int = 50 * 1024 * 1024  # 50MB

    # ── API server settings ───────────────────────────────────────────
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    # BUG-9 fix: was a hardcoded localhost-only list — env-driven now so a
    # deployed frontend's real origin can be added without editing source.
    CORS_ORIGINS: List[str] = field(
        default_factory=lambda: [
            origin.strip()
            for origin in os.getenv("CORS_ORIGINS", "http://localhost:8501").split(",")
            if origin.strip()
        ]
    )

