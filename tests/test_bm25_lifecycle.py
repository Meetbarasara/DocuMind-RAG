"""Regression test for BUG-4 and BUG-5 (see BUGFIXES.md).

update_bm25_index() only ever held whatever documents were passed into the
*most recent* call (BUG-5: a second upload wiped the first file's keyword
coverage), and was pure in-process memory populated only by an upload event
in *this specific* process (BUG-4: empty after a restart, on a different
uvicorn worker, or for files uploaded in an earlier session).

Inspects RetrievalManager's BM25 state directly (`_bm25_docs`) rather than
going through `_hybrid_retrieve`'s merged dense+BM25 output — dense search
isn't stale (it always hits the real, current Pinecone data), so testing
only the merged result could pass for the wrong reason, with dense search
silently masking a completely broken BM25 component.

Builds RetrievalManager via __new__ to skip its network-calling __init__ —
constructing a real PineconeVectorStore makes an immediate HTTP call to
Pinecone's control plane (confirmed empirically; a fake key gets a real
401, not a safe no-op) — and injects a fake vectorstore instead.
"""

from langchain_core.documents import Document

from src.components.config import Config
from src.components.retrieval import RetrievalManager


class FakeVectorStore:
    """Stands in for Pinecone — `docs` is mutable so tests can simulate uploads."""

    def __init__(self, docs):
        self.docs = docs

    def similarity_search(self, query, k, filter=None):
        return list(self.docs)


def make_retrieval_manager(initial_docs):
    rm = RetrievalManager.__new__(RetrievalManager)
    rm.config = Config(
        PINECONE_API_KEY="fake",
        OPENAI_API_KEY="sk-fake",
        USE_HYBRID_SEARCH=True,
        TOP_K=10,
    )
    rm.vectorstore = FakeVectorStore(initial_docs)
    rm._bm25_retriever = None
    rm._bm25_docs = []
    rm._bm25_dirty = True
    rm._cross_encoder = None
    return rm


def _doc(filename, text, chunk_id):
    return Document(page_content=text, metadata={"filename": filename, "chunk_id": chunk_id})


def test_second_upload_does_not_drop_first_files_keywords():
    """BUG-5: uploading file2 must not wipe file1's BM25 coverage."""
    file1_doc = _doc("file1.txt", "apple banana cherry", "f1c1")
    rm = make_retrieval_manager([file1_doc])

    rm._ensure_bm25_index()
    assert {d.page_content for d in rm._bm25_docs} == {"apple banana cherry"}

    # Simulate a second upload: Pinecone now holds both files' chunks.
    file2_doc = _doc("file2.txt", "dragon elephant fox", "f2c1")
    rm.vectorstore.docs = [file1_doc, file2_doc]
    rm.invalidate_bm25_index()
    rm._ensure_bm25_index()

    bm25_contents = {d.page_content for d in rm._bm25_docs}
    assert "apple banana cherry" in bm25_contents, "file1 dropped from BM25 after a second upload"
    assert "dragon elephant fox" in bm25_contents, "file2 should be present in BM25 after its upload"


def test_fresh_instance_pulls_existing_docs_on_first_use():
    """BUG-4: a brand-new RetrievalManager (simulating a restart or a
    different uvicorn worker) must see previously-ingested documents on its
    very first query — it never had update_bm25_index() called on it, so
    coverage has to come from Pinecone, not this process's memory.
    """
    file1_doc = _doc("file1.txt", "apple banana cherry", "f1c1")
    file2_doc = _doc("file2.txt", "dragon elephant fox", "f2c1")
    rm = make_retrieval_manager([file1_doc, file2_doc])

    assert rm._bm25_retriever is None  # cold — nothing has populated it yet

    rm._ensure_bm25_index()

    bm25_contents = {d.page_content for d in rm._bm25_docs}
    assert "apple banana cherry" in bm25_contents
    assert "dragon elephant fox" in bm25_contents
