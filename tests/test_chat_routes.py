"""Regression test for BUG-1 (see BUGFIXES.md).

RAGPipeline.query / query_stream are async, but the chat routes called them
without `await` / `async for`. This hits the real FastAPI app over ASGI with
a fake pipeline (no OpenAI/Pinecone/Supabase calls) so it exercises the exact
route code that broke chat.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.dependencies import get_current_user, get_pipeline
from src.api.main import app


class FakePipeline:
    """Stands in for RAGPipeline: same async interface, no real network calls."""

    async def query(self, question, namespace="", chat_history=None, filename_filter=None):
        return {
            "answer": f"Answer to: {question}",
            "sources": [
                {"source_id": 1, "filename": "doc.pdf", "page": 1, "chunk_type": "text", "chunk_id": "abc"}
            ],
            "rewritten_query": question,
            "num_sources_used": 1,
            "namespace": namespace,
        }

    async def query_stream(self, question, namespace="", chat_history=None, filename_filter=None):
        yield 'data: {"type": "sources", "sources": []}\n\n'
        yield f'data: {{"type": "token", "content": "Answer to: {question}"}}\n\n'
        yield "data: [DONE]\n\n"


async def _fake_current_user():
    return {"user": SimpleNamespace(id="test-user-id"), "access_token": "fake-token"}


@pytest.fixture(autouse=True)
def override_deps():
    app.dependency_overrides[get_pipeline] = lambda: FakePipeline()
    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_chat_query_returns_real_answer(client):
    resp = await client.post("/api/chat/query", json={"question": "What is DocuMind?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "Answer to: What is DocuMind?"
    assert body["num_sources_used"] == 1


@pytest.mark.asyncio
async def test_chat_query_stream_returns_sse_tokens(client):
    async with client.stream("POST", "/api/chat/query/stream", json={"question": "hi"}) as resp:
        assert resp.status_code == 200
        chunks = [chunk async for chunk in resp.aiter_text()]
    body = "".join(chunks)
    assert "Answer to: hi" in body
    assert "[DONE]" in body
