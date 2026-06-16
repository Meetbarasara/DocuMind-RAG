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

    # Feature B: Contextual Compression / Re-ranking
    USE_RERANKING: bool = True
    RERANKER_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    RERANKER_TOP_K: int = 3                 # keep top N after re-ranking

    # Feature C: Multi-Query Retrieval
    USE_MULTI_QUERY: bool = True
    MULTI_QUERY_COUNT: int = 3              # number of query reformulations

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

    SUPABASE_URL: str = os.getenv("SUPABASE_URL")
    SUPABASE_ANON_KEY: str = os.getenv("SUPABASE_ANON_KEY")
    SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    SUPABASE_STORAGE_BUCKET: str = "documents"

    PINECONE_API_KEY: str = os.getenv("PINECONE_API_KEY")
    PINECONE_INDEX_NAME: str = os.getenv("PINECONE_INDEX_NAME", "documind")
    PINECONE_NAMESPACE: str = ""             # must be set per-user at runtime

    # ── File handling ─────────────────────────────────────────────────
    # Temp directory for files downloaded from Supabase during processing
    UPLOAD_DIR: str = os.path.join(_PROJECT_ROOT, "tmp_uploads")
    SUPPORTED_FILE_TYPES: tuple = (
        "pdf", "docx", "pptx", "txt", "xlsx", "csv", "html",
    )

    # ── API server settings ───────────────────────────────────────────
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    CORS_ORIGINS: List[str] = field(default_factory=lambda: ["http://localhost:8501"])

