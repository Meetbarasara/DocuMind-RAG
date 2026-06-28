"""Session persistence (backend): POST /api/auth/refresh.

Supabase access tokens are short-lived (~1h). This endpoint exchanges a still-
valid refresh token for a fresh access+refresh pair so a session can renew
transparently instead of bouncing the user to login mid-use. A bad/expired
refresh token returns 401 (the client then sends the user to login). Supabase
is mocked — no network.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.dependencies import get_db
from src.api.main import app
from src.exception import CustomException


class FakeDb:
    def __init__(self, succeed: bool = True):
        self.succeed = succeed
        self.calls: list = []

    def refresh_session(self, refresh_token: str):
        self.calls.append(refresh_token)
        if not self.succeed:
            raise CustomException("invalid or expired refresh token")
        return {
            "user": SimpleNamespace(id="user-123", email="u@example.com"),
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
        }


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_refresh_returns_a_fresh_token_pair(client):
    fake_db = FakeDb(succeed=True)
    app.dependency_overrides[get_db] = lambda: fake_db

    resp = await client.post("/api/auth/refresh", json={"refresh_token": "old-refresh"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == "new-access-token"
    assert body["refresh_token"] == "new-refresh-token"
    assert body["user_id"] == "user-123"
    assert body["email"] == "u@example.com"
    assert fake_db.calls == ["old-refresh"]   # the supplied token was the one exchanged


@pytest.mark.asyncio
async def test_refresh_with_invalid_token_returns_401(client):
    app.dependency_overrides[get_db] = lambda: FakeDb(succeed=False)

    resp = await client.post("/api/auth/refresh", json={"refresh_token": "bad-or-expired"})

    assert resp.status_code == 401   # client should route the user to login


@pytest.mark.asyncio
async def test_refresh_requires_a_refresh_token(client):
    app.dependency_overrides[get_db] = lambda: FakeDb(succeed=True)

    resp = await client.post("/api/auth/refresh", json={})

    assert resp.status_code == 422   # pydantic validation: refresh_token is required
