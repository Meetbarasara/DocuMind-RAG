"""L4: embed-once reuse — when the pipeline already embedded the query (for the
C2 semantic-cache lookup), retrieval must reuse that vector instead of embedding
the same text a second time. Verifies both the dense and the native-hybrid path,
and that the old behaviour is unchanged when no vector is supplied.

Fakes the vectorstore/embeddings (mirrors test_native_hybrid.py) so nothing hits
the network.
"""

from types import SimpleNamespace

from langchain_core.documents import Document

from src.components.config import Config
from src.components.retrieval import RetrievalManager

_VEC = [0.11, 0.22, 0.33]


def _match(text, **meta):
    return SimpleNamespace(metadata={"text": text, **meta})


def _make_rm(matches, hybrid=True):
    rm = RetrievalManager.__new__(RetrievalManager)
    rm.config = Config(
        USE_HYBRID_SEARCH=hybrid, HYBRID_ALPHA=0.5, TOP_K=5,
        SIMILARITY_THRESHOLD=0.0, USE_RERANKING=False, USE_CHUNK_DEDUP=False,
    )
    rm._cohere_client = None
    captured = {}

    class FakeIndex:
        def query(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(matches=matches)

    class FakeEmbeddings:
        def __init__(self):
            self.calls = 0

        def embed_query(self, text):
            self.calls += 1
            return [0.9, 0.9, 0.9]   # deliberately != _VEC so reuse is detectable

    class FakeVS:
        _text_key = "text"

        def __init__(self):
            self.index = FakeIndex()
            self.by_text = []
            self.by_vector = []

        def similarity_search_with_score(self, query, k, filter=None):
            self.by_text.append(query)
            return [(Document(page_content="dense ctx", metadata={"filename": "f.pdf"}), 0.9)]

        def similarity_search_by_vector_with_score(self, embedding, k, filter=None):
            self.by_vector.append(embedding)
            return [(Document(page_content="dense ctx", metadata={"filename": "f.pdf"}), 0.9)]

    rm._embeddings = FakeEmbeddings()
    rm.vectorstore = FakeVS()
    return rm, captured


# ── Dense path ────────────────────────────────────────────────────────────────

def test_dense_reuses_supplied_vector_and_skips_embedding():
    rm, _ = _make_rm([], hybrid=False)

    docs = rm._dense_retrieve("q", query_vector=_VEC)

    assert [d.page_content for d in docs] == ["dense ctx"]
    assert rm.vectorstore.by_vector == [_VEC]   # searched by the supplied vector
    assert rm.vectorstore.by_text == []         # never re-embedded via text search
    assert rm._embeddings.calls == 0


def test_dense_without_vector_embeds_as_before():
    rm, _ = _make_rm([], hybrid=False)

    docs = rm._dense_retrieve("q")

    assert [d.page_content for d in docs] == ["dense ctx"]
    assert rm.vectorstore.by_text == ["q"]      # falls back to text search
    assert rm.vectorstore.by_vector == []


# ── Native hybrid path ──────────────────────────────────────────────────────────

def test_hybrid_reuses_supplied_vector_and_skips_embedding():
    rm, captured = _make_rm([_match("hybrid ctx", filename="f.pdf", page_number=3)])

    docs = rm._hybrid_retrieve("q", query_vector=_VEC)

    assert [d.page_content for d in docs] == ["hybrid ctx"]
    assert rm._embeddings.calls == 0            # dense vector was reused, not recomputed
    # the dense half of the fused query is the supplied vector, convex-scaled by alpha
    assert captured.get("vector") == [v * rm.config.HYBRID_ALPHA for v in _VEC]


def test_hybrid_without_vector_embeds_as_before():
    rm, captured = _make_rm([_match("hybrid ctx", filename="f.pdf", page_number=3)])

    rm._hybrid_retrieve("q")

    assert rm._embeddings.calls == 1
    assert captured.get("vector")               # dense vector still sent
