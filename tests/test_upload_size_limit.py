"""Regression test for SEC-6 (see BUGFIXES.md).

upload_document read the entire multipart file into memory with no size
cap before doing anything else — a large-enough upload is a memory/DoS
risk. This proves a too-large upload is rejected instead of accepted.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.dependencies import get_current_user, get_db, get_pipeline
from src.api.main import app


class FakeDb:
    def upload_file(self, user_id, file_bytes, filename, content_type="application/octet-stream"):
        return f"{user_id}/{filename}"

    def record_upload(self, user_id, filename, file_type, size_bytes):
        return {"filename": filename}


class FakePipeline:
    def __init__(self, upload_dir: str):
        self.config = SimpleNamespace(
            SUPPORTED_FILE_TYPES=("pdf", "docx", "txt"),
            UPLOAD_DIR=upload_dir,
            MAX_UPLOAD_SIZE_BYTES=1024,  # 1KB — tiny, so the test file is fast to build
        )

    def ingest_file(self, file_path, user_id="default", namespace=""):
        return 1


async def _fake_current_user():
    return {"user": SimpleNamespace(id="test-user-id"), "access_token": "fake-token"}


@pytest.fixture(autouse=True)
def override_deps(tmp_path):
    app.dependency_overrides[get_db] = lambda: FakeDb()
    app.dependency_overrides[get_pipeline] = lambda: FakePipeline(upload_dir=str(tmp_path))
    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_upload_over_limit_is_rejected(client):
    """A file bigger than MAX_UPLOAD_SIZE_BYTES (1KB here) must be rejected, not buffered."""
    oversized_content = b"x" * (2 * 1024)  # 2KB, double the 1KB test limit

    resp = await client.post(
        "/api/documents/upload",
        files={"file": ("big.txt", oversized_content, "text/plain")},
    )

    assert resp.status_code == 413, f"expected 413 Payload Too Large, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_upload_under_limit_still_works(client):
    """Regression check: a normal small file is unaffected by the size cap.

    Part C: upload now returns 202 + a job id (ingestion runs in the
    background) rather than 201 with the final result -- accepted is enough
    to prove the size cap didn't reject it; test_upload_rollback.py covers
    a job actually completing.
    """
    resp = await client.post(
        "/api/documents/upload",
        files={"file": ("small.txt", b"tiny file", "text/plain")},
    )

    assert resp.status_code == 202
    assert "job_id" in resp.json()
