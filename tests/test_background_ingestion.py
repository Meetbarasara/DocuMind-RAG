"""Part C: background ingestion — POST /upload returns 202 + a job id
immediately; GET /upload-status/{job_id} is polled for the actual result.

Covers the contract itself (not just that pre-existing tests still pass):
  - 202 + job id on accept, scoped per user.
  - an unknown or someone-else's job id polls as 404 (indistinguishable).
  - a completed job's status carries chunks_ingested.
  - the in-process job dict is bounded (oldest entries evicted).
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.dependencies import get_current_user, get_db, get_pipeline
from src.api.main import app
from src.api.router import documents


class FakeDb:
    def upload_file(self, user_id, file_bytes, filename, content_type="application/octet-stream"):
        return f"{user_id}/{filename}"

    def record_upload(self, user_id, filename, file_type, size_bytes):
        return {"filename": filename}


class FakePipeline:
    def __init__(self, upload_dir: str, chunks: int = 3):
        self.config = SimpleNamespace(
            SUPPORTED_FILE_TYPES=("txt",),
            UPLOAD_DIR=upload_dir,
            MAX_UPLOAD_SIZE_BYTES=999_999_999,
        )
        self._chunks = chunks

    def ingest_file(self, file_path, user_id="default", namespace=""):
        return self._chunks


def _fake_user(user_id):
    async def _inner():
        return {"user": SimpleNamespace(id=user_id), "access_token": "fake-token"}
    return _inner


@pytest.fixture(autouse=True)
def override_deps(tmp_path):
    app.dependency_overrides[get_db] = lambda: FakeDb()
    app.dependency_overrides[get_pipeline] = lambda: FakePipeline(upload_dir=str(tmp_path))
    app.dependency_overrides[get_current_user] = _fake_user("user-a")
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_upload_returns_202_with_job_id_immediately(client):
    resp = await client.post(
        "/api/documents/upload",
        files={"file": ("report.txt", b"hello", "text/plain")},
    )

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "processing"
    assert body["filename"] == "report.txt"
    assert isinstance(body["job_id"], str) and body["job_id"]


@pytest.mark.asyncio
async def test_completed_job_status_reports_chunks_ingested(client):
    app.dependency_overrides[get_pipeline] = lambda: FakePipeline(upload_dir="ignored", chunks=7)

    resp = await client.post(
        "/api/documents/upload",
        files={"file": ("report.txt", b"hello", "text/plain")},
    )
    job_id = resp.json()["job_id"]

    status_resp = await client.get(f"/api/documents/upload-status/{job_id}")

    assert status_resp.status_code == 200
    job = status_resp.json()
    assert job["status"] == "completed"
    assert job["chunks_ingested"] == 7
    assert job["error"] is None
    assert "user_id" not in job, "internal user_id must not leak into the response"


@pytest.mark.asyncio
async def test_unknown_job_id_is_404(client):
    resp = await client.get("/api/documents/upload-status/does-not-exist")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_another_users_job_id_is_404_not_403(client):
    """Same job-id space for everyone; visibility is scoped per user like
    every other per-user resource in this API. A 404 (not 403) means an
    unknown id and someone else's id are indistinguishable to a prober."""
    upload_resp = await client.post(
        "/api/documents/upload",
        files={"file": ("report.txt", b"hello", "text/plain")},
    )
    job_id = upload_resp.json()["job_id"]

    app.dependency_overrides[get_current_user] = _fake_user("user-b")
    status_resp = await client.get(f"/api/documents/upload-status/{job_id}")

    assert status_resp.status_code == 404


def test_job_dict_is_bounded(monkeypatch):
    """Oldest jobs are evicted once the tracked-job cap is exceeded -- a
    long-running process can't accumulate unbounded job history."""
    monkeypatch.setattr(documents, "_upload_jobs", {})
    monkeypatch.setattr(documents, "_MAX_TRACKED_UPLOAD_JOBS", 3)

    job_ids = [documents._new_upload_job(f"f{i}.txt", "user-a") for i in range(5)]

    assert len(documents._upload_jobs) == 3
    # the 2 oldest were evicted; the 3 most recent remain
    assert job_ids[0] not in documents._upload_jobs
    assert job_ids[1] not in documents._upload_jobs
    assert all(jid in documents._upload_jobs for jid in job_ids[2:])
