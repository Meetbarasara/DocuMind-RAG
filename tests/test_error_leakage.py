"""Regression test for SEC-4 (see BUGFIXES.md).

Several route handlers — and the streaming pipeline's error event — put
raw exception text straight into the client-facing response
(`detail=str(e)`, f"...{e}", or an SSE error event's `message`). That can
leak stack/provider details or internal hostnames, and combined with
SEC-5 lets a client tell "email already registered" apart from a generic
failure on signup/login.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.dependencies import get_current_user, get_db, get_pipeline
from src.api.main import app

# A stand-in for the kind of internal detail a real exception might contain —
# DB error text, an internal hostname, a stack-trace fragment. None of this
# should ever reach the client. Deliberately has no quote/backslash chars,
# since those would get JSON-escaped and silently defeat a plain substring
# check on the raw response body regardless of whether the fix is applied.
_SENSITIVE = "duplicate key value violates unique constraint users_pkey at host 10.0.4.12 internal-debug-token=abc123"


class FakeDbRaisingOnSignUp:
    def sign_up(self, email, password):
        raise Exception(_SENSITIVE)


class FakeDbRaisingOnSignIn:
    def sign_in(self, email, password):
        raise Exception(_SENSITIVE)


class FakeDbRaisingOnUpload:
    def upload_file(self, **kwargs):
        raise Exception(_SENSITIVE)


class FakeDbForDelete:
    def delete_file(self, user_id, filename):
        return True

    def delete_document_record(self, user_id, filename):
        return True


class FakePipelineRaisingOnQuery:
    async def query(self, **kwargs):
        raise Exception(_SENSITIVE)


class FakePipelineRaisingOnDelete:
    def delete_document(self, filename, namespace=""):
        raise Exception(_SENSITIVE)


class FakePipelineForUpload:
    config = SimpleNamespace(
        SUPPORTED_FILE_TYPES=("txt",),
        MAX_UPLOAD_SIZE_BYTES=999_999_999,
        UPLOAD_DIR="should-not-be-reached",
    )


async def _fake_current_user():
    return {"user": SimpleNamespace(id="test-user-id"), "access_token": "fake-token"}


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_signup_failure_does_not_leak_raw_error(client):
    app.dependency_overrides[get_db] = lambda: FakeDbRaisingOnSignUp()

    resp = await client.post(
        "/api/auth/signup", json={"email": "x@example.com", "password": "secret123"}
    )

    assert _SENSITIVE not in resp.text, f"raw exception text leaked: {resp.text}"
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_login_failure_does_not_leak_raw_error(client):
    app.dependency_overrides[get_db] = lambda: FakeDbRaisingOnSignIn()

    resp = await client.post(
        "/api/auth/login", json={"email": "x@example.com", "password": "wrong"}
    )

    assert _SENSITIVE not in resp.text, f"raw exception text leaked: {resp.text}"
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_upload_failure_does_not_leak_raw_error(client):
    """Part C: ingestion runs in the background now, so the failure (and any
    leaked text) would surface via the status-poll endpoint, not the POST's
    immediate 202 -- check both."""
    app.dependency_overrides[get_db] = lambda: FakeDbRaisingOnUpload()
    app.dependency_overrides[get_pipeline] = lambda: FakePipelineForUpload()
    app.dependency_overrides[get_current_user] = _fake_current_user

    resp = await client.post(
        "/api/documents/upload", files={"file": ("a.txt", b"hi", "text/plain")}
    )
    assert _SENSITIVE not in resp.text, f"raw exception text leaked: {resp.text}"
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    status_resp = await client.get(f"/api/documents/upload-status/{job_id}")
    assert _SENSITIVE not in status_resp.text, f"raw exception text leaked: {status_resp.text}"
    assert status_resp.json()["status"] == "failed"


@pytest.mark.asyncio
async def test_delete_pinecone_failure_does_not_leak_raw_error(client):
    app.dependency_overrides[get_db] = lambda: FakeDbForDelete()
    app.dependency_overrides[get_pipeline] = lambda: FakePipelineRaisingOnDelete()
    app.dependency_overrides[get_current_user] = _fake_current_user

    resp = await client.request("DELETE", "/api/documents/report.pdf")

    assert _SENSITIVE not in resp.text, f"raw exception text leaked: {resp.text}"
    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_chat_query_failure_does_not_leak_raw_error(client):
    app.dependency_overrides[get_pipeline] = lambda: FakePipelineRaisingOnQuery()
    app.dependency_overrides[get_current_user] = _fake_current_user

    resp = await client.post("/api/chat/query", json={"question": "hi"})

    assert _SENSITIVE not in resp.text, f"raw exception text leaked: {resp.text}"
    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_query_stream_error_event_does_not_leak_raw_error():
    """Unit-level: pipeline.query_stream's own try/except, not the route layer."""
    from src.components.config import Config
    from src.pipeline.pipeline import RAGPipeline

    pipeline = RAGPipeline(Config())

    async def _raise_rewrite(*args, **kwargs):
        raise Exception(_SENSITIVE)

    pipeline.generation_manager.rewrite_query = _raise_rewrite

    chunks = [event async for event in pipeline.query_stream("question", namespace="ns")]
    body = "".join(chunks)

    assert _SENSITIVE not in body, f"raw exception text leaked: {body}"
    assert "[DONE]" in body
