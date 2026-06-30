"""Regression test for Logical Mistake #4 (see BUGFIXES.md / CODE_REVIEW.md §3).

_deduplicate_chunks kept whichever of two near-duplicate chunks had MORE
CHARACTERS, regardless of which one retrieval had actually ranked higher.
Chunk length has no relationship to query relevance; retrieval rank does --
`docs` arrives at `_deduplicate_chunks` already ordered best-first (RRF
score for hybrid search, similarity score for dense-only), since dedup runs
inside `retrieve_candidates()` right after `_hybrid_retrieve`/
`_dense_retrieve` and before any re-ranking. The longer-but-less-relevant
chunk could silently displace a shorter, genuinely-more-relevant one before
the cross-encoder ever got a chance to compare them.
"""

from langchain_core.documents import Document

from src.components.config import Config
from src.components.retrieval import RetrievalManager


def _make_retrieval_manager():
    rm = RetrievalManager.__new__(RetrievalManager)
    rm.config = Config(
        PINECONE_API_KEY="fake",
        GROQ_API_KEY="gsk-fake",
        USE_CHUNK_DEDUP=True,
        CHUNK_DEDUP_THRESHOLD=0.85,
    )
    return rm


def test_dedup_keeps_higher_ranked_chunk_not_longer_chunk():
    # Higher-ranked (first in the list = retrieval thinks it's more
    # relevant) but shorter.
    more_relevant = Document(
        page_content="alpha beta gamma delta epsilon zeta eta theta iota kappa",
        metadata={"id": "relevant"},
    )
    # Near-duplicate of the above (same 10 words plus one repeated filler
    # word -- Jaccard similarity ~0.91, comfortably over the 0.85
    # threshold) but padded much longer, and ranked lower (second).
    less_relevant_but_longer = Document(
        page_content=more_relevant.page_content + " filler" * 50,
        metadata={"id": "padded"},
    )
    assert len(less_relevant_but_longer.page_content) > len(more_relevant.page_content) * 5

    rm = _make_retrieval_manager()
    result = rm._deduplicate_chunks([more_relevant, less_relevant_but_longer])

    assert len(result) == 1, "near-duplicates above threshold should collapse to one"
    assert result[0].metadata["id"] == "relevant", (
        "dedup should keep the higher-ranked chunk, not whichever is textually longer — "
        f"kept {result[0].metadata['id']!r} instead"
    )


def test_dedup_leaves_genuinely_distinct_chunks_alone():
    a = Document(page_content="the weather today is sunny and warm", metadata={"id": "a"})
    b = Document(page_content="quantum computers use superconducting qubits", metadata={"id": "b"})

    rm = _make_retrieval_manager()
    result = rm._deduplicate_chunks([a, b])

    assert {d.metadata["id"] for d in result} == {"a", "b"}, "unrelated chunks must not be deduped"
