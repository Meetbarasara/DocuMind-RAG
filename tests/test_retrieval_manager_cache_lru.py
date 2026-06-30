"""Regression test for Latency Optimization #6 (see BUGFIXES.md / CODE_REVIEW.md §4).

RAGPipeline._retrieval_managers cached one RetrievalManager per namespace
forever, with no eviction -- unbounded growth as distinct users/namespaces
accumulate over the app's lifetime. Each cached RetrievalManager holds a
PineconeVectorStore client, a GoogleGenerativeAIEmbeddings client, and (once hybrid
search runs) a full BM25 corpus of every chunk in that namespace, in RAM.

Builds RAGPipeline for real (safe -- its __init__ doesn't hit network; only
RetrievalManager's __init__ does, confirmed in the BUG-4/5 investigation),
but fakes out RetrievalManager itself so repeatedly populating the cache
across many namespaces doesn't try to hit Pinecone for real.
"""

from types import SimpleNamespace

import pytest

from src.components.config import Config
from src.pipeline.pipeline import RAGPipeline


@pytest.fixture
def pipeline(monkeypatch):
    monkeypatch.setattr(
        "src.pipeline.pipeline.RetrievalManager",
        lambda cfg: SimpleNamespace(config=cfg),
    )
    return RAGPipeline(Config(MAX_CACHED_RETRIEVAL_MANAGERS=2))


def test_cache_does_not_grow_past_the_configured_bound(pipeline):
    for i in range(10):
        pipeline._get_retrieval_manager(f"user-{i}")

    assert len(pipeline._retrieval_managers) <= 2, (
        f"cache grew to {len(pipeline._retrieval_managers)} entries despite a "
        "configured bound of 2 -- unbounded growth"
    )


def test_cache_evicts_the_least_recently_used_entry(pipeline):
    pipeline._get_retrieval_manager("user-a")
    pipeline._get_retrieval_manager("user-b")
    # Touch "user-a" again so "user-b" becomes the least-recently-used.
    pipeline._get_retrieval_manager("user-a")

    pipeline._get_retrieval_manager("user-c")  # cache is full -> must evict someone

    assert "user-b" not in pipeline._retrieval_managers, "least-recently-used entry should be evicted"
    assert "user-a" in pipeline._retrieval_managers, "recently-touched entry should survive"
    assert "user-c" in pipeline._retrieval_managers


def test_cache_reuses_an_existing_entry_without_evicting_anything(pipeline):
    rm_a1 = pipeline._get_retrieval_manager("user-a")
    rm_a2 = pipeline._get_retrieval_manager("user-a")

    assert rm_a1 is rm_a2, "repeated lookups for the same namespace must reuse the cached instance"
    assert len(pipeline._retrieval_managers) == 1
