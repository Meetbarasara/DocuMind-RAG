"""POST /api/compliance/suggest — grounded remediation drafts.

Exercises the real engine (suggest_remediation builds the prompt and returns the
model text) with a fake Groq LLM, plus the route's validation. Only the network
is faked.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import src.api.router.compliance as compliance_router
from src.api.dependencies import get_current_user, get_pipeline
from src.api.main import app


class FakeGroq:
    """Stand-in for ChatGroq: returns a fixed draft and records the prompt so we
    can assert the requirement + current clause were fed in."""

    last_human = None

    def __init__(self, **kwargs):
        pass

    async def ainvoke(self, messages):
        FakeGroq.last_human = messages[-1].content
        return SimpleNamespace(
            content="The company shall retain records of customer identity and "
            "transactions for at least five years after the account is closed."
        )


class FakePipeline:
    config = SimpleNamespace(
        LLM_MODEL_NAME="llama-3.1-8b-instant",
        LLM_TEMPERATURE=0.2,
        GROQ_API_KEY="gsk-fake",
    )


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setattr(compliance_router, "ChatGroq", FakeGroq)
    app.dependency_overrides[get_current_user] = lambda: {"user": SimpleNamespace(id="user-1")}
    app.dependency_overrides[get_pipeline] = lambda: FakePipeline()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_suggest_returns_grounded_draft(client):
    resp = await client.post(
        "/api/compliance/suggest",
        json={
            "requirement": "Retain KYC records for at least five years after account closure.",
            "status": "Conflict",
            "policy_clause": "Records are retained for three years after closure.",
            "rationale": "three years is less than the five-year minimum",
        },
    )
    assert resp.status_code == 200
    assert "five years" in resp.json()["suggestion"].lower()
    # the engine fed BOTH the requirement and the company's current clause to the model
    assert "Retain KYC records" in FakeGroq.last_human
    assert "three years" in FakeGroq.last_human


@pytest.mark.asyncio
async def test_suggest_rejects_empty_requirement(client):
    resp = await client.post("/api/compliance/suggest", json={"requirement": "   "})
    assert resp.status_code == 400
