"""B-hybrid frontend support: GET /api/documents/page-image/{filename}/{page}
serves the rendered page snapshot (or 404) so the chat UI can show the actual
page a multimodal answer read from. Hits the real app over ASGI with a fake db.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.dependencies import get_current_user, get_db
from src.api.main import app


class FakeDB:
    def __init__(self, data):
        self._data = data
        self.calls = []

    def download_page_image(self, namespace, filename, page_number):
        self.calls.append((namespace, filename, page_number))
        return self._data


async def _fake_user():
    return {"user": SimpleNamespace(id="user-1"), "access_token": "tok"}


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_page_image_returns_png(client):
    db = FakeDB(b"\x89PNG-snapshot")
    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_db] = lambda: db
    try:
        resp = await client.get("/api/documents/page-image/report.pdf/3")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.content == b"\x89PNG-snapshot"
        assert db.calls == [("user-1", "report.pdf", 3)]  # scoped to this user
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_page_image_404_when_missing(client):
    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_db] = lambda: FakeDB(None)
    try:
        resp = await client.get("/api/documents/page-image/report.pdf/9")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()
