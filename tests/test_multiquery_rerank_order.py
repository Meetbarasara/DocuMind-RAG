"""Regression test for BUG-6 (see BUGFIXES.md).

RetrievalManager.retrieve() does hybrid search -> dedup -> re-rank, cutting
to RERANKER_TOP_K *inside a single call*. But multi-query retrieval called
.retrieve() once per sub-query and merged the results afterward — so each
sub-query's results were independently truncated to RERANKER_TOP_K *before*
the merge ever happened, and the cross-encoder ran once per sub-query
instead of once over the full merged candidate pool.

Uses a fake retrieval_manager double (not a real Pinecone-backed
RetrievalManager) so this stays a pure unit test of pipeline.py's
orchestration logic.
"""

import pytest
from langchain_core.documents import Document

from src.components.config import Config
from src.pipeline.pipeline import RAGPipeline

_RERANKER_TOP_K = 3


class FakeRetrievalManager:
    """Exposes both the old (retrieve) and new (retrieve_candidates + rerank)
    interfaces so the same double works whether the code calls one or the
    other — that's exactly what distinguishes pre-fix from post-fix here."""

    def __init__(self, docs_per_query=5):
        self.docs_per_query = docs_per_query
        self.retrieve_calls = []
        self.retrieve_candidates_calls = []
        self.rerank_calls = []

    def _make_docs(self, query):
        return [
            Document(page_content=f"{query}-doc{i}", metadata={"chunk_id": f"{query}-{i}"})
            for i in range(self.docs_per_query)
        ]

    def retrieve(self, query, filename_filter=None, query_vector=None):
        """Old interface: simulates the bug — reranks AND truncates per sub-query."""
        self.retrieve_calls.append(query)
        return self._make_docs(query)[:_RERANKER_TOP_K]

    def retrieve_candidates(self, query, filename_filter=None, query_vector=None):
        """New interface: full candidate set, no truncation, no rerank yet."""
        self.retrieve_candidates_calls.append(query)
        return self._make_docs(query)

    def rerank(self, query, docs):
        self.rerank_calls.append((query, list(docs)))
        return docs[:_RERANKER_TOP_K]


@pytest.fixture
def pipeline():
    p = RAGPipeline(Config())

    async def fake_generate_multi_queries(query):
        return ["q1", "q2", "q3"]

    p.generation_manager.generate_multi_queries = fake_generate_multi_queries
    return p


@pytest.mark.asyncio
async def test_rerank_runs_once_over_full_merged_pool(pipeline):
    fake_rm = FakeRetrievalManager(docs_per_query=5)

    await pipeline._multi_query_retrieve_async("original query", fake_rm, filename_filter=None)

    assert fake_rm.retrieve_calls == [], (
        "retrieve() truncates to RERANKER_TOP_K *before* the multi-query merge — "
        "multi-query retrieval should use retrieve_candidates() instead"
    )
    assert fake_rm.retrieve_candidates_calls == ["q1", "q2", "q3"]

    assert len(fake_rm.rerank_calls) == 1, (
        f"expected exactly 1 rerank call over the merged pool, got {len(fake_rm.rerank_calls)} "
        "(reranking per sub-query wastes 3x the cross-encoder calls)"
    )

    _, docs_seen_by_rerank = fake_rm.rerank_calls[0]
    assert len(docs_seen_by_rerank) == 15, (
        f"rerank should see all 3 queries x 5 docs = 15 merged candidates, "
        f"got {len(docs_seen_by_rerank)} — looks like candidates were truncated before merging"
    )
