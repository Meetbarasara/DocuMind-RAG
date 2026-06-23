"""Regression test for BUG-7 (see BUGFIXES.md).

delete_document_by_filename used similarity_search(query="", k=10_000,
filter={"filename": ...}) to enumerate vectors to delete — an embedded
empty string fed into a *ranked* top-k search, not a guaranteed exhaustive
listing. Pinecone's index.list(prefix=...) is an actual paginated listing
of every matching vector ID, which is what this should have used all
along. Relies on chunk_id being f"{filename}::{content_hash}" (fixed in
embeddings.py alongside this) so filenames form a stable, listable prefix.
"""

from src.components.config import Config
from src.components.retrieval import RetrievalManager


class FakeIndex:
    """Simulates Pinecone's real index.list(prefix=...) — an exhaustive,
    paginated ID listing, not a ranked search. Yields in two batches
    whenever there's more than one match, to prove pagination is handled."""

    def __init__(self, all_ids):
        self.all_ids = all_ids
        self.list_calls = []

    def list(self, prefix=None, namespace=None, **kwargs):
        self.list_calls.append({"prefix": prefix, "namespace": namespace})
        matching = [vid for vid in self.all_ids if prefix is None or vid.startswith(prefix)]
        if not matching:
            return
        mid = max(1, len(matching) // 2)
        if mid < len(matching):
            yield matching[:mid]
            yield matching[mid:]
        else:
            yield matching


class FakeVectorStore:
    def __init__(self, all_ids):
        self.index = FakeIndex(all_ids)
        self.deleted_ids = None

    def delete(self, ids):
        self.deleted_ids = list(ids)


def make_retrieval_manager(all_ids, namespace="ns1"):
    rm = RetrievalManager.__new__(RetrievalManager)
    rm.config = Config(PINECONE_NAMESPACE=namespace)
    rm.vectorstore = FakeVectorStore(all_ids)
    return rm


def test_delete_by_filename_enumerates_all_matching_ids_via_list():
    all_ids = [
        "report.pdf::aaa", "report.pdf::bbb", "report.pdf::ccc", "report.pdf::ddd",
        "other.pdf::xxx",
    ]
    rm = make_retrieval_manager(all_ids)

    rm.delete_document_by_filename("report.pdf")

    assert set(rm.vectorstore.deleted_ids) == {
        "report.pdf::aaa", "report.pdf::bbb", "report.pdf::ccc", "report.pdf::ddd",
    }
    # other.pdf's vector must not be touched
    assert "other.pdf::xxx" not in rm.vectorstore.deleted_ids
    # proves the fake's two-batch pagination was actually exercised, not bypassed
    assert rm.vectorstore.index.list_calls == [{"prefix": "report.pdf::", "namespace": "ns1"}]


def test_delete_by_filename_no_match_deletes_nothing():
    rm = make_retrieval_manager(["other.pdf::xxx"])

    rm.delete_document_by_filename("missing.pdf")

    assert rm.vectorstore.deleted_ids is None
