"""C1/C3 pipeline integration.

A repeated query (no chat history) is served from the cache without re-running
retrieval/generation; invalidation forces a fresh run; and a query *with*
history bypasses the cache entirely (the raw question isn't a safe key then).

Uses fakeredis + monkeypatched retrieval/generation/rewrite so nothing hits the
network.
"""

import fakeredis
import pytest
from langchain_core.documents import Document

from src.components.cache import QueryCache
from src.components.config import Config
from src.pipeline.pipeline import RAGPipeline


def _pipeline_with_fake_cache(monkeypatch):
    p = RAGPipeline(Config())
    p.cache = QueryCache(Config(), client=fakeredis.FakeRedis(decode_responses=True))

    calls = {"retrieve": 0, "generate": 0}

    async def fake_rewrite(question, chat_history=None):
        return question  # no LLM call

    async def fake_retrieve(rewritten, rm, filename_filter=None):
        calls["retrieve"] += 1
        return [Document(page_content="ctx", metadata={"filename": "f.pdf"})]

    async def fake_generate(query, docs, chat_history=None, page_images=None):
        calls["generate"] += 1
        return {"answer": "the answer", "sources": [{"filename": "f.pdf"}],
                "num_sources_used": 1}

    monkeypatch.setattr(p, "_get_retrieval_manager", lambda ns: object())
    monkeypatch.setattr(p, "_multi_query_retrieve_async", fake_retrieve)
    monkeypatch.setattr(p.generation_manager, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(p.generation_manager, "generate", fake_generate)
    return p, calls


@pytest.mark.asyncio
async def test_second_identical_query_is_a_cache_hit(monkeypatch):
    p, calls = _pipeline_with_fake_cache(monkeypatch)

    r1 = await p.query("what is x?", namespace="ns1")
    r2 = await p.query("what is x?", namespace="ns1")

    assert r1["answer"] == r2["answer"] == "the answer"
    assert r2.get("cached") is True
    assert calls["retrieve"] == 1   # 2nd call short-circuited at the cache
    assert calls["generate"] == 1


@pytest.mark.asyncio
async def test_invalidation_forces_a_fresh_run(monkeypatch):
    p, calls = _pipeline_with_fake_cache(monkeypatch)

    await p.query("what is x?", namespace="ns1")
    p.cache.invalidate("ns1")               # e.g. an upload/delete happened
    await p.query("what is x?", namespace="ns1")

    assert calls["generate"] == 2           # cache was cleared -> ran again


@pytest.mark.asyncio
async def test_history_bypasses_cache(monkeypatch):
    p, calls = _pipeline_with_fake_cache(monkeypatch)

    history = [{"role": "human", "content": "earlier"}]
    await p.query("what is x?", namespace="ns1", chat_history=history)
    await p.query("what is x?", namespace="ns1", chat_history=history)

    # With history present the raw question isn't a safe key -> never cached.
    assert calls["generate"] == 2
