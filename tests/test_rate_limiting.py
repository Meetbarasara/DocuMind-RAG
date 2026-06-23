"""Regression test for SEC-7 (see BUGFIXES.md).

slowapi's Limiter was constructed in main.py and registered with FastAPI's
exception handler, but no route ever had an @limiter.limit(...) decorator,
so nothing actually enforced a limit — login could be hammered forever
with no 429.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.dependencies import get_db
from src.api.main import app


class FakeDb:
    """Simulates always-wrong credentials — no real Supabase call needed."""

    def sign_in(self, email, password):
        raise Exception("Invalid login credentials")


@pytest.fixture(autouse=True)
def override_deps():
    app.dependency_overrides[get_db] = lambda: FakeDb()
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_login_gets_rate_limited_after_repeated_attempts(client):
    """Repeated rapid login attempts from one client must eventually get a 429."""
    statuses = []
    for _ in range(10):
        resp = await client.post(
            "/api/auth/login", json={"email": "attacker@example.com", "password": "wrong"}
        )
        statuses.append(resp.status_code)

    assert statuses[0] != 429, "the very first request should never be throttled"
    assert 429 in statuses, (
        f"expected a 429 somewhere in 10 rapid login attempts, got {statuses} — "
        "rate limiting isn't actually enforced"
    )
