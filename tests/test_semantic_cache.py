"""C2: semantic cache — a near-identical past question (cosine on the query
embedding) is served without retrieval or the LLM. fakeredis + synthetic
embeddings, so no API.
"""

import fakeredis
import pytest
from langchain_core.documents import Document

from src.components.cache import QueryCache
from src.components.config import Config
from src.pipeline.pipeline import RAGPipeline


def _cache():
    return QueryCache(Config(), client=fakeredis.FakeRedis(decode_responses=True))


def test_cosine_basics():
    assert QueryCache._cosine([1, 0], [1, 0]) == 1.0
    assert QueryCache._cosine([1, 0], [0, 1]) == 0.0
    assert QueryCache._cosine([1, 0], [0, 0]) == 0.0  # zero vector -> 0, no div error


def test_semantic_hit_identical_miss_dissimilar():
    c = _cache()
    c.add_semantic("ns1", [1.0, 0.0, 0.0], {"answer": "X"})
    assert c.get_semantic("ns1", [1.0, 0.0, 0.0])["answer"] == "X"   # identical -> hit
    assert c.get_semantic("ns1", [0.0, 1.0, 0.0]) is None            # orthogonal -> miss


def test_semantic_near_identical_hits_above_threshold():
    c = _cache()
    c.add_semantic("ns1", [1.0, 0.0], {"answer": "X"})
    assert c.get_semantic("ns1", [0.99, 0.01])["answer"] == "X"      # cosine ~1.0 >= 0.95


def test_semantic_namespace_and_filter_isolation():
    c = _cache()
    c.add_semantic("ns1", [1.0, 0.0], {"answer": "X"})
    assert c.get_semantic("ns2", [1.0, 0.0]) is None                          # other user
    assert c.get_semantic("ns1", [1.0, 0.0], filename_filter="d.pdf") is None  # other filter


def test_invalidate_clears_semantic_entries():
    c = _cache()
    c.add_semantic("ns1", [1.0, 0.0], {"answer": "X"})
    c.invalidate("ns1")
    assert c.get_semantic("ns1", [1.0, 0.0]) is None


def test_semantic_is_a_noop_without_client():
    c = QueryCache(Config(REDIS_URL=""))
    c.add_semantic("ns1", [1.0, 0.0], {"answer": "X"})  # must not raise
    assert c.get_semantic("ns1", [1.0, 0.0]) is None


@pytest.mark.asyncio
async def test_pipeline_serves_semantically_similar_query_from_cache(monkeypatch):
    p = RAGPipeline(Config())
    p.cache = QueryCache(Config(), client=fakeredis.FakeRedis(decode_responses=True))

    calls = {"retrieve": 0, "generate": 0}

    async def fake_rewrite(q, h=None):
        return q

    async def fake_retrieve(rw, rm, filename_filter=None):
        calls["retrieve"] += 1
        return [Document(page_content="ctx", metadata={"filename": "f.pdf"})]

    async def fake_generate(q, docs, chat_history=None, page_images=None):
        calls["generate"] += 1
        return {"answer": "the answer", "sources": [], "num_sources_used": 1}

    monkeypatch.setattr(p, "_get_retrieval_manager", lambda ns: object())
    monkeypatch.setattr(p, "_multi_query_retrieve_async", fake_retrieve)
    monkeypatch.setattr(p.generation_manager, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(p.generation_manager, "generate", fake_generate)
    # Both phrasings embed to the same vector -> 2nd query is a semantic hit.
    monkeypatch.setattr(p.embedding_manager, "embed_query", lambda text: [1.0, 0.0, 0.0])

    r1 = await p.query("what is x?", namespace="ns1")
    r2 = await p.query("whats x ??", namespace="ns1")   # different exact text, same meaning

    assert r1["answer"] == r2["answer"] == "the answer"
    assert r2.get("cached") == "semantic"
    assert calls["retrieve"] == 1 and calls["generate"] == 1  # 2nd skipped retrieval + LLM
