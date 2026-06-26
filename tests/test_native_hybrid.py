"""L1: Pinecone native hybrid query — dense + sparse fused server-side, with a
dense fallback (e.g. if the index isn't dotproduct). Injects a fake vectorstore
via __new__ so nothing hits the network.
"""

from types import SimpleNamespace

from langchain_core.documents import Document

from src.components.config import Config
from src.components.retrieval import RetrievalManager


def _match(text, **meta):
    return SimpleNamespace(metadata={"text": text, **meta})


def _make_rm(matches, hybrid=True, query_raises=False):
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
            if query_raises:
                raise RuntimeError("index is not dotproduct")
            return SimpleNamespace(matches=matches)

    class FakeEmbeddings:
        def embed_query(self, text):
            return [0.1, 0.2, 0.3]

    class FakeVS:
        _text_key = "text"

        def __init__(self):
            self.index = FakeIndex()
            self.dense_calls = []

        def similarity_search_with_score(self, query, k, filter=None):
            self.dense_calls.append(query)
            return [(Document(page_content="dense ctx", metadata={"filename": "f.pdf"}), 0.9)]

    rm._embeddings = FakeEmbeddings()
    rm.vectorstore = FakeVS()
    return rm, captured


def test_hybrid_query_sends_dense_and_sparse_and_reconstructs_docs():
    rm, captured = _make_rm([_match("hybrid ctx", filename="f.pdf", page_number=3)])

    docs = rm._hybrid_retrieve("traffic signal reinforcement learning")

    assert [d.page_content for d in docs] == ["hybrid ctx"]
    assert docs[0].metadata["filename"] == "f.pdf"
    assert "text" not in docs[0].metadata          # internal text key stripped out
    assert captured.get("vector")                   # dense vector sent
    assert "sparse_vector" in captured              # sparse vector sent
    assert captured["top_k"] == 5
    assert captured["include_metadata"] is True


def test_hybrid_off_uses_plain_dense():
    rm, captured = _make_rm([], hybrid=False)

    docs = rm._hybrid_retrieve("q")

    assert [d.page_content for d in docs] == ["dense ctx"]
    assert rm.vectorstore.dense_calls == ["q"]
    assert captured == {}  # the hybrid index.query was never called


def test_hybrid_error_falls_back_to_dense():
    rm, _ = _make_rm([], hybrid=True, query_raises=True)

    docs = rm._hybrid_retrieve("q")  # index.query raises -> dense fallback

    assert [d.page_content for d in docs] == ["dense ctx"]
    assert rm.vectorstore.dense_calls == ["q"]
