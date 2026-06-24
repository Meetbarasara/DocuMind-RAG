"""Regression test for BUG-10 (see BUGFIXES.md).

upload_document didn't roll back on partial failure in either direction:
- If ingestion failed after the storage upload succeeded, the storage
  object was left orphaned (only the local temp file got cleaned up).
- record_upload's return value was never checked. When it fails (returns
  None), the response still said 201 success, but the file had no
  metadata row — invisible in the UI, yet its storage object and Pinecone
  vectors still existed, still consumed quota, and could still answer
  queries.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.dependencies import get_current_user, get_db, get_pipeline
from src.api.main import app


class FakeDb:
    def __init__(self, record_upload_succeeds=True):
        self.record_upload_succeeds = record_upload_succeeds
        self.upload_file_calls = []
        self.delete_file_calls = []
        self.record_upload_calls = []

    def upload_file(self, user_id, file_bytes, filename, content_type="application/octet-stream"):
        self.upload_file_calls.append(filename)
        return f"{user_id}/{filename}"

    def delete_file(self, user_id, filename):
        self.delete_file_calls.append(filename)
        return True

    def record_upload(self, user_id, filename, file_type, size_bytes):
        self.record_upload_calls.append(filename)
        return {"filename": filename} if self.record_upload_succeeds else None


class FakePipeline:
    def __init__(self, upload_dir, ingest_should_fail=False):
        self.config = SimpleNamespace(
            SUPPORTED_FILE_TYPES=("txt",),
            MAX_UPLOAD_SIZE_BYTES=999_999_999,
            UPLOAD_DIR=upload_dir,
        )
        self.ingest_should_fail = ingest_should_fail
        self.delete_calls = []

    def ingest_file(self, file_path, user_id="default", namespace=""):
        if self.ingest_should_fail:
            raise RuntimeError("simulated ingestion failure")
        return 3

    def delete_document(self, filename, namespace=""):
        self.delete_calls.append(filename)


async def _fake_current_user():
    return {"user": SimpleNamespace(id="test-user-id"), "access_token": "fake-token"}


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_ingestion_failure_cleans_up_orphaned_storage_object(client, tmp_path):
    fake_db = FakeDb()
    fake_pipeline = FakePipeline(str(tmp_path), ingest_should_fail=True)
    app.dependency_overrides[get_db] = lambda: fake_db
    app.dependency_overrides[get_pipeline] = lambda: fake_pipeline
    app.dependency_overrides[get_current_user] = _fake_current_user

    resp = await client.post(
        "/api/documents/upload",
        files={"file": ("report.txt", b"hello", "text/plain")},
    )

    assert resp.status_code == 500
    assert fake_db.upload_file_calls == ["report.txt"]
    assert fake_db.delete_file_calls == ["report.txt"], (
        "storage object should be cleaned up after ingestion fails, not left orphaned"
    )


@pytest.mark.asyncio
async def test_record_upload_failure_rolls_back_storage_and_pinecone(client, tmp_path):
    fake_db = FakeDb(record_upload_succeeds=False)
    fake_pipeline = FakePipeline(str(tmp_path), ingest_should_fail=False)
    app.dependency_overrides[get_db] = lambda: fake_db
    app.dependency_overrides[get_pipeline] = lambda: fake_pipeline
    app.dependency_overrides[get_current_user] = _fake_current_user

    resp = await client.post(
        "/api/documents/upload",
        files={"file": ("report.txt", b"hello", "text/plain")},
    )

    assert resp.status_code != 201, "must not report success when no metadata row got recorded"
    assert fake_db.delete_file_calls == ["report.txt"], "storage object should be rolled back"
    assert fake_pipeline.delete_calls == ["report.txt"], "Pinecone vectors should be rolled back"


@pytest.mark.asyncio
async def test_successful_upload_does_not_trigger_any_rollback(client, tmp_path):
    fake_db = FakeDb(record_upload_succeeds=True)
    fake_pipeline = FakePipeline(str(tmp_path), ingest_should_fail=False)
    app.dependency_overrides[get_db] = lambda: fake_db
    app.dependency_overrides[get_pipeline] = lambda: fake_pipeline
    app.dependency_overrides[get_current_user] = _fake_current_user

    resp = await client.post(
        "/api/documents/upload",
        files={"file": ("report.txt", b"hello", "text/plain")},
    )

    assert resp.status_code == 201
    assert fake_db.delete_file_calls == []
    assert fake_pipeline.delete_calls == []
