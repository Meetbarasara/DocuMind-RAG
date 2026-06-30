import os
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode
from typing_extensions import Annotated

load_dotenv()

# Project root = two levels up from this file (src/components/config.py → project root)
_PROJECT_ROOT = str(Path(__file__).parent.parent.parent)

# Part C / A10: secrets the app cannot function without. Validated at startup
# (Config() construction, which happens at import of src.api.main) so a
# misconfigured deployment fails immediately with a clear error instead of
# surfacing as a confusing provider error on the first real request.
_REQUIRED_SECRETS = (
    "GOOGLE_API_KEY",
    "PINECONE_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
)


class Config(BaseSettings):
    """Centralized configuration for the DocuMind RAG pipeline.

    Part C / A10: a `pydantic-settings` BaseSettings (was a plain dataclass).
    Every field below is automatically overridable by an env var of the same
    name (read fresh on each Config() construction, including by .env via the
    load_dotenv() above) — so the required secrets actually fail fast, and
    every tunable knob is genuinely env-driven, not just the few that used to
    have a manual os.getenv() call.
    """

    # ── Model settings (Google Gemini) ────────────────────────────────
    # Gemini embeddings are 768-dim (text-embedding-004), NOT 1536 like the old
    # OpenAI text-embedding-3-small. The Pinecone index dimension is fixed at
    # creation, so switching providers requires a 768-dim index + re-ingest.
    EMBEDDING_MODEL_NAME: str = "models/text-embedding-004"
    LLM_MODEL_NAME: str = "gemini-2.0-flash"

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

    # ── Embedding parameters ──────────────────────────────────────────
    EMBEDDING_BATCH_SIZE: int = 100

    # ── RAG Quality Feature Flags ─────────────────────────────────────
    # Feature A: Pinecone *native* hybrid (dense + sparse, fused server-side; L1).
    # Default OFF — it needs a dotproduct index (cosine indexes reject sparse
    # vectors). Set USE_HYBRID_SEARCH=true once you've created a dotproduct index
    # and re-ingested. HYBRID_ALPHA weights dense vs sparse (1.0 = dense only).
    USE_HYBRID_SEARCH: bool = False
    HYBRID_ALPHA: float = 0.5

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
    # Required (Part C / A10) — Config() raises at construction if any of
    # these are missing or blank. See _validate_required_secrets below.
    GOOGLE_API_KEY: str
    PINECONE_API_KEY: str
    SUPABASE_URL: str
    SUPABASE_ANON_KEY: str
    SUPABASE_SERVICE_ROLE_KEY: str

    # Optional — the rest of the codebase already degrades gracefully when
    # these are absent (Cohere rerank skips to retrieval order, LangSmith
    # tracing is just off), so they stay optional rather than fail-fast.
    COHERE_API_KEY: Optional[str] = None
    LANGSMITH_API_KEY: Optional[str] = None

    SUPABASE_STORAGE_BUCKET: str = "documents"

    PINECONE_INDEX_NAME: str = "documind"
    PINECONE_NAMESPACE: str = ""             # must be set per-user at runtime

    # ── Observability: LangSmith (O1) ─────────────────────────────────
    # LangChain auto-traces every chain (rewrite/multi-query/generate) to
    # LangSmith when LANGSMITH_TRACING is true and LANGSMITH_API_KEY is set —
    # langchain-core reads these straight from the environment, and the
    # load_dotenv() at the top of this module puts any .env values there.
    # Default OFF so no trace data ever leaves the process unless opted in.
    LANGSMITH_TRACING: bool = False
    LANGSMITH_PROJECT: str = "documind"

    # ── Caching: Redis query cache (C1) ───────────────────────────────
    # Exact-match, per-namespace cache in front of the pipeline. Empty
    # REDIS_URL => cache disabled (fail-open no-op), so the app runs
    # unchanged until Redis is configured. Invalidated on every
    # ingest/delete so a user never gets a stale answer (C3).
    REDIS_URL: str = ""
    CACHE_TTL_SECONDS: int = 3600
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
    # NoDecode: CORS_ORIGINS is a plain comma-separated string in the env
    # (not JSON, which is pydantic-settings' default for list-typed fields),
    # so the validator below does the splitting itself.
    CORS_ORIGINS: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:8501"]
    )

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_cors_origins(cls, v):
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @field_validator(*_REQUIRED_SECRETS)
    @classmethod
    def _required_secret_not_blank(cls, v: str, info) -> str:
        if not v or not v.strip():
            raise ValueError(
                f"{info.field_name} is required — set it in your environment or .env file"
            )
        return v
