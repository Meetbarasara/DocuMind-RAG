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
    SIMILARITY_THRESHOLD: float = 0.30
    USE_HYBRID_SEARCH: bool = False          # wired up later

    # ── Generation parameters ─────────────────────────────────────────
    LLM_TEMPERATURE: float = 0.1
    LLM_MAX_TOKENS: int = 2048
    STREAMING: bool = True

    # ── Embedding parameters ──────────────────────────────────────────
    EMBEDDING_BATCH_SIZE: int = 100

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
