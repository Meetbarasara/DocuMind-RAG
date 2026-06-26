"""conftest.py — shared pytest fixtures.

(The old unstructured-partition module stubs are gone: ingestion now uses
PyMuPDF + python-docx, which import fast and offline, so there's nothing heavy
or network-dependent to stub before importing src.* anymore.)
"""

import pytest


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
