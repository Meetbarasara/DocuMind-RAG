"""conftest.py — shared pytest fixtures.

(The old unstructured-partition module stubs are gone: ingestion now uses
PyMuPDF + python-docx, which import fast and offline, so there's nothing heavy
or network-dependent to stub before importing src.* anymore.)
"""

import os

# Hermetic tests: don't let a developer's real .env leak live secrets *or
# feature flags* in. config.py calls load_dotenv() at import, and from a git
# worktree python-dotenv walks up and finds the *parent checkout's* real .env.
# The shell-provided fake keys (GROQ/PINECONE/SUPABASE) override it, but
# anything NOT passed on the test command leaks through:
#   - COHERE_API_KEY made the rerank-fallback test hit the live Cohere API,
#   - LANGSMITH_* would emit real traces, REDIS_URL would point at a real server,
#   - USE_HYBRID_SEARCH=true (set once a dev enables native hybrid locally) flips
#     create_vector_store onto the _upsert_hybrid path, which calls embed_documents
#     on test fakes that only stub the dense path → AttributeError. Since Part C's
#     pydantic-settings migration made EVERY config field env-overridable, feature
#     flags leak the same way secrets did, so they need pinning too.
# Pin these to inert values *before* config is imported (conftest runs first);
# setdefault so an explicit command-line value still wins for an intentional live run.
for _var, _val in (
    ("COHERE_API_KEY", ""),
    ("LANGSMITH_API_KEY", ""),
    ("LANGSMITH_TRACING", "false"),
    ("REDIS_URL", ""),
    ("USE_HYBRID_SEARCH", "false"),
    # Judge-model keys leak the same way (a dev's real .env sets CEREBRAS_API_KEY
    # once they configure the compliance judge) — pin them inert so the judge
    # factory's "missing key -> clear error" path stays testable.
    ("CEREBRAS_API_KEY", ""),
    ("OPENROUTER_API_KEY", ""),
):
    os.environ.setdefault(_var, _val)

import pytest  # noqa: E402  (must follow the env pinning above)


class _FakeLocalEmbeddings:
    """Stands in for HuggingFaceEmbeddings so tests never load the real ~420MB
    sentence-transformers model (slow, RAM-heavy, needs a one-time network
    download). Embeddings are now LOCAL, so constructing EmbeddingManager /
    RetrievalManager / RAGPipeline for real would otherwise load the model.
    Tests that care about actual vectors set their own `_embeddings` fake."""

    def __init__(self, *args, **kwargs):
        pass

    def embed_query(self, text):
        return [0.0] * 768

    def embed_documents(self, texts):
        return [[0.0] * 768 for _ in texts]


@pytest.fixture(autouse=True)
def _stub_local_embeddings(monkeypatch):
    """Replace HuggingFaceEmbeddings before any test builds a real manager.
    Both EmbeddingManager and RetrievalManager construct it through
    embeddings.load_local_embeddings, so one patch point covers everything.
    A test may still override with its own fake."""
    import src.components.embeddings as _emb

    monkeypatch.setattr(_emb, "HuggingFaceEmbeddings", _FakeLocalEmbeddings)


@pytest.fixture(autouse=True)
def _reset_rate_limiter_storage():
    """The slowapi Limiter's in-memory storage is a single object shared by
    the whole test session (it lives on the module-level `app`/`limiter`
    singletons, imported once per session). Any test file that fires
    several requests at a rate-limited route (uploads, login, etc.)
    permanently consumes part of that route's quota for every *other* test
    file that runs afterward in the same process -- confirmed empirically:
    adding a new test file with several /api/documents/upload requests
    made later, unrelated upload tests fail with 429 instead of their
    expected status, purely because of run order. Reset before and after
    every test so no test file's rate-limit usage leaks into another's.
    """
    from src.api.limiter import limiter

    limiter._storage.reset()
    yield
    limiter._storage.reset()
