"""Part C / A10: pydantic-settings + fail-fast secret validation.

Two characterizations of the gap this closes:
  1. A missing or blank required secret (GROQ/PINECONE/SUPABASE) must raise at
     Config() construction time -- not silently become None/"" and fail later,
     deep inside a request, with a confusing provider error.
  2. RetrievalManager / EmbeddingManager must not mutate the global os.environ
     to hand Pinecone its key (a surprising constructor side effect) -- the key
     is passed directly to the client instead.

Optional secrets (COHERE_API_KEY, LANGSMITH_API_KEY) are deliberately NOT in
this set -- the rest of the codebase already degrades gracefully when they're
absent (Cohere rerank skips to retrieval order, LangSmith tracing is just off).
"""

import os

import pytest
from pydantic import ValidationError

import src.components.retrieval as retrieval_module
from src.components.config import Config
from src.components.embeddings import EmbeddingManager
from src.components.retrieval import RetrievalManager


class _RecordingVectorStore:
    """Stands in for PineconeVectorStore -- no real network calls, and records
    the kwargs it was built with so we can assert the API key was passed
    directly (A10) rather than picked up from a mutated os.environ."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        type(self).captured = kwargs

_REQUIRED = dict(
    GROQ_API_KEY="gsk-fake",
    PINECONE_API_KEY="fake",
    SUPABASE_URL="https://fake.supabase.co",
    SUPABASE_ANON_KEY="fake",
    SUPABASE_SERVICE_ROLE_KEY="fake",
)


@pytest.mark.parametrize("missing", list(_REQUIRED))
def test_missing_required_secret_fails_fast(missing):
    kwargs = {**_REQUIRED, missing: None}
    with pytest.raises(ValidationError):
        Config(**kwargs)


@pytest.mark.parametrize("blank", list(_REQUIRED))
def test_blank_required_secret_fails_fast(blank):
    """An empty/whitespace value (e.g. `GROQ_API_KEY=` in a .env) is just as
    broken as a missing one and must be rejected the same way."""
    kwargs = {**_REQUIRED, blank: "   "}
    with pytest.raises(ValidationError):
        Config(**kwargs)


def test_all_required_secrets_present_constructs_cleanly():
    cfg = Config(**_REQUIRED)
    assert cfg.GROQ_API_KEY == "gsk-fake"


def test_optional_secrets_stay_optional():
    cfg = Config(**_REQUIRED, COHERE_API_KEY=None, LANGSMITH_API_KEY=None)
    assert cfg.COHERE_API_KEY is None
    assert cfg.LANGSMITH_API_KEY is None


def test_retrieval_manager_does_not_mutate_global_environ(monkeypatch):
    monkeypatch.setattr(retrieval_module, "PineconeVectorStore", _RecordingVectorStore)
    monkeypatch.delenv("PINECONE_API_KEY", raising=False)
    cfg = Config(**{**_REQUIRED, "PINECONE_API_KEY": "explicit-key-not-in-environ"})

    RetrievalManager(cfg)

    assert "PINECONE_API_KEY" not in os.environ
    assert _RecordingVectorStore.captured.get("pinecone_api_key") == "explicit-key-not-in-environ"


def test_embedding_manager_does_not_mutate_global_environ(monkeypatch):
    monkeypatch.delenv("PINECONE_API_KEY", raising=False)
    cfg = Config(**{**_REQUIRED, "PINECONE_API_KEY": "explicit-key-not-in-environ"})

    EmbeddingManager(cfg)

    assert "PINECONE_API_KEY" not in os.environ
