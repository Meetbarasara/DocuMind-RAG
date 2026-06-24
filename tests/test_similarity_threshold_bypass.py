"""Regression test for Logical Mistake #3 (see BUGFIXES.md / CODE_REVIEW.md §3).

`_dense_retrieve` correctly filters by `SIMILARITY_THRESHOLD`, but
`_hybrid_retrieve`'s Reciprocal Rank Fusion merges by *rank*, not score --
so a document that BM25 surfaces gets a real RRF contribution regardless of
whether it has any genuine relevance to the query at all.

Confirmed empirically against the real, installed `rank_bm25`/
`langchain_community.BM25Retriever`: `BM25Retriever.invoke()` (via
`rank_bm25`'s `get_top_n`) is a plain `argsort` over the *entire* corpus and
always returns exactly `k` documents, with no minimum-score cutoff. In a
small namespace (typical for this per-user app), that means documents with
a BM25 score of exactly 0.0 -- zero shared terms with the query, i.e. no
lexical relevance whatsoever -- still get returned and fused in, bypassing
any quality gate.

Reusing SIMILARITY_THRESHOLD's raw numeric value against BM25 scores would
itself be a bug -- cosine similarity (~[0,1], higher better) and BM25 scores
(unbounded, corpus-dependent) are different scales. The correct, scale-
appropriate quality gate for the BM25 branch is BM25's own score: anything
that didn't share a single term (score <= 0) has no business in the merged
result.
"""

from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document

from src.components.config import Config
from src.components.retrieval import RetrievalManager


class FakeVectorStore:
    def __init__(self, dense_results):
        self._dense_results = dense_results

    def similarity_search_with_score(self, query, k, filter=None):
        return list(self._dense_results)


def _make_retrieval_manager(dense_docs_with_scores, bm25_seed_docs):
    config = Config(
        PINECONE_API_KEY="fake",
        OPENAI_API_KEY="sk-fake",
        USE_HYBRID_SEARCH=True,
        USE_CHUNK_DEDUP=False,
        USE_RERANKING=False,
        SIMILARITY_THRESHOLD=0.5,
        TOP_K=10,
    )
    rm = RetrievalManager.__new__(RetrievalManager)
    rm.config = config
    rm.vectorstore = FakeVectorStore(dense_docs_with_scores)
    rm._cross_encoder = None
    rm._bm25_dirty = False
    rm._bm25_docs = bm25_seed_docs
    rm._bm25_retriever = BM25Retriever.from_documents(bm25_seed_docs, k=len(bm25_seed_docs))
    return rm


def test_zero_relevance_bm25_docs_do_not_bypass_the_quality_gate():
    fox_doc = Document(page_content="The quick brown fox jumps over the lazy dog", metadata={"id": "fox"})
    physics_doc = Document(page_content="Quantum entanglement and superposition in physics", metadata={"id": "physics"})
    stocks_doc = Document(page_content="Stock market trading strategies for beginners", metadata={"id": "stocks"})
    dense_hit = Document(page_content="Dense match about foxes and dogs roaming forests", metadata={"id": "dense_hit"})

    # Sanity check the premise against the real rank_bm25 scorer: "physics"
    # and "stocks" share zero terms with "fox dog" and must score exactly 0.
    retriever = BM25Retriever.from_documents([fox_doc, physics_doc, stocks_doc], k=3)
    scores = retriever.vectorizer.get_scores(retriever.preprocess_func("fox dog"))
    scores_by_id = {d.metadata["id"]: s for d, s in zip(retriever.docs, scores)}
    assert scores_by_id["physics"] == 0.0
    assert scores_by_id["stocks"] == 0.0
    assert scores_by_id["fox"] > 0.0

    rm = _make_retrieval_manager(
        dense_docs_with_scores=[(dense_hit, 0.91)],
        bm25_seed_docs=[fox_doc, physics_doc, stocks_doc],
    )

    merged_ids = {d.metadata["id"] for d in rm._hybrid_retrieve("fox dog")}

    assert "dense_hit" in merged_ids, "the genuinely good dense match should always survive"
    assert "fox" in merged_ids, "a real BM25 keyword match (score > 0) should still survive"
    assert "physics" not in merged_ids, "a zero-relevance BM25 doc must not bypass the quality gate"
    assert "stocks" not in merged_ids, "a zero-relevance BM25 doc must not bypass the quality gate"
