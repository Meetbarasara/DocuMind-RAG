"""conftest.py — shared pytest fixtures.

(The old unstructured-partition module stubs are gone: ingestion now uses
PyMuPDF + python-docx, which import fast and offline, so there's nothing heavy
or network-dependent to stub before importing src.* anymore.)
"""

import os

# Hermetic tests: don't let a developer's real .env leak live secrets *or
# feature flags* in. config.py calls load_dotenv() at import, and from a git
# worktree python-dotenv walks up and finds the *parent checkout's* real .env.
# The shell-provided fake keys (OPENAI/PINECONE/SUPABASE) override it, but
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
):
    os.environ.setdefault(_var, _val)

import pytest  # noqa: E402  (must follow the env pinning above)


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
