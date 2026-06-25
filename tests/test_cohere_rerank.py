"""Tests for Cohere-based reranking (L2).

The local sentence-transformers cross-encoder was replaced by Cohere's hosted
Rerank API. These cover the behaviours that matter without a network call:
(1) with a Cohere client, docs are reordered by Cohere's ranking and cut to
RERANKER_TOP_K; (2) with no client (no key/SDK), reranking degrades gracefully
to retrieval order truncated to RERANKER_TOP_K — it never crashes the query;
(3) USE_RERANKING=False leaves the docs untouched.

Builds RetrievalManager via __new__ (its __init__ makes a real Pinecone
control-plane call) and injects a fake Cohere client, so no key or network is
needed and the `cohere` package itself is never imported.
"""

from types import SimpleNamespace

from langchain_core.documents import Document

from src.components.config import Config
from src.components.retrieval import RetrievalManager


class FakeCohereClient:
    """Mimics cohere.ClientV2: rerank() returns results referencing input
    document indices, best (highest relevance) first."""

    def __init__(self, order):
        self.order = order          # indices into the documents list, best first
        self.calls = []

    def rerank(self, *, model, query, documents, top_n=None, **kwargs):
        self.calls.append({"model": model, "query": query, "n_docs": len(documents), "top_n": top_n})
        results = [
            SimpleNamespace(index=idx, relevance_score=1.0 - rank * 0.1)
            for rank, idx in enumerate(self.order)
        ]
        return SimpleNamespace(results=results[:top_n] if top_n else results)


def _make_rm(cohere_client=None, use_reranking=True, top_k=3):
    rm = RetrievalManager.__new__(RetrievalManager)
    rm.config = Config(USE_RERANKING=use_reranking, RERANKER_TOP_K=top_k)
    rm._cohere_client = cohere_client
    return rm


def _docs(n):
    return [Document(page_content=f"doc{i}", metadata={"id": i}) for i in range(n)]


def test_rerank_reorders_by_cohere_and_truncates():
    docs = _docs(5)
    # Cohere decides the best order is doc3, doc1, doc0, doc4, doc2; keep top 3.
    rm = _make_rm(cohere_client=FakeCohereClient(order=[3, 1, 0, 4, 2]), top_k=3)

    out = rm.rerank("q", docs)

    assert [d.metadata["id"] for d in out] == [3, 1, 0]
    call = rm._cohere_client.calls[0]
    assert call["n_docs"] == 5 and call["top_n"] == 3  # all candidates sent, top_n requested


def test_rerank_without_client_falls_back_to_retrieval_order():
    docs = _docs(5)
    rm = _make_rm(cohere_client=None, top_k=3)   # no key/SDK

    out = rm.rerank("q", docs)

    # graceful: first RERANKER_TOP_K in existing retrieval order, no crash
    assert [d.metadata["id"] for d in out] == [0, 1, 2]


def test_rerank_api_error_falls_back_to_retrieval_order():
    class BoomClient:
        def rerank(self, **kwargs):
            raise RuntimeError("cohere is down")

    docs = _docs(5)
    rm = _make_rm(cohere_client=BoomClient(), top_k=2)

    out = rm.rerank("q", docs)

    assert [d.metadata["id"] for d in out] == [0, 1]  # error → top_k by retrieval order


def test_rerank_disabled_returns_docs_unchanged():
    docs = _docs(5)
    rm = _make_rm(cohere_client=FakeCohereClient(order=[4, 3, 2, 1, 0]), use_reranking=False)

    out = rm.rerank("q", docs)

    assert out == docs  # USE_RERANKING=False: untouched, not even truncated
