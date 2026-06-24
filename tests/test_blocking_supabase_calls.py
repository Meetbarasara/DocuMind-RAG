"""Regression test for Latency Optimization #7 (see BUGFIXES.md / CODE_REVIEW.md §4).

SupabaseManager's methods (get_current_user, sign_up, sign_in, sign_out,
upload_file, record_upload, get_user_documents, delete_file,
delete_document_record) and RAGPipeline's ingest_file/delete_document are
all plain, synchronous, network-bound calls -- but every one of them was
invoked directly inside an `async def` FastAPI route or dependency with no
`asyncio.to_thread` wrapper. A blocking call inside an async function
doesn't yield control back to the event loop for its entire duration, so
two "concurrent" requests actually serialize -- same bug class as BUG-3,
just in the auth/database layer instead of the LLM layer.

`get_current_user` is the highest-impact instance: it's a dependency that
runs on *every* authenticated request (chat, documents, evaluate, auth/me,
logout), so blocking there serializes the entire API's throughput, not
just one route.

Each test runs two calls/requests concurrently via asyncio.gather and
measures wall time: a blocking implementation forces them to serialize
(~2x one call's delay); a truly async one lets them overlap (~1x).

upload_document and delete_document each make 3 *sequential* blocking
calls per request, not 1 -- a single combined "are all 3 fast enough"
timing test turned out to have a real blind spot (confirmed empirically
while writing this: reverting just one of the three calls back to a bare
blocking call still passed the combined-timing assertion, since 3
sequential steps where only 2 of 3 are non-blocking still lands in a
similar ballpark to all 3 being non-blocking). The parametrized tests
below isolate one call site at a time -- only the call under test
actually sleeps, the other two are instant -- so each call site is
checked independently rather than averaged together.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.dependencies import get_current_user, get_db, get_pipeline
from src.api.main import app
from src.components.config import Config

_DELAY = 0.2

_ALL_DB_METHODS = frozenset({
    "get_current_user", "sign_up", "sign_in", "sign_out",
    "upload_file", "record_upload", "get_user_documents",
    "delete_file", "delete_document_record",
})
_ALL_PIPELINE_METHODS = frozenset({"ingest_file", "delete_document"})

_UPLOAD_SEQUENTIAL_CALLS = ("upload_file", "ingest_file", "record_upload")
_DELETE_SEQUENTIAL_CALLS = ("delete_file", "delete_document_record", "delete_document")


class FakeDB:
    """slow: names of methods that should actually sleep; everything else
    on this instance returns instantly, so a test can isolate exactly one
    call site within a multi-call route."""

    def __init__(self, slow=frozenset()):
        self._slow = slow

    def _delay(self, name):
        if name in self._slow:
            time.sleep(_DELAY)

    def get_current_user(self, token):
        self._delay("get_current_user")
        return SimpleNamespace(id="user-id", email="a@b.com", created_at="2026-01-01")

    def sign_up(self, email, password):
        self._delay("sign_up")
        return {"user": SimpleNamespace(id="user-id", email=email)}

    def sign_in(self, email, password):
        self._delay("sign_in")
        return {
            "user": SimpleNamespace(id="user-id", email=email),
            "access_token": "tok",
            "refresh_token": "rtok",
        }

    def sign_out(self, access_token):
        self._delay("sign_out")
        return True

    def upload_file(self, user_id, file_bytes, filename, content_type="application/octet-stream"):
        self._delay("upload_file")
        return f"{user_id}/{filename}"

    def record_upload(self, user_id, filename, file_type, size_bytes):
        self._delay("record_upload")
        return {"id": "row-1"}

    def get_user_documents(self, user_id):
        self._delay("get_user_documents")
        return []

    def delete_file(self, user_id, filename):
        self._delay("delete_file")
        return True

    def delete_document_record(self, user_id, filename):
        self._delay("delete_document_record")
        return True


class FakePipeline:
    def __init__(self, slow=frozenset()):
        self.config = Config()
        self._slow = slow

    def _delay(self, name):
        if name in self._slow:
            time.sleep(_DELAY)

    def ingest_file(self, file_path, user_id="default", namespace=""):
        self._delay("ingest_file")
        return 3

    def delete_document(self, filename, namespace=""):
        self._delay("delete_document")


async def _fast_fake_current_user():
    return {"user": SimpleNamespace(id="user-id"), "access_token": "fake-token"}


@pytest.fixture(autouse=True)
def override_deps():
    app.dependency_overrides[get_db] = lambda: FakeDB(slow=_ALL_DB_METHODS)
    app.dependency_overrides[get_pipeline] = lambda: FakePipeline(slow=_ALL_PIPELINE_METHODS)
    app.dependency_overrides[get_current_user] = _fast_fake_current_user
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _assert_concurrent(elapsed, label, sequential_calls=1):
    """sequential_calls: how many blocking calls the route under test makes
    one after another *within a single request*. Even fully fixed, that
    request's own wall time is ~sequential_calls * _DELAY -- the
    "concurrent" win is that a *second* request's chain overlaps with the
    first's instead of adding on top of it, not that one request's own
    sequential steps somehow take zero time.
    """
    expected_if_concurrent = sequential_calls * _DELAY
    threshold = expected_if_concurrent * 1.5
    assert elapsed < threshold, (
        f"{label} blocked the event loop: 2 concurrent calls took {elapsed:.2f}s, "
        f"expected ~{expected_if_concurrent:.2f}s if truly concurrent"
    )


# ── get_current_user dependency (runs on every authenticated request) ──────


@pytest.mark.asyncio
async def test_get_current_user_dependency_does_not_block_event_loop():
    start = time.perf_counter()
    await asyncio.gather(
        get_current_user(authorization="Bearer tok1", db=FakeDB(slow=_ALL_DB_METHODS)),
        get_current_user(authorization="Bearer tok2", db=FakeDB(slow=_ALL_DB_METHODS)),
    )
    elapsed = time.perf_counter() - start
    _assert_concurrent(elapsed, "get_current_user")


# ── auth routes ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signup_does_not_block_event_loop(client):
    start = time.perf_counter()
    await asyncio.gather(
        client.post("/api/auth/signup", json={"email": "a@b.com", "password": "secret123"}),
        client.post("/api/auth/signup", json={"email": "c@d.com", "password": "secret123"}),
    )
    _assert_concurrent(time.perf_counter() - start, "signup")


@pytest.mark.asyncio
async def test_login_does_not_block_event_loop(client):
    start = time.perf_counter()
    await asyncio.gather(
        client.post("/api/auth/login", json={"email": "a@b.com", "password": "secret123"}),
        client.post("/api/auth/login", json={"email": "c@d.com", "password": "secret123"}),
    )
    _assert_concurrent(time.perf_counter() - start, "login")


@pytest.mark.asyncio
async def test_logout_does_not_block_event_loop(client):
    start = time.perf_counter()
    await asyncio.gather(
        client.post("/api/auth/logout"),
        client.post("/api/auth/logout"),
    )
    _assert_concurrent(time.perf_counter() - start, "logout")


# ── document routes ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_document_does_not_block_event_loop(client):
    """Coarse sanity check with all 3 calls slow -- see the parametrized
    per-call-site test below for the version that actually isolates each
    call (this one alone has a proven blind spot for partial regressions)."""
    files = {"file": ("a.txt", b"hello world", "text/plain")}

    start = time.perf_counter()
    await asyncio.gather(
        client.post("/api/documents/upload", files=files),
        client.post("/api/documents/upload", files={"file": ("b.txt", b"hello again", "text/plain")}),
    )
    _assert_concurrent(time.perf_counter() - start, "upload_document", sequential_calls=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("slow_call", _UPLOAD_SEQUENTIAL_CALLS)
async def test_upload_document_each_call_site_is_individually_non_blocking(client, slow_call):
    app.dependency_overrides[get_db] = lambda: FakeDB(slow={slow_call} & _ALL_DB_METHODS)
    app.dependency_overrides[get_pipeline] = lambda: FakePipeline(slow={slow_call} & _ALL_PIPELINE_METHODS)

    start = time.perf_counter()
    await asyncio.gather(
        client.post("/api/documents/upload", files={"file": ("a.txt", b"hello world", "text/plain")}),
        client.post("/api/documents/upload", files={"file": ("b.txt", b"hello again", "text/plain")}),
    )
    _assert_concurrent(time.perf_counter() - start, f"upload_document[{slow_call}]")


@pytest.mark.asyncio
async def test_list_documents_does_not_block_event_loop(client):
    start = time.perf_counter()
    await asyncio.gather(
        client.get("/api/documents/"),
        client.get("/api/documents/"),
    )
    _assert_concurrent(time.perf_counter() - start, "list_documents")


@pytest.mark.asyncio
async def test_delete_document_does_not_block_event_loop(client):
    """Coarse sanity check -- see the parametrized per-call-site test below."""
    start = time.perf_counter()
    await asyncio.gather(
        client.delete("/api/documents/a.txt"),
        client.delete("/api/documents/b.txt"),
    )
    _assert_concurrent(time.perf_counter() - start, "delete_document", sequential_calls=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("slow_call", _DELETE_SEQUENTIAL_CALLS)
async def test_delete_document_each_call_site_is_individually_non_blocking(client, slow_call):
    app.dependency_overrides[get_db] = lambda: FakeDB(slow={slow_call} & _ALL_DB_METHODS)
    app.dependency_overrides[get_pipeline] = lambda: FakePipeline(slow={slow_call} & _ALL_PIPELINE_METHODS)

    start = time.perf_counter()
    await asyncio.gather(
        client.delete("/api/documents/a.txt"),
        client.delete("/api/documents/b.txt"),
    )
    _assert_concurrent(time.perf_counter() - start, f"delete_document[{slow_call}]")
