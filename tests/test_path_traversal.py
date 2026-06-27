"""Regression test for SEC-2 (see BUGFIXES.md).

`filename` from the client (multipart upload name, or the DELETE path param)
was used raw to build local temp paths and Supabase storage keys. A filename
like "../escape.txt" could write outside the intended upload sandbox.

Note on the DELETE route: a probe confirmed that FastAPI/Starlette already
reject "/" inside the default (non-":path") `{filename}` segment, and that
literal/encoded ".." dot-segments get normalized away before routing ever
matches (always 404, never reaches the handler) — so it isn't actually
reachable via HTTP today. We still test the *handler function* and the
shared `sanitize_filename`/`_storage_path`/`download_file` helpers directly,
since they're the real defense-in-depth if that routing behavior ever
changes (e.g. a future ":path" converter) or another caller is added.

Side effects are contained to pytest's own `tmp_path` fixture (a disposable
per-test directory) — nothing outside it is ever touched, even pre-fix.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.dependencies import get_current_user, get_db, get_pipeline
from src.api.main import app
from src.api.router import documents
from src.components.config import Config
from src.components.database import SupabaseManager
from src.exception import CustomException
from src.utils import sanitize_filename


class FakeDb:
    """Records exactly which filename each storage call received."""

    def __init__(self):
        self.calls = []

    def upload_file(self, user_id, file_bytes, filename, content_type="application/octet-stream"):
        self.calls.append(("upload_file", filename))
        return f"{user_id}/{filename}"

    def record_upload(self, user_id, filename, file_type, size_bytes):
        self.calls.append(("record_upload", filename))
        return {"filename": filename}

    def delete_file(self, user_id, filename):
        self.calls.append(("delete_file", filename))
        return True

    def delete_document_record(self, user_id, filename):
        self.calls.append(("delete_document_record", filename))
        return True


class FakePipeline:
    """Captures the exact path it was asked to ingest, without touching disk."""

    def __init__(self, upload_dir: str):
        self.config = SimpleNamespace(
            SUPPORTED_FILE_TYPES=("pdf", "docx", "pptx", "txt", "xlsx", "csv", "html"),
            UPLOAD_DIR=upload_dir,
            MAX_UPLOAD_SIZE_BYTES=50 * 1024 * 1024,
        )
        self.ingest_calls = []
        self.delete_calls = []

    def ingest_file(self, file_path, user_id="default", namespace=""):
        self.ingest_calls.append(file_path)
        return 1

    def delete_document(self, filename, namespace=""):
        self.delete_calls.append(filename)


async def _fake_current_user():
    return {"user": SimpleNamespace(id="test-user-id"), "access_token": "fake-token"}


# ── HTTP-level: the genuinely exploitable vector (multipart filename) ──────


@pytest.fixture
def sandbox(tmp_path):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    return upload_dir


@pytest.fixture
def fake_db():
    return FakeDb()


@pytest.fixture
def fake_pipeline(sandbox):
    return FakePipeline(upload_dir=str(sandbox))


@pytest.fixture(autouse=True)
def override_deps(fake_db, fake_pipeline):
    app.dependency_overrides[get_db] = lambda: fake_db
    app.dependency_overrides[get_pipeline] = lambda: fake_pipeline
    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_upload_traversal_filename_stays_inside_sandbox(client, fake_pipeline, sandbox):
    """'../escape.txt' must resolve inside the sandbox, not its parent dir."""
    resp = await client.post(
        "/api/documents/upload",
        files={"file": ("../escape.txt", b"malicious content", "text/plain")},
    )

    # Part C: 202 + background ingestion -- under ASGITransport (no real
    # network boundary) the background task still runs to completion before
    # post() returns, so ingest_calls is already populated below.
    assert resp.status_code == 202
    assert len(fake_pipeline.ingest_calls) == 1
    ingested_path = Path(fake_pipeline.ingest_calls[0]).resolve()
    assert ingested_path == (sandbox / "escape.txt").resolve(), (
        f"path traversal escaped the upload sandbox: ingested {ingested_path}, "
        f"expected it inside {sandbox.resolve()}"
    )


@pytest.mark.asyncio
async def test_upload_normal_filename_still_works(client, fake_pipeline, sandbox, fake_db):
    """Regression check: an ordinary filename is unaffected by the fix."""
    resp = await client.post(
        "/api/documents/upload",
        files={"file": ("report.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )

    assert resp.status_code == 202
    assert resp.json()["filename"] == "report.pdf"
    assert ("upload_file", "report.pdf") in fake_db.calls


# ── Handler-level: DELETE route, called directly (HTTP can't deliver ".."  ──
# ── to it — see module docstring) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_handler_rejects_dot_dot_filename(fake_db, fake_pipeline):
    """If a malicious filename ever did reach the handler, it must be rejected."""
    with pytest.raises(Exception) as exc_info:
        await documents.delete_document(
            filename="..",
            current_user={"user": SimpleNamespace(id="test-user-id")},
            db=fake_db,
            pipeline=fake_pipeline,
        )
    assert getattr(exc_info.value, "status_code", None) == 400
    assert fake_db.calls == []


@pytest.mark.asyncio
async def test_delete_handler_normal_filename_still_works(fake_db, fake_pipeline):
    result = await documents.delete_document(
        filename="report.pdf",
        current_user={"user": SimpleNamespace(id="test-user-id")},
        db=fake_db,
        pipeline=fake_pipeline,
    )
    assert "report.pdf" in result["message"]
    assert ("delete_file", "report.pdf") in fake_db.calls


# ── Unit level: the shared sanitizer, and the two database.py call sites ───


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("normal.pdf", "normal.pdf"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\evil.txt", "evil.txt"),
        ("/etc/passwd", "passwd"),
        ("foo/../../bar.txt", "bar.txt"),
        ("C:/Windows/System32/evil.dll", "evil.dll"),
    ],
)
def test_sanitize_filename_strips_traversal(raw, expected):
    assert sanitize_filename(raw) == expected


@pytest.mark.parametrize("raw", ["", ".", "..", None])
def test_sanitize_filename_rejects_degenerate_input(raw):
    with pytest.raises(ValueError):
        sanitize_filename(raw)


@pytest.fixture(scope="module")
def supabase_manager():
    config = Config(
        SUPABASE_URL="https://fake.supabase.co",
        SUPABASE_ANON_KEY="fake-anon",
        SUPABASE_SERVICE_ROLE_KEY="fake-service",
    )
    return SupabaseManager(config)


def test_storage_path_sanitizes_traversal_even_called_directly(supabase_manager):
    """Defense-in-depth: malicious input passed straight to the method, no HTTP layer involved.

    Traversal segments are silently reduced to a safe basename (matching the
    upload route's behavior) — they don't escape the user's prefix.
    Only a degenerate basename (".."  with nothing left to keep) raises.
    """
    assert supabase_manager._storage_path("user123", "../other_user/secret.pdf") == "user123/secret.pdf"
    assert supabase_manager._storage_path("user123", "report.pdf") == "user123/report.pdf"

    with pytest.raises(CustomException):
        supabase_manager._storage_path("user123", "..")


def test_download_file_rejects_traversal_before_touching_disk(supabase_manager, monkeypatch, tmp_path):
    """Sanitization happens before any Supabase Storage network call."""
    monkeypatch.setattr(supabase_manager.config, "UPLOAD_DIR", str(tmp_path))

    with pytest.raises(CustomException):
        supabase_manager.download_file("user123", "../escape.txt")
