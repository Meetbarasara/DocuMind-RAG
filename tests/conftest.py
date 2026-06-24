"""conftest.py — stub heavy, irrelevant deps before any src.* import.

The chat-route tests only exercise async control flow (BUG-1), not document
parsing. But importing src.pipeline.pipeline transitively imports
src.components.ingestion, which eagerly imports unstructured's partition
modules — slow, and on a sandboxed/offline machine some of them hang trying
to reach the network for NLTK data. Stub them so these tests stay fast and
hermetic regardless of network access.
"""

import sys
import types

import pytest

_UNSTRUCTURED_STUBS = {
    "unstructured.chunking.title": ["chunk_by_title"],
    "unstructured.partition.csv": ["partition_csv"],
    "unstructured.partition.docx": ["partition_docx"],
    "unstructured.partition.email": ["partition_email"],
    "unstructured.partition.html": ["partition_html"],
    "unstructured.partition.json": ["partition_json"],
    "unstructured.partition.pdf": ["partition_pdf"],
    "unstructured.partition.pptx": ["partition_pptx"],
    "unstructured.partition.text": ["partition_text"],
    "unstructured.partition.xlsx": ["partition_xlsx"],
    "unstructured.partition.xml": ["partition_xml"],
}

if "unstructured.partition.pdf" not in sys.modules:
    for dotted, names in _UNSTRUCTURED_STUBS.items():
        parts = dotted.split(".")
        for i in range(1, len(parts)):
            pkg = ".".join(parts[:i])
            sys.modules.setdefault(pkg, types.ModuleType(pkg))

        mod = types.ModuleType(dotted)
        for name in names:
            setattr(mod, name, lambda *a, **k: [])
        sys.modules[dotted] = mod


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
