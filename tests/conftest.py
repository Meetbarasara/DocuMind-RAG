"""conftest.py — shared pytest fixtures.

(The old unstructured-partition module stubs are gone: ingestion now uses
PyMuPDF + python-docx, which import fast and offline, so there's nothing heavy
or network-dependent to stub before importing src.* anymore.)
"""

import os

# Hermetic tests: don't let a developer's real .env leak live secrets in.
# config.py calls load_dotenv() at import, and from a git worktree python-dotenv
# walks up and finds the *parent checkout's* real .env. The shell-provided fake
# keys (OPENAI/PINECONE/SUPABASE) override it, but anything NOT passed on the test
# command leaks through — COHERE_API_KEY made the rerank-fallback test hit the
# live Cohere API (returning real rankings instead of the no-client fallback),
# LANGSMITH_* would emit real traces, and REDIS_URL would point cache tests at a
# real server. Pin these to inert values *before* config is imported (conftest
# runs first); setdefault so an explicit command-line value still wins for an
# intentional live run.
for _var, _val in (
    ("COHERE_API_KEY", ""),
    ("LANGSMITH_API_KEY", ""),
    ("LANGSMITH_TRACING", "false"),
    ("REDIS_URL", ""),
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
