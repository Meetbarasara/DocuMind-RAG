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

    # ── Chunking parameters (Q1: token-based, not character-based) ─────
    # LLMs read tokens, not characters, so we split on token boundaries for
    # predictable context size and cost (~512 tokens, 64 overlap).
    CHUNK_SIZE_TOKENS: int = 512
    CHUNK_OVERLAP_TOKENS: int = 64

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

    # ── Embedding parameters ──────────────────────────────────────────
    EMBEDDING_BATCH_SIZE: int = 100

    # ── RAG Quality Feature Flags ─────────────────────────────────────
    # Feature A: Pinecone *native* hybrid (dense + sparse, fused server-side; L1).
    # Default OFF — it needs a dotproduct index (cosine indexes reject sparse
    # vectors). Set USE_HYBRID_SEARCH=true once you've created a dotproduct index
    # and re-ingested. HYBRID_ALPHA weights dense vs sparse (1.0 = dense only).
    USE_HYBRID_SEARCH: bool = os.getenv("USE_HYBRID_SEARCH", "false").strip().lower() == "true"
    HYBRID_ALPHA: float = float(os.getenv("HYBRID_ALPHA", "0.5"))

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
    # C2 semantic cache: serve a near-identical past question (cosine on the
    # query embedding) without retrieval/LLM. Scoped per namespace + filter.
    USE_SEMANTIC_CACHE: bool = True
    SEMANTIC_CACHE_THRESHOLD: float = 0.95   # cosine to treat two questions as the same
    SEMANTIC_CACHE_MAX: int = 25             # recent (embedding, answer) entries kept

    # ── Multimodal image answering — B-hybrid (PDF page snapshots) ─────
    # Render PDF pages that contain images/tables to a snapshot at ingest;
    # at answer time hand the relevant page(s) to the multimodal LLM so it
    # reads tables/charts/figures in place. PDF only (DOCX can't be
    # page-rendered with lightweight tools). Off => text-only, no snapshots.
    USE_IMAGE_ANSWERING: bool = True
    PAGE_IMAGE_DPI: int = 130                # legible text, smaller than 300
    MAX_PAGE_IMAGES_PER_ANSWER: int = 2      # cap vision tokens per answer

    # ── File handling ─────────────────────────────────────────────────
    # Temp directory for files downloaded from Supabase during processing
    UPLOAD_DIR: str = os.path.join(_PROJECT_ROOT, "tmp_uploads")
    # B1/B2: lightweight parsers (PyMuPDF + python-docx) cover these three;
    # images inside PDFs/DOCX are extracted for the multimodal step.
    SUPPORTED_FILE_TYPES: tuple = ("pdf", "docx", "txt")
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

